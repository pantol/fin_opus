"""Pack A.4: data-quality monitor + override journal + generic Telegram text."""
import pytest

from app import cli
from app.alerts import telegram
from app.db import connect, init_db
from app.ingestion import quality, stooq

from tests.conftest import make_stooq_csv, synthetic_series


def _ingest(conn, ticker, rows, **inst):
    iid = stooq.upsert_instrument(conn, {"ticker": ticker, "name": ticker, **inst},
                                  is_index=inst.get("is_index", False))
    stooq.store_bars(conn, iid, stooq.parse_csv(make_stooq_csv(rows)), source="stooq")
    return iid


DQ_CFG = {"max_jump_pct": 0.25, "stale_sessions": 5, "max_examples": 10}


def _universe(*tickers):
    return {
        "benchmark": {"ticker": "wig20tr", "is_index": True},
        "indices": [],
        "instruments": [{"ticker": t, "sector": "x"} for t in tickers],
    }


def test_clean_data_reports_no_issues(conn):
    rows = synthetic_series(n=60, base=100, drift=0.0005)
    _ingest(conn, "wig20tr", synthetic_series(n=60, base=2000), is_index=True)
    _ingest(conn, "ok", rows, sector="x")
    conn.commit()
    report = quality.run_checks(conn, _universe("ok"), DQ_CFG)
    assert report.ok
    assert report.checked_instruments == 1
    assert "No issues" in quality.format_report(report)


def test_missing_sessions_detected(conn):
    rows = synthetic_series(n=60, base=100, drift=0.0)
    _ingest(conn, "wig20tr", rows, is_index=True)
    _ingest(conn, "gappy", rows[:30] + rows[31:], sector="x")  # one hole
    conn.commit()
    report = quality.run_checks(conn, _universe("gappy"), DQ_CFG)
    cats = report.counts_by_category()
    assert cats.get("missing_sessions") == 1
    assert rows[30][0] in report.issues[0].detail


def test_zero_volume_detected(conn):
    rows = synthetic_series(n=60, base=100, drift=0.0)
    bad = [(d, o, h, l, c, 0.0 if i == 10 else v)
           for i, (d, o, h, l, c, v) in enumerate(rows)]
    _ingest(conn, "wig20tr", rows, is_index=True)
    _ingest(conn, "zvol", bad, sector="x")
    conn.commit()
    report = quality.run_checks(conn, _universe("zvol"), DQ_CFG)
    assert report.counts_by_category().get("bad_volume") == 1


def test_jump_without_action_flagged_with_action_not(conn):
    rows = synthetic_series(n=60, base=100, drift=0.0)
    jumped = [(d, o, h, l, c * 0.6 if i >= 30 else c, v)
              for i, (d, o, h, l, c, v) in enumerate(rows)]
    _ingest(conn, "wig20tr", rows, is_index=True)
    iid = _ingest(conn, "jumpy", jumped, sector="x")
    conn.commit()

    report = quality.run_checks(conn, _universe("jumpy"), DQ_CFG)
    assert report.counts_by_category().get("unexplained_jump") == 1
    assert rows[30][0] in [i for i in report.issues if i.category == "unexplained_jump"][0].detail

    # the same gap WITH a matching corporate action is explained -> not flagged
    conn.execute(
        "INSERT INTO corporate_actions (instrument_id, action_type, ex_date, value_or_ratio, source)"
        " VALUES (?, 'dividend', ?, 40.0, 'test')", (iid, rows[30][0]))
    conn.commit()
    report2 = quality.run_checks(conn, _universe("jumpy"), DQ_CFG)
    assert "unexplained_jump" not in report2.counts_by_category()


def test_stale_ticker_detected_only_when_alive(conn):
    rows = synthetic_series(n=60, base=100, drift=0.0)
    _ingest(conn, "wig20tr", rows, is_index=True)
    _ingest(conn, "stale", rows[:40], sector="x")  # stops printing 20 sessions early
    _ingest(conn, "dead", rows[:40], sector="x", delisted_on=rows[40][0])
    conn.commit()
    report = quality.run_checks(conn, _universe("stale", "dead"), DQ_CFG)
    stale_issues = [i for i in report.issues if i.category == "stale_ticker"]
    assert [i.ticker for i in stale_issues] == ["stale"], (
        "alive-but-silent flagged; a delisted instrument is not stale"
    )


def test_never_ingested_ticker_reported(conn):
    _ingest(conn, "wig20tr", synthetic_series(n=10), is_index=True)
    conn.commit()
    report = quality.run_checks(conn, _universe("phantom"), DQ_CFG)
    assert report.counts_by_category().get("no_data") == 1


def test_polish_alert_card_lists_categories(conn):
    rows = synthetic_series(n=60, base=100, drift=0.0)
    _ingest(conn, "wig20tr", rows, is_index=True)
    _ingest(conn, "gappy", rows[:30] + rows[31:], sector="x")
    conn.commit()
    report = quality.run_checks(conn, _universe("gappy"), DQ_CFG)
    card = quality.format_alert_pl(report)
    assert "Kontrola danych" in card
    assert "brakujace sesje" in card


def test_send_text_dry_run_contract(monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    out = telegram.send_text("hello alert")
    assert out == {"mode": "dry-run", "sent": False, "card": "hello alert"}
    assert "hello alert" in capsys.readouterr().out


def test_missing_benchmark_is_an_issue_not_a_clean_bill(conn):
    """No benchmark bars must NEVER report ok=True (the calendar is blind)."""
    rows = synthetic_series(n=60, base=100, drift=0.0)
    _ingest(conn, "silent", rows[:40], sector="x")  # stale, but undetectable
    conn.commit()
    report = quality.run_checks(conn, _universe("silent"), DQ_CFG)
    assert not report.ok
    assert any(i.ticker == "wig20tr" and i.category == "no_data"
               for i in report.issues)


def test_stale_benchmark_flagged_when_equities_are_newer(conn):
    rows = synthetic_series(n=60, base=100, drift=0.0)
    _ingest(conn, "wig20tr", rows[:40], is_index=True)  # calendar stops early
    _ingest(conn, "fresh", rows, sector="x")
    conn.commit()
    report = quality.run_checks(conn, _universe("fresh"), DQ_CFG)
    assert any(i.ticker == "wig20tr" and i.category == "stale_ticker"
               for i in report.issues)


def test_crash_on_ex_date_still_flagged_beyond_action_magnitude(conn):
    """A tiny dividend cannot absolve a 40% crash on the same date."""
    rows = synthetic_series(n=60, base=100, drift=0.0)
    jumped = [(d, o, h, l, c * 0.6 if i >= 30 else c, v)
              for i, (d, o, h, l, c, v) in enumerate(rows)]
    _ingest(conn, "wig20tr", rows, is_index=True)
    iid = _ingest(conn, "crashy", jumped, sector="x")
    conn.execute(
        "INSERT INTO corporate_actions (instrument_id, action_type, ex_date, value_or_ratio, source)"
        " VALUES (?, 'dividend', ?, 1.0, 'test')", (iid, rows[30][0]))
    conn.commit()
    report = quality.run_checks(conn, _universe("crashy"), DQ_CFG)
    issues = [i for i in report.issues if i.category == "unexplained_jump"]
    assert issues, "a -40% move on a 1-PLN-dividend ex-date must still flag"
    assert "beyond the recorded action" in issues[0].detail


# --- override journal --------------------------------------------------------

def test_override_cli_appends_row(tmp_path):
    db = str(tmp_path / "t.db")
    rc = cli.main(["--db", db, "override", "--action", "skipped the ENTER signal",
                   "--reason", "earnings call tomorrow", "--user", "u1"])
    assert rc == 0
    conn = connect(db)
    rows = conn.execute("SELECT * FROM overrides").fetchall()
    assert len(rows) == 1
    assert rows[0]["user_id"] == "u1"
    assert rows[0]["action_taken"] == "skipped the ENTER signal"
    assert rows[0]["reason"] == "earnings call tomorrow"
    assert rows[0]["decision_id"] is None
    assert rows[0]["timestamp"]  # ISO timestamp recorded
    conn.close()


def test_override_cli_rejects_unknown_decision_id(tmp_path):
    db = str(tmp_path / "t.db")
    rc = cli.main(["--db", db, "override", "--decision-id", "999",
                   "--action", "x", "--reason", "y", "--user", "u1"])
    assert rc == 1
    conn = connect(db)
    assert conn.execute("SELECT COUNT(*) FROM overrides").fetchone()[0] == 0
    conn.close()


def test_override_journal_is_append_only(tmp_path):
    """Two overrides -> two rows; nothing in the code path updates or deletes."""
    db = str(tmp_path / "t.db")
    cli.main(["--db", db, "override", "--action", "a1", "--reason", "r1", "--user", "u"])
    cli.main(["--db", db, "override", "--action", "a2", "--reason", "r2", "--user", "u"])
    conn = connect(db)
    rows = conn.execute("SELECT action_taken FROM overrides ORDER BY id").fetchall()
    assert [r["action_taken"] for r in rows] == ["a1", "a2"]
    conn.close()

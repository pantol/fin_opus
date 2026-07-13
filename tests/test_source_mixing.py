"""Provenance guard: demo (synthetic) and real price rows never share a DB.

Regression tests for the mixing scenarios: demo ingest into a real database,
live ingest into a demo database, the demo-blind incremental resume, the
idempotent `source` column migration, and the purge-demo escape hatch.
"""
from datetime import date

import pytest

from app.db import column_exists, connect, init_db
from app.ingestion import demo, gpw_archive as gpw, provenance, refdata, stooq

from tests.conftest import make_stooq_csv


def _universe():
    return {
        "benchmark": {"ticker": "wig20tr", "name": "WIG20TR", "is_index": True},
        "indices": [],
        "instruments": [{"ticker": "pko", "name": "PKO", "isin": "PLPKO0000016"}],
    }


def _fake_stooq_fetch(ticker):
    return make_stooq_csv([("2020-01-02", 10.0, 10.5, 9.8, 10.2, 1000)])


def _gpw_session_rows(day: date):
    d = day.isoformat()
    return [gpw.SessionRow(d, "PKOBP", "PLPKO0000016", "PLN",
                           10.0, 10.5, 9.8, 10.2, 1000.0)]


def _gpw_index_bars(name, start, end):
    return [stooq.Bar("2026-06-25", "2026-06-25", 8100.0, 8200.0, 8050.0, 8150.0, 0.0)]


def _sources(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT DISTINCT source FROM prices")}


# --- source tagging per ingest path -----------------------------------------

def test_stooq_ingest_tags_rows_stooq(conn):
    report = stooq.ingest_universe(conn, _universe(), fetcher=_fake_stooq_fetch)
    assert report.ok
    assert _sources(conn) == {"stooq"}


def test_demo_ingest_tags_rows_demo(conn):
    report = demo.ingest_offline(conn, _universe(), n_days=3)
    assert report.ok
    assert _sources(conn) == {"demo"}


def test_gpw_archive_tags_rows_gpw(conn):
    report = gpw.ingest_range(
        conn, _universe(), date(2026, 6, 25), date(2026, 6, 25),
        delay_seconds=0.0,
        fetch_session_rows=_gpw_session_rows, fetch_index_bars=_gpw_index_bars,
    )
    assert report.counts["pko"] == 1
    assert _sources(conn) == {"gpw"}


# --- mixing refusals ----------------------------------------------------------

def test_demo_refuses_db_with_real_rows(conn):
    stooq.ingest_universe(conn, _universe(), fetcher=_fake_stooq_fetch)
    before = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    with pytest.raises(provenance.DataMixingError, match="Refusing to write DEMO"):
        demo.ingest_offline(conn, _universe(), n_days=3)
    # the refusal happened before anything was written
    assert conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == before
    assert _sources(conn) == {"stooq"}


def test_live_stooq_refuses_db_with_demo_rows(conn):
    demo.ingest_offline(conn, _universe(), n_days=3)
    before = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    with pytest.raises(provenance.DataMixingError, match="DEMO price rows"):
        stooq.ingest_universe(conn, _universe(), fetcher=_fake_stooq_fetch)
    assert conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == before
    assert _sources(conn) == {"demo"}


def test_live_gpw_refuses_demo_db_before_any_fetch(conn):
    demo.ingest_offline(conn, _universe(), n_days=3)

    def _must_not_fetch(*args, **kwargs):
        raise AssertionError("guard must refuse before any network fetch")

    with pytest.raises(provenance.DataMixingError, match="DEMO price rows"):
        gpw.ingest_range(conn, _universe(), date(2026, 6, 25), date(2026, 6, 25),
                         delay_seconds=0.0,
                         fetch_session_rows=_must_not_fetch,
                         fetch_index_bars=_must_not_fetch)


def test_demo_rerun_into_demo_db_is_fine(conn):
    r1 = demo.ingest_offline(conn, _universe(), n_days=3)
    r2 = demo.ingest_offline(conn, _universe(), n_days=3)  # idempotent re-run
    assert r1.ok and r2.ok
    assert conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 3 * 2


def test_gpw_and_stooq_may_coexist(conn):
    # both are REAL sources; Stooq is the documented fallback path
    stooq.ingest_universe(conn, _universe(), fetcher=_fake_stooq_fetch)
    gpw.ingest_range(conn, _universe(), date(2026, 6, 25), date(2026, 6, 25),
                     delay_seconds=0.0,
                     fetch_session_rows=_gpw_session_rows,
                     fetch_index_bars=_gpw_index_bars)
    assert _sources(conn) == {"stooq", "gpw"}


def test_assert_no_mixing_rejects_unknown_source(conn):
    with pytest.raises(ValueError, match="Unknown price source"):
        provenance.assert_no_mixing(conn, "live")


def test_store_bars_requires_explicit_valid_source(conn):
    iid = stooq.upsert_instrument(conn, {"ticker": "tst", "name": "Test"})
    bars = stooq.parse_csv(make_stooq_csv([("2020-01-02", 10, 11, 9, 10, 500)]))
    with pytest.raises(TypeError):
        stooq.store_bars(conn, iid, bars)  # forgotten source must not default
    with pytest.raises(ValueError, match="Unknown price source"):
        stooq.store_bars(conn, iid, bars, source="live")
    assert conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 0


def test_guard_takes_write_lock_closing_concurrent_race(tmp_path):
    # Two ingests racing on an empty DB: the first to reach the guard holds
    # the write lock until its first commit, so the second cannot pass the
    # check in parallel and interleave demo with real writes.
    db = str(tmp_path / "gpw.db")
    c1 = connect(db)
    init_db(c1)
    provenance.assert_no_mixing(c1, "gpw")
    assert c1.in_transaction  # lock held until the caller's first commit

    c2 = connect(db)
    c2.execute("PRAGMA busy_timeout = 0")
    with pytest.raises(provenance.DataMixingError, match="locked"):
        provenance.assert_no_mixing(c2, "demo")

    c1.commit()  # releases the lock; c2 may now take its turn
    provenance.assert_no_mixing(c2, "demo")
    c1.close()
    c2.close()


# --- incremental resume ignores demo rows -------------------------------------

def test_last_real_bar_date_ignores_demo_rows(conn):
    iid = stooq.upsert_instrument(conn, {"ticker": "tst", "name": "Test"})
    real = stooq.parse_csv(make_stooq_csv([("2020-01-10", 10, 11, 9, 10, 500)]))
    stooq.store_bars(conn, iid, real, source="gpw")
    # demo rows far in the future must NOT anchor the incremental resume
    fake = stooq.parse_csv(make_stooq_csv([("2026-01-05", 10, 11, 9, 10, 500)]))
    stooq.store_bars(conn, iid, fake, source="demo")
    assert provenance.last_real_bar_date(conn) == "2020-01-10"


def test_last_real_bar_date_none_when_demo_only(conn):
    demo.ingest_offline(conn, _universe(), n_days=3)
    assert provenance.last_real_bar_date(conn) is None


def test_last_real_bar_date_none_on_empty_db(conn):
    assert provenance.last_real_bar_date(conn) is None


# --- migration ------------------------------------------------------------------

def _preexisting_db():
    """A pre-`source` schema database, as shipped before this migration existed."""
    conn = connect(":memory:")
    conn.execute("""
        CREATE TABLE instruments (
            id INTEGER PRIMARY KEY, ticker TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
            market TEXT NOT NULL DEFAULT 'GPW', sector TEXT, isin TEXT,
            is_index INTEGER NOT NULL DEFAULT 0, listed_from TEXT, delisted_on TEXT
        )""")
    conn.execute("""
        CREATE TABLE prices (
            instrument_id INTEGER NOT NULL REFERENCES instruments(id),
            date TEXT NOT NULL, as_of_date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            adjusted INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (instrument_id, date, adjusted)
        )""")
    return conn


def test_migration_backfills_preexisting_rows_as_gpw():
    conn = _preexisting_db()
    conn.execute("INSERT INTO instruments (id, ticker, name) VALUES (1, 'pko', 'PKO')")
    conn.execute(
        "INSERT INTO prices (instrument_id, date, as_of_date, close) "
        "VALUES (1, '2020-01-02', '2020-01-02', 10.0)")
    conn.commit()

    init_db(conn)
    init_db(conn)  # migration is idempotent

    assert column_exists(conn, "prices", "source")
    row = conn.execute("SELECT source FROM prices").fetchone()
    assert row["source"] == "gpw"  # pre-existing rows assumed real
    conn.close()


def test_migration_detects_preexisting_demo_bars():
    # Only the demo generator produces a 2015-01-01 bar (GPW was closed that
    # day), so instruments holding one are relabelled demo WHOLESALE — even
    # their later bars, which pre-guard live ingest may have appended.
    conn = _preexisting_db()
    conn.execute("INSERT INTO instruments (id, ticker, name) VALUES (1, 'pko', 'PKO')")
    conn.execute("INSERT INTO instruments (id, ticker, name) VALUES (2, 'kgh', 'KGHM')")
    for d in ("2015-01-01", "2019-08-07", "2026-07-10"):
        conn.execute(
            "INSERT INTO prices (instrument_id, date, as_of_date, close) "
            f"VALUES (1, '{d}', '{d}', 10.0)")
    conn.execute(
        "INSERT INTO prices (instrument_id, date, as_of_date, close) "
        "VALUES (2, '2020-01-02', '2020-01-02', 10.0)")
    conn.commit()

    init_db(conn)

    by_inst = {r[0]: r[1] for r in conn.execute(
        "SELECT instrument_id, GROUP_CONCAT(DISTINCT source) FROM prices "
        "GROUP BY instrument_id")}
    assert by_inst[1] == "demo"   # all three rows, including the real-looking tail
    assert by_inst[2] == "gpw"    # untouched: no 2015-01-01 bar
    # ... and the relabelled demo rows never anchor the incremental resume.
    assert provenance.last_real_bar_date(conn) == "2020-01-02"
    conn.close()


# --- derived adjusted series -----------------------------------------------------

def test_adjusted_series_inherits_raw_source(conn):
    iid = stooq.upsert_instrument(conn, {"ticker": "tst", "name": "Test"})
    bars = stooq.parse_csv(make_stooq_csv([
        ("2020-01-02", 10.0, 10.5, 9.8, 10.2, 1000),
        ("2020-01-03", 10.2, 10.6, 10.0, 10.4, 1200),
    ]))
    stooq.store_bars(conn, iid, bars, source="demo")
    conn.execute(
        "INSERT INTO corporate_actions (instrument_id, action_type, ex_date, value_or_ratio) "
        "VALUES (?, 'dividend', '2020-01-03', 0.5)", (iid,))
    conn.commit()
    n = refdata.derive_adjusted_series(conn, iid)
    assert n == 2
    adj_sources = {r[0] for r in conn.execute(
        "SELECT DISTINCT source FROM prices WHERE adjusted = 1")}
    assert adj_sources == {"demo"}


def test_adjusted_series_segments_mixed_real_sources(conn):
    # A real series may legitimately mix gpw and stooq segments; each adjusted
    # row must inherit the source of the raw row it derives from, per segment.
    iid = stooq.upsert_instrument(conn, {"ticker": "tst", "name": "Test"})
    stooq.store_bars(conn, iid, stooq.parse_csv(make_stooq_csv([
        ("2020-01-02", 10.0, 10.5, 9.8, 10.2, 1000),
        ("2020-01-03", 10.2, 10.6, 10.0, 10.4, 1200),
    ])), source="stooq")
    stooq.store_bars(conn, iid, stooq.parse_csv(make_stooq_csv([
        ("2020-01-06", 10.4, 10.8, 10.2, 10.6, 1100),
        ("2020-01-07", 10.6, 11.0, 10.4, 10.8, 1300),
    ])), source="gpw")
    conn.execute(
        "INSERT INTO corporate_actions (instrument_id, action_type, ex_date, value_or_ratio) "
        "VALUES (?, 'dividend', '2020-01-06', 0.5)", (iid,))
    conn.commit()
    assert refdata.derive_adjusted_series(conn, iid) == 4
    got = [(r["date"], r["source"]) for r in conn.execute(
        "SELECT date, source FROM prices WHERE adjusted = 1 ORDER BY date")]
    assert got == [("2020-01-02", "stooq"), ("2020-01-03", "stooq"),
                   ("2020-01-06", "gpw"), ("2020-01-07", "gpw")]


# --- CLI surface -------------------------------------------------------------------

def test_cli_ingest_offline_refuses_real_db(tmp_path, monkeypatch):
    import app.cli as cli
    from app import config as cfg

    monkeypatch.setattr(cfg, "load_universe", _universe)
    db = str(tmp_path / "gpw.db")
    c = connect(db)
    init_db(c)
    stooq.ingest_universe(c, _universe(), fetcher=_fake_stooq_fetch)
    c.close()

    rc = cli.main(["--db", db, "ingest", "--offline"])
    assert rc == 2  # clean error path, no traceback

    c = connect(db)
    assert _sources(c) == {"stooq"}  # nothing was written
    c.close()


def test_cli_purge_demo_unblocks_live_ingest(tmp_path):
    import app.cli as cli

    db = str(tmp_path / "gpw.db")
    c = connect(db)
    init_db(c)
    demo.ingest_offline(c, _universe(), n_days=3)
    c.close()

    assert cli.main(["--db", db, "purge-demo"]) == 0
    assert cli.main(["--db", db, "purge-demo"]) == 0  # idempotent

    c = connect(db)
    assert _sources(c) == set()
    report = stooq.ingest_universe(c, _universe(), fetcher=_fake_stooq_fetch)
    assert report.ok
    assert _sources(c) == {"stooq"}
    c.close()


def _seed_derived_rows(c):
    """One row in every prices-derived table (as a backtest/paper run leaves)."""
    iid = c.execute("SELECT id FROM instruments LIMIT 1").fetchone()[0]
    c.execute(
        "INSERT INTO strategy_trials (config_hash, strategy_name, strategy_version,"
        " run_at, metrics_json) VALUES ('h', 's', 1, '2020-01-01T00:00:00Z', '{}')")
    cur = c.execute(
        "INSERT INTO decisions (user_id, instrument_id, decision_date, action,"
        " features_json, created_at) "
        "VALUES ('default', ?, '2020-01-02', 'ENTER', '{}', '2020-01-02T18:00:00Z')",
        (iid,))
    dec_id = cur.lastrowid
    c.execute(
        "INSERT INTO trades (user_id, instrument_id, side, qty, price, fee,"
        " slippage, trade_date, decision_id) "
        "VALUES ('default', ?, 'BUY', 1, 10.0, 0.0, 0.0, '2020-01-03', ?)",
        (iid, dec_id))
    c.execute("INSERT INTO equity_curve VALUES ('default', '2020-01-03', 100, 100, 0)")
    c.execute(
        "INSERT INTO paper_state (user_id, cash, peak_equity, initial_capital,"
        " inception_date, last_settled_date, config_hash, updated_at) "
        "VALUES ('paper:u', 100, 100, 100, '2020-01-02', '2020-01-02', 'h', 'now')")
    c.commit()


_DERIVED = ("strategy_trials", "decisions", "trades", "equity_curve", "paper_state")


def test_purge_demo_wipes_derived_state_on_demo_only_db(tmp_path):
    # On a demo-only DB every derived row described fake prices: leaving e.g.
    # strategy_trials behind would pollute the Deflated Sharpe of future REAL
    # backtests, and a demo-anchored paper_state would wedge the loop.
    import app.cli as cli

    db = str(tmp_path / "gpw.db")
    c = connect(db)
    init_db(c)
    demo.ingest_offline(c, _universe(), n_days=3)
    _seed_derived_rows(c)
    c.close()

    assert cli.main(["--db", db, "purge-demo"]) == 0

    c = connect(db)
    for table in _DERIVED:
        assert c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    c.close()


def test_purge_demo_keeps_derived_state_when_real_rows_remain(tmp_path):
    # On a (pre-guard) mixed DB derived rows cannot be attributed to demo vs
    # real, so they must survive — deleting a real track record is worse.
    import app.cli as cli

    db = str(tmp_path / "gpw.db")
    c = connect(db)
    init_db(c)
    iid = stooq.upsert_instrument(c, {"ticker": "pko", "name": "PKO"})
    stooq.store_bars(c, iid, stooq.parse_csv(make_stooq_csv(
        [("2020-01-02", 10, 11, 9, 10, 500)])), source="gpw")
    stooq.store_bars(c, iid, stooq.parse_csv(make_stooq_csv(
        [("2019-01-04", 10, 11, 9, 10, 500)])), source="demo")
    _seed_derived_rows(c)
    c.close()

    assert cli.main(["--db", db, "purge-demo"]) == 0

    c = connect(db)
    assert _sources(c) == {"gpw"}  # demo prices gone, real prices kept
    for table in _DERIVED:
        assert c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1, table
    c.close()


def test_cli_ingest_exits_2_on_mixed_db_even_when_up_to_date(tmp_path, monkeypatch):
    # The guard must fire BEFORE the incremental 'Already up to date' early
    # return, or a mixed DB whose real bars are current reports success.
    import datetime as dt

    import app.cli as cli
    from app import config as cfg

    monkeypatch.setattr(cfg, "load_universe", _universe)
    db = str(tmp_path / "gpw.db")
    c = connect(db)
    init_db(c)
    iid = stooq.upsert_instrument(c, {"ticker": "pko", "name": "PKO"})
    today = dt.date.today().isoformat()
    stooq.store_bars(c, iid, stooq.parse_csv(make_stooq_csv(
        [(today, 10, 11, 9, 10, 500)])), source="gpw")
    stooq.store_bars(c, iid, stooq.parse_csv(make_stooq_csv(
        [("2019-01-04", 10, 11, 9, 10, 500)])), source="demo")
    c.commit()
    c.close()

    assert cli.main(["--db", db, "ingest"]) == 2  # refused, no network touched


def test_cli_ingest_up_to_date_exits_0_on_clean_db(tmp_path, monkeypatch):
    import datetime as dt

    import app.cli as cli
    from app import config as cfg

    monkeypatch.setattr(cfg, "load_universe", _universe)
    db = str(tmp_path / "gpw.db")
    c = connect(db)
    init_db(c)
    iid = stooq.upsert_instrument(c, {"ticker": "pko", "name": "PKO"})
    today = dt.date.today().isoformat()
    stooq.store_bars(c, iid, stooq.parse_csv(make_stooq_csv(
        [(today, 10, 11, 9, 10, 500)])), source="gpw")
    c.commit()
    c.close()

    assert cli.main(["--db", db, "ingest"]) == 0  # 'Already up to date', no network


def test_status_pages_when_demo_bars_present(conn):
    from app import status as statusmod

    demo.ingest_offline(conn, _universe(), n_days=3)
    report = statusmod.run_status(conn, {})
    assert any("DEMO" in line for line in report.lines)
    assert any("DEMO" in entry for entry in report.stale)  # alerts + exit 2


def test_paper_loop_refuses_demo_data(conn):
    from app.paper import loop as paper_loop

    demo.ingest_offline(conn, _universe(), n_days=3)
    code, report = paper_loop.run_signals(
        conn,
        universe=_universe(),
        bt_cfg={"user_id": "tester"},
        strategy_cfg={},
        send_fn=None,
    )
    assert code == paper_loop.EXIT_REFUSED
    assert "DEMO" in report.reason
    # no paper state was created from the synthetic bars
    assert conn.execute("SELECT COUNT(*) FROM paper_state").fetchone()[0] == 0

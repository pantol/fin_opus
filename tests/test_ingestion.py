"""Ingestion tests: parsing, storage, point-in-time as_of_date, anti-survivorship."""
from app.ingestion import stooq

from tests.conftest import make_stooq_csv


def test_parse_csv_basic():
    csv_text = make_stooq_csv(
        [
            ("2020-01-02", 10.0, 10.5, 9.8, 10.2, 1000),
            ("2020-01-03", 10.2, 10.6, 10.0, 10.4, 1200),
        ]
    )
    bars = stooq.parse_csv(csv_text)
    assert len(bars) == 2
    assert bars[0].date == "2020-01-02"
    # EOD bar is available only after that day's close.
    assert bars[0].as_of_date == bars[0].date
    assert bars[1].close == 10.4


def test_parse_csv_skips_non_numeric_rows():
    csv_text = "Data,Otwarcie,Najwyzszy,Najnizszy,Zamkniecie,Wolumen\n2020-01-02,N/D,N/D,N/D,N/D,N/D"
    assert stooq.parse_csv(csv_text) == []


def test_ingest_universe_with_injected_fetcher_no_network(conn):
    universe = {
        "benchmark": {"ticker": "wig20tr", "name": "WIG20TR", "is_index": True},
        "indices": [{"ticker": "wig", "name": "WIG", "is_index": True}],
        "instruments": [
            {"ticker": "pko", "name": "PKO", "sector": "banking", "listed_from": "2004-11-10"},
            {"ticker": "ple", "name": "Petrolinvest", "sector": "energy",
             "listed_from": "2007-07-31", "delisted_on": "2018-06-30"},
        ],
    }

    def fake_fetch(ticker):
        return make_stooq_csv([("2020-01-02", 10.0, 10.5, 9.8, 10.2, 1000)])

    report = stooq.ingest_universe(conn, universe, fetcher=fake_fetch)
    assert report.ok
    assert report.counts["pko"] == 1

    # Anti-survivorship: delisted ticker stored with delisted_on.
    row = conn.execute(
        "SELECT delisted_on FROM instruments WHERE ticker='ple'"
    ).fetchone()
    assert row["delisted_on"] == "2018-06-30"

    # Raw prices flagged adjusted=0.
    n_raw = conn.execute("SELECT COUNT(*) FROM prices WHERE adjusted=0").fetchone()[0]
    assert n_raw == 4  # wig20tr + wig + pko + ple


def test_failure_reason_detects_stooq_refusals():
    assert stooq._failure_reason("Access denied") is not None
    assert stooq._failure_reason("Przekroczona dzienna liczba wywolan") is not None
    assert stooq._failure_reason("Exceeded the daily hits limit") is not None
    assert stooq._failure_reason("<!DOCTYPE html><html>bot check</html>") is not None
    assert stooq._failure_reason(
        "Data,Otwarcie,Najwyzszy,Najnizszy,Zamkniecie,Wolumen\n2020-01-02,10,11,9,10,500"
    ) is None


def test_ingest_universe_one_failure_does_not_abort_the_rest(conn):
    universe = {
        "benchmark": {"ticker": "wig20tr", "name": "WIG20TR", "is_index": True},
        "instruments": [
            {"ticker": "pko", "name": "PKO"},
            {"ticker": "bad", "name": "Blocked"},
            {"ticker": "emp", "name": "EmptyHistory"},
        ],
    }

    def fake_fetch(ticker):
        if ticker == "bad":
            raise stooq.StooqUnavailableError("Stooq unavailable for 'bad': blocked")
        if ticker == "emp":
            return "Data,Otwarcie,Najwyzszy,Najnizszy,Zamkniecie,Wolumen\n"
        return make_stooq_csv([("2020-01-02", 10.0, 10.5, 9.8, 10.2, 1000)])

    report = stooq.ingest_universe(conn, universe, fetcher=fake_fetch)
    assert not report.ok
    assert report.counts == {"wig20tr": 1, "pko": 1}
    assert "bad" in report.failures and "emp" in report.failures
    # Successes were committed despite the failures.
    n = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    assert n == 2


def test_store_bars_is_idempotent(conn):
    inst_id = stooq.upsert_instrument(conn, {"ticker": "tst", "name": "Test"})
    bars = stooq.parse_csv(make_stooq_csv([("2020-01-02", 10, 11, 9, 10, 500)]))
    stooq.store_bars(conn, inst_id, bars, source="stooq")
    stooq.store_bars(conn, inst_id, bars, source="stooq")  # again
    n = conn.execute("SELECT COUNT(*) FROM prices WHERE instrument_id=?", (inst_id,)).fetchone()[0]
    assert n == 1


def test_cmd_ingest_zero_bar_hint_matches_failure_cause(tmp_path, capsys, monkeypatch):
    """All-failed ingest diagnoses the actual cause (network / calendar /
    config / source), never a blanket network accusation."""
    import app.cli as cli
    from app import config as cfg
    from app.ingestion import gpw_archive

    from tests.test_cli_llm import _universe

    monkeypatch.setattr(cfg, "load_universe", _universe)
    db = str(tmp_path / "x.db")

    def run(argv, report):
        monkeypatch.setattr(gpw_archive, "ingest_range", lambda *a, **k: report)
        monkeypatch.setattr(stooq, "ingest_universe", lambda *a, **k: report)
        rc = cli.main(argv)
        assert rc == 2
        return capsys.readouterr().out

    # gpw: session-file fetches failed -> network/WAF hint + retry advice.
    out = run(["--db", db, "ingest"],
              stooq.IngestReport(failures={"session:2026-07-06": "HTTP 403"},
                                 sessions=0))
    assert "GPW archive requests are failing" in out
    assert "Stooq is refusing" not in out

    # gpw: window had no trading days at all -> calendar, not network.
    out = run(["--db", db, "ingest"],
              stooq.IngestReport(
                  failures={"pko": "ISIN PLPKO0000016 not found in any "
                                   "session file 2026-07-04..2026-07-05 "
                                   "(0 sessions)"},
                  sessions=0))
    assert "No trading sessions in the requested window" in out
    assert "Retry later" not in out

    # gpw: session files came down fine, nothing matched -> config hint.
    out = run(["--db", db, "ingest"],
              stooq.IngestReport(failures={"pko": "no ISIN in universe config"},
                                 sessions=3))
    assert "no bars matched the configured universe" in out

    # stooq keeps its own bot-check message.
    out = run(["--db", db, "ingest", "--source", "stooq"],
              stooq.IngestReport(failures={"pko": "Access denied"}))
    assert "Stooq is refusing automated CSV access" in out
    assert "GPW archive requests are failing" not in out

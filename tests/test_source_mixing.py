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

def test_migration_backfills_preexisting_rows_as_gpw():
    conn = connect(":memory:")
    # pre-`source` schema, as shipped before this migration existed
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

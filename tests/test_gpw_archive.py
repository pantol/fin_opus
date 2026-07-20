"""GPW archive ingestion tests — fully offline (injected session/index seams).

Covers: pure parsing of session grids and chart-json payloads, point-in-time
as_of_date == session date, universe ISIN matching, full-market mode
(anti-survivorship), per-day failure resilience, and honest reporting of
universe entries without ISINs or with ISINs absent from the files.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.ingestion import gpw_archive as gpw
from app.ingestion.stooq import Bar

HEADER = ["Data", "Nazwa", "ISIN", "Waluta", "Kurs otwarcia", "Kurs max",
          "Kurs min", "Kurs zamknięcia", "Zmiana", "Wolumen",
          "Liczba Transakcji", "Obrót", "x", "y", "z"]


def _grid(rows):
    return [HEADER] + rows


def test_rows_from_grid_parses_and_skips_suspended():
    grid = _grid([
        ["2026-06-26", "PKOBP", "PLPKO0000016", "PLN",
         103.9, 103.9, 102.58, 103.62, -0.56, 1902333.0, 9825.0, 196135.28, 0, 0, 0],
        # suspended instrument: empty prices -> skipped
        ["2026-06-26", "DEADCO", "PLDEAD000013", "PLN",
         "", "", "", "", "", "", "", "", 0, 0, 0],
    ])
    rows = gpw.rows_from_grid(grid)
    assert len(rows) == 1
    r = rows[0]
    assert (r.name, r.isin, r.close, r.volume) == ("PKOBP", "PLPKO0000016", 103.62, 1902333.0)


def test_rows_from_grid_rejects_unknown_header():
    with pytest.raises(gpw.GpwArchiveError):
        gpw.rows_from_grid([["totally", "different", "file"], ["a", "b", "c"]])


def test_bars_from_chart_json_dates_and_base_repair():
    # t = Warsaw midnight of the session date; first point has o=0 (base quirk).
    payload = [{"data": [
        {"t": 1104706800, "o": 0, "c": 1966.69, "h": 1966.69, "l": 1966.69},
        {"t": 1104966000, "o": 1960.0, "c": 1970.5, "h": 1975.0, "l": 1955.0},
    ]}]
    bars = gpw.bars_from_chart_json(payload)
    assert bars[0].date == "2005-01-03" and bars[0].as_of_date == "2005-01-03"
    assert bars[0].open == bars[0].close == 1966.69  # repaired base bar
    assert bars[1].date == "2005-01-06" and bars[1].open == 1960.0


def _universe():
    return {
        "benchmark": {"ticker": "wig20tr", "name": "WIG20TR", "is_index": True},
        "indices": [],
        "instruments": [
            {"ticker": "pko", "name": "PKO BP", "isin": "PLPKO0000016",
             "sector": "banking"},
            {"ticker": "noisin", "name": "No Isin Co"},
        ],
    }


def _session_rows_for(day: date):
    if day.weekday() >= 5:
        return None
    d = day.isoformat()
    return [
        gpw.SessionRow(d, "PKOBP", "PLPKO0000016", "PLN", 10.0, 10.5, 9.8, 10.2, 1000.0),
        # a company NOT in the universe (dead ticker in full-market mode)
        gpw.SessionRow(d, "DEADCO", "PLDEAD000013", "PLN", 1.0, 1.1, 0.9, 1.0, 500.0),
        # non-PLN line is always skipped
        gpw.SessionRow(d, "EUROCO", "PLEURO000011", "EUR", 5.0, 5.0, 5.0, 5.0, 10.0),
    ]


def _index_bars(name, start, end):
    assert name == "WIG20TR"
    return [Bar("2026-06-25", "2026-06-25", 8100.0, 8200.0, 8050.0, 8150.0, 0.0),
            Bar("2026-06-26", "2026-06-26", 8150.0, 8250.0, 8100.0, 8200.0, 0.0)]


def test_ingest_range_universe_only(conn):
    report = gpw.ingest_range(
        conn, _universe(), date(2026, 6, 25), date(2026, 6, 28),  # Thu..Sun
        delay_seconds=0.0,
        fetch_session_rows=_session_rows_for, fetch_index_bars=_index_bars,
    )
    assert report.counts["wig20tr"] == 2
    assert report.counts["pko"] == 2          # Thu + Fri sessions; weekend skipped
    assert "noisin" in report.failures        # no ISIN -> honestly reported
    # not in universe and full_market=False -> not stored
    n = conn.execute(
        "SELECT COUNT(*) FROM instruments WHERE ticker LIKE 'pldead%'").fetchone()[0]
    assert n == 0
    # point-in-time: as_of_date == session date on every stored bar
    rows = conn.execute("SELECT date, as_of_date FROM prices").fetchall()
    assert rows and all(r["date"] == r["as_of_date"] for r in rows)


def test_ingest_range_sessionless_window_is_benign(conn):
    """A weekend/holiday-only incremental window must yield ZERO failures and
    skip the index fetch entirely (the chart-json endpoint answers sessionless
    ranges with a request echo, not bars). `make signals` runs ingest first —
    a phantom non-zero exit on a weekday holiday would silently skip the
    whole evening loop."""
    uni = _universe()
    uni["instruments"] = [e for e in uni["instruments"] if e.get("isin")]

    def no_index(name, start, end):
        raise AssertionError("index fetch must be skipped for a sessionless window")

    report = gpw.ingest_range(
        conn, uni, date(2026, 7, 18), date(2026, 7, 19),  # Sat..Sun
        delay_seconds=0.0,
        fetch_session_rows=_session_rows_for, fetch_index_bars=no_index,
    )
    assert report.sessions == 0
    assert report.counts == {} and report.failures == {}


def test_ingest_range_full_market_stores_dead_tickers(conn):
    report = gpw.ingest_range(
        conn, _universe(), date(2026, 6, 25), date(2026, 6, 26),
        full_market=True, delay_seconds=0.0,
        fetch_session_rows=_session_rows_for, fetch_index_bars=_index_bars,
    )
    # DEADCO auto-created keyed by its ISIN (anti-survivorship)
    row = conn.execute(
        "SELECT id, name FROM instruments WHERE ticker = 'pldead000013'").fetchone()
    assert row is not None and row["name"] == "DEADCO"
    assert report.counts["pldead000013"] == 2
    # EUR line still excluded
    assert conn.execute(
        "SELECT COUNT(*) FROM instruments WHERE ticker LIKE 'pleuro%'").fetchone()[0] == 0


def test_ingest_range_one_bad_day_does_not_abort(conn):
    def flaky(day: date):
        if day == date(2026, 6, 25):
            raise gpw.GpwArchiveError("boom")
        return _session_rows_for(day)

    report = gpw.ingest_range(
        conn, _universe(), date(2026, 6, 25), date(2026, 6, 26),
        delay_seconds=0.0, fetch_session_rows=flaky, fetch_index_bars=_index_bars,
    )
    assert "session:2026-06-25" in report.failures
    assert report.counts["pko"] == 1          # Friday still ingested


def test_ingest_range_index_name_falls_back_to_ticker(conn):
    # Config name "WIG20 Total Return" is unknown to GPW files; the ticker
    # "wig20tr" (upper-cased by the resolver) is the short name that matches.
    universe = {
        "benchmark": {"ticker": "wig20tr", "name": "WIG20 Total Return",
                      "is_index": True},
        "instruments": [],
    }

    def picky_index_bars(name, start, end):
        if name != "wig20tr":
            raise gpw.GpwArchiveError(f"index '{name}' not present")
        return [Bar("2026-06-26", "2026-06-26", 8150.0, 8250.0, 8100.0, 8200.0, 0.0)]

    report = gpw.ingest_range(
        conn, universe, date(2026, 6, 26), date(2026, 6, 26),
        delay_seconds=0.0,
        fetch_session_rows=lambda d: [], fetch_index_bars=picky_index_bars,
    )
    assert report.counts["wig20tr"] == 1 and not report.failures


def test_ingest_range_reports_isin_never_seen(conn):
    universe = {
        "benchmark": {"ticker": "wig20tr", "name": "WIG20TR", "is_index": True},
        "instruments": [{"ticker": "ghost", "name": "Ghost", "isin": "PLGHOST00010"}],
    }
    report = gpw.ingest_range(
        conn, universe, date(2026, 6, 25), date(2026, 6, 26),
        delay_seconds=0.0,
        fetch_session_rows=_session_rows_for, fetch_index_bars=_index_bars,
    )
    assert "ghost" in report.failures and "not found in any session file" in report.failures["ghost"]


def test_ingest_range_delisted_absence_is_not_a_failure(conn):
    # A window entirely AFTER an instrument's delisting: its absence from the
    # session files is expected, so incremental runs must not fail forever.
    universe = {
        "benchmark": {"ticker": "wig20tr", "name": "WIG20TR", "is_index": True},
        "instruments": [
            {"ticker": "dead", "name": "Dead Co", "isin": "PLDEADX00011",
             "listed_from": "2000-01-01", "delisted_on": "2018-06-30"},
            {"ticker": "unborn", "name": "Future Co", "isin": "PLFUTUR00015",
             "listed_from": "2030-01-01"},
        ],
    }
    report = gpw.ingest_range(
        conn, universe, date(2026, 6, 25), date(2026, 6, 26),
        delay_seconds=0.0,
        fetch_session_rows=lambda d: [], fetch_index_bars=_index_bars,
    )
    assert "dead" not in report.failures
    assert "unborn" not in report.failures


def test_default_ingest_end_excludes_today_until_session_close():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.cli import _default_ingest_end

    warsaw = ZoneInfo("Europe/Warsaw")
    # Intraday (09:30): today's archive file holds PARTIAL bars -> yesterday.
    morning = datetime(2026, 7, 2, 9, 30, tzinfo=warsaw)
    assert _default_ingest_end(morning) == date(2026, 7, 1)
    # After the close (18:05): today's bar is final -> today.
    evening = datetime(2026, 7, 2, 18, 5, tzinfo=warsaw)
    assert _default_ingest_end(evening) == date(2026, 7, 2)


def test_ingest_range_is_idempotent(conn):
    for _ in range(2):
        gpw.ingest_range(
            conn, _universe(), date(2026, 6, 25), date(2026, 6, 26),
            delay_seconds=0.0,
            fetch_session_rows=_session_rows_for, fetch_index_bars=_index_bars,
        )
    n = conn.execute(
        "SELECT COUNT(*) FROM prices p JOIN instruments i ON i.id = p.instrument_id "
        "WHERE i.ticker = 'pko'").fetchone()[0]
    assert n == 2

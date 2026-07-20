"""Delayed intraday recorder + stop monitor — fully offline (injected fetch).

Covers: chart-payload parsing (null gaps skipped, in-progress last bar
dropped), append-only storage (first write wins), per-ticker failure
isolation, point-in-time as_of_ts stamping, the session window, and the
monitor's warning states + dedupe + its hard boundary: ZERO writes to any
money table.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.alerts import monitor, telegram
from app.ingestion import intraday
from app.ingestion.stooq import upsert_instrument

WARSAW = intraday.WARSAW

DAY = datetime(2026, 7, 20, tzinfo=WARSAW)  # a Monday


def _ts(hour, minute):
    return int(DAY.replace(hour=hour, minute=minute).timestamp())


def _chart_payload(bars):
    """bars: list of (hour, minute, o, h, l, c, v); None o/h/l/c = gap."""
    return {"chart": {"result": [{
        "timestamp": [_ts(h, m) for h, m, *_ in bars],
        "indicators": {"quote": [{
            "open": [b[2] for b in bars],
            "high": [b[3] for b in bars],
            "low": [b[4] for b in bars],
            "close": [b[5] for b in bars],
            "volume": [b[6] for b in bars],
        }]},
    }]}}


def test_yahoo_symbol_maps_to_wse():
    assert intraday.yahoo_symbol("pko") == "PKO.WA"


def test_bars_from_chart_skips_gaps_and_drops_forming_last_bar():
    payload = _chart_payload([
        (9, 5, 10.0, 10.2, 9.9, 10.1, 500.0),
        (9, 10, None, None, None, None, 0.0),   # no trades in interval
        (9, 15, 10.1, 10.3, 10.0, 10.2, 300.0),
        (9, 20, 10.2, 10.2, 10.2, 10.2, 10.0),  # newest: may still be forming
    ])
    bars = intraday.bars_from_chart(payload)
    assert [b.close for b in bars] == [10.1, 10.2]
    assert bars[0].bar_start == DAY.replace(hour=9, minute=5).isoformat()
    assert bars[0].bar_start.endswith("+02:00")  # Warsaw offset preserved


def test_bars_from_chart_rejects_garbage():
    with pytest.raises(intraday.IntradayError):
        intraday.bars_from_chart({"finance": {"error": "boom"}})


def test_bars_from_chart_empty_before_open():
    assert intraday.bars_from_chart({"chart": {"result": [{}]}}) == []


def test_store_is_append_only(conn):
    iid = upsert_instrument(conn, {"ticker": "aaa", "name": "AAA"})
    bar = intraday.IntradayBar(DAY.replace(hour=10).isoformat(),
                               10.0, 10.5, 9.9, 10.2, 100.0)
    kw = dict(interval_min=5, source="yahoo_delayed", as_of_ts="t0")
    assert intraday.store_intraday_bars(conn, iid, [bar], **kw) == 1
    # a re-observation NEVER rewrites the stored bar (first write wins)
    revised = intraday.IntradayBar(bar.bar_start, 11.0, 11.0, 11.0, 11.0, 1.0)
    assert intraday.store_intraday_bars(conn, iid, [revised], **kw) == 0
    row = conn.execute("SELECT close, as_of_ts FROM prices_intraday").fetchone()
    assert row["close"] == 10.2 and row["as_of_ts"] == "t0"


def test_record_cycle_isolates_failures_and_stamps_as_of(conn):
    for tk in ("aaa", "bbb"):
        upsert_instrument(conn, {"ticker": tk, "name": tk.upper()})
    universe = {"instruments": [
        {"ticker": "aaa"},
        {"ticker": "bbb"},
        {"ticker": "dead", "delisted_on": "2020-01-01"},  # never fetched
        {"ticker": "nocov"},  # feed-gap ticker: skipped, no failure entry
    ]}
    calls = []

    def fetch(symbol, interval_min):
        calls.append(symbol)
        if symbol == "BBB.WA":
            raise intraday.IntradayError("HTTP 429")
        return _chart_payload([
            (9, 5, 10.0, 10.2, 9.9, 10.1, 500.0),
            (9, 10, 10.1, 10.3, 10.0, 10.2, 300.0),
        ])

    now = DAY.replace(hour=9, minute=30)
    report = intraday.record_cycle(conn, universe, fetch_chart=fetch,
                                   delay_seconds=0.0, now=now, skip={"nocov"})
    assert report.counts == {"aaa": 1}          # last bar dropped -> 1 stored
    assert report.failures == {"bbb": "HTTP 429"}  # skip-listed ticker absent
    assert calls == ["AAA.WA", "BBB.WA"]        # delisted + skipped not fetched
    row = conn.execute("SELECT as_of_ts, bar_start FROM prices_intraday").fetchone()
    # point-in-time: the bar is stamped with OUR observation time, after bar start
    assert row["as_of_ts"] == now.isoformat() and row["as_of_ts"] > row["bar_start"]


def test_session_window():
    cfg = {"session": {"days": [0, 1, 2, 3, 4], "start": "09:05", "end": "17:30"}}
    assert intraday.session_is_open(cfg, DAY.replace(hour=10))
    assert not intraday.session_is_open(cfg, DAY.replace(hour=8))
    saturday = DAY + timedelta(days=5)
    assert not intraday.session_is_open(cfg, saturday.replace(hour=10))


# --- monitor -------------------------------------------------------------------

def _seed_position(conn, ticker="pko", stop=100.0, close=None, qty=10):
    iid = upsert_instrument(conn, {"ticker": ticker, "name": ticker.upper()})
    conn.execute(
        "INSERT INTO positions (user_id, instrument_id, qty, entry_date,"
        " entry_price, stop_price, status) VALUES (?, ?, ?, ?, ?, ?, 'OPEN')",
        ("paper:default", iid, qty, "2026-07-14", 110.0, stop))
    if close is not None:
        intraday.store_intraday_bars(
            conn, iid,
            [intraday.IntradayBar(DAY.replace(hour=10, minute=5).isoformat(),
                                  close, close, close, close, 100.0)],
            interval_min=5, source="yahoo_delayed",
            as_of_ts=DAY.replace(hour=10, minute=25).isoformat())
    return iid


def _money_state(conn):
    return [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("decisions", "trades", "paper_orders")] + [
        tuple(r) for r in conn.execute(
            "SELECT qty, stop_price, status FROM positions ORDER BY id")]


def test_monitor_breach_warns_once_and_touches_no_money_tables(conn):
    _seed_position(conn, stop=100.0, close=99.0)
    before = _money_state(conn)
    sent = []
    now = DAY.replace(hour=10, minute=30)
    warnings = monitor.check_positions(conn, send_fn=sent.append, now=now)
    assert [w["state"] for w in warnings] == [monitor.STOP_BREACH]
    assert "PONIZEJ" in sent[0] and "opoznione" in sent[0] and "PKO" in sent[0]
    # dedupe: the same state never re-alerts within the session
    assert monitor.check_positions(conn, send_fn=sent.append, now=now) == []
    assert len(sent) == 1
    assert _money_state(conn) == before  # the monitor decided NOTHING


def test_monitor_near_stop_state(conn):
    _seed_position(conn, stop=100.0, close=101.5)
    sent = []
    warnings = monitor.check_positions(conn, near_pct=0.02, send_fn=sent.append,
                                       now=DAY.replace(hour=11))
    assert [w["state"] for w in warnings] == [monitor.NEAR_STOP]
    assert "blisko stopa" in sent[0]


def test_monitor_quiet_when_price_safe_or_data_stale(conn):
    _seed_position(conn, ticker="aaa", stop=100.0, close=105.0)  # safe
    iid = _seed_position(conn, ticker="bbb", stop=100.0)          # stale bar
    intraday.store_intraday_bars(
        conn, iid,
        [intraday.IntradayBar((DAY - timedelta(days=3)).isoformat(),
                              99.0, 99.0, 99.0, 99.0, 1.0)],
        interval_min=5, source="yahoo_delayed", as_of_ts="t")
    assert monitor.check_positions(conn, send_fn=None,
                                   now=DAY.replace(hour=11)) == []


def test_intraday_warning_card_is_polish_and_informational():
    card = telegram.format_intraday_warning_pl({
        "state": "NEAR_STOP", "ticker": "kgh", "price": 292.0,
        "stop_price": 287.59, "qty": 28,
        "bar_start": "2026-07-20T10:05:00+02:00"})
    assert card.splitlines()[0].startswith("🟡")
    assert "KGH" in card and "WYLACZNIE na zamknieciu" in card

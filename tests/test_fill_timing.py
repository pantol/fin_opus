"""Pack A.1: next-open fill enforcement.

Signals are computed on day T's close; execution must happen on a LATER bar
(default T+1) at that bar's OPEN. No fill may ever derive from the signal
day's close, same-bar fills are rejected outright, and fill-bar anomalies
(open missing, bar missing) leave an audit trace instead of passing silently.
"""
import numpy as np
import pandas as pd
import pytest

from app import config as cfg
from app.backtest import engine
from app.ingestion import stooq

from tests.conftest import make_stooq_csv, synthetic_series


def _ingest(conn, ticker, rows, **inst):
    iid = stooq.upsert_instrument(conn, {"ticker": ticker, "name": ticker, **inst},
                                  is_index=inst.get("is_index", False))
    stooq.store_bars(conn, iid, stooq.parse_csv(make_stooq_csv(rows)), source="stooq")
    return iid


def _seed(conn):
    _ingest(conn, "wig20tr", synthetic_series(n=900, base=2000, drift=0.0005), is_index=True)
    _ingest(conn, "aaa", synthetic_series(n=900, base=100, drift=0.0009), sector="tech",
            listed_from="2015-01-01")
    _ingest(conn, "bbb", synthetic_series(n=900, base=50, drift=0.0006), sector="banking",
            listed_from="2015-01-01")
    conn.commit()


def _universe():
    return {
        "benchmark": {"ticker": "wig20tr", "is_index": True},
        "indices": [],
        "instruments": [{"ticker": "aaa", "sector": "tech"},
                        {"ticker": "bbb", "sector": "banking"}],
    }


def _run(conn, bt_cfg=None):
    uni = _universe()
    bt_cfg = bt_cfg or cfg.load_backtest_config()
    strat = cfg.load_strategy("trend_momentum")
    instruments, bench = engine.load_instruments(conn, uni, "wig20tr")
    return engine.run_walk_forward(instruments, bench, strat, bt_cfg), instruments


def test_no_fill_on_signal_day(conn):
    """Every fill happens strictly after the decision date."""
    _seed(conn)
    res, _ = _run(conn)
    assert res.decisions, "expected trades in the synthetic run"
    for d in res.decisions:
        assert d["fill_date"] > d["decision_date"], (
            f"{d['action']} {d['ticker']}: filled {d['fill_date']} "
            f"on/before signal day {d['decision_date']}"
        )


def test_fill_price_derives_from_fill_day_open(conn):
    """Fill price = fill-day OPEN +/- half-spread and slippage, never T's close."""
    _seed(conn)
    res, instruments = _run(conn)
    inst_by_ticker = {i.ticker: i for i in instruments}
    costs = cfg.load_backtest_config()["costs"]
    half_spread = costs["spread_bps"] * 1e-4 / 2.0
    slip = costs["slippage_bps"] * 1e-4
    checked = 0
    for d in res.decisions:
        inst = inst_by_ticker[d["ticker"]]
        fill_day = pd.Timestamp(d["fill_date"])
        if fill_day not in inst.prices.index:
            continue  # forced end-of-run close uses the close reference
        ref = float(inst.prices.at[fill_day, "open"])
        if np.isnan(ref):
            continue
        factor = (1 + half_spread + slip) if d["action"] == "ENTER" else (1 - half_spread - slip)
        assert d["price"] == pytest.approx(ref * factor, rel=1e-9)
        checked += 1
    assert checked > 0


def test_same_bar_fill_lag_rejected(conn):
    """signal_to_fill_lag_days < 1 must raise: it would peek at the signal bar."""
    _seed(conn)
    uni = _universe()
    bt_cfg = dict(cfg.load_backtest_config())
    bt_cfg["execution"] = {"signal_to_fill_lag_days": 0}
    strat = cfg.load_strategy("trend_momentum")
    instruments, bench = engine.load_instruments(conn, uni, "wig20tr")
    with pytest.raises(ValueError, match="signal_to_fill_lag_days"):
        engine.run_backtest(instruments, bench, strat, bt_cfg)


def _always_enter_strategy():
    return {
        "name": "always", "version": 1,
        "entry": {"all": [{"feature": "close", "op": "gt", "value": 0}]},
        "exit": {"any": [{"type": "atr_stop", "atr_mult": 2.5}]},
        "risk": {"risk_per_trade": 0.01, "atr_mult_stop": 2.5,
                 "max_open_positions": 8, "max_exposure_per_name": 0.20,
                 "max_total_exposure": 1.0, "drawdown_circuit_breaker": 0.25},
    }


def test_missing_fill_bar_is_audited_not_silent(conn):
    """An order whose fill bar does not exist lapses WITH an audit record."""
    # Instrument "hole" is missing exactly one session that exists on the wider
    # trading calendar (carried by "filler", a second traded instrument), placed
    # right after hole's first tradable signal.
    rows = synthetic_series(n=60, base=100, drift=0.0)
    hole_rows = rows[:20] + rows[21:]  # drop session index 20
    _ingest(conn, "wig20tr", rows, is_index=True)
    _ingest(conn, "filler", rows, sector="y", listed_from="2015-01-01")
    # alive only from session 19 -> first signal at 19, fill scheduled for the
    # dropped session 20
    _ingest(conn, "hole", hole_rows, sector="x", listed_from=rows[19][0])
    conn.commit()

    uni = {"benchmark": {"ticker": "wig20tr", "is_index": True}, "indices": [],
           "instruments": [{"ticker": "hole", "sector": "x"},
                           {"ticker": "filler", "sector": "y"}]}
    instruments, bench = engine.load_instruments(conn, uni, "wig20tr")
    bt_cfg = dict(cfg.load_backtest_config())
    res = engine.run_backtest(instruments, bench, _always_enter_strategy(), bt_cfg)

    lapsed = [a for a in res.fill_anomalies if a["type"] == "order_lapsed_no_bar"]
    assert lapsed, "missing fill bar must be recorded as an anomaly"
    assert lapsed[0]["ticker"] == "hole"
    # the lapse day is the dropped session
    assert lapsed[0]["fill_date"] == rows[20][0]


def test_nan_open_falls_back_to_close_with_audit(conn):
    """A NULL open on the fill bar falls back to that bar's close (never NaN)."""
    rows = synthetic_series(n=60, base=100, drift=0.0)
    _ingest(conn, "wig20tr", rows, is_index=True)
    # alive from session 19 -> first signal at 19, fill on session 20
    iid = _ingest(conn, "nanopen", rows, sector="x", listed_from=rows[19][0])
    # null out the open on session 20 (the fill bar for a signal at 19)
    conn.execute("UPDATE prices SET open = NULL WHERE instrument_id = ? AND date = ?",
                 (iid, rows[20][0]))
    conn.commit()

    uni = {"benchmark": {"ticker": "wig20tr", "is_index": True}, "indices": [],
           "instruments": [{"ticker": "nanopen", "sector": "x"}]}
    instruments, bench = engine.load_instruments(conn, uni, "wig20tr")
    res = engine.run_backtest(instruments, bench, _always_enter_strategy(),
                              cfg.load_backtest_config())

    fallbacks = [a for a in res.fill_anomalies
                 if a["type"] == "open_missing_close_reference"]
    assert fallbacks and fallbacks[0]["ticker"] == "nanopen"
    # no NaN ever reached accounting
    assert not res.equity_curve.isna().any()
    for d in res.decisions:
        assert not np.isnan(d["price"])

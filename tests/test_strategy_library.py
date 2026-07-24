"""Phase 4 — academic strategy library: the new per-instrument features
(mom_12_1, pct_52w_high), cross-sectional percentile attachment (PIT-safe,
strategy-scoped), and end-to-end engine behavior of the four library YAMLs
(xs_momentum, week52_high, low_vol, falling_knife)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from app import config as cfg
from app.backtest import engine
from app.features import compute
from app.features import cross_sectional as xs
from app.ingestion import stooq

from tests.conftest import bt_config_no_gate, make_stooq_csv

LIBRARY = ["xs_momentum", "week52_high", "low_vol", "falling_knife"]


def _price_df(closes, start="2015-01-02"):
    idx = pd.bdate_range(start, periods=len(closes))
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99,
                         "close": c, "volume": 1_000_000.0}, index=idx)


def _closes(*segments, base=100.0):
    out, level = [], base
    for n, drift in segments:
        for _ in range(n):
            level *= (1.0 + drift)
            out.append(level)
    return out


# --- per-instrument features -------------------------------------------------

def test_mom_12_1_skips_the_last_month():
    df = _price_df(_closes((300, 0.001)))
    feats = compute.compute_features(df)
    t = 280
    expected = df["close"].iloc[t - compute.TD_1M] / df["close"].iloc[t - compute.TD_12M] - 1.0
    assert abs(feats["mom_12_1"].iloc[t] - expected) < 1e-12
    assert np.isnan(feats["mom_12_1"].iloc[compute.TD_12M - 1])  # undefined early


def test_pct_52w_high_is_one_at_a_new_high_and_low_after_a_crash():
    closes = _closes((300, 0.002), (60, -0.02))  # long rise then a crash
    feats = compute.compute_features(_price_df(closes))
    assert abs(feats["pct_52w_high"].iloc[299] - 1.0) < 1e-12   # rising = at the high
    assert feats["pct_52w_high"].iloc[-1] < 0.60                # deep below after
    assert np.isnan(feats["pct_52w_high"].iloc[compute.TD_12M - 2])


# --- cross-sectional attachment ----------------------------------------------

class _Inst:
    def __init__(self, ticker, features):
        self.instrument_id = abs(hash(ticker)) % 1000
        self.ticker = ticker
        self.sector = None
        self.listed_from = None
        self.delisted_on = None
        self.prices = None
        self.features = features
        self.llm_scores = None
        self.llm_relevance = None
        self.actions = None


def _inst_with(ticker, mom):
    idx = pd.bdate_range("2020-01-01", periods=3)
    return _Inst(ticker, pd.DataFrame({"mom_12_1": mom, "realized_vol": mom},
                                      index=idx))


def test_xs_percentiles_rank_across_defined_values_only():
    a = _inst_with("aaa", [0.9, 0.9, 0.9])
    b = _inst_with("bbb", [0.5, 0.5, np.nan])
    c = _inst_with("ccc", [0.1, np.nan, np.nan])
    panels = xs.cross_sectional_panels([a, b, c])
    p = panels["mom_12_1_pct"]
    assert list(p.loc[p.index[0], ["aaa", "bbb", "ccc"]]) == [1.0, 2 / 3, 1 / 3]
    # day 2: ccc undefined -> rank over two names; ccc stays NaN
    assert list(p.loc[p.index[1], ["aaa", "bbb"]]) == [1.0, 0.5]
    assert np.isnan(p.loc[p.index[1], "ccc"])
    # day 3: only aaa defined -> pct 1.0 of a one-name cross-section
    assert p.loc[p.index[2], "aaa"] == 1.0


def test_attach_is_strategy_scoped():
    for name in LIBRARY:
        assert xs.strategy_uses_cross_sectional(cfg.load_strategy(name)) == (
            name in ("xs_momentum", "low_vol"))
    assert not xs.strategy_uses_cross_sectional(cfg.load_strategy("trend_momentum"))


def test_library_yamls_parse_and_rank():
    from app.strategy.engine import entry_ranking_spec

    for name in LIBRARY:
        strat = cfg.load_strategy(name)
        assert strat["name"] == name
        assert entry_ranking_spec(strat)  # parses + validates
        assert strat["risk"]["max_total_exposure"] <= 1.0  # no leverage, ever


# --- engine end-to-end -------------------------------------------------------

def _seed(conn, series_by_ticker):
    def rows(closes):
        dates = pd.bdate_range("2015-01-02", periods=len(closes))
        return [(d.date().isoformat(), c, c * 1.01, c * 0.99, c, 1_000_000.0)
                for d, c in zip(dates, closes)]

    for ticker, closes in series_by_ticker.items():
        iid = stooq.upsert_instrument(
            conn, {"ticker": ticker, "name": ticker, "sector": "misc",
                   "listed_from": "2015-01-01"},
            is_index=(ticker == "wig20tr"))
        stooq.store_bars(conn, iid, stooq.parse_csv(make_stooq_csv(rows(closes))),
                         source="stooq")
    conn.commit()
    uni = {"benchmark": {"ticker": "wig20tr", "is_index": True}, "indices": [],
           "instruments": [{"ticker": t, "sector": "misc"}
                           for t in series_by_ticker if t != "wig20tr"]}
    return uni


def test_xs_momentum_enters_the_winner_not_the_loser(conn):
    uni = _seed(conn, {
        "wig20tr": _closes((600, 0.0005), base=2000),
        "win": _closes((600, 0.002)),          # top-percentile 12-1 momentum
        "mid": _closes((600, 0.0005)),
        "los": _closes((600, -0.001)),
    })
    bt = bt_config_no_gate()
    instruments, bench = engine.load_instruments(conn, uni, "wig20tr")
    strat = cfg.load_strategy("xs_momentum")
    instruments = engine.prepare_strategy_inputs(conn, instruments, strat, bt)
    res = engine.run_backtest(instruments, bench, strat, bt)
    entered = {d["ticker"] for d in res.decisions if d["action"] == "ENTER"}
    assert "win" in entered
    assert "los" not in entered


def test_falling_knife_waits_for_stabilization_then_takes_profit(conn):
    # 300 up, 120 hard down (-55%), 40 sideways-drifting-down, then a real
    # recovery. Entry must fire only in the recovery leg; the position must
    # exit on the 85%-of-high target as the name heals.
    knife = _closes((300, 0.002), (120, -0.0066), (40, -0.0005), (140, 0.012))
    uni = _seed(conn, {"wig20tr": _closes((600, 0.0003), base=2000),
                       "kni": knife})
    bt = bt_config_no_gate()
    instruments, bench = engine.load_instruments(conn, uni, "wig20tr")
    strat = cfg.load_strategy("falling_knife")
    instruments = engine.prepare_strategy_inputs(conn, instruments, strat, bt)
    res = engine.run_backtest(instruments, bench, strat, bt)
    entries = [d for d in res.decisions if d["action"] == "ENTER" and d["ticker"] == "kni"]
    assert entries, "the stabilized knife must be entered"
    dates = pd.bdate_range("2015-01-02", periods=600)
    crash_end = dates[459]
    assert all(pd.Timestamp(d["decision_date"]) > crash_end for d in entries), (
        "no entry may fire while the knife is still falling")
    feats = entries[0].get("features") or {}
    assert feats.get("pct_52w_high") is not None and feats["pct_52w_high"] <= 0.60
    exits = [d for d in res.decisions if d["action"] == "EXIT" and d["ticker"] == "kni"]
    assert exits, "the recovery target must eventually close the trade"


def test_low_vol_prefers_the_calm_name(conn):
    rng = np.random.default_rng(7)

    def walk(vol):
        return list(100 * np.cumprod(1 + rng.normal(0.0006, vol, 600)))

    # 5-name cross-section: the calmest name sits at percentile 0.2 (<= 0.30
    # gate); a 2-name universe could never clear it (min pct = 0.5).
    uni = _seed(conn, {"wig20tr": _closes((600, 0.0005), base=2000),
                       "calm": walk(0.004), "m1": walk(0.015), "m2": walk(0.018),
                       "m3": walk(0.022), "wild": walk(0.045)})
    bt = bt_config_no_gate()
    instruments, bench = engine.load_instruments(conn, uni, "wig20tr")
    strat = cfg.load_strategy("low_vol")
    instruments = engine.prepare_strategy_inputs(conn, instruments, strat, bt)
    res = engine.run_backtest(instruments, bench, strat, bt)
    entered = {d["ticker"] for d in res.decisions if d["action"] == "ENTER"}
    assert "calm" in entered
    assert "wild" not in entered


def test_regime_blind_baseline_snapshots_stay_clean_of_xs_keys(conn):
    uni = _seed(conn, {"wig20tr": _closes((400, 0.0005), base=2000),
                       "aaa": _closes((400, 0.001))})
    bt = bt_config_no_gate()
    instruments, bench = engine.load_instruments(conn, uni, "wig20tr")
    strat = cfg.load_strategy("trend_momentum")
    instruments = engine.prepare_strategy_inputs(conn, instruments, strat, bt)
    res = engine.run_backtest(instruments, bench, strat, bt)
    assert all("mom_12_1_pct" not in (d.get("features") or {})
               for d in res.decisions)

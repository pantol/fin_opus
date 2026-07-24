"""Phase 3 — market-regime radar: component math, point-in-time safety, LLM
verdict decay ("a verdict must age out"), hysteresis, the engine entry gate
(entries blocked in risk-off, exits ALWAYS evaluate), the false-alarm report,
and the config-fingerprint rule (regime retunes break only regime-gated books).
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd

from app import config as cfg
from app.alerts import telegram
from app.backtest import engine
from app.features import regime
from app.ingestion import stooq
from app.paper import loop as paper_loop

from tests.conftest import bt_config_no_gate, make_stooq_csv


def _rows(closes, start="2015-01-02"):
    dates = pd.bdate_range(start, periods=len(closes))
    return [(d.date().isoformat(), c, c * 1.01, c * 0.99, c, 1_000_000.0)
            for d, c in zip(dates, closes)]


def _closes(*segments, base=100.0):
    """Chain (n, daily_drift) segments into a deterministic close series."""
    out, level = [], base
    for n, drift in segments:
        for _ in range(n):
            level *= (1.0 + drift)
            out.append(level)
    return out


def _fake_inst(ticker, closes, start="2015-01-02", llm_scores=None):
    dates = pd.bdate_range(start, periods=len(closes))
    prices = pd.DataFrame({"close": closes}, index=dates)
    return SimpleNamespace(ticker=ticker, prices=prices, llm_scores=llm_scores,
                           instrument_id=hash(ticker) % 1000)


def _bench(closes, start="2015-01-02"):
    dates = pd.bdate_range(start, periods=len(closes))
    return pd.Series(closes, index=dates, dtype=float)


BT = {"regime": {}}  # defaults


# --- component math ----------------------------------------------------------

def test_trend_component_sign_follows_benchmark_vs_sma():
    closes = _closes((400, 0.001), (200, -0.005), base=2000)
    bench = _bench(closes)
    feats = regime.compute_market_features([], bench, BT)
    assert feats["market_trend"].iloc[0] > 0.5      # long rise: above SMA
    assert feats["market_trend"].iloc[-1] == -1.0   # deep in the crash: clipped


def test_breadth_neutral_when_too_few_names():
    bench = _bench(_closes((300, 0.001), base=2000))
    insts = [_fake_inst("aaa", _closes((300, 0.001)))]  # 1 name < min_names=10
    feats = regime.compute_market_features(insts, bench, BT)
    assert (feats["market_breadth"] == 0.0).all()   # 2*0.5-1 = neutral


def test_breadth_counts_names_above_their_sma():
    bench = _bench(_closes((300, 0.0005), base=2000))
    bt = {"regime": {"breadth": {"min_names": 2, "stale_limit_sessions": 5}}}
    up = [_fake_inst(f"u{i}", _closes((300, 0.002))) for i in range(3)]
    down = [_fake_inst("d0", _closes((300, -0.002)))]
    feats = regime.compute_market_features(up + down, bench, bt)
    # 3 of 4 names above their SMA -> breadth 0.75 -> component +0.5
    assert abs(feats["market_breadth"].iloc[-1] - 0.5) < 1e-9


def test_no_look_ahead_future_data_never_changes_past_rows():
    closes = _closes((350, 0.001), base=2000)
    insts = [_fake_inst("aaa", _closes((350, 0.001)))]
    feats_a = regime.compute_market_features(insts, _bench(closes), BT)
    boosted = list(closes)
    boosted[-30:] = [c * 3.0 for c in boosted[-30:]]  # violent future move
    insts_b = [_fake_inst("aaa", _closes((350, 0.001)))]
    feats_b = regime.compute_market_features(insts_b, _bench(boosted), BT)
    cutoff = feats_a.index[-40]
    pd.testing.assert_frame_equal(feats_a.loc[:cutoff], feats_b.loc[:cutoff])


# --- LLM verdict decay -------------------------------------------------------

def test_llm_component_decays_and_expires():
    n = 260
    dates = pd.bdate_range("2015-01-02", periods=n)
    verdict_day = dates[220]
    inst = _fake_inst("aaa", _closes((n, 0.0)),
                      llm_scores=pd.Series([0.8], index=[verdict_day]))
    bt = {"regime": {"llm": {"half_life_sessions": 10, "max_age_sessions": 30,
                             "min_count": 1}}}
    bench = _bench(_closes((n, 0.001), base=2000))
    feats = regime.compute_market_features([inst], bench, bt)
    on_day = feats.loc[verdict_day, "market_llm"]
    assert abs(on_day - 0.8) < 1e-6                       # fresh: full weight
    ten_later = feats["market_llm"].iloc[feats.index.get_loc(verdict_day) + 10]
    assert abs(ten_later - 0.4) < 1e-6                    # one half-life
    expired = feats["market_llm"].iloc[feats.index.get_loc(verdict_day) + 31]
    assert expired == 0.0                                 # aged out entirely
    before = feats["market_llm"].iloc[feats.index.get_loc(verdict_day) - 1]
    assert before == 0.0                                  # never before as_of


def test_llm_component_neutral_with_no_verdicts():
    bench = _bench(_closes((260, 0.001), base=2000))
    feats = regime.compute_market_features(
        [_fake_inst("aaa", _closes((260, 0.001)))], bench, BT)
    assert (feats["market_llm"] == 0.0).all()


# --- hysteresis + report -----------------------------------------------------

def test_hysteresis_no_flap_between_thresholds():
    score = np.array([0.5, 0.05, -0.05, -0.2, -0.05, 0.05, 0.2])
    state = regime._hysteresis_state(score, {"risk_on_above": 0.10,
                                             "risk_off_below": -0.10})
    assert list(state) == [1, 1, 1, 0, 0, 0, 1]


def test_false_alarm_report_judges_switches():
    idx = pd.bdate_range("2015-01-02", periods=120)
    # OFF at position 10; benchmark then falls 8% within the horizon.
    feats = pd.DataFrame({
        "market_risk_on": [1.0] * 10 + [0.0] * 110,
        "market_risk_score": [-0.2] * 120,
    }, index=idx)
    bench_falls = pd.Series(
        [100.0] * 11 + list(np.linspace(99, 90, 30)) + [90.0] * 79, index=idx)
    bt = {"regime": {"radar": {"false_alarm": {"dd_threshold": 0.05,
                                               "horizon_sessions": 40}}}}
    rep = regime.false_alarm_report(feats, bench_falls, bt)
    assert rep["n_judged"] == 1 and rep["n_false"] == 0
    bench_flat = pd.Series([100.0] * 120, index=idx)
    rep2 = regime.false_alarm_report(feats, bench_flat, bt)
    assert rep2["n_judged"] == 1 and rep2["n_false"] == 1
    assert rep2["false_alarm_rate"] == 1.0


# --- engine gate -------------------------------------------------------------

def _seed_engine_db(conn):
    def ingest(ticker, closes, **inst):
        iid = stooq.upsert_instrument(
            conn, {"ticker": ticker, "name": ticker, **inst},
            is_index=inst.get("is_index", False))
        stooq.store_bars(conn, iid, stooq.parse_csv(make_stooq_csv(_rows(closes))),
                         source="stooq")
        return iid

    ingest("wig20tr", _closes((400, 0.001), (200, -0.005), base=2000), is_index=True)
    # aaa: rises long enough to be entered, then breaks down IN the risk-off leg.
    ingest("aaa", _closes((450, 0.0009), (150, -0.005)), sector="tech",
           listed_from="2015-01-01")
    # ccc: qualifies (mom6m>0, close>SMA200) only LATE, deep in risk-off.
    ingest("ccc", _closes((450, -0.001), (150, 0.006)), sector="misc",
           listed_from="2015-01-01")
    conn.commit()


def _engine_setup(conn):
    _seed_engine_db(conn)
    uni = {"benchmark": {"ticker": "wig20tr", "is_index": True}, "indices": [],
           "instruments": [{"ticker": "aaa", "sector": "tech"},
                           {"ticker": "ccc", "sector": "misc"}]}
    bt = bt_config_no_gate()
    bt["regime"] = {"weights": {"trend": 1.0, "breadth": 0.0, "vol": 0.0,
                                "drawdown": 0.0, "llm": 0.0}}
    instruments, bench = engine.load_instruments(conn, uni, "wig20tr")
    feats = regime.compute_market_features(instruments, bench, bt)
    off_dates = feats.index[feats["market_risk_on"] == 0.0]
    assert len(off_dates), "scenario must produce a risk-off leg"
    return instruments, bench, bt, off_dates[0]


def test_regime_gate_blocks_new_entries_in_risk_off_but_exits_run(conn):
    instruments, bench, bt, first_off = _engine_setup(conn)
    base_cfg = cfg.load_strategy("trend_momentum")
    reg_cfg = cfg.load_strategy("trend_momentum_regime")

    base_res = engine.run_backtest(instruments, bench, base_cfg, bt)
    reg_res = engine.run_backtest(instruments, bench, reg_cfg, bt)

    def buys(res, tk=None):
        return [d for d in res.decisions if d["action"] == "ENTER"
                and (tk is None or d["ticker"] == tk)]

    # Baseline chases ccc's late breakout inside the crash; the regime book
    # refuses it (that is the whole point of the filter).
    assert any(pd.Timestamp(d["decision_date"]) >= first_off
               for d in buys(base_res, "ccc"))
    assert buys(reg_res, "ccc") == []
    # Both books entered aaa while the regime was ON...
    assert any(pd.Timestamp(d["decision_date"]) < first_off
               for d in buys(reg_res, "aaa"))
    # ...and the regime book still EXITS aaa during risk-off: the gate stops
    # entries, never exits.
    reg_exits = [d for d in reg_res.decisions if d["action"] == "EXIT"
                 and d["ticker"] == "aaa"]
    assert any(pd.Timestamp(d["decision_date"]) >= first_off for d in reg_exits)


def test_regime_blind_strategy_snapshots_carry_no_market_keys(conn):
    instruments, bench, bt, _ = _engine_setup(conn)
    base_res = engine.run_backtest(instruments, bench,
                                   cfg.load_strategy("trend_momentum"), bt)
    assert all("market_risk_on" not in (d.get("features") or {})
               for d in base_res.decisions)


# --- paper fingerprint + radar card ------------------------------------------

def test_config_hash_regime_binds_only_market_gated_strategies():
    uni = {"benchmark": {"ticker": "wig20tr"}, "instruments": []}
    bt_a = bt_config_no_gate()
    bt_b = bt_config_no_gate()
    bt_b["regime"] = {"weights": {"trend": 0.9, "breadth": 0.1, "vol": 0.0,
                                  "drawdown": 0.0, "llm": 0.0}}
    blind = cfg.load_strategy("trend_momentum")
    gated = cfg.load_strategy("trend_momentum_regime")
    assert (paper_loop.config_hash(blind, bt_a, uni)
            == paper_loop.config_hash(blind, bt_b, uni))
    assert (paper_loop.config_hash(gated, bt_a, uni)
            != paper_loop.config_hash(gated, bt_b, uni))


def test_regime_flip_detection_and_card():
    idx = pd.bdate_range("2026-07-20", periods=3)
    feats = pd.DataFrame({
        "market_risk_on": [1.0, 1.0, 0.0],
        "market_risk_score": [0.3, 0.1, -0.25],
        "market_trend": [0.5, 0.1, -0.6], "market_breadth": [0.2, 0.0, -0.1],
        "market_vol": [0.1, 0.0, -0.2], "market_drawdown": [0.9, 0.6, 0.1],
        "market_llm": [0.0, 0.0, 0.1],
    }, index=idx)
    assert paper_loop._regime_flip(feats, idx[1]) is None
    flip = paper_loop._regime_flip(feats, idx[2])
    assert flip["to_state"] == "risk_off"
    card = telegram.format_regime_radar_pl(flip)
    assert "RISK-OFF" in card and "Sesja: " in card
    assert "trend -0.60" in card and "deterministyczne" in card

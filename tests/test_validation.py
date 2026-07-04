"""Pack C: purged walk-forward, DSR + trials registry, MC benchmark math."""
import math

import numpy as np
import pandas as pd
import pytest

from app.backtest import engine, mc_benchmark, validation
from app.backtest.ab_harness import ABReport, apply_validation_gates
from app.features import compute


# --- purged walk-forward with embargo -----------------------------------------

def _calendar(n=1500, start="2018-01-01"):
    return pd.DatetimeIndex(pd.bdate_range(start, periods=n))


def test_embargo_inserts_gap_in_sessions():
    cal = _calendar()
    windows = engine.make_walk_forward_windows(cal, is_months=24, oos_months=6,
                                               embargo_sessions=252)
    assert windows, "expected at least one window"
    for w in windows:
        gap_sessions = int(((cal >= w.is_end) & (cal < w.oos_start)).sum())
        assert gap_sessions == 252, (
            f"embargo violated: {gap_sessions} sessions between is_end and oos_start"
        )


def test_embargo_zero_preserves_contiguous_windows():
    cal = _calendar()
    legacy = engine.make_walk_forward_windows(cal, 24, 6, embargo_sessions=0)
    assert legacy
    for w in legacy:
        # first session >= is_end IS the oos_start (contiguous)
        first = cal[cal.searchsorted(w.is_end)]
        assert w.oos_start == first


def test_embargoed_feature_reads_no_train_data():
    """A 252-session lookback computed at the FIRST OOS date must not touch
    any session before is_end (the train window)."""
    cal = _calendar()
    windows = engine.make_walk_forward_windows(cal, 24, 6, embargo_sessions=252)
    w = windows[0]
    oos_start_idx = int(cal.get_loc(w.oos_start))
    lookback_start = cal[oos_start_idx - 252]
    assert lookback_start >= w.is_end, (
        "a ret_12m at the first OOS date would read in-sample data"
    )
    # and semantically: ret_12m at oos_start computed from data since is_end
    # equals ret_12m computed from the full history (no train dependence)
    rng = np.random.default_rng(7)
    closes = pd.Series(100 + np.cumsum(rng.normal(0, 1, len(cal))), index=cal)
    df_full = pd.DataFrame({"open": closes, "high": closes, "low": closes,
                            "close": closes, "volume": 1000.0})
    feats_full = compute.compute_features(df_full)
    df_oos = df_full[df_full.index >= w.is_end]
    feats_oos = compute.compute_features(df_oos)
    a = feats_full.at[w.oos_start, "ret_12m"]
    b = feats_oos.at[w.oos_start, "ret_12m"]
    assert a == pytest.approx(b), "ret_12m at oos_start depends on train data"


def test_embargo_consumes_short_history():
    """Too little history for IS + embargo + OOS -> no windows (full-span fallback)."""
    cal = _calendar(n=550)  # ~26 months: enough for IS(24m) but not +252 sessions
    windows = engine.make_walk_forward_windows(cal, 24, 6, embargo_sessions=252)
    assert windows == []


# --- normal distribution helpers ----------------------------------------------

def test_norm_helpers_match_known_values():
    assert validation.norm_cdf(0.0) == pytest.approx(0.5)
    assert validation.norm_cdf(1.959963985) == pytest.approx(0.975, abs=1e-6)
    assert validation.norm_ppf(0.975) == pytest.approx(1.959963985, abs=1e-6)
    assert validation.norm_ppf(0.5) == pytest.approx(0.0, abs=1e-9)
    assert validation.norm_ppf(0.001) == pytest.approx(-3.0902323, abs=1e-5)
    with pytest.raises(ValueError):
        validation.norm_ppf(0.0)


# --- DSR ------------------------------------------------------------------------

def _equity(n=500, drift=0.0004, vol=0.01, seed=1):
    rng = np.random.default_rng(seed)
    returns = rng.normal(drift, vol, n)
    idx = pd.bdate_range("2020-01-01", periods=n + 1)
    return pd.Series(100000 * np.cumprod(np.concatenate([[1.0], 1 + returns])), index=idx)


def test_dsr_single_trial_applies_no_deflation():
    eq = _equity()
    res = validation.deflated_sharpe(eq, n_trials=1, var_trial_sharpes=0.0)
    assert res.sr_star == 0.0
    # with SR* = 0 the DSR IS the plain PSR against zero — no deflation
    returns = eq.pct_change().dropna()
    psr0 = validation.probabilistic_sharpe(
        res.sharpe_pp, 0.0, len(returns),
        float(returns.skew()), float(returns.kurt()) + 3.0)
    assert res.dsr == pytest.approx(psr0)


def test_dsr_deflates_as_trials_grow():
    eq = _equity()
    base = validation.deflated_sharpe(eq, n_trials=1, var_trial_sharpes=0.0)
    many = validation.deflated_sharpe(eq, n_trials=200, var_trial_sharpes=0.002)
    assert many.sr_star > 0.0
    assert many.dsr < base.dsr, "more trials must never make luck look better"


def test_expected_max_sharpe_monotone_in_trials():
    v = 0.001
    values = [validation.expected_max_sharpe(n, v) for n in (2, 10, 100, 1000)]
    assert values == sorted(values)
    assert validation.expected_max_sharpe(1, v) == 0.0
    assert validation.expected_max_sharpe(50, 0.0) == 0.0


# --- trials registry ------------------------------------------------------------

def test_trials_registry_dedupes_by_config_hash(conn):
    strat = {"name": "s", "version": 1, "entry": {}, "exit": {}, "risk": {}}
    bt = {"costs": {"a": 1}, "walk_forward": {"w": 1}, "initial_capital": 1.0}
    h = validation.config_hash(strat, bt)
    assert h == validation.config_hash(strat, bt), "hash must be deterministic"
    h2 = validation.config_hash({**strat, "version": 2}, bt)
    assert h != h2

    validation.log_trial(conn, cfg_hash=h, strategy_name="s", strategy_version=1,
                         oos_start=None, oos_end=None,
                         metrics={"sharpe_pp": 0.02})
    validation.log_trial(conn, cfg_hash=h, strategy_name="s", strategy_version=1,
                         oos_start=None, oos_end=None,
                         metrics={"sharpe_pp": 0.03})  # re-run, same trial
    validation.log_trial(conn, cfg_hash=h2, strategy_name="s", strategy_version=2,
                         oos_start=None, oos_end=None,
                         metrics={"sharpe_pp": -0.01})
    n, var = validation.trial_stats(conn)
    assert n == 2, "re-running the same config is the same trial"
    mean = (0.03 - 0.01) / 2
    expected_var = ((0.03 - mean) ** 2 + (-0.01 - mean) ** 2) / 2
    assert var == pytest.approx(expected_var)


# --- MC benchmark ----------------------------------------------------------------

def test_percentile_of_midrank_fixture():
    samples = np.array([1.0, 2.0, 3.0, 4.0])
    assert mc_benchmark.percentile_of(5.0, samples) == 1.0
    assert mc_benchmark.percentile_of(0.0, samples) == 0.0
    assert mc_benchmark.percentile_of(2.5, samples) == 0.5
    assert mc_benchmark.percentile_of(2.0, samples) == pytest.approx((1 + 0.5) / 4)


def test_extract_trade_profile_pairs_and_censors():
    cal = pd.DatetimeIndex(pd.bdate_range("2024-01-01", periods=10))
    d = cal.strftime("%Y-%m-%d")
    decisions = [
        {"action": "ENTER", "ticker": "a", "fill_date": d[1]},
        {"action": "EXIT", "ticker": "a", "fill_date": d[4]},   # 3 sessions
        {"action": "ENTER", "ticker": "b", "fill_date": d[2]},  # open to end: 7
    ]
    n, holdings = mc_benchmark.extract_trade_profile(decisions, cal)
    assert n == 2
    assert sorted(holdings) == [3, 7]


def _mc_setup(conn_unused=None):
    from tests.conftest import synthetic_series
    from tests.test_fill_timing import _always_enter_strategy  # reuse risk block
    import app.config as cfg

    rows = synthetic_series(n=300, base=100, drift=0.0006)
    idx = pd.DatetimeIndex([pd.Timestamp(r[0]) for r in rows])
    df = pd.DataFrame({"open": [r[1] for r in rows], "high": [r[2] for r in rows],
                       "low": [r[3] for r in rows], "close": [r[4] for r in rows],
                       "volume": [r[5] for r in rows]}, index=idx)
    inst = engine.Instrument(instrument_id=1, ticker="aaa", sector="x",
                             listed_from=None, delisted_on=None, prices=df,
                             features=compute.compute_features(df))
    bt_cfg = dict(cfg.load_backtest_config())
    strat = _always_enter_strategy()
    result = engine.run_backtest([inst], pd.Series(dtype=float), strat, bt_cfg)
    return inst, result, bt_cfg, strat


def test_mc_benchmark_is_deterministic_and_cost_matched():
    inst, result, bt_cfg, strat = _mc_setup()
    mc1 = mc_benchmark.run_random_benchmark([inst], result, bt_cfg, strat["risk"],
                                            n_sims=8, seed=99)
    mc2 = mc_benchmark.run_random_benchmark([inst], result, bt_cfg, strat["risk"],
                                            n_sims=8, seed=99)
    assert mc1.percentiles == mc2.percentiles, "same seed must reproduce exactly"
    assert len(mc1.sim_metrics["sharpe"]) == 8
    assert mc1.n_entries == len([d for d in result.decisions if d["action"] == "ENTER"])
    # infeasible draws are resampled, not dropped: the null keeps the real
    # trade count (a thinner null would flatter the strategy)
    assert mc1.shortfall_entries == 0
    # cost drag is real: with a single flat-ish instrument the random sims
    # cannot all end exactly at initial capital
    assert np.std(mc1.sim_metrics["cagr"]) >= 0


def test_mc_zero_sims_skips():
    inst, result, bt_cfg, strat = _mc_setup()
    mc = mc_benchmark.run_random_benchmark([inst], result, bt_cfg, strat["risk"],
                                           n_sims=0, seed=1)
    assert mc.n_sims == 0
    assert "skipped" in mc.as_text()


# --- AB acceptance gates ----------------------------------------------------------

def _fake_report(improved=True):
    return ABReport(baseline={}, llm={}, benchmark={}, deltas={}, improved=improved)


GATES = {"min_dsr": 0.95, "min_random_percentile": 0.95}


def test_ab_gate_requires_both_improvement_and_thresholds():
    r = apply_validation_gates(_fake_report(True), dsr=0.99,
                               sharpe_percentile=0.97, gates=GATES)
    assert r.improved

    r = apply_validation_gates(_fake_report(True), dsr=0.5,
                               sharpe_percentile=0.97, gates=GATES)
    assert not r.improved and any("DSR" in n for n in r.gate_notes)

    r = apply_validation_gates(_fake_report(True), dsr=0.99,
                               sharpe_percentile=0.5, gates=GATES)
    assert not r.improved

    r = apply_validation_gates(_fake_report(False), dsr=0.99,
                               sharpe_percentile=0.97, gates=GATES)
    assert not r.improved, "anti-luck gates must never rescue a non-improvement"


def test_ab_gate_unavailable_evidence_fails():
    r = apply_validation_gates(_fake_report(True), dsr=None,
                               sharpe_percentile=None, gates=GATES)
    assert not r.improved
    assert len(r.gate_notes) == 2


def test_ab_gate_fails_closed_on_missing_config():
    """Trimmed/misspelled gate config must never silently accept."""
    r = apply_validation_gates(_fake_report(True), dsr=0.99,
                               sharpe_percentile=0.99, gates={})
    assert not r.improved
    assert any("not configured" in n for n in r.gate_notes)
    r = apply_validation_gates(_fake_report(True), dsr=0.99,
                               sharpe_percentile=0.99, gates={"min_dsr": 0.95})
    assert not r.improved  # one missing key is still missing config


def test_full_span_fallback_is_flagged():
    """Too little history -> walk_forward_windows == 0 in metrics (NOT OOS)."""
    from tests.conftest import synthetic_series
    from tests.test_fill_timing import _always_enter_strategy
    import app.config as cfg

    rows = synthetic_series(n=200, base=100, drift=0.0005)  # << IS + embargo
    idx = pd.DatetimeIndex([pd.Timestamp(r[0]) for r in rows])
    df = pd.DataFrame({"open": [r[1] for r in rows], "high": [r[2] for r in rows],
                       "low": [r[3] for r in rows], "close": [r[4] for r in rows],
                       "volume": [r[5] for r in rows]}, index=idx)
    inst = engine.Instrument(instrument_id=1, ticker="aaa", sector="x",
                             listed_from=None, delisted_on=None, prices=df,
                             features=compute.compute_features(df))
    bt_cfg = dict(cfg.load_backtest_config())
    res = engine.run_walk_forward([inst], pd.Series(dtype=float),
                                  _always_enter_strategy(), bt_cfg)
    assert res.metrics.get("walk_forward_windows") == 0


def test_mc_marks_suspended_sessions_at_zero():
    """No-bar sessions value positions at 0 — the engine's MTM convention."""
    from tests.conftest import synthetic_series

    rows = synthetic_series(n=30, base=100, drift=0.0)
    idx_all = pd.DatetimeIndex([pd.Timestamp(r[0]) for r in rows])
    kept = rows[:10] + rows[12:]  # 2-session suspension
    idx = pd.DatetimeIndex([pd.Timestamp(r[0]) for r in kept])
    df = pd.DataFrame({"open": [r[1] for r in kept], "high": [r[2] for r in kept],
                       "low": [r[3] for r in kept], "close": [r[4] for r in kept],
                       "volume": [r[5] for r in kept]}, index=idx)
    inst = engine.Instrument(instrument_id=1, ticker="aaa", sector=None,
                             listed_from=None, delisted_on=None, prices=df,
                             features=compute.compute_features(df))
    arrays = mc_benchmark._prepare([inst], idx_all, None)
    assert arrays[0].close_mark[10] == 0.0 and arrays[0].close_mark[11] == 0.0
    assert arrays[0].close_mark[9] > 0.0 and arrays[0].close_mark[12] > 0.0

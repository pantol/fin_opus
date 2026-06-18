"""Strategy engine: signal generation from YAML config (deterministic, no money)."""
from app import config as cfg
from app.strategy.engine import EvalContext, Signal, evaluate


def _strat():
    return cfg.load_strategy("trend_momentum")


def test_enter_when_uptrend_and_momentum_positive():
    feats = {"close_vs_sma200": 0.05, "momentum_6m": 0.10}
    sig = evaluate(_strat(), feats, EvalContext(in_position=False))
    assert sig == Signal.ENTER


def test_no_entry_when_below_sma200():
    feats = {"close_vs_sma200": -0.02, "momentum_6m": 0.10}
    sig = evaluate(_strat(), feats, EvalContext(in_position=False))
    assert sig == Signal.HOLD


def test_no_entry_when_momentum_negative():
    feats = {"close_vs_sma200": 0.05, "momentum_6m": -0.01}
    sig = evaluate(_strat(), feats, EvalContext(in_position=False))
    assert sig == Signal.HOLD


def test_exit_on_trend_break():
    feats = {"close_vs_sma200": -0.01, "momentum_6m": 0.10}
    ctx = EvalContext(in_position=True, last_close=100, stop_price=90)
    assert evaluate(_strat(), feats, ctx) == Signal.EXIT


def test_exit_on_atr_stop_breach():
    feats = {"close_vs_sma200": 0.05, "momentum_6m": 0.10}  # trend still up
    ctx = EvalContext(in_position=True, last_close=89.0, stop_price=90.0)
    assert evaluate(_strat(), feats, ctx) == Signal.EXIT


def test_hold_when_in_position_and_no_exit():
    feats = {"close_vs_sma200": 0.05, "momentum_6m": 0.10}
    ctx = EvalContext(in_position=True, last_close=110.0, stop_price=90.0)
    assert evaluate(_strat(), feats, ctx) == Signal.HOLD


def test_undefined_feature_fails_condition_no_guessing():
    feats = {"close_vs_sma200": None, "momentum_6m": 0.10}
    assert evaluate(_strat(), feats, EvalContext(in_position=False)) == Signal.HOLD


def test_deterministic_same_input_same_output():
    feats = {"close_vs_sma200": 0.05, "momentum_6m": 0.10}
    ctx = EvalContext(in_position=False)
    results = {evaluate(_strat(), feats, ctx) for _ in range(20)}
    assert results == {Signal.ENTER}

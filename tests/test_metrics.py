"""Metrics sanity on known equity curves."""
import numpy as np
import pandas as pd
import pytest

from app.backtest import metrics


def _equity(values, start="2020-01-01"):
    idx = pd.date_range(start, periods=len(values), freq="D")
    return pd.Series(values, index=idx, dtype=float)


def test_max_drawdown_known():
    eq = _equity([100, 120, 90, 110])
    # peak 120 -> trough 90 => -25%
    assert metrics.max_drawdown(eq) == pytest.approx(-0.25)


def test_total_return_and_cagr_positive_for_growth():
    eq = _equity([100.0 * (1.001 ** i) for i in range(400)])
    m = metrics.compute_metrics(eq, trade_pnls=[10, -5, 20], total_buy_notional=1000)
    assert m.total_return > 0
    assert m.cagr > 0
    assert m.max_drawdown <= 0


def test_win_rate_and_profit_factor():
    pnls = [10.0, 20.0, -5.0, -5.0]
    assert metrics.win_rate(pnls) == pytest.approx(0.5)
    # gains 30 / losses 10 = 3.0
    assert metrics.profit_factor(pnls) == pytest.approx(3.0)


def test_profit_factor_no_losses_is_inf():
    assert metrics.profit_factor([1.0, 2.0]) == float("inf")


def test_sharpe_zero_for_constant_equity():
    eq = _equity([100.0] * 50)
    rets = eq.pct_change().dropna()
    assert metrics.sharpe(rets, 252) == 0.0


def test_turnover_reflects_buy_notional():
    eq = _equity([100.0] * 10)
    m = metrics.compute_metrics(eq, trade_pnls=[], total_buy_notional=500.0)
    assert m.turnover == pytest.approx(5.0)  # 500 / avg equity 100

"""Performance metrics computed from an equity curve and trade list.

All metrics are deterministic. Returns/Sharpe etc. are derived from the equity
curve; trade-based metrics (win rate, profit factor) from closed trades.
Reported against the WIG20TR benchmark by the harness — never SPY.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

TRADING_DAYS = 252


@dataclass
class Metrics:
    cagr: float
    sharpe: float
    sortino: float
    max_drawdown: float
    calmar: float
    win_rate: float
    profit_factor: float
    turnover: float
    total_return: float
    n_trades: int

    def as_dict(self) -> dict:
        return asdict(self)


def _periods_per_year(index: pd.DatetimeIndex) -> float:
    if len(index) < 2:
        return TRADING_DAYS
    days = (index[-1] - index[0]).days
    if days <= 0:
        return TRADING_DAYS
    years = days / 365.25
    return len(index) / years if years > 0 else TRADING_DAYS


def cagr(equity: pd.Series) -> float:
    if len(equity) < 2 or equity.iloc[0] <= 0:
        return 0.0
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    if years <= 0:
        return 0.0
    return (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1.0


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    running_max = equity.cummax()
    dd = equity / running_max - 1.0
    return float(dd.min())  # negative number


def sharpe(returns: pd.Series, periods_per_year: float, rf: float = 0.0) -> float:
    if returns.std(ddof=0) == 0 or returns.empty:
        return 0.0
    excess = returns - rf / periods_per_year
    return float(np.sqrt(periods_per_year) * excess.mean() / returns.std(ddof=0))


def sortino(returns: pd.Series, periods_per_year: float, rf: float = 0.0) -> float:
    if returns.empty:
        return 0.0
    excess = returns - rf / periods_per_year
    downside = excess[excess < 0]
    dd_std = downside.std(ddof=0)
    if dd_std == 0 or np.isnan(dd_std):
        return 0.0
    return float(np.sqrt(periods_per_year) * excess.mean() / dd_std)


def profit_factor(trade_pnls: list[float]) -> float:
    gains = sum(p for p in trade_pnls if p > 0)
    losses = -sum(p for p in trade_pnls if p < 0)
    if losses == 0:
        return float("inf") if gains > 0 else 0.0
    return gains / losses


def win_rate(trade_pnls: list[float]) -> float:
    if not trade_pnls:
        return 0.0
    wins = sum(1 for p in trade_pnls if p > 0)
    return wins / len(trade_pnls)


def compute_metrics(
    equity: pd.Series,
    trade_pnls: list[float],
    total_buy_notional: float = 0.0,
) -> Metrics:
    """Build the full metrics bundle from an equity curve and trade PnLs."""
    equity = equity.sort_index()
    rets = equity.pct_change().dropna()
    ppy = _periods_per_year(equity.index)

    mdd = max_drawdown(equity)
    cg = cagr(equity)
    calmar = (cg / abs(mdd)) if mdd < 0 else 0.0
    total_ret = (equity.iloc[-1] / equity.iloc[0] - 1.0) if len(equity) >= 2 else 0.0

    avg_equity = equity.mean() if not equity.empty else 1.0
    turnover = (total_buy_notional / avg_equity) if avg_equity > 0 else 0.0

    return Metrics(
        cagr=cg,
        sharpe=sharpe(rets, ppy),
        sortino=sortino(rets, ppy),
        max_drawdown=mdd,
        calmar=calmar,
        win_rate=win_rate(trade_pnls),
        profit_factor=profit_factor(trade_pnls),
        turnover=turnover,
        total_return=total_ret,
        n_trades=len(trade_pnls),
    )

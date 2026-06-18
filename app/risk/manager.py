"""Deterministic risk layer.

ZERO LLM in this path. All sizing/stop/exposure/circuit-breaker decisions are
pure functions of numbers and config. For identical inputs they produce
identical outputs (no randomness).

Sizing model: fixed-fractional risk.
    risk_amount = equity * risk_per_trade
    stop_distance = atr_mult * ATR
    qty = floor(risk_amount / stop_distance)
Then cap qty by:
    - per-name exposure limit (max_exposure_per_name * equity)
    - per-sector exposure limit (max_exposure_per_sector * equity, incl. existing)
    - remaining total exposure budget (max_total_exposure * equity)
    - available cash
Quantities are whole shares and never negative.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class PortfolioState:
    """Snapshot the risk layer needs. Provided by the backtest engine."""
    equity: float
    cash: float
    peak_equity: float
    open_positions: int = 0
    exposure_by_name: dict[str, float] = field(default_factory=dict)   # ticker -> market value
    exposure_by_sector: dict[str, float] = field(default_factory=dict)  # sector -> market value

    @property
    def total_exposure_value(self) -> float:
        return sum(self.exposure_by_name.values())

    @property
    def drawdown(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, 1.0 - self.equity / self.peak_equity)


@dataclass(frozen=True)
class SizingResult:
    qty: int
    stop_price: float
    rejected_reason: str | None = None

    @property
    def accepted(self) -> bool:
        return self.qty > 0 and self.rejected_reason is None


def compute_stop_price(entry_price: float, atr: float, atr_mult: float) -> float:
    """Initial long stop = entry - atr_mult * ATR (>= 0)."""
    return max(0.0, entry_price - atr_mult * atr)


def update_trailing_stop(current_stop: float, last_close: float, atr: float, atr_mult: float) -> float:
    """Long trailing stop ratchets up only, never down."""
    candidate = last_close - atr_mult * atr
    return max(current_stop, candidate)


def can_open_new_position(state: PortfolioState, risk_cfg: dict) -> tuple[bool, str | None]:
    """Gate checks independent of a specific instrument."""
    if state.open_positions >= int(risk_cfg["max_open_positions"]):
        return False, "max_open_positions"
    if state.drawdown > float(risk_cfg["drawdown_circuit_breaker"]):
        return False, "drawdown_circuit_breaker"
    return True, None


def size_position(
    *,
    entry_price: float,
    atr: float,
    state: PortfolioState,
    risk_cfg: dict,
    ticker: str,
    sector: str | None,
) -> SizingResult:
    """Compute a deterministic, bounded position size for a long entry."""
    if entry_price <= 0 or atr <= 0:
        return SizingResult(0, 0.0, "invalid_price_or_atr")

    ok, reason = can_open_new_position(state, risk_cfg)
    if not ok:
        return SizingResult(0, 0.0, reason)

    atr_mult = float(risk_cfg["atr_mult_stop"])
    stop_price = compute_stop_price(entry_price, atr, atr_mult)
    stop_distance = entry_price - stop_price
    if stop_distance <= 0:
        return SizingResult(0, 0.0, "non_positive_stop_distance")

    risk_amount = state.equity * float(risk_cfg["risk_per_trade"])
    qty = math.floor(risk_amount / stop_distance)
    if qty <= 0:
        return SizingResult(0, stop_price, "risk_budget_too_small")

    # --- exposure caps (all in market-value terms) ---
    name_cap = float(risk_cfg["max_exposure_per_name"]) * state.equity
    name_used = state.exposure_by_name.get(ticker, 0.0)
    qty = _cap_qty_by_value(qty, entry_price, name_cap - name_used)

    if sector is not None and "max_exposure_per_sector" in risk_cfg:
        sector_cap = float(risk_cfg["max_exposure_per_sector"]) * state.equity
        sector_used = state.exposure_by_sector.get(sector, 0.0)
        qty = _cap_qty_by_value(qty, entry_price, sector_cap - sector_used)

    total_cap = float(risk_cfg["max_total_exposure"]) * state.equity
    total_remaining = total_cap - state.total_exposure_value
    qty = _cap_qty_by_value(qty, entry_price, total_remaining)

    # cannot spend more cash than available
    qty = _cap_qty_by_value(qty, entry_price, state.cash)

    if qty <= 0:
        return SizingResult(0, stop_price, "exposure_or_cash_limit")

    return SizingResult(int(qty), stop_price, None)


def _cap_qty_by_value(qty: int, price: float, value_budget: float) -> int:
    """Reduce qty so qty*price <= value_budget. Never negative."""
    if value_budget <= 0:
        return 0
    max_by_value = math.floor(value_budget / price)
    return max(0, min(qty, max_by_value))

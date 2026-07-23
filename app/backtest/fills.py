"""Realistic fill modeling (deterministic).

- Buy at ASK, sell at BID: spread is split half each side around the reference
  price (the next bar's open/close), so a buy pays up and a sell receives less.
- Slippage: a further adverse move scaled by liquidity participation.
- Commission: max(commission_bps * notional, commission_min) per side.
- Volume cap: a single fill cannot exceed `max_volume_participation` of the
  bar's volume (GPW small caps are illiquid). Excess quantity is not filled.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

BPS = 1e-4


@dataclass(frozen=True)
class Fill:
    qty: int            # actually filled quantity (may be < requested due to volume cap)
    price: float        # per-share fill price incl. spread + slippage
    fee: float          # commission for the fill
    slippage: float     # total slippage cost (per fill, money terms)
    requested_qty: int
    capped: bool        # True if volume cap reduced the quantity


def _commission(notional: float, costs: dict) -> float:
    bps = float(costs["commission_bps"]) * BPS
    return max(notional * bps, float(costs["commission_min"]))


def resolve_costs(costs: dict, median_turnover: float | None) -> dict:
    """Effective costs for one order under the liquidity-tier model.

    `costs["liquidity_tiers"]` (optional) is a list of
    {min_median_turnover_pln, spread_bps, slippage_bps}. The tier with the
    HIGHEST floor <= `median_turnover` wins; spread/slippage of that tier
    replace the flat values. Commission is broker-schedule, never tiered.

    - No tiers configured -> `costs` returned unchanged (flat legacy model).
    - `median_turnover` None/NaN (instrument has no full liquidity window)
      -> the LOWEST-floor tier: unknown liquidity is priced at the most
      expensive configured bucket, never optimistically.

    Deterministic pure function; callers pass the POINT-IN-TIME median
    turnover measured on the DECISION session (never the fill bar).
    """
    tiers = costs.get("liquidity_tiers")
    if not tiers:
        return costs
    by_floor = sorted(tiers, key=lambda t: float(t["min_median_turnover_pln"]))
    chosen = by_floor[0]  # conservative fallback: most expensive bucket
    if median_turnover is not None and not math.isnan(median_turnover):
        for tier in by_floor:
            if float(median_turnover) >= float(tier["min_median_turnover_pln"]):
                chosen = tier
    resolved = dict(costs)
    resolved["spread_bps"] = float(chosen["spread_bps"])
    resolved["slippage_bps"] = float(chosen["slippage_bps"])
    return resolved


def apply_volume_cap(requested_qty: int, bar_volume: float, costs: dict) -> int:
    """Maximum fillable quantity given the bar's volume."""
    participation = float(costs["max_volume_participation"])
    max_qty = math.floor(max(0.0, bar_volume) * participation)
    return max(0, min(requested_qty, max_qty))


def simulate_fill(
    *,
    side: str,                 # "BUY" or "SELL"
    requested_qty: int,
    reference_price: float,    # next-bar reference (e.g. open), no same-bar look-ahead
    bar_volume: float,
    costs: dict,
) -> Fill:
    """Produce a deterministic fill for a single order."""
    side = side.upper()
    if side not in ("BUY", "SELL"):
        raise ValueError(f"Unknown side: {side}")
    if requested_qty <= 0 or reference_price <= 0:
        return Fill(0, 0.0, 0.0, 0.0, requested_qty, capped=False)

    qty = apply_volume_cap(requested_qty, bar_volume, costs)
    capped = qty < requested_qty
    if qty <= 0:
        return Fill(0, 0.0, 0.0, 0.0, requested_qty, capped=True)

    half_spread = reference_price * (float(costs["spread_bps"]) * BPS) / 2.0
    slip_per_share = reference_price * (float(costs["slippage_bps"]) * BPS)

    if side == "BUY":
        # pay the ask + slippage
        price = reference_price + half_spread + slip_per_share
    else:
        # receive the bid - slippage
        price = reference_price - half_spread - slip_per_share
    price = max(0.0, price)

    notional = price * qty
    fee = _commission(notional, costs)
    slippage_cost = (half_spread + slip_per_share) * qty

    return Fill(
        qty=qty,
        price=price,
        fee=fee,
        slippage=slippage_cost,
        requested_qty=requested_qty,
        capped=capped,
    )

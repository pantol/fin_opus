"""Random-entry Monte Carlo benchmark: does the strategy beat cost-matched luck?

For a given strategy run, simulate N random strategies with the SAME number of
trades, the SAME holding-period distribution (bootstrapped from the real
trades), the SAME universe/alive/membership gating, the SAME fixed-fractional
sizing rule, and the SAME cost model (next-open fills, spread, commission,
slippage, volume cap) over the SAME window. The strategy's percentile within
that distribution is the evidence: a strategy that does not clearly beat
cost-matched randomness has no edge, even if it beats the index.

Simulation conventions (kept deliberately aligned with the engine):
  - entries fill on the session AFTER the sampled signal session, at the open;
  - positions are valued at the actual close, and at ZERO on sessions with no
    bar (suspensions/delistings) — the engine's mark-to-market convention;
  - cash available to an entry includes only proceeds booked ON OR BEFORE its
    signal session (no look-ahead into future partial-exit fills);
  - infeasible random entries (full book, no eligible instrument, sizing
    reject) are RESAMPLED at a later time instead of dropped, so the null
    strategies carry the same trade count — a thinner null would flatter the
    real strategy.

Known limitation (documented, not hidden): the null strategies have no
trailing-stop overlay — bootstrapped holding periods reproduce the DURATION
distribution of the real trades but not the stop's path-dependent loss
truncation. A strategy whose entries are noise but whose exit rule has value
can therefore look better than this benchmark; the percentile measures
entries+exits jointly against time-based randomness, per the pack spec.
Deterministic given the seed. ZERO LLM.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.backtest import fills as fillmod
from app.backtest import metrics as metricsmod
from app.backtest.engine import Instrument, _member_on, liquidity_gate
from app.risk import manager as risk


@dataclass
class MCResult:
    n_sims: int
    n_entries: int
    holding_sessions: list[int]
    percentiles: dict[str, float] = field(default_factory=dict)
    sim_metrics: dict[str, np.ndarray] = field(default_factory=dict)
    shortfall_entries: int = 0  # entries dropped across all sims (no eligible instrument)

    def as_text(self) -> str:
        if self.n_sims == 0:
            return "Random-entry benchmark: skipped (mc_sims = 0)."
        lines = [f"Random-entry Monte Carlo benchmark (n={self.n_sims} sims, "
                 f"{self.n_entries} trades each, cost-matched):"]
        for metric, pct in self.percentiles.items():
            lines.append(f"  {metric:<14} strategy at percentile {pct:.2f} of randomness")
        if self.shortfall_entries:
            lines.append(f"  note: {self.shortfall_entries} random entries dropped "
                         f"(no eligible instrument at the sampled session)")
        return "\n".join(lines)


def percentile_of(value: float, samples: np.ndarray) -> float:
    """Mid-rank percentile of `value` within `samples` (ties count half)."""
    if len(samples) == 0:
        return 0.5
    below = float(np.count_nonzero(samples < value))
    ties = float(np.count_nonzero(samples == value))
    return (below + 0.5 * ties) / len(samples)


def extract_trade_profile(decisions: list[dict],
                          calendar: pd.DatetimeIndex) -> tuple[int, list[int]]:
    """(n_entries, holding periods in sessions) from the real run's decisions.

    ENTER/EXIT fill dates are paired FIFO per ticker; positions still open at
    the end of the window count as held to the final session (censored).
    """
    pos = {d: i for i, d in enumerate(calendar.strftime("%Y-%m-%d"))}
    open_entries: dict[str, list[int]] = {}
    holdings: list[int] = []
    n_entries = 0
    for d in decisions:
        idx = pos.get(d["fill_date"])
        if idx is None:
            continue
        if d["action"] == "ENTER":
            n_entries += 1
            open_entries.setdefault(d["ticker"], []).append(idx)
        elif d["action"] == "EXIT":
            queue = open_entries.get(d["ticker"])
            if queue:
                holdings.append(max(1, idx - queue.pop(0)))
    last = len(calendar) - 1
    for queue in open_entries.values():
        holdings.extend(max(1, last - e) for e in queue)
    if not holdings:
        holdings = [1]
    return n_entries, holdings


@dataclass
class _InstArrays:
    ticker: str
    sector: str | None
    instrument_id: int
    open: np.ndarray       # NaN where no bar
    close_mark: np.ndarray  # close, 0.0 on sessions with no bar (engine's MTM rule)
    volume: np.ndarray     # 0 where no bar
    atr: np.ndarray        # NaN where unavailable
    has_bar: np.ndarray    # bool
    alive: np.ndarray      # bool (listing window + membership if provided)
    # Point-in-time liquidity per session: turnover_med_63 as of each calendar
    # day (last known value <= day). Drives the SAME entry gate and the SAME
    # liquidity-tiered costs the real strategy pays; has_turnover=False keeps
    # the flat legacy cost model for feature panels without the column.
    turn_at: np.ndarray = None  # type: ignore[assignment]
    has_turnover: bool = False


def _prepare(instruments: list[Instrument], calendar: pd.DatetimeIndex,
             membership: dict[int, list[tuple]] | None) -> list[_InstArrays]:
    out = []
    for inst in instruments:
        prices = inst.prices.reindex(calendar)
        # No forward-fill: the engine marks a position at ZERO on a session
        # with no bar; the null strategies must suffer the same convention or
        # the two sides of the percentile use different accounting.
        close_mark = prices["close"].fillna(0.0).to_numpy(dtype=float)
        atr = (inst.features["atr"].reindex(calendar).to_numpy(dtype=float)
               if "atr" in inst.features.columns else np.full(len(calendar), np.nan))
        has_bar = prices["close"].notna().to_numpy()
        alive = np.ones(len(calendar), dtype=bool)
        if inst.listed_from:
            alive &= np.asarray(calendar >= pd.to_datetime(inst.listed_from))
        if inst.delisted_on:
            alive &= np.asarray(calendar <= pd.to_datetime(inst.delisted_on))
        if membership is not None:
            ranges = membership.get(inst.instrument_id)
            alive &= np.array([_member_on(ranges, day) for day in calendar])
        has_turnover = "turnover_med_63" in inst.features.columns
        turn_at = (inst.features["turnover_med_63"].reindex(calendar).ffill()
                   .to_numpy(dtype=float)
                   if has_turnover else np.full(len(calendar), np.nan))
        out.append(_InstArrays(
            ticker=inst.ticker, sector=inst.sector, instrument_id=inst.instrument_id,
            open=prices["open"].to_numpy(dtype=float),
            close_mark=close_mark,
            volume=prices["volume"].fillna(0.0).to_numpy(dtype=float),
            atr=atr, has_bar=has_bar, alive=alive,
            turn_at=turn_at, has_turnover=has_turnover,
        ))
    return out


def _entry_ok_matrix(arrays: list[_InstArrays], calendar_len: int,
                     bt_cfg: dict) -> np.ndarray:
    """[session, instrument] eligibility for a random ENTRY signal.

    Vectorized version of the engine's per-day candidate conditions: alive,
    bar on the signal session AND the fill session, valid ATR, and the same
    deterministic liquidity floor the real strategy is gated by. A per-attempt
    Python scan over instruments is O(universe) per attempt and dominates the
    simulation at full-market scale; one boolean matrix makes it O(1)-ish.
    """
    gate = liquidity_gate(bt_cfg)
    ok_matrix = np.zeros((calendar_len, len(arrays)), dtype=bool)
    for j, a in enumerate(arrays):
        ok = a.alive & a.has_bar & ~np.isnan(a.atr) & (a.atr > 0)
        if calendar_len:
            ok[:-1] &= a.has_bar[1:]
            ok[-1] = False  # no next session to fill on
        if gate is not None and gate["min_turnover"] > 0.0:
            if a.has_turnover:
                ok &= ~np.isnan(a.turn_at) & (a.turn_at >= gate["min_turnover"])
            else:
                ok[:] = False  # unmeasurable liquidity fails closed (engine parity)
        ok_matrix[:, j] = ok
    return ok_matrix


def _simulate_one(rng: np.random.Generator, arrays: list[_InstArrays],
                  entry_ok: np.ndarray, calendar: pd.DatetimeIndex,
                  n_entries: int, holdings: list[int],
                  bt_cfg: dict, risk_cfg: dict) -> tuple[dict, int]:
    """One random strategy run. Returns (metrics dict, shortfall count).

    Attempts are processed chronologically; an infeasible attempt (full book,
    no eligible instrument, sizing reject, zero fill) is RESAMPLED at a random
    later session so the null strategy keeps the real trade count instead of
    quietly trading less. Cash available at any session is derived from the
    deltas booked up to that session — proceeds of not-yet-executed partial
    exits are never visible early.
    """
    import bisect

    costs = bt_cfg["costs"]
    capital = float(bt_cfg["initial_capital"])
    max_open = int(risk_cfg["max_open_positions"])
    calendar_len = len(calendar)
    last = calendar_len - 1

    peak_equity = capital
    cash_deltas = np.zeros(calendar_len)
    holdings_curve = np.zeros(calendar_len)
    trade_pnls: list[float] = []
    total_buy_notional = 0.0
    open_positions: list[dict] = []  # {arr, qty, entry_price, exit_idx}

    def _cash_at(idx: int) -> float:
        return capital + float(cash_deltas[:idx + 1].sum())

    def _close_position(p: dict, idx: int) -> None:
        """Execute a time-based exit; volume-capped remainders retry on later
        sessions (deltas land on their true sessions, never earlier).

        Costs are resolved ONCE at the exit-decision session (idx - 1, the
        engine's decision->next-open contract); retries keep that tier, exactly
        like the engine's re-queued orders keep their decision-day snapshot.
        """
        arr = p["arr"]
        if arr.has_turnover:
            exit_costs = fillmod.resolve_costs(
                costs, float(arr.turn_at[idx - 1]) if idx >= 1 else None)
        else:
            exit_costs = costs
        remaining = p["qty"]
        t = idx
        while remaining > 0 and t <= last:
            if arr.has_bar[t]:
                ref = arr.open[t] if not np.isnan(arr.open[t]) else arr.close_mark[t]
                if ref and ref > 0:
                    fill = fillmod.simulate_fill(side="SELL", requested_qty=remaining,
                                                 reference_price=float(ref),
                                                 bar_volume=float(arr.volume[t]),
                                                 costs=exit_costs)
                    if fill.qty > 0:
                        cash_deltas[t] += fill.price * fill.qty - fill.fee
                        trade_pnls.append((fill.price - p["entry_price"]) * fill.qty - fill.fee)
                        holdings_curve[t:] -= fill.qty * arr.close_mark[t:]
                        remaining -= fill.qty
            t += 1
        # shares that never found volume stay as market value (0.0 once the
        # instrument stops printing bars) — the engine's forced-close convention

    # chronological attempt queue; failures respawn at a later random session
    attempts = sorted(int(x) for x in rng.integers(0, max(1, last), size=n_entries))
    entered = 0
    budget = n_entries * 10  # hard bound on total attempts
    while attempts and budget > 0:
        budget -= 1
        signal_idx = attempts.pop(0)
        fill_idx = signal_idx + 1

        # execute exits due by now (their proceeds become visible via cash_deltas)
        for p in [p for p in open_positions if p["exit_idx"] <= signal_idx]:
            _close_position(p, p["exit_idx"])
            open_positions.remove(p)

        def _respawn():
            if signal_idx < last - 1:
                bisect.insort(attempts, int(rng.integers(signal_idx + 1, last)))

        if fill_idx > last or len(open_positions) >= max_open:
            _respawn()
            continue

        # ALL eligible instruments at this session, picked uniformly (a partial
        # random scan would miss eligible names and thin out the null). The
        # precomputed matrix folds in the engine's entry conditions, including
        # the liquidity gate the real strategy is subject to.
        eligible_idx = np.flatnonzero(entry_ok[signal_idx])
        if eligible_idx.size == 0:
            _respawn()
            continue
        chosen = arrays[int(eligible_idx[int(rng.integers(0, eligible_idx.size))])]

        cash_now = _cash_at(signal_idx)
        equity_now = cash_now + float(holdings_curve[signal_idx])
        peak_equity = max(peak_equity, equity_now)
        exposure_by_name = {p["arr"].ticker: p["qty"] * p["arr"].close_mark[signal_idx]
                            for p in open_positions}
        exposure_by_sector: dict[str, float] = {}
        for p in open_positions:
            if p["arr"].sector is not None:
                exposure_by_sector[p["arr"].sector] = (
                    exposure_by_sector.get(p["arr"].sector, 0.0)
                    + p["qty"] * p["arr"].close_mark[signal_idx])
        state = risk.PortfolioState(
            equity=equity_now, cash=max(0.0, cash_now), peak_equity=peak_equity,
            open_positions=len(open_positions),
            exposure_by_name=exposure_by_name, exposure_by_sector=exposure_by_sector,
        )
        sizing = risk.size_position(
            entry_price=float(chosen.close_mark[signal_idx]),
            atr=float(chosen.atr[signal_idx]), state=state, risk_cfg=risk_cfg,
            ticker=chosen.ticker, sector=chosen.sector,
        )
        if not sizing.accepted:
            _respawn()
            continue

        ref = chosen.open[fill_idx]
        if np.isnan(ref) or ref <= 0:
            ref = chosen.close_mark[fill_idx]
        # Same tier resolution as the engine: liquidity measured on the SIGNAL
        # session (point-in-time), never the fill bar.
        entry_costs = (fillmod.resolve_costs(costs, float(chosen.turn_at[signal_idx]))
                       if chosen.has_turnover else costs)
        fill = fillmod.simulate_fill(side="BUY", requested_qty=sizing.qty,
                                     reference_price=float(ref),
                                     bar_volume=float(chosen.volume[fill_idx]),
                                     costs=entry_costs)
        if fill.qty <= 0:
            _respawn()
            continue
        cash_deltas[fill_idx] -= fill.price * fill.qty + fill.fee
        total_buy_notional += fill.price * fill.qty
        holdings_curve[fill_idx:] += fill.qty * chosen.close_mark[fill_idx:]
        holding = int(rng.choice(np.asarray(holdings)))
        open_positions.append({"arr": chosen, "qty": fill.qty,
                               "entry_price": fill.price,
                               "exit_idx": min(fill_idx + holding, last)})
        entered += 1
        if entered >= n_entries:
            break

    # remaining exits (and end-of-window forced closes)
    for p in sorted(open_positions, key=lambda p: p["exit_idx"]):
        _close_position(p, p["exit_idx"])

    equity = capital + np.cumsum(cash_deltas) + holdings_curve
    series = pd.Series(equity, index=calendar)  # real dates: annualization needs the span
    m = metricsmod.compute_metrics(series, trade_pnls, total_buy_notional)
    return m.as_dict(), n_entries - entered


def run_random_benchmark(instruments: list[Instrument], real_result, bt_cfg: dict,
                         risk_cfg: dict, *, n_sims: int, seed: int,
                         membership: dict[int, list[tuple]] | None = None) -> MCResult:
    """Percentile of the real strategy vs `n_sims` cost-matched random runs."""
    calendar = real_result.equity_curve.index
    n_entries, holdings = extract_trade_profile(real_result.decisions, calendar)
    result = MCResult(n_sims=n_sims, n_entries=n_entries, holding_sessions=holdings)
    if n_sims <= 0 or n_entries == 0 or len(calendar) < 3:
        result.n_sims = 0
        return result

    arrays = _prepare(instruments, calendar, membership)
    entry_ok = _entry_ok_matrix(arrays, len(calendar), bt_cfg)
    sims: dict[str, list[float]] = {"cagr": [], "sharpe": [], "max_drawdown": []}
    for i in range(n_sims):
        rng = np.random.default_rng([int(seed), i])
        metrics, shortfall = _simulate_one(rng, arrays, entry_ok, calendar,
                                           n_entries, holdings, bt_cfg, risk_cfg)
        result.shortfall_entries += shortfall
        for k in sims:
            sims[k].append(float(metrics[k]))

    for k, values in sims.items():
        arr = np.asarray(values)
        result.sim_metrics[k] = arr
        # higher is better for all three (max_drawdown is negative: closer to 0 wins)
        result.percentiles[k] = percentile_of(float(real_result.metrics[k]), arr)
    return result

"""Event-driven, point-in-time backtest engine + walk-forward OOS harness.

Loop, per trading day T (decision on T's close):
  1. Build feature snapshot per instrument using ONLY bars with date <= T
     (and only within each instrument's [listed_from, delisted_on] life).
  2. Strategy engine emits ENTER / EXIT / HOLD (signals only).
  3. Risk layer sizes any entries deterministically.
  4. Orders fill on the NEXT bar's open (signal_to_fill_lag_days) with realistic
     costs (spread + commission + slippage + volume cap). No same-bar look-ahead.
  5. Mark-to-market equity, update trailing stops, record equity curve.

Walk-forward: the harness rolls an in-sample window then evaluates the next
out-of-sample window. Phase-1 strategy has FIXED thresholds (no tuning), so
in-sample is a clean no-op seam; ONLY out-of-sample days are traded/measured.
Benchmark = WIG20TR (buy-and-hold), never SPY.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.backtest import fills as fillmod
from app.backtest import metrics as metricsmod
from app.features import compute
from app.risk import manager as risk
from app.strategy.engine import EvalContext, Signal, evaluate


@dataclass
class Position:
    ticker: str
    sector: str | None
    instrument_id: int
    qty: int
    entry_price: float
    entry_date: str
    stop_price: float


@dataclass
class Instrument:
    instrument_id: int
    ticker: str
    sector: str | None
    listed_from: str | None
    delisted_on: str | None
    prices: pd.DataFrame      # full history (date-indexed)
    features: pd.DataFrame    # precomputed feature panel (date-indexed)
    # Optional point-in-time LLM scores (date-indexed Series in [-1, 1]).
    # When present, the as-of value is injected as `llm_score` into the snapshot.
    # The LLM is ALWAYS only an INPUT here -- sizing/risk stays deterministic.
    llm_scores: pd.Series | None = None
    # Optional numeric relevance encoding (pipeline.RELEVANCE_TO_SCORE),
    # injected as `llm_relevance`. Same point-in-time rules as llm_scores.
    llm_relevance: pd.Series | None = None
    # Corporate actions keyed by ISO ex-date -> list of
    # {action_type, value_or_ratio}. Used to shield stops/positions from gaps
    # that are not market moves (splits, dividends, rights issues).
    actions: dict[str, list[dict]] | None = None


@dataclass
class BacktestResult:
    equity_curve: pd.Series
    benchmark_curve: pd.Series
    trade_pnls: list[float]
    total_buy_notional: float
    metrics: dict
    benchmark_metrics: dict
    decisions: list[dict] = field(default_factory=list)
    cash_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    exposure_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    # Fill audit: orders whose fill bar had no open (close fallback) or no bar
    # at all (lapsed). Not financial events -- kept out of `decisions`.
    fill_anomalies: list[dict] = field(default_factory=list)


def _alive(inst: Instrument, day: pd.Timestamp) -> bool:
    if inst.listed_from and day < pd.to_datetime(inst.listed_from):
        return False
    if inst.delisted_on and day > pd.to_datetime(inst.delisted_on):
        return False
    return True


def load_instruments(conn, universe: dict, benchmark_ticker: str) -> tuple[list[Instrument], pd.Series]:
    """Load tradable instruments (restricted to the universe config) + benchmark.

    Only tickers listed under `universe["instruments"]` are traded; this prevents
    a reused SQLite DB from trading stale or out-of-config tickers.
    """
    bench_close = _load_close_series(conn, benchmark_ticker)

    allowed = {i["ticker"].lower() for i in universe.get("instruments", [])}

    instruments: list[Instrument] = []
    rows = conn.execute(
        "SELECT id, ticker, sector, listed_from, delisted_on, is_index FROM instruments"
    ).fetchall()
    bench_for_features = bench_close
    for r in rows:
        if r["is_index"]:
            continue  # indices are not traded
        if r["ticker"].lower() not in allowed:
            continue  # not part of the configured universe
        df = compute.load_prices_asof(conn, r["id"], as_of="9999-12-31")
        if df.empty:
            continue
        feats = compute.compute_features(df, benchmark_close=bench_for_features)
        instruments.append(
            Instrument(
                instrument_id=r["id"],
                ticker=r["ticker"],
                sector=r["sector"],
                listed_from=r["listed_from"],
                delisted_on=r["delisted_on"],
                prices=df,
                features=feats,
                actions=_load_actions_map(conn, r["id"]),
            )
        )
    return instruments, bench_close


def _load_actions_map(conn, instrument_id: int) -> dict[str, list[dict]] | None:
    rows = conn.execute(
        "SELECT action_type, ex_date, value_or_ratio FROM corporate_actions"
        " WHERE instrument_id = ? ORDER BY ex_date",
        (instrument_id,),
    ).fetchall()
    if not rows:
        return None
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["ex_date"], []).append(
            {"action_type": r["action_type"], "value_or_ratio": float(r["value_or_ratio"])}
        )
    return out


def load_membership_map(conn, index_name: str) -> dict[int, list[tuple]]:
    """Point-in-time membership ranges per instrument_id for one index.

    Each value is a list of (date_from, date_to) Timestamp tuples; date_to is
    None while the instrument is still a member.
    """
    rows = conn.execute(
        "SELECT instrument_id, date_from, date_to FROM index_membership"
        " WHERE index_name = ?",
        (index_name.lower(),),
    ).fetchall()
    out: dict[int, list[tuple]] = {}
    for r in rows:
        out.setdefault(int(r["instrument_id"]), []).append((
            pd.to_datetime(r["date_from"]),
            pd.to_datetime(r["date_to"]) if r["date_to"] else None,
        ))
    return out


def _member_on(ranges: list[tuple] | None, day: pd.Timestamp) -> bool:
    if not ranges:
        return False
    for date_from, date_to in ranges:
        if day >= date_from and (date_to is None or day <= date_to):
            return True
    return False


def _load_close_series(conn, ticker: str) -> pd.Series:
    row = conn.execute("SELECT id FROM instruments WHERE ticker=?", (ticker.lower(),)).fetchone()
    if not row:
        return pd.Series(dtype=float)
    df = compute.load_prices_asof(conn, row["id"], as_of="9999-12-31")
    return df["close"] if not df.empty else pd.Series(dtype=float)


def _trading_calendar(instruments: list[Instrument]) -> pd.DatetimeIndex:
    all_dates: set[pd.Timestamp] = set()
    for inst in instruments:
        all_dates.update(inst.prices.index.tolist())
    return pd.DatetimeIndex(sorted(all_dates))


def run_backtest(
    instruments: list[Instrument],
    benchmark_close: pd.Series,
    strategy_cfg: dict,
    bt_cfg: dict,
    *,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
    membership: dict[int, list[tuple]] | None = None,
) -> BacktestResult:
    """Run the event-driven simulation over [start, end] (OOS window).

    `membership` (optional): point-in-time index membership ranges per
    instrument_id (see load_membership_map). When given, NEW entries are only
    evaluated for instruments that are members as of T; exits on held
    positions always run so a removed member can still be sold.
    """
    seed = int(bt_cfg.get("seed", 42))
    random.seed(seed)
    np.random.seed(seed)  # determinism guard (no randomness is used, but pinned anyway)

    costs = bt_cfg["costs"]
    risk_cfg = strategy_cfg["risk"]
    lag = int(bt_cfg.get("execution", {}).get("signal_to_fill_lag_days", 1))
    if lag < 1:
        raise ValueError(
            "execution.signal_to_fill_lag_days must be >= 1: a same-bar fill "
            "would execute at prices already used to generate the signal"
        )
    atr_mult = float(risk_cfg["atr_mult_stop"])

    calendar = _trading_calendar(instruments)
    if start is not None:
        calendar = calendar[calendar >= start]
    if end is not None:
        calendar = calendar[calendar <= end]
    # The strategy-vs-benchmark comparison (rule 5) is only meaningful where
    # the benchmark actually existed: sessions before its first bar are
    # dropped rather than back-filled with future values.
    benchmark_clamp: str | None = None
    if benchmark_close is not None and not benchmark_close.empty and len(calendar):
        first_bench = benchmark_close.index[0]
        if calendar[0] < first_bench:
            calendar = calendar[calendar >= first_bench]
            benchmark_clamp = first_bench.date().isoformat()

    cash = float(bt_cfg["initial_capital"])
    peak_equity = cash
    positions: dict[str, Position] = {}
    pending_orders: list[dict] = []   # orders to execute on a future bar
    trade_pnls: list[float] = []
    total_buy_notional = 0.0
    # equity records carry the full money breakdown for the audit trail.
    equity_records: list[tuple[pd.Timestamp, float, float, float]] = []
    decisions_log: list[dict] = []
    fill_anomalies: list[dict] = []

    inst_by_ticker = {i.ticker: i for i in instruments}

    for di, day in enumerate(calendar):
        # --- 0. corporate actions effective today (ex-date, before the open) ---
        # A gap explained by a split/dividend/rights issue is NOT a market move:
        # held positions and in-flight orders are re-based so the ATR stop and
        # the accounting see continuous economics, not the mechanical gap.
        # Ex-dates recorded on non-session days (weekend/holiday data entry)
        # bridge forward to the next simulated bar via the (prev_day, day] window.
        prev_day = calendar[di - 1] if di > 0 else None
        for tk, pos in list(positions.items()):
            inst = inst_by_ticker[tk]
            actions = actions_in_window(inst, prev_day, day)
            if not actions:
                continue
            prev_close = _close_before(inst, day)
            for action in actions:
                cash += apply_corporate_action(pos, action, prev_close)
            if pos.qty <= 0:
                # consolidated below one share: full cash-in-lieu exit above
                del positions[tk]
        for order in pending_orders:
            inst = inst_by_ticker.get(order["ticker"])
            if inst is None:
                continue
            for action in actions_in_window(inst, prev_day, day):
                apply_action_to_order(order, action)

        # --- 1. execute pending orders scheduled for today (next-bar fills) ---
        # Cash is mutated ATOMICALLY here (before mark-to-market), so the equity
        # recorded this bar correctly reflects today's buys/sells.
        still_pending = []
        for order in pending_orders:
            if order["fill_on_index"] == di:
                inst_o = inst_by_ticker[order["ticker"]]
                if _open_on(inst_o, day) is None:
                    # Fill-bar audit: the open is the contract; falling back to
                    # the fill bar's close (or lapsing) must leave a trace.
                    # "close_reference" describes the price source only -- the
                    # order may still fill partially or not at all (volume cap).
                    fill_anomalies.append({
                        "type": ("order_lapsed_no_bar" if _close_on(inst_o, day) is None
                                 else "open_missing_close_reference"),
                        "ticker": order["ticker"], "side": order["side"],
                        "decision_date": order["decision_date"],
                        "fill_date": day.date().isoformat(),
                    })
                cash_delta, buy_notional, unfilled_sell_qty = _execute_order(
                    order, day, inst_by_ticker, costs, positions, trade_pnls,
                    decisions_log,
                )
                cash += cash_delta
                total_buy_notional += buy_notional
                # Partial sell: re-queue the remainder for the next bar so unsold
                # shares are not silently discarded.
                if unfilled_sell_qty > 0:
                    still_pending.append({
                        **order, "qty": unfilled_sell_qty, "fill_on_index": di + lag,
                    })
            elif order["fill_on_index"] > di:
                still_pending.append(order)
            # orders with fill_on_index < di are dropped (instrument gone)
        pending_orders = still_pending

        # Tickers with a buy/sell already queued (visible to signals + risk).
        pending_buys = {o["ticker"]: o for o in pending_orders if o["side"] == "BUY"}
        pending_sells = {o["ticker"] for o in pending_orders if o["side"] == "SELL"}

        # --- 2. mark-to-market & update trailing stops ---
        state, equity, holdings_value, peak_equity = build_day_state(
            day=day, positions=positions, pending_buys=pending_buys,
            inst_by_ticker=inst_by_ticker, cash=cash, peak_equity=peak_equity,
            atr_mult=atr_mult,
        )
        exposure_ratio = (holdings_value / equity) if equity > 0 else 0.0
        equity_records.append((day, equity, cash, exposure_ratio))

        # last bar: no future bar to fill on
        if di + lag >= len(calendar):
            continue
        fill_index = di + lag

        # --- 3. evaluate signals on each instrument's close at T ---
        for inst in instruments:
            if not _alive(inst, day):
                continue
            # Point-in-time universe: non-members as of T are not candidates for
            # NEW entries; held positions keep evaluating so exits still fire.
            if (membership is not None
                    and inst.ticker not in positions
                    and not _member_on(membership.get(inst.instrument_id), day)):
                continue
            snap = compute.features_at(inst.features, day.date().isoformat())
            if snap is None:
                continue
            close = snap.get("close")
            if close is None:
                continue
            # Inject the point-in-time LLM features (only data with date <= T) as
            # plain features. They feed the YAML rules like any other feature;
            # sizing and risk remain deterministic (CLAUDE.md rule 1).
            llm_score = _series_asof(inst.llm_scores, day)
            if llm_score is not None:
                snap["llm_score"] = llm_score
            llm_relevance = _series_asof(inst.llm_relevance, day)
            if llm_relevance is not None:
                snap["llm_relevance"] = llm_relevance

            in_pos = inst.ticker in positions
            has_pending_buy = inst.ticker in pending_buys
            has_pending_sell = inst.ticker in pending_sells
            pos = positions.get(inst.ticker)
            ctx = EvalContext(
                in_position=in_pos,
                entry_price=pos.entry_price if pos else None,
                stop_price=pos.stop_price if pos else None,
                last_close=close,
            )
            sig = evaluate(strategy_cfg, snap, ctx)

            if sig == Signal.ENTER and not in_pos and not has_pending_buy:
                atr_val = snap.get("atr")
                if not atr_val:
                    continue
                sizing = risk.size_position(
                    entry_price=close, atr=atr_val, state=state, risk_cfg=risk_cfg,
                    ticker=inst.ticker, sector=inst.sector,
                )
                if not sizing.accepted:
                    continue
                pending_orders.append({
                    "side": "BUY", "ticker": inst.ticker, "qty": sizing.qty,
                    "stop_price": sizing.stop_price, "fill_on_index": fill_index,
                    "decision_date": day.date().isoformat(), "features": snap,
                })
                pending_buys[inst.ticker] = pending_orders[-1]
                # reserve exposure within the same day to avoid over-allocating
                est_cost = close * sizing.qty
                state.exposure_by_name[inst.ticker] = state.exposure_by_name.get(inst.ticker, 0.0) + est_cost
                if inst.sector is not None:
                    state.exposure_by_sector[inst.sector] = state.exposure_by_sector.get(inst.sector, 0.0) + est_cost
                state.open_positions += 1

            elif sig == Signal.EXIT and in_pos and not has_pending_sell:
                pending_orders.append({
                    "side": "SELL", "ticker": inst.ticker, "qty": pos.qty,
                    "fill_on_index": fill_index, "decision_date": day.date().isoformat(),
                    "features": snap,
                })
                pending_sells.add(inst.ticker)

    # --- close any open positions at the final available close (paper) ---
    # Apply realistic exit costs on the forced close (consistent with live exits).
    if len(calendar):
        last_day = calendar[-1]
        for tk, pos in list(positions.items()):
            inst = inst_by_ticker[tk]
            # a name suspended on the last session force-closes at its last
            # known close (same mark the equity carried); delisted -> written off
            ref = _mark_price(inst, last_day)
            if ref is None:
                continue
            fill = fillmod.simulate_fill(
                side="SELL", requested_qty=pos.qty, reference_price=ref,
                bar_volume=_volume_on(inst, last_day), costs=costs,
            )
            if fill.qty <= 0:
                continue
            cash += fill.price * fill.qty - fill.fee
            trade_pnls.append((fill.price - pos.entry_price) * fill.qty - fill.fee)
            pos.qty -= fill.qty
            if pos.qty <= 0:
                del positions[tk]
        # update the final equity record to reflect the forced closes
        if equity_records:
            d, _eq, _c, _ex = equity_records[-1]
            remaining_mv = sum(
                _mark_price(inst_by_ticker[t], last_day) * p.qty
                for t, p in positions.items()
                if _mark_price(inst_by_ticker[t], last_day) is not None
            )
            final_equity = cash + remaining_mv
            ratio = (remaining_mv / final_equity) if final_equity > 0 else 0.0
            equity_records[-1] = (d, final_equity, cash, ratio)

    equity_series = pd.Series(
        {d: eq for d, eq, _c, _ex in equity_records}, dtype=float
    ).sort_index()
    cash_series = pd.Series({d: c for d, _eq, c, _ex in equity_records}, dtype=float).sort_index()
    exposure_series = pd.Series({d: x for d, _eq, _c, x in equity_records}, dtype=float).sort_index()
    if equity_series.empty:
        equity_series = pd.Series([float(bt_cfg["initial_capital"])],
                                  index=[calendar[0] if len(calendar) else pd.Timestamp.today()])

    bench_curve = _benchmark_buy_and_hold(benchmark_close, equity_series.index, float(bt_cfg["initial_capital"]))

    m = metricsmod.compute_metrics(equity_series, trade_pnls, total_buy_notional)
    bm = metricsmod.compute_metrics(bench_curve, [], 0.0)
    m_dict = m.as_dict()
    if benchmark_clamp is not None:
        m_dict["oos_start_clamped_to_benchmark"] = benchmark_clamp

    return BacktestResult(
        equity_curve=equity_series,
        benchmark_curve=bench_curve,
        trade_pnls=trade_pnls,
        total_buy_notional=total_buy_notional,
        metrics=m_dict,
        benchmark_metrics=bm.as_dict(),
        decisions=decisions_log,
        cash_curve=cash_series,
        exposure_curve=exposure_series,
        fill_anomalies=fill_anomalies,
    )


# --- helpers -----------------------------------------------------------------

def build_day_state(
    *,
    day: pd.Timestamp,
    positions: dict[str, Position],
    pending_buys: dict[str, dict],
    inst_by_ticker: dict[str, Instrument],
    cash: float,
    peak_equity: float,
    atr_mult: float,
) -> tuple[risk.PortfolioState, float, float, float]:
    """Mark-to-market at `day`'s close and build the risk layer's inputs.

    Shared by the backtest engine and the live paper loop so the sizing inputs
    can never drift between the two (the money math exists exactly once).

    Mutates trailing stops on held positions (ratchet up only) and reserves the
    estimated cost of pending BUY orders in cash/exposure so the risk layer
    does not over-allocate. Returns (state, equity, holdings_value,
    peak_equity) with the updated running peak.
    """
    exposure_by_name: dict[str, float] = {}
    exposure_by_sector: dict[str, float] = {}
    holdings_value = 0.0
    for tk, pos in positions.items():
        inst = inst_by_ticker[tk]
        close = _mark_price(inst, day)
        if close is None:
            continue
        mv = close * pos.qty
        holdings_value += mv
        exposure_by_name[tk] = mv
        exposure_by_sector[pos.sector] = exposure_by_sector.get(pos.sector, 0.0) + mv
        atr_val = _feature_on(inst, day, "atr")
        if atr_val:
            pos.stop_price = risk.update_trailing_stop(pos.stop_price, close, atr_val, atr_mult)

    equity = cash + holdings_value
    peak_equity = max(peak_equity, equity)

    # Reserve estimated cost of pending buys so risk does not over-allocate.
    cash_available = cash
    for tk, o in pending_buys.items():
        inst = inst_by_ticker.get(tk)
        if inst is None:
            continue
        ref = _open_on(inst, day) or _close_on(inst, day)
        if ref:
            reserved = ref * o["qty"]
            cash_available -= reserved
            exposure_by_name[tk] = exposure_by_name.get(tk, 0.0) + reserved
            if inst.sector is not None:
                exposure_by_sector[inst.sector] = exposure_by_sector.get(inst.sector, 0.0) + reserved

    state = risk.PortfolioState(
        equity=equity, cash=max(0.0, cash_available), peak_equity=peak_equity,
        open_positions=len(positions) + len(pending_buys),
        exposure_by_name=exposure_by_name,
        exposure_by_sector=exposure_by_sector,
    )
    return state, equity, holdings_value, peak_equity


def _close_on(inst: Instrument, day: pd.Timestamp):
    if day in inst.prices.index:
        val = inst.prices.at[day, "close"]
        # NULL columns surface as NaN; a NaN reference price must never reach
        # the fill model (it would fill at NaN and poison cash/equity).
        return None if pd.isna(val) else float(val)
    return None


def _mark_price(inst: Instrument, day: pd.Timestamp):
    """Mark-to-market reference for a HELD position on `day`.

    Today's close when the instrument printed a bar; otherwise the last known
    close while it is still listed (a suspension is not a -100% market move —
    zero-marking created phantom drawdowns that tripped the circuit breaker
    and freed the exposure caps). Once delisted the position is worth zero
    (implicit write-off): there is no market left to sell into.
    """
    close = _close_on(inst, day)
    if close is None and _alive(inst, day):
        close = _close_before(inst, day)
    return close


def _open_on(inst: Instrument, day: pd.Timestamp):
    if day in inst.prices.index:
        val = inst.prices.at[day, "open"]
        return None if pd.isna(val) else float(val)
    return None


def _volume_on(inst: Instrument, day: pd.Timestamp) -> float:
    if day in inst.prices.index:
        val = inst.prices.at[day, "volume"]
        return 0.0 if pd.isna(val) else float(val)
    return 0.0


def _feature_on(inst: Instrument, day: pd.Timestamp, name: str):
    if day in inst.features.index:
        val = inst.features.at[day, name]
        return None if pd.isna(val) else float(val)
    return None


def actions_in_window(inst: Instrument, prev_day: pd.Timestamp | None,
                      day: pd.Timestamp) -> list[dict]:
    """Actions with ex_date in (prev_day, day] -- ISO string comparison.

    Bridges ex-dates recorded on non-session days (weekend/holiday, or a day
    this instrument printed no bar) to the next simulated bar instead of
    silently skipping them. On the first calendar day only an exact match
    applies (there are no positions or orders to re-base yet anyway).
    """
    if not inst.actions:
        return []
    day_iso = day.date().isoformat()
    if prev_day is None:
        entries = inst.actions.get(day_iso, [])
        return list(entries)
    prev_iso = prev_day.date().isoformat()
    out: list[dict] = []
    for ex_date in sorted(inst.actions):
        if prev_iso < ex_date <= day_iso:
            out.extend(inst.actions[ex_date])
    return out


def _close_before(inst: Instrument, day: pd.Timestamp):
    """Last non-NaN close strictly before `day` (the cum price), or None."""
    idx = inst.prices.index
    prior = idx[idx < day]
    if len(prior) == 0:
        return None
    val = inst.prices.at[prior[-1], "close"]
    return None if pd.isna(val) else float(val)


def apply_corporate_action(pos: Position, action: dict, prev_close: float | None) -> float:
    """Re-base a held position for an ex-date action. Returns the cash delta.

    split (r new per old): qty scaled with FLOOR; the fractional remainder is
        paid out as cash-in-lieu at the theoretical post-split price
        (prev_close / r), so equity is conserved exactly and a reverse split
        can never conjure or destroy value. A position consolidated below one
        share ends with qty 0 -- the caller removes it (full cash-in-lieu).
    dividend (D per share): cash credit qty*D on ex-date (paper simplification:
        payment date = ex-date); stop AND entry price shielded by the
        mechanical -D gap so per-trade PnL stays total-return-consistent.
    rights_issue (factor f): stop shielded by the theoretical ex-rights factor;
        entry price is NOT adjusted, so the drop shows as (conservative) PnL --
        the value of the rights themselves is not modeled.
    """
    kind = action["action_type"]
    value = float(action["value_or_ratio"])
    if kind == "split":
        exact = pos.qty * value
        new_qty = int(exact + 1e-9)  # floor, guarded against float wobble
        remainder = max(0.0, exact - new_qty)
        cash_delta = 0.0
        if remainder > 0 and prev_close and prev_close > 0:
            cash_delta = remainder * (prev_close / value)  # post-split price
        pos.qty = new_qty
        pos.entry_price /= value
        pos.stop_price /= value
        return cash_delta
    if kind == "dividend":
        cash_delta = pos.qty * value
        pos.stop_price = max(0.0, pos.stop_price - value)
        pos.entry_price = max(0.0, pos.entry_price - value)
        return cash_delta
    if kind == "rights_issue":
        pos.stop_price *= value
        return 0.0
    raise ValueError(f"unknown corporate action type: {kind}")


def apply_action_to_order(order: dict, action: dict) -> None:
    """Re-base an in-flight order for an ex-date action.

    Both sides scale their share count on a split: a BUY queued at cum prices
    would otherwise fill its old share count at post-split prices (a reverse
    split would silently buy r-times the intended notional on margin that does
    not exist); a SELL's share count follows the re-based position. Stops
    (computed from cum prices) are re-based for the same reason.
    """
    kind = action["action_type"]
    value = float(action["value_or_ratio"])
    if kind == "split":
        order["qty"] = max(0, int(order["qty"] * value + 1e-9))
        if order.get("stop_price") is not None:
            order["stop_price"] /= value
    elif kind == "dividend":
        if order.get("stop_price") is not None:
            order["stop_price"] = max(0.0, order["stop_price"] - value)
    elif kind == "rights_issue":
        if order.get("stop_price") is not None:
            order["stop_price"] *= value


def _series_asof(series: pd.Series | None, day: pd.Timestamp):
    """Point-in-time read: the last value with date <= `day`, or None.

    No look-ahead: a value materialized for a later date is never visible at T.
    """
    if series is None or series.empty:
        return None
    eligible = series.loc[series.index <= day]
    if eligible.empty:
        return None
    val = eligible.iloc[-1]
    return None if pd.isna(val) else float(val)


def strategy_uses_llm_features(strategy_cfg: dict) -> bool:
    """True if any condition references an `llm_*` feature (llm_score,
    llm_relevance, ...). Used to decide whether to attach materialized LLM
    features before a backtest (so an LLM strategy is not silently starved of
    its gate).
    """
    def _walk(node) -> bool:
        if isinstance(node, dict):
            feature = node.get("feature")
            if isinstance(feature, str) and feature.startswith("llm_"):
                return True
            return any(_walk(v) for v in node.values())
        if isinstance(node, (list, tuple)):
            return any(_walk(v) for v in node)
        return False

    return _walk(strategy_cfg.get("entry")) or _walk(strategy_cfg.get("exit"))


# Backwards-compatible aliases (pre-Pack-D names).
strategy_uses_llm_score = strategy_uses_llm_features

# Backwards-compatible aliases (pre-paper-loop private names).
_actions_in_window = actions_in_window
_apply_corporate_action = apply_corporate_action
_apply_action_to_order = apply_action_to_order


def _llm_score_on(inst: Instrument, day: pd.Timestamp):
    return _series_asof(inst.llm_scores, day)


def attach_llm_scores(conn, instruments: list[Instrument]) -> list[Instrument]:
    """Return copies of `instruments` carrying point-in-time `llm_scores` read
    deterministically from materialized `llm_features` (no LLM call here).
    """
    from app.llm import pipeline  # local import: keeps engine LLM-free at import

    out: list[Instrument] = []
    for inst in instruments:
        scores = pipeline.load_llm_scores(conn, inst.instrument_id)
        relevance = pipeline.load_llm_relevance(conn, inst.instrument_id)
        out.append(
            Instrument(
                instrument_id=inst.instrument_id, ticker=inst.ticker,
                sector=inst.sector, listed_from=inst.listed_from,
                delisted_on=inst.delisted_on, prices=inst.prices,
                features=inst.features, llm_scores=scores,
                llm_relevance=relevance, actions=inst.actions,
            )
        )
    return out


def _execute_order(order, day, inst_by_ticker, costs, positions, trade_pnls, decisions_log):
    """Fill a pending order on `day` at the bar open with realistic costs.

    Returns (cash_delta, buy_notional, unfilled_sell_qty). The caller applies the
    cash delta immediately. `unfilled_sell_qty` lets the caller re-queue the
    remainder of a volume-capped SELL so unsold shares are never discarded.
    """
    inst = inst_by_ticker[order["ticker"]]
    ref = _open_on(inst, day)
    if ref is None:
        ref = _close_on(inst, day)
    if ref is None:
        return 0.0, 0.0, 0  # no bar to fill on (e.g., delisted) -> order lapses
    vol = _volume_on(inst, day)

    fill = fillmod.simulate_fill(
        side=order["side"], requested_qty=order["qty"], reference_price=ref,
        bar_volume=vol, costs=costs,
    )
    if fill.qty <= 0:
        # nothing filled; for a SELL the whole quantity remains to retry
        return 0.0, 0.0, (order["qty"] if order["side"] == "SELL" else 0)

    if order["side"] == "BUY":
        cash_delta = -(fill.price * fill.qty + fill.fee)
        positions[order["ticker"]] = Position(
            ticker=order["ticker"], sector=inst.sector, instrument_id=inst.instrument_id,
            qty=fill.qty, entry_price=fill.price, entry_date=day.date().isoformat(),
            stop_price=order["stop_price"],
        )
        decisions_log.append({
            "action": "ENTER", "ticker": order["ticker"], "instrument_id": inst.instrument_id,
            "decision_date": order["decision_date"], "fill_date": day.date().isoformat(),
            "qty": fill.qty, "price": fill.price, "fee": fill.fee, "slippage": fill.slippage,
            "stop_price": order["stop_price"], "features": order["features"],
            "cash_delta": cash_delta,
        })
        return cash_delta, fill.price * fill.qty, 0

    # SELL
    pos = positions.get(order["ticker"])
    if pos is None:
        return 0.0, 0.0, 0
    sold = min(fill.qty, pos.qty)
    cash_delta = fill.price * sold - fill.fee
    pnl = (fill.price - pos.entry_price) * sold - fill.fee
    trade_pnls.append(pnl)
    pos.qty -= sold
    unfilled = max(0, order["qty"] - sold)  # remainder to retry next bar
    if pos.qty <= 0:
        del positions[order["ticker"]]
    decisions_log.append({
        "action": "EXIT", "ticker": order["ticker"], "instrument_id": inst.instrument_id,
        "decision_date": order["decision_date"], "fill_date": day.date().isoformat(),
        "qty": sold, "price": fill.price, "fee": fill.fee, "slippage": fill.slippage,
        "features": order["features"],
        "cash_delta": cash_delta,
    })
    return cash_delta, 0.0, unfilled


def _benchmark_buy_and_hold(bench_close: pd.Series, index: pd.DatetimeIndex, capital: float) -> pd.Series:
    if bench_close is None or bench_close.empty:
        return pd.Series([capital] * len(index), index=index, dtype=float)
    # ffill only: back-filling would fabricate a flat zero-return benchmark
    # segment out of FUTURE values for dates before the index's first real bar,
    # silently understating WIG20TR. run_backtest clamps the calendar to the
    # benchmark's availability, so a leading NaN here is a caller bug.
    aligned = bench_close.reindex(index).ffill()
    if len(aligned) and pd.isna(aligned.iloc[0]):
        raise ValueError(
            "benchmark history starts after the requested span — clamp the "
            "calendar to the benchmark's first bar (run_backtest does this)")
    if aligned.empty or aligned.iloc[0] == 0:
        return pd.Series([capital] * len(index), index=index, dtype=float)
    return capital * aligned / aligned.iloc[0]


# --- walk-forward harness ----------------------------------------------------

@dataclass
class WalkForwardWindow:
    is_start: pd.Timestamp
    is_end: pd.Timestamp
    oos_start: pd.Timestamp
    oos_end: pd.Timestamp


def make_walk_forward_windows(calendar: pd.DatetimeIndex, is_months: int, oos_months: int,
                              embargo_sessions: int = 0) -> list[WalkForwardWindow]:
    """Roll [IS | embargo | OOS] windows across the calendar.

    `embargo_sessions` purges a gap of that many TRADING SESSIONS between the
    end of each in-sample window and the start of its out-of-sample window, so
    a feature with a lookback of up to `embargo_sessions` computed anywhere in
    the OOS window can never read in-sample data (set it >= the longest
    feature lookback — 252 sessions for ret_12m). 0 = contiguous (legacy).
    """
    if len(calendar) == 0:
        return []
    windows: list[WalkForwardWindow] = []
    final = calendar[-1]
    is_delta = pd.DateOffset(months=is_months)
    oos_delta = pd.DateOffset(months=oos_months)

    is_start = calendar[0]
    while True:
        is_end = is_start + is_delta
        # embargo: OOS starts `embargo_sessions` trading sessions AFTER is_end
        first_idx = int(calendar.searchsorted(is_end))  # first session >= is_end
        oos_idx = first_idx + int(embargo_sessions)
        if oos_idx >= len(calendar):
            break
        oos_start = calendar[oos_idx]
        oos_end = oos_start + oos_delta
        windows.append(WalkForwardWindow(is_start, is_end, oos_start, min(oos_end, final)))
        if oos_end >= final:
            break
        # rolling: slide the whole [IS|OOS] block forward by one OOS length
        is_start = is_start + oos_delta
    return windows


def run_walk_forward(
    instruments: list[Instrument],
    benchmark_close: pd.Series,
    strategy_cfg: dict,
    bt_cfg: dict,
    *,
    membership: dict[int, list[tuple]] | None = None,
) -> BacktestResult:
    """Walk-forward OOS evaluation as ONE continuous simulation.

    Phase-1 strategy has fixed parameters, so the in-sample windows are a no-op
    tuning seam (Phase 2 will fit params per IS window here). Rather than running
    each OOS segment in isolation -- which would start each segment flat and
    force-close at its boundary, creating artificial round-trips and exit costs --
    we simulate ONE continuous pass from the first OOS start to the last OOS end.
    This is equivalent to the contiguous union of the OOS windows and avoids
    boundary churn. Only this out-of-sample span is measured, vs WIG20TR.

    When Phase 2 introduces per-window parameter changes, switch to per-segment
    runs that carry portfolio state across boundaries (no forced close).
    """
    calendar = _trading_calendar(instruments)
    wf = bt_cfg["walk_forward"]
    windows = make_walk_forward_windows(
        calendar, wf["in_sample_months"], wf["out_sample_months"],
        embargo_sessions=int(wf.get("embargo_sessions", 0)),
    )

    if not windows:
        # Not enough history for IS + embargo + OOS -> single full-span pass.
        # This is NOT out-of-sample; flag it so reports and the trials registry
        # cannot silently present full-history metrics as OOS evidence.
        result = run_backtest(instruments, benchmark_close, strategy_cfg, bt_cfg,
                              membership=membership)
        result.metrics["walk_forward_windows"] = 0
        return result

    oos_start = windows[0].oos_start
    oos_end = windows[-1].oos_end
    result = run_backtest(
        instruments, benchmark_close, strategy_cfg, bt_cfg,
        start=oos_start, end=oos_end, membership=membership,
    )
    result.metrics["walk_forward_windows"] = len(windows)
    return result

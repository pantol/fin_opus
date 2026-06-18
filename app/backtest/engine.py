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


@dataclass
class BacktestResult:
    equity_curve: pd.Series
    benchmark_curve: pd.Series
    trade_pnls: list[float]
    total_buy_notional: float
    metrics: dict
    benchmark_metrics: dict
    decisions: list[dict] = field(default_factory=list)


def _alive(inst: Instrument, day: pd.Timestamp) -> bool:
    if inst.listed_from and day < pd.to_datetime(inst.listed_from):
        return False
    if inst.delisted_on and day > pd.to_datetime(inst.delisted_on):
        return False
    return True


def load_instruments(conn, universe: dict, benchmark_ticker: str) -> tuple[list[Instrument], pd.Series]:
    """Load all tradable instruments + benchmark close series from the DB."""
    bench_close = _load_close_series(conn, benchmark_ticker)

    instruments: list[Instrument] = []
    rows = conn.execute(
        "SELECT id, ticker, sector, listed_from, delisted_on, is_index FROM instruments"
    ).fetchall()
    bench_for_features = bench_close
    for r in rows:
        if r["is_index"]:
            continue  # indices are not traded
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
            )
        )
    return instruments, bench_close


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
) -> BacktestResult:
    """Run the event-driven simulation over [start, end] (OOS window)."""
    seed = int(bt_cfg.get("seed", 42))
    random.seed(seed)
    np.random.seed(seed)  # determinism guard (no randomness is used, but pinned anyway)

    costs = bt_cfg["costs"]
    risk_cfg = strategy_cfg["risk"]
    lag = int(bt_cfg.get("execution", {}).get("signal_to_fill_lag_days", 1))
    atr_mult = float(risk_cfg["atr_mult_stop"])

    calendar = _trading_calendar(instruments)
    if start is not None:
        calendar = calendar[calendar >= start]
    if end is not None:
        calendar = calendar[calendar <= end]

    cash = float(bt_cfg["initial_capital"])
    peak_equity = cash
    positions: dict[str, Position] = {}
    pending_orders: list[dict] = []   # orders to execute on a future bar
    trade_pnls: list[float] = []
    total_buy_notional = 0.0
    equity_records: list[tuple[pd.Timestamp, float]] = []
    decisions_log: list[dict] = []

    inst_by_ticker = {i.ticker: i for i in instruments}

    for di, day in enumerate(calendar):
        # --- 1. execute pending orders scheduled for today (next-bar fills) ---
        still_pending = []
        for order in pending_orders:
            if order["fill_on_index"] == di:
                _execute_order(
                    order, day, inst_by_ticker, costs, positions, trade_pnls,
                    decisions_log,
                )
                if order["side"] == "BUY" and order.get("_filled_notional"):
                    total_buy_notional += order["_filled_notional"]
            elif order["fill_on_index"] > di:
                still_pending.append(order)
            # orders with fill_on_index < di are dropped (instrument gone)
        pending_orders = still_pending

        # --- 2. mark-to-market & update trailing stops ---
        exposure_by_name: dict[str, float] = {}
        exposure_by_sector: dict[str, float] = {}
        holdings_value = 0.0
        for tk, pos in positions.items():
            inst = inst_by_ticker[tk]
            close = _close_on(inst, day)
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
        equity_records.append((day, equity))

        state = risk.PortfolioState(
            equity=equity, cash=cash, peak_equity=peak_equity,
            open_positions=len(positions),
            exposure_by_name=exposure_by_name,
            exposure_by_sector=exposure_by_sector,
        )

        # last bar: no future bar to fill on
        if di + lag >= len(calendar):
            continue
        fill_index = di + lag

        # --- 3. evaluate signals on each instrument's close at T ---
        for inst in instruments:
            if not _alive(inst, day):
                continue
            snap = compute.features_at(inst.features, day.date().isoformat())
            if snap is None:
                continue
            close = snap.get("close")
            if close is None:
                continue

            in_pos = inst.ticker in positions
            pos = positions.get(inst.ticker)
            ctx = EvalContext(
                in_position=in_pos,
                entry_price=pos.entry_price if pos else None,
                stop_price=pos.stop_price if pos else None,
                last_close=close,
            )
            sig = evaluate(strategy_cfg, snap, ctx)

            if sig == Signal.ENTER and not in_pos:
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
                # reserve exposure within the same day to avoid over-allocating
                est_cost = close * sizing.qty
                state.exposure_by_name[inst.ticker] = state.exposure_by_name.get(inst.ticker, 0.0) + est_cost
                state.exposure_by_sector[inst.sector] = state.exposure_by_sector.get(inst.sector, 0.0) + est_cost
                state.open_positions += 1

            elif sig == Signal.EXIT and in_pos:
                pending_orders.append({
                    "side": "SELL", "ticker": inst.ticker, "qty": pos.qty,
                    "fill_on_index": fill_index, "decision_date": day.date().isoformat(),
                    "features": snap,
                })

        # apply cash changes from today's executed orders
        cash = _recompute_cash_from_decisions(cash, decisions_log, day)

    # --- close any open positions at the final available close (paper) ---
    if len(calendar):
        last_day = calendar[-1]
        for tk, pos in list(positions.items()):
            inst = inst_by_ticker[tk]
            close = _close_on(inst, last_day)
            if close is None:
                continue
            proceeds = close * pos.qty
            cash += proceeds
            trade_pnls.append((close - pos.entry_price) * pos.qty)
        positions.clear()

    equity_series = pd.Series(
        {d: v for d, v in equity_records}, dtype=float
    ).sort_index()
    if equity_series.empty:
        equity_series = pd.Series([float(bt_cfg["initial_capital"])],
                                  index=[calendar[0] if len(calendar) else pd.Timestamp.today()])

    bench_curve = _benchmark_buy_and_hold(benchmark_close, equity_series.index, float(bt_cfg["initial_capital"]))

    m = metricsmod.compute_metrics(equity_series, trade_pnls, total_buy_notional)
    bm = metricsmod.compute_metrics(bench_curve, [], 0.0)

    return BacktestResult(
        equity_curve=equity_series,
        benchmark_curve=bench_curve,
        trade_pnls=trade_pnls,
        total_buy_notional=total_buy_notional,
        metrics=m.as_dict(),
        benchmark_metrics=bm.as_dict(),
        decisions=decisions_log,
    )


# --- helpers -----------------------------------------------------------------

def _close_on(inst: Instrument, day: pd.Timestamp):
    if day in inst.prices.index:
        return float(inst.prices.at[day, "close"])
    return None


def _open_on(inst: Instrument, day: pd.Timestamp):
    if day in inst.prices.index:
        return float(inst.prices.at[day, "open"])
    return None


def _volume_on(inst: Instrument, day: pd.Timestamp) -> float:
    if day in inst.prices.index:
        return float(inst.prices.at[day, "volume"])
    return 0.0


def _feature_on(inst: Instrument, day: pd.Timestamp, name: str):
    if day in inst.features.index:
        val = inst.features.at[day, name]
        return None if pd.isna(val) else float(val)
    return None


def _execute_order(order, day, inst_by_ticker, costs, positions, trade_pnls, decisions_log):
    """Fill a pending order on `day` at the bar open with realistic costs."""
    inst = inst_by_ticker[order["ticker"]]
    ref = _open_on(inst, day)
    if ref is None:
        ref = _close_on(inst, day)
    if ref is None:
        return  # no bar to fill on (e.g., delisted) -> order lapses
    vol = _volume_on(inst, day)

    fill = fillmod.simulate_fill(
        side=order["side"], requested_qty=order["qty"], reference_price=ref,
        bar_volume=vol, costs=costs,
    )
    if fill.qty <= 0:
        return

    if order["side"] == "BUY":
        positions[order["ticker"]] = Position(
            ticker=order["ticker"], sector=inst.sector, instrument_id=inst.instrument_id,
            qty=fill.qty, entry_price=fill.price, entry_date=day.date().isoformat(),
            stop_price=order["stop_price"],
        )
        order["_filled_notional"] = fill.price * fill.qty
        decisions_log.append({
            "action": "ENTER", "ticker": order["ticker"], "instrument_id": inst.instrument_id,
            "decision_date": order["decision_date"], "fill_date": day.date().isoformat(),
            "qty": fill.qty, "price": fill.price, "fee": fill.fee, "slippage": fill.slippage,
            "stop_price": order["stop_price"], "features": order["features"],
            "cash_delta": -(fill.price * fill.qty + fill.fee),
        })
    else:  # SELL
        pos = positions.get(order["ticker"])
        if pos is None:
            return
        pnl = (fill.price - pos.entry_price) * fill.qty - fill.fee
        trade_pnls.append(pnl)
        decisions_log.append({
            "action": "EXIT", "ticker": order["ticker"], "instrument_id": inst.instrument_id,
            "decision_date": order["decision_date"], "fill_date": day.date().isoformat(),
            "qty": fill.qty, "price": fill.price, "fee": fill.fee, "slippage": fill.slippage,
            "features": order["features"],
            "cash_delta": fill.price * fill.qty - fill.fee,
        })
        del positions[order["ticker"]]


def _recompute_cash_from_decisions(cash, decisions_log, day):
    """Apply cash deltas for decisions filled today (idempotent via _applied flag)."""
    iso = day.date().isoformat()
    for d in decisions_log:
        if d.get("fill_date") == iso and not d.get("_applied"):
            cash += d["cash_delta"]
            d["_applied"] = True
    return cash


def _benchmark_buy_and_hold(bench_close: pd.Series, index: pd.DatetimeIndex, capital: float) -> pd.Series:
    if bench_close is None or bench_close.empty:
        return pd.Series([capital] * len(index), index=index, dtype=float)
    aligned = bench_close.reindex(index).ffill().bfill()
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


def make_walk_forward_windows(calendar: pd.DatetimeIndex, is_months: int, oos_months: int) -> list[WalkForwardWindow]:
    """Roll [IS | OOS] windows across the calendar. OOS windows are contiguous."""
    if len(calendar) == 0:
        return []
    windows: list[WalkForwardWindow] = []
    final = calendar[-1]
    is_delta = pd.DateOffset(months=is_months)
    oos_delta = pd.DateOffset(months=oos_months)

    is_start = calendar[0]
    while True:
        is_end = is_start + is_delta          # = oos_start
        oos_start = is_end
        oos_end = oos_start + oos_delta
        if oos_start > final:
            break
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
) -> BacktestResult:
    """Run rolling walk-forward; stitch OOS equity into one curve.

    Phase-1 strategy has fixed params, so IS is a no-op seam (Phase 2 tunes here).
    Only OOS segments are simulated and measured, vs WIG20TR.
    """
    calendar = _trading_calendar(instruments)
    wf = bt_cfg["walk_forward"]
    windows = make_walk_forward_windows(calendar, wf["in_sample_months"], wf["out_sample_months"])

    if not windows:
        # not enough history for a full IS+OOS split -> single OOS pass
        return run_backtest(instruments, benchmark_close, strategy_cfg, bt_cfg)

    capital = float(bt_cfg["initial_capital"])
    equity_segments: list[pd.Series] = []
    all_trade_pnls: list[float] = []
    total_buy = 0.0
    all_decisions: list[dict] = []

    running_capital = capital
    for w in windows:
        seg_cfg = dict(bt_cfg)
        seg_cfg["initial_capital"] = running_capital
        res = run_backtest(
            instruments, benchmark_close, strategy_cfg, seg_cfg,
            start=w.oos_start, end=w.oos_end,
        )
        if res.equity_curve.empty:
            continue
        equity_segments.append(res.equity_curve)
        all_trade_pnls.extend(res.trade_pnls)
        total_buy += res.total_buy_notional
        all_decisions.extend(res.decisions)
        running_capital = float(res.equity_curve.iloc[-1])

    if not equity_segments:
        return run_backtest(instruments, benchmark_close, strategy_cfg, bt_cfg)

    stitched = pd.concat(equity_segments)
    stitched = stitched[~stitched.index.duplicated(keep="last")].sort_index()

    bench_curve = _benchmark_buy_and_hold(benchmark_close, stitched.index, capital)
    m = metricsmod.compute_metrics(stitched, all_trade_pnls, total_buy)
    bm = metricsmod.compute_metrics(bench_curve, [], 0.0)

    return BacktestResult(
        equity_curve=stitched,
        benchmark_curve=bench_curve,
        trade_pnls=all_trade_pnls,
        total_buy_notional=total_buy,
        metrics=m.as_dict(),
        benchmark_metrics=bm.as_dict(),
        decisions=all_decisions,
    )

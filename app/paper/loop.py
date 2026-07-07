"""Daily paper-trading loop: settle -> mark -> decide, one session at a time.

Runs each evening (cron/systemd) AFTER `make ingest`. Implements the exact
backtest contract live: a signal decided on session T's close becomes a
persisted PENDING order that fills at session T+1's open — computable only the
following evening, so each run first SETTLES yesterday's orders at today's
open, then DECIDES today's signals.

Money math is never re-implemented here: settlement goes through
engine._execute_order, sizing inputs through engine.build_day_state, signals
through strategy.evaluate + risk.size_position — the same code the backtest
runs (drift between the two is caught by tests/test_paper_parity.py).

ZERO LLM in this path: llm_* features are read from pre-materialized rows.
Alerts are flushed AFTER commit (at-least-once via NULL alert timestamps); a
Telegram outage can never corrupt or lose money state.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from app.alerts import telegram
from app.backtest import engine
from app.features import compute
from app.logging import decisions as declog
from app.paper import store
from app.risk import manager as risk
from app.strategy.engine import EvalContext, Signal, evaluate

WARSAW = ZoneInfo("Europe/Warsaw")

EXIT_OK = 0
EXIT_REFUSED = 2


@dataclass
class SessionResult:
    date: str
    fills: list[dict] = field(default_factory=list)      # engine decision entries
    lapses: list[dict] = field(default_factory=list)     # {ticker, side, reason}
    new_orders: list[dict] = field(default_factory=list)  # {ticker, side, qty, stop_price}
    anomalies: list[dict] = field(default_factory=list)
    equity: float = 0.0
    cash: float = 0.0
    n_open: int = 0


@dataclass
class PaperRunReport:
    status: str                    # ok / noop / refused
    reason: str | None = None
    user_id: str | None = None
    dry_run: bool = False
    sessions: list[SessionResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_text(self) -> str:
        lines = [f"paper[{self.user_id}]: {self.status}"
                 + (f" — {self.reason}" if self.reason else "")
                 + (" (dry-run, rolled back)" if self.dry_run else "")]
        for s in self.sessions:
            lines.append(
                f"  session {s.date}: {len(s.fills)} fill(s), {len(s.lapses)} lapse(s), "
                f"{len(s.new_orders)} new signal(s) | equity {s.equity:,.2f}, "
                f"cash {s.cash:,.2f}, {s.n_open} open position(s)")
            for f in s.fills:
                lines.append(f"    fill  {f['action']:<5} {f['ticker']:<8} "
                             f"qty {f['qty']} @ {f['price']:.2f} (fee {f['fee']:.2f})")
            for l in s.lapses:
                lines.append(f"    lapse {l['side']:<5} {l['ticker']:<8} ({l['reason']})")
            for o in s.new_orders:
                stop = f", stop {o['stop_price']:.2f}" if o.get("stop_price") else ""
                lines.append(f"    signal {o['side']:<4} {o['ticker']:<8} qty {o['qty']}{stop} "
                             f"(fills next session open)")
        for w in self.warnings:
            lines.append(f"  WARNING: {w}")
        return "\n".join(lines)


def config_hash(strategy_cfg: dict, bt_cfg: dict) -> str:
    """Pin of everything that makes paper results comparable over time."""
    payload = {
        "strategy": strategy_cfg,
        "costs": bt_cfg["costs"],
        "execution": bt_cfg.get("execution", {}),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _paper_cfg(bt_cfg: dict) -> dict:
    p = dict(bt_cfg.get("paper") or {})
    p.setdefault("initial_capital", bt_cfg["initial_capital"])
    p.setdefault("max_staleness_days", 4)
    p.setdefault("min_session_coverage", 0.5)
    p.setdefault("catchup_max_sessions", 10)
    return p


def _begin_immediate(conn) -> None:
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")


def run_signals(
    conn,
    *,
    universe: dict,
    bt_cfg: dict,
    strategy_cfg: dict,
    now: datetime | None = None,
    session_end: str | None = None,
    accept_config_change: bool = False,
    dry_run: bool = False,
    send_fn=telegram.send_text,
) -> tuple[int, PaperRunReport]:
    """One evening run. Returns (exit_code, report).

    `session_end` (ISO date) clamps the calendar — an ops/test hook, never a
    way to peek forward (fills always use only bars of the processed session).
    `dry_run` processes everything in one transaction and rolls it back.
    """
    user_id = store.paper_user_id(str(bt_cfg["user_id"]))
    report = PaperRunReport(status="refused", user_id=user_id, dry_run=dry_run)

    lag = int(bt_cfg.get("execution", {}).get("signal_to_fill_lag_days", 1))
    if lag != 1:
        report.reason = (f"signal_to_fill_lag_days={lag} is not supported live: the "
                         "paper loop settles PENDING orders on the next processed "
                         "session (lag 1), matching config/backtest.yaml")
        return EXIT_REFUSED, report

    conn.execute("PRAGMA busy_timeout = 30000")

    # --- load the exact universe/feature pipeline the backtest uses ----------
    bench = universe["benchmark"]["ticker"]
    instruments, _bench_close = engine.load_instruments(conn, universe, bench)
    if not instruments:
        report.reason = "no price data — run `make ingest` first"
        return EXIT_REFUSED, report
    if engine.strategy_uses_llm_features(strategy_cfg):
        # Reads pre-materialized llm_features rows; NO LLM call happens here.
        instruments = engine.attach_llm_scores(conn, instruments)

    membership = None
    index_name = (bt_cfg.get("universe") or {}).get("index")
    if index_name:
        membership = engine.load_membership_map(conn, index_name)
        if not membership:
            report.reason = (f"universe.index='{index_name}' is set but the "
                             "index_membership table is empty — run refdata first")
            return EXIT_REFUSED, report

    calendar = engine._trading_calendar(instruments)
    if session_end is not None:
        calendar = calendar[calendar <= pd.to_datetime(session_end)]
    if len(calendar) == 0:
        report.reason = "empty trading calendar"
        return EXIT_REFUSED, report
    latest = calendar[-1]

    # --- data gates: refuse to decide on stale or half-ingested sessions -----
    paper_cfg = _paper_cfg(bt_cfg)
    today = (now or datetime.now(WARSAW)).date()
    age_days = (today - latest.date()).days
    if age_days > int(paper_cfg["max_staleness_days"]):
        report.reason = (f"latest session {latest.date().isoformat()} is {age_days} "
                         f"days old (max_staleness_days="
                         f"{paper_cfg['max_staleness_days']}) — ingest broken?")
        _alert_refusal(send_fn, report.reason, dry_run)
        return EXIT_REFUSED, report
    alive = [i for i in instruments if engine._alive(i, latest)]
    with_bar = [i for i in alive if latest in i.prices.index]
    if not alive or (len(with_bar) / len(alive)) < float(paper_cfg["min_session_coverage"]):
        report.reason = (f"only {len(with_bar)}/{len(alive)} universe instruments "
                         f"printed a bar on {latest.date().isoformat()} — partial "
                         "ingest? refusing to decide")
        _alert_refusal(send_fn, report.reason, dry_run)
        return EXIT_REFUSED, report

    # --- state / watermark ----------------------------------------------------
    chash = config_hash(strategy_cfg, bt_cfg)
    state_row = store.load_state(conn, user_id)
    if state_row is not None and state_row["config_hash"] != chash and not accept_config_change:
        report.reason = ("strategy/cost config changed since the paper track record "
                         "started — rerun with --accept-config-change to acknowledge "
                         "(results before/after are not comparable)")
        return EXIT_REFUSED, report

    if dry_run:
        _begin_immediate(conn)

    strategy_id = declog.register_strategy(
        conn, strategy_cfg["name"], int(strategy_cfg["version"]),
        json.dumps(strategy_cfg, sort_keys=True),
    )

    if state_row is None:
        # Bootstrap: start the book at the latest session with nothing to settle.
        before = calendar[calendar < latest]
        last_settled = (before[-1].date().isoformat() if len(before)
                        else (latest.date() - timedelta(days=1)).isoformat())
        store.init_state(
            conn, user_id=user_id, initial_capital=float(paper_cfg["initial_capital"]),
            inception_date=latest.date().isoformat(), last_settled_date=last_settled,
            strategy_id=strategy_id, config_hash=chash,
        )
        state_row = store.load_state(conn, user_id)
        report.warnings.append("bootstrap: new paper portfolio "
                               f"(capital {float(paper_cfg['initial_capital']):,.2f})")
    elif state_row["config_hash"] != chash:
        store.update_config_hash(conn, user_id=user_id, config_hash=chash)
        report.warnings.append("config change accepted: track record continuity break")

    last_settled = pd.to_datetime(state_row["last_settled_date"])
    todo = [d for d in calendar if last_settled < d <= latest]
    if len(todo) > int(paper_cfg["catchup_max_sessions"]):
        if dry_run:
            conn.rollback()
        elif conn.in_transaction:
            conn.commit()  # keep bootstrap/config-ack writes
        report.reason = (f"{len(todo)} unprocessed sessions exceed "
                         f"catchup_max_sessions={paper_cfg['catchup_max_sessions']} — "
                         "a gap this large is an ops problem; raise the cap "
                         "explicitly to catch up")
        return EXIT_REFUSED, report

    if not todo:
        if dry_run:
            conn.rollback()
        elif conn.in_transaction:
            conn.commit()
        report.status = "noop"
        report.reason = f"session {latest.date().isoformat()} already processed"
        if not dry_run:
            _flush_alerts(conn, user_id, send_fn, report)
        return EXIT_OK, report

    if not dry_run and conn.in_transaction:
        conn.commit()  # bootstrap/config-ack committed before per-session txns

    inst_by_ticker = {i.ticker: i for i in instruments}
    inst_by_id = {i.instrument_id: i for i in instruments}
    costs = bt_cfg["costs"]
    risk_cfg = strategy_cfg["risk"]
    atr_mult = float(risk_cfg["atr_mult_stop"])

    try:
        for day in todo:
            if not dry_run:
                _begin_immediate(conn)
            # Re-read state INSIDE the write lock: two overlapping runs (cron
            # double-fire) both computed `todo` up front; BEGIN IMMEDIATE
            # serializes them, and this check makes the loser skip sessions the
            # winner already settled instead of double-processing them (which
            # would fill freshly-queued orders on their own decision day).
            fresh = store.load_state(conn, user_id)
            if pd.to_datetime(fresh["last_settled_date"]) >= day:
                if not dry_run:
                    conn.commit()
                report.warnings.append(
                    f"session {day.date().isoformat()} already settled by a "
                    "concurrent run — skipped")
                continue
            cash = float(fresh["cash"])
            peak_equity = float(fresh["peak_equity"])
            before = calendar[calendar < day]
            prev_day = before[-1] if len(before) else None
            cash, peak_equity, session = _process_session(
                conn, day=day, prev_day=prev_day, user_id=user_id,
                strategy_id=strategy_id, instruments=instruments,
                inst_by_ticker=inst_by_ticker, inst_by_id=inst_by_id,
                membership=membership, strategy_cfg=strategy_cfg, risk_cfg=risk_cfg,
                costs=costs, atr_mult=atr_mult, cash=cash, peak_equity=peak_equity,
                warnings=report.warnings,
            )
            store.save_state(conn, user_id=user_id, cash=cash, peak_equity=peak_equity,
                             last_settled_date=day.date().isoformat())
            if not dry_run:
                conn.commit()
            report.sessions.append(session)
    except BaseException:
        # a failed session must never commit half-written money state; prior
        # sessions are already committed, so a re-run resumes exactly here
        conn.rollback()
        raise

    if dry_run:
        conn.rollback()

    report.status = "ok"
    if not dry_run:
        _flush_alerts(conn, user_id, send_fn, report)
    return EXIT_OK, report


def _process_session(
    conn, *, day, prev_day, user_id, strategy_id, instruments, inst_by_ticker,
    inst_by_id, membership, strategy_cfg, risk_cfg, costs, atr_mult, cash,
    peak_equity, warnings,
) -> tuple[float, float, SessionResult]:
    """Settle -> mark -> decide for one session, mirroring the engine loop."""
    day_iso = day.date().isoformat()
    session = SessionResult(date=day_iso)

    positions, pos_rowids, orphans = store.load_open_positions(conn, user_id, inst_by_id)
    for o in orphans:
        warnings.append(f"OPEN position on instrument_id={o['instrument_id']} is not "
                        "in the current universe — it cannot be priced or exited")
    orders = store.load_pending_orders(conn, user_id, inst_by_id)
    n_pending_rows = conn.execute(
        "SELECT COUNT(*) FROM paper_orders WHERE user_id = ? AND status = 'PENDING'",
        (user_id,)).fetchone()[0]
    if n_pending_rows != len(orders):
        warnings.append(f"{n_pending_rows - len(orders)} PENDING order(s) reference "
                        "instruments outside the current universe — left untouched")

    # --- 0. corporate actions effective today (ex-date, before the open) ------
    for tk, pos in list(positions.items()):
        inst = inst_by_ticker[tk]
        actions = engine.actions_in_window(inst, prev_day, day)
        if not actions:
            continue
        prev_close = engine._close_before(inst, day)
        for action in actions:
            cash += engine.apply_corporate_action(pos, action, prev_close)
        if pos.qty <= 0:
            # consolidated below one share: full cash-in-lieu exit
            store.close_position_row(conn, pos_rowids[tk], exit_date=day_iso,
                                     exit_price=None)
            del positions[tk]
            del pos_rowids[tk]
        else:
            store.update_position_row(conn, pos_rowids[tk], qty=pos.qty,
                                      entry_price=pos.entry_price,
                                      stop_price=pos.stop_price)
    for order in orders:
        inst = inst_by_ticker.get(order["ticker"])
        if inst is None:
            continue
        actions = engine.actions_in_window(inst, prev_day, day)
        for action in actions:
            engine.apply_action_to_order(order, action)
        if actions:
            store.rebase_order(conn, order["_row_id"], qty=order["qty"],
                               stop_price=order.get("stop_price"))

    # --- 1. settle: fill yesterday's orders at TODAY's open -------------------
    decisions_session: list[dict] = []
    trade_pnls: list[float] = []
    still_pending: list[dict] = []
    for order in orders:
        inst = inst_by_ticker[order["ticker"]]
        ref_open = engine._open_on(inst, day)
        ref_close = engine._close_on(inst, day)
        if ref_open is None:
            session.anomalies.append({
                "type": ("order_lapsed_no_bar" if ref_close is None
                         else "open_missing_close_reference"),
                "ticker": order["ticker"], "side": order["side"],
                "decision_date": order["decision_date"], "fill_date": day_iso,
            })
        if ref_open is None and ref_close is None:
            # no bar to fill on: order lapses (engine drops it the same way)
            store.mark_order_lapsed(conn, order["_row_id"], fill_date=day_iso,
                                    reason="no_bar")
            session.lapses.append({"ticker": order["ticker"], "side": order["side"],
                                   "reason": "no_bar"})
            continue

        n_before = len(decisions_session)
        cash_delta, _buy_notional, unfilled = engine._execute_order(
            order, day, inst_by_ticker, costs, positions, trade_pnls,
            decisions_session,
        )
        cash += cash_delta
        filled = len(decisions_session) > n_before

        if order["side"] == "BUY":
            if not filled:
                # volume cap reduced the fill to zero; the engine drops the order
                store.mark_order_lapsed(conn, order["_row_id"], fill_date=day_iso,
                                        reason="volume")
                session.lapses.append({"ticker": order["ticker"], "side": "BUY",
                                       "reason": "volume"})
                continue
            d = decisions_session[-1]
            dec_id = _persist_fill(conn, user_id, strategy_id, strategy_cfg, d)
            store.mark_order_filled(conn, order["_row_id"], status="FILLED",
                                    fill_date=day_iso, fill_qty=int(d["qty"]),
                                    fill_price=float(d["price"]), decision_id=dec_id)
            pos_rowids[order["ticker"]] = store.open_position_row(
                conn, user_id, positions[order["ticker"]])
            session.fills.append(d)
            continue

        # SELL
        if not filled:
            if unfilled > 0:
                # zero fill under the volume cap: the engine re-queues the whole
                # quantity for the next bar — the row simply stays PENDING
                still_pending.append(order)
            else:
                # defensive: SELL without a matching position (should not happen)
                store.mark_order_lapsed(conn, order["_row_id"], fill_date=day_iso,
                                        reason="no_position")
                session.lapses.append({"ticker": order["ticker"], "side": "SELL",
                                       "reason": "no_position"})
            continue
        d = decisions_session[-1]
        dec_id = _persist_fill(conn, user_id, strategy_id, strategy_cfg, d)
        pos = positions.get(order["ticker"])
        if pos is None:
            store.close_position_row(conn, pos_rowids.pop(order["ticker"]),
                                     exit_date=day_iso, exit_price=float(d["price"]))
        else:
            store.update_position_row(conn, pos_rowids[order["ticker"]], qty=pos.qty,
                                      entry_price=pos.entry_price,
                                      stop_price=pos.stop_price)
        if unfilled > 0:
            # partial fill: remainder re-queued for the next session (engine parity)
            store.mark_order_filled(conn, order["_row_id"], status="PARTIAL",
                                    fill_date=day_iso, fill_qty=int(d["qty"]),
                                    fill_price=float(d["price"]), decision_id=dec_id)
            remainder = {
                "side": "SELL", "ticker": order["ticker"], "qty": int(unfilled),
                "decision_date": order["decision_date"], "features": order["features"],
            }
            remainder["_row_id"] = store.insert_order(
                conn, user_id, instrument_id=inst.instrument_id, side="SELL",
                qty=int(unfilled), stop_price=None,
                decision_date=order["decision_date"], features=order["features"])
            store.mark_signal_alerted(conn, remainder["_row_id"])  # not a new signal
            still_pending.append(remainder)
        else:
            store.mark_order_filled(conn, order["_row_id"], status="FILLED",
                                    fill_date=day_iso, fill_qty=int(d["qty"]),
                                    fill_price=float(d["price"]), decision_id=dec_id)
        session.fills.append(d)

    pending_buys = {o["ticker"]: o for o in still_pending if o["side"] == "BUY"}
    pending_sells = {o["ticker"] for o in still_pending if o["side"] == "SELL"}

    # --- 2. mark-to-market & trailing stops (shared with the backtest) --------
    state, equity, holdings_value, peak_equity = engine.build_day_state(
        day=day, positions=positions, pending_buys=pending_buys,
        inst_by_ticker=inst_by_ticker, cash=cash, peak_equity=peak_equity,
        atr_mult=atr_mult,
    )
    exposure_ratio = (holdings_value / equity) if equity > 0 else 0.0
    declog.record_equity(conn, user_id=user_id, date=day_iso, equity=equity,
                         cash=cash, exposure=exposure_ratio)
    for tk, pos in positions.items():
        store.update_position_row(conn, pos_rowids[tk], qty=pos.qty,
                                  entry_price=pos.entry_price,
                                  stop_price=pos.stop_price)

    # --- 3. decide on today's close; orders fill at the NEXT session's open ---
    for inst in instruments:
        if not engine._alive(inst, day):
            continue
        if (membership is not None
                and inst.ticker not in positions
                and not engine._member_on(membership.get(inst.instrument_id), day)):
            continue
        snap = compute.features_at(inst.features, day_iso)
        if snap is None:
            continue
        close = snap.get("close")
        if close is None:
            continue
        llm_score = engine._series_asof(inst.llm_scores, day)
        if llm_score is not None:
            snap["llm_score"] = llm_score
        llm_relevance = engine._series_asof(inst.llm_relevance, day)
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
            store.insert_order(conn, user_id, instrument_id=inst.instrument_id,
                               side="BUY", qty=sizing.qty, stop_price=sizing.stop_price,
                               decision_date=day_iso, features=snap)
            pending_buys[inst.ticker] = {"side": "BUY", "ticker": inst.ticker,
                                         "qty": sizing.qty}
            session.new_orders.append({"ticker": inst.ticker, "side": "BUY",
                                       "qty": sizing.qty,
                                       "stop_price": sizing.stop_price})
            # reserve exposure within the same day to avoid over-allocating
            est_cost = close * sizing.qty
            state.exposure_by_name[inst.ticker] = (
                state.exposure_by_name.get(inst.ticker, 0.0) + est_cost)
            if inst.sector is not None:
                state.exposure_by_sector[inst.sector] = (
                    state.exposure_by_sector.get(inst.sector, 0.0) + est_cost)
            state.open_positions += 1

        elif sig == Signal.EXIT and in_pos and not has_pending_sell:
            store.insert_order(conn, user_id, instrument_id=inst.instrument_id,
                               side="SELL", qty=pos.qty, stop_price=None,
                               decision_date=day_iso, features=snap)
            pending_sells.add(inst.ticker)
            session.new_orders.append({"ticker": inst.ticker, "side": "SELL",
                                       "qty": pos.qty, "stop_price": None})

    session.equity = equity
    session.cash = cash
    session.n_open = len(positions)
    return cash, peak_equity, session


def _persist_fill(conn, user_id: str, strategy_id: int, strategy_cfg: dict,
                  d: dict) -> int:
    """decisions + trades rows for one fill — same shape as cmd_backtest's
    _persist_results so paper and backtest ledgers stay uniform."""
    dec_id = declog.log_decision(
        conn, user_id=user_id, strategy_id=strategy_id,
        instrument_id=d["instrument_id"], decision_date=d["decision_date"],
        action=d["action"], features=d.get("features", {}), params=strategy_cfg,
    )
    declog.log_trade(
        conn, user_id=user_id, instrument_id=d["instrument_id"],
        side="BUY" if d["action"] == "ENTER" else "SELL",
        qty=d["qty"], price=d["price"], fee=d["fee"], slippage=d["slippage"],
        trade_date=d["fill_date"], decision_id=dec_id,
    )
    return dec_id


# --- alert flush (post-commit, at-least-once, never in the money path) --------

def _alert_refusal(send_fn, reason: str, dry_run: bool) -> None:
    if dry_run or send_fn is None:
        return
    try:
        send_fn("⚠️ Sygnaly GPW (paper): WSTRZYMANE\n"
                f"Powod: {reason}\nSzczegoly: make signals")
    except Exception:  # noqa: BLE001 — monitoring must never break the app
        pass


def _flush_alerts(conn, user_id: str, send_fn, report: PaperRunReport) -> None:
    """Send Polish cards for unalerted signals/outcomes, then one summary.

    Sends happen AFTER the money-state commits; each row is marked only after
    a successful send, so a crash or Telegram outage re-delivers next run
    (at-least-once) and never double-books a fill.
    """
    if send_fn is None:
        return
    try:
        for row in store.unalerted_outcomes(conn, user_id):
            send_fn(telegram.format_order_outcome_pl(dict(row)))
            store.mark_outcome_alerted(conn, row["id"])
            conn.commit()
        for row in store.unalerted_signals(conn, user_id):
            # During a multi-session catch-up an order created at S is already
            # settled at S+1 by flush time — a "fills at next session" card
            # would be stale noise; its outcome card (above) tells the story.
            if row["status"] == "PENDING":
                send_fn(telegram.format_order_signal_pl(dict(row)))
            store.mark_signal_alerted(conn, row["id"])
            conn.commit()
        if report.sessions:
            last = report.sessions[-1]
            send_fn(telegram.format_paper_summary_pl(
                date=last.date, equity=last.equity, cash=last.cash,
                n_open=last.n_open))
    except Exception as exc:  # noqa: BLE001 — alerting must never break the loop
        report.warnings.append(f"alert delivery failed (will retry next run): {exc}")

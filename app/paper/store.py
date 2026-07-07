"""Persistence for the paper-trading loop.

Round-trips the simulation state (open positions, pending next-open orders,
cash/peak-equity scalars) between SQLite and the exact in-memory shapes the
backtest engine primitives operate on, so the daily loop can call the SAME
money code the backtest uses. Paper rows in the shared tables
(decisions/trades/equity_curve/positions) live under user_id 'paper:<user>'.

Load order is pinned (ORDER BY id) so restored dict/list iteration order
equals the engine's insertion order — required for identical float summation
and fill sequencing in the parity test.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app.backtest.engine import Instrument, Position

PAPER_PREFIX = "paper:"


def paper_user_id(base_user_id: str) -> str:
    """The single place the paper namespace is constructed."""
    return f"{PAPER_PREFIX}{base_user_id}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- paper_state (cash / peak_equity / watermark) -----------------------------

def load_state(conn, user_id: str):
    return conn.execute(
        "SELECT * FROM paper_state WHERE user_id = ?", (user_id,)
    ).fetchone()


def init_state(conn, *, user_id: str, initial_capital: float, inception_date: str,
               last_settled_date: str, strategy_id: int, config_hash: str) -> None:
    # OR IGNORE: two concurrent first runs may both reach bootstrap; the loser
    # must adopt the winner's row (callers re-load) instead of crashing.
    conn.execute(
        """
        INSERT OR IGNORE INTO paper_state
            (user_id, cash, peak_equity, initial_capital, inception_date,
             last_settled_date, strategy_id, config_hash, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, initial_capital, initial_capital, initial_capital,
         inception_date, last_settled_date, strategy_id, config_hash, _now()),
    )


def save_state(conn, *, user_id: str, cash: float, peak_equity: float,
               last_settled_date: str) -> None:
    conn.execute(
        """
        UPDATE paper_state
        SET cash = ?, peak_equity = ?, last_settled_date = ?, updated_at = ?
        WHERE user_id = ?
        """,
        (cash, peak_equity, last_settled_date, _now(), user_id),
    )


def update_config_hash(conn, *, user_id: str, config_hash: str) -> None:
    conn.execute(
        "UPDATE paper_state SET config_hash = ?, updated_at = ? WHERE user_id = ?",
        (config_hash, _now(), user_id),
    )


# --- open positions (shared `positions` table) --------------------------------

def load_open_positions(
    conn, user_id: str, inst_by_id: dict[int, Instrument]
) -> tuple[dict[str, Position], dict[str, int], list]:
    """OPEN rows -> engine Position dicts keyed by ticker (insertion order).

    Returns (positions, row_id_by_ticker, orphans) where orphans are OPEN rows
    whose instrument is not in the current universe — they cannot be priced or
    exited by the loop and must be surfaced loudly, never silently dropped.
    """
    rows = conn.execute(
        "SELECT id, instrument_id, qty, entry_date, entry_price, stop_price"
        " FROM positions WHERE user_id = ? AND status = 'OPEN' ORDER BY id",
        (user_id,),
    ).fetchall()
    positions: dict[str, Position] = {}
    row_ids: dict[str, int] = {}
    orphans = []
    for r in rows:
        inst = inst_by_id.get(int(r["instrument_id"]))
        if inst is None:
            orphans.append(r)
            continue
        positions[inst.ticker] = Position(
            ticker=inst.ticker, sector=inst.sector, instrument_id=inst.instrument_id,
            qty=int(r["qty"]), entry_price=float(r["entry_price"]),
            entry_date=r["entry_date"], stop_price=float(r["stop_price"]),
        )
        row_ids[inst.ticker] = int(r["id"])
    return positions, row_ids, orphans


def open_position_row(conn, user_id: str, pos: Position) -> int:
    cur = conn.execute(
        """
        INSERT INTO positions
            (user_id, instrument_id, qty, entry_date, entry_price, stop_price, status)
        VALUES (?, ?, ?, ?, ?, ?, 'OPEN')
        """,
        (user_id, pos.instrument_id, pos.qty, pos.entry_date,
         pos.entry_price, pos.stop_price),
    )
    return int(cur.lastrowid)


def update_position_row(conn, row_id: int, *, qty: int, entry_price: float,
                        stop_price: float) -> None:
    conn.execute(
        "UPDATE positions SET qty = ?, entry_price = ?, stop_price = ? WHERE id = ?",
        (qty, entry_price, stop_price, row_id),
    )


def close_position_row(conn, row_id: int, *, exit_date: str,
                       exit_price: float | None) -> None:
    conn.execute(
        "UPDATE positions SET qty = 0, status = 'CLOSED', exit_date = ?,"
        " exit_price = ? WHERE id = ?",
        (exit_date, exit_price, row_id),
    )


# --- pending orders (paper_orders) --------------------------------------------

def load_pending_orders(conn, user_id: str, inst_by_id: dict[int, Instrument]) -> list[dict]:
    """PENDING rows -> engine order dicts (insertion order) with `_row_id`."""
    rows = conn.execute(
        "SELECT id, instrument_id, side, qty, stop_price, decision_date, features_json"
        " FROM paper_orders WHERE user_id = ? AND status = 'PENDING' ORDER BY id",
        (user_id,),
    ).fetchall()
    orders: list[dict] = []
    for r in rows:
        inst = inst_by_id.get(int(r["instrument_id"]))
        if inst is None:
            continue  # orphaned order: instrument left the universe; reported by caller
        order = {
            "side": r["side"], "ticker": inst.ticker, "qty": int(r["qty"]),
            "decision_date": r["decision_date"],
            "features": json.loads(r["features_json"]),
            "_row_id": int(r["id"]),
        }
        if r["stop_price"] is not None:
            order["stop_price"] = float(r["stop_price"])
        orders.append(order)
    return orders


def insert_order(conn, user_id: str, *, instrument_id: int, side: str, qty: int,
                 stop_price: float | None, decision_date: str, features: dict) -> int:
    cur = conn.execute(
        """
        INSERT INTO paper_orders
            (user_id, instrument_id, side, qty, stop_price, decision_date,
             features_json, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)
        """,
        (user_id, instrument_id, side, qty, stop_price, decision_date,
         json.dumps(features, sort_keys=True), _now()),
    )
    return int(cur.lastrowid)


def rebase_order(conn, row_id: int, *, qty: int, stop_price: float | None) -> None:
    """Persist a corporate-action rebase of an in-flight order."""
    conn.execute(
        "UPDATE paper_orders SET qty = ?, stop_price = ? WHERE id = ?",
        (qty, stop_price, row_id),
    )


def mark_order_filled(conn, row_id: int, *, status: str, fill_date: str,
                      fill_qty: int, fill_price: float, decision_id: int) -> None:
    conn.execute(
        """
        UPDATE paper_orders
        SET status = ?, fill_date = ?, fill_qty = ?, fill_price = ?, decision_id = ?
        WHERE id = ?
        """,
        (status, fill_date, fill_qty, fill_price, decision_id, row_id),
    )


def mark_order_lapsed(conn, row_id: int, *, fill_date: str, reason: str) -> None:
    conn.execute(
        "UPDATE paper_orders SET status = 'LAPSED', fill_date = ?, lapse_reason = ?"
        " WHERE id = ?",
        (fill_date, reason, row_id),
    )


def mark_order_requeued(conn, row_id: int, *, fill_date: str) -> None:
    """Volume-capped zero fill: the retry is a FRESH row (see db.py) so the
    next session's ORDER BY id settlement matches the engine's requeue order."""
    conn.execute(
        "UPDATE paper_orders SET status = 'REQUEUED', fill_date = ? WHERE id = ?",
        (fill_date, row_id),
    )


# --- alert delivery bookkeeping (at-least-once, post-commit) -------------------

def unalerted_signals(conn, user_id: str) -> list:
    return conn.execute(
        "SELECT o.*, i.ticker FROM paper_orders o"
        " JOIN instruments i ON i.id = o.instrument_id"
        " WHERE o.user_id = ? AND o.signal_alerted_at IS NULL ORDER BY o.id",
        (user_id,),
    ).fetchall()


def unalerted_outcomes(conn, user_id: str) -> list:
    """Orders that reached a terminal status but whose outcome card is unsent."""
    return conn.execute(
        "SELECT o.*, i.ticker FROM paper_orders o"
        " JOIN instruments i ON i.id = o.instrument_id"
        " WHERE o.user_id = ? AND o.fill_alerted_at IS NULL"
        " AND o.status IN ('FILLED', 'PARTIAL', 'LAPSED') ORDER BY o.id",
        (user_id,),
    ).fetchall()


def mark_signal_alerted(conn, row_id: int) -> None:
    conn.execute(
        "UPDATE paper_orders SET signal_alerted_at = ? WHERE id = ?",
        (_now(), row_id),
    )


def mark_outcome_alerted(conn, row_id: int) -> None:
    conn.execute(
        "UPDATE paper_orders SET fill_alerted_at = ? WHERE id = ?",
        (_now(), row_id),
    )

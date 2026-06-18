"""Persist decisions (with full feature snapshot) and trades to SQLite.

Every decision is stored with its complete feature snapshot and strategy params
so any decision is fully reproducible (see CLAUDE.md rule 8). No LLM here.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def register_strategy(conn, name: str, version: int, config_yaml: str) -> int:
    conn.execute(
        """
        INSERT INTO strategies (name, version, config_yaml, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(name, version) DO UPDATE SET config_yaml=excluded.config_yaml
        """,
        (name, version, config_yaml, _now()),
    )
    row = conn.execute(
        "SELECT id FROM strategies WHERE name=? AND version=?", (name, version)
    ).fetchone()
    return int(row[0])


def log_decision(
    conn,
    *,
    user_id: str,
    strategy_id: int | None,
    instrument_id: int,
    decision_date: str,
    action: str,
    features: dict,
    params: dict | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO decisions
            (user_id, strategy_id, instrument_id, decision_date, action,
             features_json, params_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            strategy_id,
            instrument_id,
            decision_date,
            action,
            json.dumps(features, sort_keys=True),
            json.dumps(params, sort_keys=True) if params is not None else None,
            _now(),
        ),
    )
    return int(cur.lastrowid)


def log_trade(
    conn,
    *,
    user_id: str,
    instrument_id: int,
    side: str,
    qty: float,
    price: float,
    fee: float,
    slippage: float,
    trade_date: str,
    decision_id: int | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO trades
            (user_id, instrument_id, side, qty, price, fee, slippage, trade_date, decision_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, instrument_id, side, qty, price, fee, slippage, trade_date, decision_id),
    )
    return int(cur.lastrowid)


def record_equity(conn, *, user_id: str, date: str, equity: float, cash: float, exposure: float) -> None:
    conn.execute(
        """
        INSERT INTO equity_curve (user_id, date, equity, cash, exposure)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id, date) DO UPDATE SET
            equity=excluded.equity, cash=excluded.cash, exposure=excluded.exposure
        """,
        (user_id, date, equity, cash, exposure),
    )

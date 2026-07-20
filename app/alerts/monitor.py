"""Intraday stop monitor — INFORMATIONAL tier only (ZERO decisions).

Compares the latest recorded (delayed) intraday price of each OPEN paper
position against its trailing stop and sends an early-warning Telegram card
when the price is near or below the stop. The evening `signals` run remains
the ONLY place decisions happen; this module never writes to decisions /
positions / paper_orders / trades — its sole write is the `intraday_alerts`
dedupe table (at most one card per position, session and state).

Cards are ephemeral (stale within minutes), so unlike the paper loop's
at-least-once queue, a card is marked sent on the ATTEMPT — an unconfigured
Telegram prints to the console once instead of every cycle.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.alerts import telegram

WARSAW = ZoneInfo("Europe/Warsaw")

NEAR_STOP = "NEAR_STOP"
STOP_BREACH = "STOP_BREACH"


def latest_intraday(conn, instrument_id: int):
    """Newest recorded bar (any source) for an instrument, or None."""
    return conn.execute(
        "SELECT bar_start, close FROM prices_intraday WHERE instrument_id = ? "
        "ORDER BY bar_start DESC LIMIT 1",
        (instrument_id,),
    ).fetchone()


def check_positions(
    conn,
    *,
    near_pct: float = 0.02,
    send_fn=telegram.send_text,
    now: datetime | None = None,
) -> list[dict]:
    """One monitor pass over all OPEN positions. Returns the warnings raised.

    A warning fires once per (position, session, state): NEAR_STOP when the
    latest delayed price sits within `near_pct` above the stop, STOP_BREACH
    when it is at/below the stop. No state, no price, no position is ever
    modified here.
    """
    now = now or datetime.now(WARSAW)
    session_date = now.date().isoformat()
    warnings: list[dict] = []
    rows = conn.execute(
        "SELECT p.id, p.instrument_id, p.qty, p.stop_price, i.ticker "
        "FROM positions p JOIN instruments i ON i.id = p.instrument_id "
        "WHERE p.status = 'OPEN' AND p.stop_price IS NOT NULL "
        "ORDER BY i.ticker",
    ).fetchall()
    for pos in rows:
        bar = latest_intraday(conn, pos["instrument_id"])
        if bar is None or bar["bar_start"][:10] != session_date:
            continue  # no fresh recording for today — nothing to compare
        price, stop = float(bar["close"]), float(pos["stop_price"])
        if price <= stop:
            state = STOP_BREACH
        elif price <= stop * (1.0 + near_pct):
            state = NEAR_STOP
        else:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO intraday_alerts "
            "(position_id, session_date, state, price, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (pos["id"], session_date, state, price, now.isoformat()),
        )
        if cur.rowcount == 0:
            continue  # already warned for this state today
        conn.commit()
        warning = {
            "state": state, "ticker": pos["ticker"], "price": price,
            "stop_price": stop, "qty": int(pos["qty"]),
            "bar_start": bar["bar_start"],
        }
        warnings.append(warning)
        if send_fn is not None:
            try:
                send_fn(telegram.format_intraday_warning_pl(warning))
            except Exception:  # noqa: BLE001 — alerting must never break the pass
                pass
    return warnings

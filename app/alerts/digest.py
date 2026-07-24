"""Morning digest — INFORMATIONAL tier only (ZERO decisions).

One Polish card at 07:30 (scheduler slot): every paper book's cash, open
positions vs their trailing stops (marked at the LAST AVAILABLE close — the
morning digest deliberately shows yesterday's close, no intraday data exists
yet), orders awaiting today's open, and fresh filings of the last 24h with
their materialized LLM verdicts where present. Reads only; the evening
`signals` run remains the only place decisions happen.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.paper.store import PAPER_PREFIX

WARSAW = ZoneInfo("Europe/Warsaw")

_MAX_FILINGS_SHOWN = 5


def _last_close(conn, instrument_id: int):
    row = conn.execute(
        "SELECT date, close FROM prices WHERE instrument_id = ? AND adjusted = 0 "
        "AND close IS NOT NULL ORDER BY date DESC LIMIT 1",
        (instrument_id,),
    ).fetchone()
    return (row["date"], float(row["close"])) if row else (None, None)


def _fresh_llm_verdict(conn, instrument_id: int, since_date: str):
    row = conn.execute(
        "SELECT llm_score FROM llm_features WHERE instrument_id = ? "
        "AND as_of_date >= ? AND llm_score IS NOT NULL "
        "ORDER BY as_of_date DESC LIMIT 1",
        (instrument_id, since_date),
    ).fetchone()
    return float(row["llm_score"]) if row else None


def _book_section(conn, user_id: str) -> list[str]:
    lines = [f"Konto: {user_id}"]
    state = conn.execute(
        "SELECT cash FROM paper_state WHERE user_id = ?", (user_id,)).fetchone()
    cash = float(state["cash"]) if state else 0.0

    positions = conn.execute(
        "SELECT p.instrument_id, p.qty, p.stop_price, i.ticker "
        "FROM positions p JOIN instruments i ON i.id = p.instrument_id "
        "WHERE p.user_id = ? AND p.status = 'OPEN' ORDER BY i.ticker",
        (user_id,),
    ).fetchall()
    mtm = 0.0
    pos_lines = []
    for p in positions:
        _, close = _last_close(conn, p["instrument_id"])
        if close is None:
            pos_lines.append(f"  {p['ticker'].upper()}: brak notowania")
            continue
        mtm += close * float(p["qty"])
        if p["stop_price"] is not None:
            stop = float(p["stop_price"])
            pct = (close / stop - 1.0) * 100.0
            pos_lines.append(
                f"  {p['ticker'].upper()}: {close:.2f} PLN, stop {stop:.2f} ({pct:+.1f}%)")
        else:
            pos_lines.append(f"  {p['ticker'].upper()}: {close:.2f} PLN, bez stopa")
    equity = cash + mtm
    lines.append(f"Kapital (wg ost. zamkniecia): {equity:,.2f} PLN "
                 f"(gotowka: {cash:,.2f} PLN)")
    if pos_lines:
        lines.append(f"Pozycje ({len(positions)}) vs stopy:")
        lines.extend(pos_lines)
    else:
        lines.append("Pozycje: brak")

    pending = conn.execute(
        "SELECT o.side, o.qty, i.ticker FROM paper_orders o "
        "JOIN instruments i ON i.id = o.instrument_id "
        "WHERE o.user_id = ? AND o.status = 'PENDING' ORDER BY i.ticker",
        (user_id,),
    ).fetchall()
    if pending:
        sides = {"BUY": "KUP", "SELL": "SPRZEDAJ"}
        lines.append("Zlecenia na dzisiejsze otwarcie: " + ", ".join(
            f"{sides.get(o['side'], o['side'])} {o['ticker'].upper()} {o['qty']} szt."
            for o in pending))
    return lines


def paper_users(conn) -> list[str]:
    return [r["user_id"] for r in conn.execute(
        "SELECT user_id FROM paper_state WHERE user_id LIKE ? ORDER BY user_id",
        (PAPER_PREFIX + "%",)).fetchall()]


def _fresh_filings(conn, now: datetime):
    since = (now - timedelta(hours=24)).isoformat()
    try:
        return conn.execute(
            "SELECT f.title, f.instrument_id, i.ticker FROM filings f "
            "LEFT JOIN instruments i ON i.id = f.instrument_id "
            "WHERE f.published_at >= ? ORDER BY f.published_at DESC",
            (since,),
        ).fetchall()
    except Exception:  # noqa: BLE001 — collector table may not exist yet
        return []


def build_user_digest(conn, user_id: str, *, now: datetime | None = None) -> str:
    """One user's morning card (routed to THAT user's chat by the scheduler)."""
    now = now or datetime.now(WARSAW)
    lines = ["🌅 Poranny przeglad GPW (paper)",
             f"Data: {now.date().isoformat()}", ""]
    lines.extend(_book_section(conn, user_id))
    lines.append("")
    lines.append("Informacja poranna — decyzje zapadaja WYLACZNIE wieczorem "
                 "(przebieg 19:30).")
    return "\n".join(lines)


def build_filings_digest(conn, *, now: datetime | None = None) -> str | None:
    """Market-wide filings card (shared/ops chat), or None with no news."""
    now = now or datetime.now(WARSAW)
    since_date = (now - timedelta(days=2)).date().isoformat()
    filings = _fresh_filings(conn, now)
    if not filings:
        return None
    lines = [f"Nowe komunikaty (24h): {len(filings)}"]
    for f in filings[:_MAX_FILINGS_SHOWN]:
        name = (f["ticker"] or "?").upper()
        title = (f["title"] or "(bez tytulu)")[:70]
        entry = f"  {name}: {title}"
        if f["instrument_id"] is not None:
            score = _fresh_llm_verdict(conn, f["instrument_id"], since_date)
            if score is not None:
                entry += f" [LLM {score:+.2f}]"
        lines.append(entry)
    if len(filings) > _MAX_FILINGS_SHOWN:
        lines.append(f"  ... i {len(filings) - _MAX_FILINGS_SHOWN} kolejnych")
    return "\n".join(lines)


def build_digest(conn, *, now: datetime | None = None) -> str | None:
    """Combined single-channel card: every book + the filings section.
    None when there is nothing to say (no paper book AND no fresh filings)."""
    now = now or datetime.now(WARSAW)
    users = paper_users(conn)
    filings_card = build_filings_digest(conn, now=now)
    if not users and filings_card is None:
        return None

    lines = ["🌅 Poranny przeglad GPW (paper)",
             f"Data: {now.date().isoformat()}"]
    for user_id in users:
        lines.append("")
        lines.extend(_book_section(conn, user_id))
    lines.append("")
    lines.append(filings_card if filings_card is not None
                 else "Nowe komunikaty (24h): 0")
    lines.append("")
    lines.append("Informacja poranna — decyzje zapadaja WYLACZNIE wieczorem "
                 "(przebieg 19:30).")
    return "\n".join(lines)

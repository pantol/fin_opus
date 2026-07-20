"""Telegram notifier (dry-run by default).

With TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID set it POSTs a real message to the
Bot API; if either is unset it runs in DRY-RUN mode and prints the alert card
to the console (no network). End-user strings are in Polish (per conventions);
code/comments stay English.

This is an OUTPUT-only notifier; it is NOT part of the money/decision path.
"""
from __future__ import annotations

import os

import requests

_API = "https://api.telegram.org/bot{token}/sendMessage"


def _format_card(decision: dict) -> str:
    """Build a Polish-language alert card from a decision dict."""
    action_pl = {"ENTER": "WEJSCIE", "EXIT": "WYJSCIE", "HOLD": "TRZYMAJ"}.get(
        decision.get("action", ""), decision.get("action", "")
    )
    lines = [
        "📈 Sygnal GPW (paper)",
        f"Akcja: {action_pl}",
        f"Walor: {decision.get('ticker', '?')}",
        f"Data: {decision.get('decision_date', '?')}",
    ]
    if decision.get("price") is not None:
        lines.append(f"Cena: {decision['price']:.2f} PLN")
    if decision.get("qty") is not None:
        lines.append(f"Ilosc: {decision['qty']}")
    if decision.get("stop_price") is not None:
        lines.append(f"Stop: {decision['stop_price']:.2f} PLN")
    return "\n".join(lines)


def send_text(text: str, *, token: str | None = None, chat_id: str | None = None) -> dict:
    """Send (or dry-run) a plain-text alert. Returns a small status dict.

    Dry-run contract: with no token/chat_id the card is printed to stdout and
    {"mode": "dry-run", "sent": False, "card": ...} is returned — callers
    (data-quality, staleness, backup, LLM budget alerts) rely on this never
    raising in an unconfigured environment.
    """
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("[telegram dry-run]\n" + text)
        return {"mode": "dry-run", "sent": False, "card": text}

    resp = requests.post(
        _API.format(token=token),
        data={"chat_id": chat_id, "text": text},
        timeout=15,
    )
    resp.raise_for_status()
    return {"mode": "live", "sent": True, "card": text}


def send_alert(decision: dict, *, token: str | None = None, chat_id: str | None = None) -> dict:
    """Send (or dry-run) a decision alert card."""
    return send_text(_format_card(decision), token=token, chat_id=chat_id)


# --- paper-loop cards (output only; the loop sends them AFTER commit) ---------

_SIDE_PL = {"BUY": "KUP", "SELL": "SPRZEDAJ"}
_LAPSE_PL = {
    "no_bar": "brak notowan na sesji realizacji",
    "volume": "wolumen sesji zbyt niski (limit udzialu)",
    "no_position": "brak pozycji do sprzedazy",
}


def format_order_signal_pl(order: dict) -> str:
    """New signal card: the order is PENDING until the next session's open."""
    side = _SIDE_PL.get(order.get("side", ""), order.get("side", "?"))
    lines = [
        "📌 Sygnal GPW (paper)",
        f"Akcja: {side} {order.get('ticker', '?').upper()}",
        f"Ilosc: {order.get('qty', '?')}",
        f"Data decyzji: {order.get('decision_date', '?')}",
    ]
    if order.get("stop_price") is not None:
        lines.append(f"Stop: {float(order['stop_price']):.2f} PLN")
    lines.append("Realizacja: otwarcie nastepnej sesji (potwierdzenie jutro)")
    return "\n".join(lines)


def format_order_outcome_pl(order: dict) -> str:
    """Fill / partial-fill / lapse card for a settled order."""
    ticker = order.get("ticker", "?").upper()
    side = _SIDE_PL.get(order.get("side", ""), order.get("side", "?"))
    status = order.get("status")
    if status == "LAPSED":
        reason = _LAPSE_PL.get(order.get("lapse_reason", ""),
                               order.get("lapse_reason", "?"))
        return "\n".join([
            "⚠️ Zlecenie GPW (paper): NIE ZREALIZOWANO",
            f"Zlecenie: {side} {ticker}, {order.get('qty', '?')} szt.",
            f"Powod: {reason}",
            f"Data: {order.get('fill_date', '?')}",
        ])
    verb = "KUPIONO" if order.get("side") == "BUY" else "SPRZEDANO"
    lines = [
        "✅ Zlecenie GPW (paper): ZREALIZOWANO",
        f"{verb} {ticker}: {order.get('fill_qty', '?')} szt. po "
        f"{float(order.get('fill_price') or 0.0):.2f} PLN",
        f"Data: {order.get('fill_date', '?')}",
    ]
    if status == "PARTIAL" or (order.get("fill_qty") or 0) < (order.get("qty") or 0):
        lines.append(f"Uwaga: zlecenie czesciowe ({order.get('fill_qty')}/"
                     f"{order.get('qty')} szt., limit wolumenu)")
    return "\n".join(lines)


def format_intraday_warning_pl(w: dict) -> str:
    """Early-warning card from the intraday monitor (informational only)."""
    pct = (w["price"] / w["stop_price"] - 1.0) * 100.0
    when = str(w.get("bar_start", ""))[11:16]
    if w["state"] == "STOP_BREACH":
        head = "🔻 Monitor GPW (paper): kurs PONIZEJ stopa"
        detail = f"{w['ticker'].upper()}: {w['price']:.2f} PLN ({pct:+.1f}% od stopa {w['stop_price']:.2f})"
    else:
        head = "🟡 Monitor GPW (paper): kurs blisko stopa"
        detail = f"{w['ticker'].upper()}: {w['price']:.2f} PLN ({pct:+.1f}% nad stopem {w['stop_price']:.2f})"
    return "\n".join([
        head,
        detail,
        f"Pozycja: {w['qty']} szt., notowanie z ok. {when} (dane opoznione ~15 min)",
        "Informacja pogladowa — decyzja zapada WYLACZNIE na zamknieciu; "
        "ewentualna sprzedaz zakolejkuje wieczorny przebieg.",
    ])


def format_paper_summary_pl(*, date: str, equity: float, cash: float,
                            n_open: int) -> str:
    return "\n".join([
        "📊 Portfel GPW (paper)",
        f"Sesja: {date}",
        f"Kapital: {equity:,.2f} PLN (gotowka: {cash:,.2f} PLN)",
        f"Otwarte pozycje: {n_open}",
    ])

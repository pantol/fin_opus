"""Telegram alert stub.

If TELEGRAM_BOT_TOKEN is unset, runs in DRY-RUN mode and prints the alert card
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


def send_alert(decision: dict, *, token: str | None = None, chat_id: str | None = None) -> dict:
    """Send (or dry-run) an alert. Returns a small status dict."""
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    card = _format_card(decision)

    if not token or not chat_id:
        print("[telegram dry-run]\n" + card)
        return {"mode": "dry-run", "sent": False, "card": card}

    resp = requests.post(
        _API.format(token=token),
        data={"chat_id": chat_id, "text": card},
        timeout=15,
    )
    resp.raise_for_status()
    return {"mode": "live", "sent": True, "card": card}

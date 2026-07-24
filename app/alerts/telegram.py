"""Telegram notifier (dry-run by default).

With TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID set it POSTs a real message to the
Bot API; if either is unset it runs in DRY-RUN mode and prints the alert card
to the console (no network). End-user strings are in Polish (per conventions);
code/comments stay English.

This is an OUTPUT-only notifier; it is NOT part of the money/decision path.
"""
from __future__ import annotations

import json
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


def _llm_verdict_pl(score: float) -> str:
    if score > 0:
        return "pozytywny"
    if score < 0:
        return "negatywny"
    return "neutralny"


def _order_llm_score(order: dict) -> float | None:
    """llm_score from the order's decision-time feature snapshot, if any.

    Accepts both the DB row shape (features_json TEXT) and an already-parsed
    dict; anything malformed reads as "no verdict" (display must never raise).
    """
    feats = order.get("features_json") or order.get("features")
    if isinstance(feats, str):
        try:
            feats = json.loads(feats)
        except (ValueError, TypeError):
            return None
    if not isinstance(feats, dict):
        return None
    score = feats.get("llm_score")
    return float(score) if isinstance(score, (int, float)) else None


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
    llm_score = _order_llm_score(order)
    if llm_score is not None and order.get("side") == "BUY":
        lines.append(f"Werdykt LLM: {llm_score:+.2f} ({_llm_verdict_pl(llm_score)})")
    lines.append("Realizacja: otwarcie nastepnej sesji (potwierdzenie jutro)")
    return "\n".join(lines)


def format_llm_radar_pl(*, date: str, permits: list, vetoes: list,
                        no_score: int) -> str:
    """Informational card: how the LLM gate shaped TODAY's entry candidates.

    Output-only — by the time this renders, the deterministic decide already
    happened; the LLM never sizes, never sets stops, never moves money.
    """
    lines = ["🧠 Radar LLM (paper, informacyjnie)", f"Sesja: {date}"]
    if vetoes:
        lines.append("Weta wejsc (score < 0): " + ", ".join(
            f"{t.upper()} {s:+.2f}" for t, s in vetoes))
    if permits:
        lines.append("Dopuszczone wejscia (score >= 0): " + ", ".join(
            f"{t.upper()} {s:+.2f}" if s is not None else t.upper()
            for t, s in permits))
    if no_score:
        lines.append(f"Kandydaci bez werdyktu: {no_score} "
                     "(wejscie zamkniete do czasu analizy)")
    lines.append("LLM tylko filtruje wejscia — sizing, stopy i wyjscia "
                 "pozostaja deterministyczne.")
    return "\n".join(lines)


def format_regime_radar_pl(flip: dict) -> str:
    """Regime state-flip card (Phase 3 radar). Output-only: rendered AFTER the
    deterministic decide; the regime never sizes, never sets stops."""
    c = flip.get("components") or {}
    if flip["to_state"] == "risk_off":
        head = "🛰️ Radar rynku: przelaczenie na RISK-OFF"
        tail = ("Strategie z bramka rezimu wstrzymuja NOWE wejscia; "
                "stopy i wyjscia dzialaja bez zmian.")
    else:
        head = "🛰️ Radar rynku: powrot do RISK-ON"
        tail = "Strategie z bramka rezimu znow dopuszczaja nowe wejscia."
    lines = [
        head,
        f"Sesja: {flip['date']}",
        f"Skladowa ryzyka: {flip['score']:+.2f} "
        f"(trend {c.get('trend', 0):+.2f}, szerokosc {c.get('breadth', 0):+.2f}, "
        f"zmiennosc {c.get('vol', 0):+.2f}, obsuniecie {c.get('drawdown', 0):+.2f}, "
        f"LLM {c.get('llm', 0):+.2f})",
        tail,
        "Informacja pogladowa — wszystkie decyzje pozostaja deterministyczne.",
    ]
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
                            n_open: int, n_candidates: int | None = None,
                            n_new_signals: int | None = None) -> str:
    lines = [
        "📊 Portfel GPW (paper)",
        f"Sesja: {date}",
        f"Kapital: {equity:,.2f} PLN (gotowka: {cash:,.2f} PLN)",
        f"Otwarte pozycje: {n_open}",
    ]
    if n_candidates is not None:
        lines.append(f"Kandydaci do wejscia: {n_candidates}"
                     + (f" • nowe sygnaly: {n_new_signals}"
                        if n_new_signals is not None else ""))
    return "\n".join(lines)

"""Decision/trade logging to SQLite + Telegram dry-run stub."""
import json

from app.alerts import telegram
from app.ingestion import stooq
from app.logging import decisions as declog


def test_log_decision_stores_full_feature_snapshot(conn):
    inst_id = stooq.upsert_instrument(conn, {"ticker": "pko", "name": "PKO"})
    feats = {"close": 50.0, "momentum_6m": 0.1, "atr": 1.2}
    dec_id = declog.log_decision(
        conn, user_id="default", strategy_id=None, instrument_id=inst_id,
        decision_date="2020-06-01", action="ENTER", features=feats,
    )
    row = conn.execute("SELECT user_id, action, features_json FROM decisions WHERE id=?",
                       (dec_id,)).fetchone()
    assert row["user_id"] == "default"          # multi-tenant column present
    assert row["action"] == "ENTER"
    assert json.loads(row["features_json"]) == feats  # full snapshot (reproducibility)


def test_log_trade_and_equity(conn):
    inst_id = stooq.upsert_instrument(conn, {"ticker": "pko", "name": "PKO"})
    declog.log_trade(conn, user_id="default", instrument_id=inst_id, side="BUY",
                     qty=10, price=50.2, fee=3.0, slippage=2.0, trade_date="2020-06-02")
    declog.record_equity(conn, user_id="default", date="2020-06-02",
                         equity=100000.0, cash=50000.0, exposure=0.5)
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM equity_curve").fetchone()[0] == 1


def test_telegram_dry_run_without_token(capsys):
    decision = {"action": "ENTER", "ticker": "pko", "decision_date": "2020-06-01",
                "price": 50.2, "qty": 10, "stop_price": 45.0}
    status = telegram.send_alert(decision, token=None, chat_id=None)
    assert status["mode"] == "dry-run"
    assert status["sent"] is False
    out = capsys.readouterr().out
    assert "Sygnal GPW" in out          # Polish end-user string
    assert "WEJSCIE" in out


def test_signal_card_shows_llm_verdict_for_buy():
    order = {"side": "BUY", "ticker": "pko", "qty": 160,
             "decision_date": "2026-07-23", "stop_price": 100.99,
             "features_json": json.dumps({"close": 107.22, "llm_score": 0.4})}
    card = telegram.format_order_signal_pl(order)
    assert "Werdykt LLM: +0.40 (pozytywny)" in card

    order["features_json"] = json.dumps({"close": 107.22, "llm_score": 0.0})
    assert "Werdykt LLM: +0.00 (neutralny)" in telegram.format_order_signal_pl(order)

    # No llm_score in the snapshot (baseline strategy) -> no LLM line at all.
    order["features_json"] = json.dumps({"close": 107.22})
    assert "Werdykt LLM" not in telegram.format_order_signal_pl(order)

    # Malformed snapshot must never break the card (display-only path).
    order["features_json"] = "{not json"
    assert "Sygnal GPW" in telegram.format_order_signal_pl(order)


def test_llm_radar_card_lists_vetoes_permits_and_no_score():
    card = telegram.format_llm_radar_pl(
        date="2026-07-23",
        permits=[("pko", 0.4), ("peo", 0.0)],
        vetoes=[("pkn", -0.65), ("alr", -0.7)],
        no_score=47,
    )
    assert "Radar LLM" in card
    assert "Weta wejsc (score < 0): PKN -0.65, ALR -0.70" in card
    assert "Dopuszczone wejscia (score >= 0): PKO +0.40, PEO +0.00" in card
    assert "Kandydaci bez werdyktu: 47" in card
    assert "deterministyczne" in card  # the input-only disclaimer stays

    # Empty sections are omitted, the disclaimer is not.
    short = telegram.format_llm_radar_pl(date="2026-07-23", permits=[],
                                         vetoes=[("pkn", -0.65)], no_score=0)
    assert "Dopuszczone" not in short and "bez werdyktu" not in short
    assert "PKN -0.65" in short


def test_summary_card_funnel_line_is_optional():
    with_funnel = telegram.format_paper_summary_pl(
        date="2026-07-23", equity=100000.0, cash=100000.0, n_open=0,
        n_candidates=52, n_new_signals=3)
    assert "Kandydaci do wejscia: 52 • nowe sygnaly: 3" in with_funnel

    legacy = telegram.format_paper_summary_pl(
        date="2026-07-23", equity=100000.0, cash=100000.0, n_open=0)
    assert "Kandydaci" not in legacy

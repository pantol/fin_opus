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

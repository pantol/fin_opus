"""Phase 6 — multi-tenant boundary: two profiled paper books plus the default
book share one database and NEVER touch each other's state; cards route to
per-user chats; --all-users never bootstraps an unstarted book. The exit
gate: a second user runs safely and separately."""
from __future__ import annotations

import pandas as pd

from app import config as cfg
from app.alerts import telegram
from app.backtest import engine  # noqa: F401 (parity import kept close)
from app.ingestion import stooq
from app.paper import loop as paper_loop
from app.paper import store as paper_store
from app.users import profiles as prof

from tests.conftest import bt_config_no_gate, make_stooq_csv
from tests.test_profiles import PROFILES_CFG


def _seed(conn, n=500):
    def rows(closes):
        dates = pd.bdate_range("2015-01-02", periods=len(closes))
        return [(d.date().isoformat(), c, c * 1.03, c * 0.97, c, 1_000_000.0)
                for d, c in zip(dates, closes)]

    def closes(drift, base=100.0):
        out, level = [], base
        for _ in range(n):
            level *= 1 + drift
            out.append(level)
        return out

    series = {"wig20tr": closes(0.0006, base=2000),
              "bnk": closes(0.0009), "tec": closes(0.0008)}
    sectors = {"bnk": "banking", "tec": "tech"}
    for t, c in series.items():
        iid = stooq.upsert_instrument(
            conn, {"ticker": t, "name": t, "sector": sectors.get(t),
                   "listed_from": "2015-01-01"},
            is_index=(t == "wig20tr"))
        stooq.store_bars(conn, iid, stooq.parse_csv(make_stooq_csv(rows(c))),
                         source="stooq")
    conn.commit()
    return {"benchmark": {"ticker": "wig20tr", "is_index": True}, "indices": [],
            "instruments": [{"ticker": t, "sector": sectors[t]}
                            for t in ("bnk", "tec")]}


def _profile(user, answers):
    return prof.build_profile(user, answers, PROFILES_CFG)


def _run(conn, uni, bt, profile, session, **kw):
    strat = prof.apply_profile(cfg.load_strategy("trend_momentum"),
                               profile, PROFILES_CFG)
    return paper_loop.run_signals(
        conn, universe=uni, bt_cfg=bt, strategy_cfg=strat,
        session_end=session, send_fn=None, user=profile["user_id"],
        excluded_sectors=prof.excluded_sectors(profile), **kw)


def test_two_books_share_a_db_but_never_state(conn):
    uni = _seed(conn)
    bt = bt_config_no_gate()
    kamil = _profile("kamil", {"horizon": "b", "reaction": "b", "max_loss": "b",
                               "experience": "c"})
    ala = _profile("ala", {"horizon": "a", "reaction": "a", "max_loss": "a",
                           "experience": "a", "exclusions": "banking"})

    code, rep = _run(conn, uni, bt, kamil, "2016-06-01")
    assert code == 0, rep.as_text()
    code, rep = _run(conn, uni, bt, ala, "2016-06-01")
    assert code == 0, rep.as_text()
    # settle the fills one session later, kamil only
    code, _ = _run(conn, uni, bt, kamil, "2016-06-02")
    assert code == 0

    k_state = paper_store.load_state(conn, "paper:kamil")
    a_state = paper_store.load_state(conn, "paper:ala")
    # Independent watermarks: kamil advanced a session, ala did not.
    assert k_state["last_settled_date"] == "2016-06-02"
    assert a_state["last_settled_date"] == "2016-06-01"

    # Sector exclusion separates the BOOKS, not just the backtest: kamil's
    # book ordered the bank, ala's never did.
    k_orders = {r["user_id"] for r in conn.execute(
        "SELECT DISTINCT o.user_id FROM paper_orders o "
        "JOIN instruments i ON i.id = o.instrument_id WHERE i.ticker = 'bnk'")}
    assert k_orders == {"paper:kamil"}

    # Every persisted row is namespaced; nothing leaks across users.
    for table in ("paper_orders", "decisions", "positions", "trades"):
        others = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE user_id NOT LIKE 'paper:%'"
        ).fetchone()[0]
        assert others == 0, f"{table} grew a non-paper row"


def test_run_for_one_user_never_touches_the_other(conn):
    uni = _seed(conn)
    bt = bt_config_no_gate()
    kamil = _profile("kamil", {"horizon": "b", "reaction": "b", "max_loss": "b",
                               "experience": "c"})
    ala = _profile("ala", {"horizon": "a", "reaction": "a", "max_loss": "a",
                           "experience": "a"})
    _run(conn, uni, bt, kamil, "2016-06-01")
    _run(conn, uni, bt, ala, "2016-06-01")
    before = dict(paper_store.load_state(conn, "paper:ala"))
    _run(conn, uni, bt, kamil, "2016-06-03")
    after = dict(paper_store.load_state(conn, "paper:ala"))
    assert before == after  # byte-for-byte untouched


def test_books_carry_distinct_config_hashes(conn):
    uni = _seed(conn)
    bt = bt_config_no_gate()
    kamil = _profile("kamil", {"horizon": "b", "reaction": "b", "max_loss": "b",
                               "experience": "c"})
    ala = _profile("ala", {"horizon": "a", "reaction": "a", "max_loss": "a",
                           "experience": "a", "exclusions": "banking"})
    _run(conn, uni, bt, kamil, "2016-06-01")
    _run(conn, uni, bt, ala, "2016-06-01")
    hashes = {r["user_id"]: r["config_hash"] for r in conn.execute(
        "SELECT user_id, config_hash FROM paper_state")}
    assert hashes["paper:kamil"] != hashes["paper:ala"]


# --- chat routing ------------------------------------------------------------

def test_chat_routing_prefers_user_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
    monkeypatch.setenv("TELEGRAM_CHAT_ID__ALA", "123")
    assert telegram.chat_id_for_user("ala") == "123"
    assert telegram.chat_id_for_user("paper:ala") == "123"
    assert telegram.chat_id_for_user("kamil") == "999"   # fallback: shared chat
    assert telegram.chat_id_for_user(None) == "999"
    monkeypatch.delenv("TELEGRAM_CHAT_ID")
    assert telegram.chat_id_for_user("kamil") is None


# --- --all-users bootstrap policy --------------------------------------------

def test_all_users_never_bootstraps_an_unstarted_book(conn, capsys, monkeypatch):
    from app import cli as cli_mod
    from app.cli import _signals_for_user

    # The CLI helper wires the real Telegram send path; force dry-prints even
    # if the running shell exports live credentials.
    monkeypatch.setattr(cli_mod.telegram, "user_send_fn",
                        lambda user: (lambda text: {"mode": "dry-run",
                                                    "sent": False}))
    uni = _seed(conn)
    bt = bt_config_no_gate()
    dron = _profile("dron", {"horizon": "c", "reaction": "c", "max_loss": "c",
                             "experience": "c"})
    prof.save_profile(conn, dron)

    class Args:
        strategy = "trend_momentum"
        session = "2016-06-01"
        accept_config_change = False
        dry_run = False

    code = _signals_for_user(conn, uni, bt, Args(), "dron", require_started=True)
    out = capsys.readouterr().out
    assert code == 0 and "skipped" in out
    assert paper_store.load_state(conn, "paper:dron") is None  # NOT bootstrapped
    # ...but the deliberate manual first run does start it.
    code = _signals_for_user(conn, uni, bt, Args(), "dron", require_started=False)
    assert code == 0
    assert paper_store.load_state(conn, "paper:dron") is not None

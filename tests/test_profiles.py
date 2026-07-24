"""Phase 5 — survey -> profile -> deterministic gating. The exit gate:
three distinct profiles produce three DIFFERENT end-to-end behaviors from the
same market data, and no profile can ever loosen risk past the hard caps."""
from __future__ import annotations

import numpy as np
import pandas as pd

from app import config as cfg
from app.backtest import engine
from app.cli import main as cli_main
from app.ingestion import stooq
from app.users import profiles as prof

from tests.conftest import bt_config_no_gate, make_stooq_csv

PROFILES_CFG = {
    "tolerances": {
        "conservative": {"strategy": "trend_momentum_regime",
                         "risk_multiplier": 0.5, "max_drawdown_pct": 0.10,
                         "max_positions": 5},
        "balanced": {"strategy": "trend_momentum", "risk_multiplier": 1.0,
                     "max_drawdown_pct": 0.25, "max_positions": 8},
        "aggressive": {"strategy": "trend_momentum_llm", "risk_multiplier": 1.5,
                       "max_drawdown_pct": 0.40, "max_positions": 10},
    },
    "hard_caps": {"risk_per_trade_max": 0.02, "max_total_exposure": 1.0,
                  "drawdown_breaker_max": 0.40},
}


# --- survey mapping ----------------------------------------------------------

def test_survey_scoring_buckets():
    assert prof.score_answers({"horizon": "a", "reaction": "a", "max_loss": "a",
                               "experience": "a"}) == "conservative"
    assert prof.score_answers({"horizon": "b", "reaction": "b", "max_loss": "b",
                               "experience": "a"}) == "balanced"
    assert prof.score_answers({"horizon": "c", "reaction": "c", "max_loss": "c",
                               "experience": "c"}) == "aggressive"
    assert prof.score_answers({}) == "conservative"  # missing answers = cautious


def test_build_profile_takes_stricter_of_answer_and_bucket():
    # Aggressive bucket allows 40% dd, but the user ANSWERED 10% -> 10% wins.
    p = prof.build_profile("dron", {"horizon": "c", "reaction": "c",
                                    "max_loss": "a", "experience": "c",
                                    "exclusions": "banking, energy"},
                           PROFILES_CFG)
    assert p["risk_tolerance"] == "aggressive"
    assert p["max_drawdown_pct"] == 0.10
    assert p["excluded_sectors"] == ["banking", "energy"]


def test_profile_persistence_roundtrip(conn):
    p = prof.build_profile("kamil", {"horizon": "b", "reaction": "b",
                                     "max_loss": "b", "experience": "c"},
                           PROFILES_CFG, display_name="Kamil")
    prof.save_profile(conn, p)
    loaded = prof.load_profile(conn, "kamil")
    assert loaded["risk_tolerance"] == "balanced"
    assert loaded["strategy"] == "trend_momentum"
    assert loaded["survey"]["max_loss"] == "b"
    # re-survey updates in place (no duplicate row)
    p2 = prof.build_profile("kamil", {"horizon": "a", "reaction": "a",
                                      "max_loss": "a", "experience": "a"},
                            PROFILES_CFG)
    prof.save_profile(conn, p2)
    assert prof.load_profile(conn, "kamil")["risk_tolerance"] == "conservative"
    assert conn.execute("SELECT COUNT(*) FROM user_profiles").fetchone()[0] == 1


def test_survey_cli_non_interactive(conn, tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    code = cli_main(["--db", str(db), "survey", "--user", "ala",
                     "--answers", "a,a,a,a,banking"])
    assert code == 0
    from app.db import connect
    c = connect(str(db))
    row = c.execute("SELECT * FROM user_profiles WHERE user_id='ala'").fetchone()
    assert row["risk_tolerance"] == "conservative"
    assert "banking" in row["excluded_sectors"]
    c.close()


# --- apply_profile risk overlay ----------------------------------------------

def test_apply_profile_scales_and_caps():
    strat = cfg.load_strategy("trend_momentum")
    aggressive = prof.build_profile(
        "dron", {"horizon": "c", "reaction": "c", "max_loss": "c",
                 "experience": "c"}, PROFILES_CFG)
    out = prof.apply_profile(strat, aggressive, PROFILES_CFG)
    assert out["risk"]["risk_per_trade"] == 0.015          # 0.01 * 1.5
    assert strat["risk"]["risk_per_trade"] == 0.01         # original untouched
    assert out["profile"]["user_id"] == "dron"
    # multiplier can never pierce the hard cap
    silly = dict(aggressive, risk_multiplier=50.0)
    capped = prof.apply_profile(strat, silly, PROFILES_CFG)
    assert capped["risk"]["risk_per_trade"] == 0.02
    conservative = prof.build_profile(
        "ala", {"horizon": "a", "reaction": "a", "max_loss": "a",
                "experience": "a"}, PROFILES_CFG)
    out_c = prof.apply_profile(strat, conservative, PROFILES_CFG)
    assert out_c["risk"]["risk_per_trade"] == 0.005
    assert out_c["risk"]["drawdown_circuit_breaker"] == 0.10
    assert out_c["risk"]["max_open_positions"] == 5


def test_profile_changes_paper_fingerprint():
    from app.paper import loop as paper_loop

    uni = {"benchmark": {"ticker": "wig20tr"}, "instruments": []}
    bt = bt_config_no_gate()
    strat = cfg.load_strategy("trend_momentum")
    balanced = prof.build_profile("kamil", {"horizon": "b", "reaction": "b",
                                            "max_loss": "b", "experience": "c"},
                                  PROFILES_CFG)
    applied = prof.apply_profile(strat, balanced, PROFILES_CFG)
    assert (paper_loop.config_hash(strat, bt, uni)
            != paper_loop.config_hash(applied, bt, uni))


# --- end-to-end gate: three profiles, three behaviors ------------------------

def _seed(conn):
    def rows(closes):
        # Wide bars (+/-3%): the ATR-derived risk position stays well under
        # the 20% per-name cap, so sizing differences between profiles are
        # visible instead of being flattened by the cap.
        dates = pd.bdate_range("2015-01-02", periods=len(closes))
        return [(d.date().isoformat(), c, c * 1.03, c * 0.97, c, 1_000_000.0)
                for d, c in zip(dates, closes)]

    def closes(drift, n=500, base=100.0):
        out, level = [], base
        for _ in range(n):
            level *= 1 + drift
            out.append(level)
        return out

    series = {"wig20tr": closes(0.0006, base=2000),
              "bnk": closes(0.0009), "tec": closes(0.0008),
              "enr": closes(0.0007)}
    sectors = {"bnk": "banking", "tec": "tech", "enr": "energy"}
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
                            for t in ("bnk", "tec", "enr")]}


def test_three_profiles_three_behaviors_end_to_end(conn):
    uni = _seed(conn)
    bt = bt_config_no_gate()
    strat = cfg.load_strategy("trend_momentum")
    instruments, bench = engine.load_instruments(conn, uni, "wig20tr")

    balanced = prof.build_profile("kamil", {"horizon": "b", "reaction": "b",
                                            "max_loss": "b", "experience": "c"},
                                  PROFILES_CFG)
    aggressive = prof.build_profile("dron", {"horizon": "c", "reaction": "c",
                                             "max_loss": "c", "experience": "c"},
                                    PROFILES_CFG)
    ala_answers = {"horizon": "a", "reaction": "a", "max_loss": "a",
                   "experience": "a", "exclusions": "banking"}
    cautious = prof.build_profile("ala", ala_answers, PROFILES_CFG)

    res = {}
    for profile in (balanced, aggressive, cautious):
        applied = prof.apply_profile(strat, profile, PROFILES_CFG)
        res[profile["user_id"]] = engine.run_backtest(
            instruments, bench, applied, bt,
            excluded_sectors=prof.excluded_sectors(profile))

    def first_buy_qty(r, tk):
        for d in r.decisions:
            if d["action"] == "ENTER" and d["ticker"] == tk:
                return d["qty"]
        return None

    # Ala's exclusion holds end-to-end: banking never entered, others are.
    ala_entered = {d["ticker"] for d in res["ala"].decisions
                   if d["action"] == "ENTER"}
    assert "bnk" not in ala_entered and "tec" in ala_entered
    assert "bnk" in {d["ticker"] for d in res["kamil"].decisions
                     if d["action"] == "ENTER"}
    # Sizing scales with the multiplier: dron > kamil > ala on the same name.
    q_kamil = first_buy_qty(res["kamil"], "tec")
    q_dron = first_buy_qty(res["dron"], "tec")
    q_ala = first_buy_qty(res["ala"], "tec")
    assert q_kamil and q_dron and q_ala
    assert q_dron > q_kamil > q_ala

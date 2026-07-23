"""Cross-sectional entry ranking: strongest candidates get the book's slots.

With a full-market universe and a max_open_positions cap, same-day entry
candidates used to be sized in instrument-id order — the book filled by
arrival, not strength. `entry_ranking` (strategy YAML) orders the day's entry
candidates by pre-materialized features (llm_score, momentum_6m, ...) before
the risk layer sizes them.

Guarantees pinned here:
- config grammar: defaults, validation errors (a typo must fail the run);
- key semantics: desc/asc, missing-after-scored per key, stable ties;
- engine behavior: ranked candidate wins the slot regardless of instrument id;
  absent ranking preserves the legacy id order;
- point-in-time: an llm_score materialized AFTER the decision date never
  affects that day's ranking;
- paper/backtest parity holds with ranking + LLM scores active (the ranking
  runs byte-identically in both loops).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.backtest import engine
from app.ingestion import stooq
from app.logging import decisions as declog
from app.paper import loop, store
from app.strategy.engine import entry_rank_key, entry_ranking_spec

from tests.conftest import bt_config_no_gate, make_stooq_csv, synthetic_series

# --- config grammar -----------------------------------------------------------


def test_spec_absent_or_empty_means_legacy_order():
    assert entry_ranking_spec({}) == []
    assert entry_ranking_spec({"entry_ranking": []}) == []


def test_spec_parses_orders_and_defaults_to_desc():
    cfg = {"entry_ranking": [{"feature": "llm_score"},
                             {"feature": "momentum_6m", "order": "desc"},
                             {"feature": "realized_vol", "order": "asc"}]}
    assert entry_ranking_spec(cfg) == [("llm_score", True),
                                       ("momentum_6m", True),
                                       ("realized_vol", False)]


def test_spec_malformed_raises():
    with pytest.raises(ValueError, match="must be a list"):
        entry_ranking_spec({"entry_ranking": {"feature": "llm_score"}})
    with pytest.raises(ValueError, match="string 'feature'"):
        entry_ranking_spec({"entry_ranking": [{"order": "desc"}]})
    with pytest.raises(ValueError, match="order must be one of"):
        entry_ranking_spec({"entry_ranking": [{"feature": "llm_score",
                                               "order": "descending"}]})


# --- key semantics ------------------------------------------------------------

SPEC = entry_ranking_spec({"entry_ranking": [{"feature": "llm_score"},
                                             {"feature": "momentum_6m"}]})


def _ranked(snaps):
    return sorted(snaps, key=lambda s: entry_rank_key(SPEC, s))


def test_scored_names_outrank_unscored_regardless_of_momentum():
    low_scored = {"llm_score": 0.1, "momentum_6m": 0.05}
    unscored_rocket = {"momentum_6m": 2.45}
    assert _ranked([unscored_rocket, low_scored]) == [low_scored, unscored_rocket]


def test_momentum_breaks_ties_and_orders_the_unscored():
    a = {"llm_score": 0.5, "momentum_6m": 0.10}
    b = {"llm_score": 0.5, "momentum_6m": 0.30}
    c = {"momentum_6m": 2.00}
    d = {"momentum_6m": 0.70}
    assert _ranked([a, c, d, b]) == [b, a, c, d]


def test_asc_orders_ascending():
    spec = entry_ranking_spec(
        {"entry_ranking": [{"feature": "realized_vol", "order": "asc"}]})
    calm, wild = {"realized_vol": 0.2}, {"realized_vol": 0.9}
    assert sorted([wild, calm], key=lambda s: entry_rank_key(spec, s)) == [calm, wild]


def test_nan_and_non_numeric_count_as_missing_never_crash():
    nan_snap = {"llm_score": float("nan"), "momentum_6m": 0.9}
    text_snap = {"llm_score": "2026-07-23", "momentum_6m": 0.8}  # bar_date-like
    none_snap = {"llm_score": None, "momentum_6m": 0.7}
    scored = {"llm_score": -0.9, "momentum_6m": 0.0}
    out = _ranked([nan_snap, text_snap, none_snap, scored])
    assert out[0] is scored  # even a negative score beats missing
    assert out[1:] == [nan_snap, text_snap, none_snap]  # momentum among missing


def test_full_ties_keep_input_order_stable():
    a, b = {"momentum_6m": 0.5, "id": 1}, {"momentum_6m": 0.5, "id": 2}
    spec = entry_ranking_spec({"entry_ranking": [{"feature": "momentum_6m"}]})
    assert sorted([a, b], key=lambda s: entry_rank_key(spec, s)) == [a, b]
    assert sorted([b, a], key=lambda s: entry_rank_key(spec, s)) == [b, a]


def test_ranking_only_llm_reference_counts_as_llm_use():
    cfg = {"entry": {"all": [{"feature": "momentum_6m", "op": "gt", "value": 0}]},
           "entry_ranking": [{"feature": "llm_score"}]}
    assert engine.strategy_uses_llm_features(cfg)
    cfg_no_llm = {"entry": {"all": [{"feature": "momentum_6m", "op": "gt", "value": 0}]},
                  "entry_ranking": [{"feature": "momentum_6m"}]}
    assert not engine.strategy_uses_llm_features(cfg_no_llm)


# --- engine: the slot goes to the ranked winner -------------------------------

N = 320  # > SMA200 warmup; all names signal the same first session


def _ingest(conn, ticker, rows, **inst):
    iid = stooq.upsert_instrument(conn, {"ticker": ticker, "name": ticker, **inst},
                                  is_index=inst.get("is_index", False))
    stooq.store_bars(conn, iid, stooq.parse_csv(make_stooq_csv(rows)), source="stooq")
    return iid


def _seed_market(conn):
    """id order = weak, strong, scored: legacy order would always pick `weak`."""
    _ingest(conn, "wig20tr", synthetic_series(n=N, base=2000, drift=0.0005),
            is_index=True)
    ids = {}
    ids["weak"] = _ingest(conn, "weak", synthetic_series(n=N, base=100, drift=0.0004),
                          sector="tech", listed_from="2018-01-01")
    ids["strong"] = _ingest(conn, "strong", synthetic_series(n=N, base=50, drift=0.0030),
                            sector="banking", listed_from="2018-01-01")
    ids["scored"] = _ingest(conn, "scored", synthetic_series(n=N, base=80, drift=0.0010),
                            sector="mining", listed_from="2018-01-01")
    conn.commit()
    return ids


def _universe():
    return {"benchmark": {"ticker": "wig20tr", "is_index": True}, "indices": [],
            "instruments": [{"ticker": "weak", "sector": "tech"},
                            {"ticker": "strong", "sector": "banking"},
                            {"ticker": "scored", "sector": "mining"}]}


def _one_slot_strategy(entry_ranking=None):
    cfg = {
        "name": "rank_test", "version": 1,
        "entry": {"all": [{"feature": "close_vs_sma200", "op": "gt", "value": 0.0},
                          {"feature": "momentum_6m", "op": "gt", "value": 0.0}]},
        "exit": {"any": [{"type": "atr_stop", "atr_mult": 2.5}]},
        "risk": {"risk_per_trade": 0.01, "atr_mult_stop": 2.5,
                 "max_open_positions": 1, "max_exposure_per_name": 0.20,
                 "max_exposure_per_sector": 0.40, "max_total_exposure": 1.0,
                 "drawdown_circuit_breaker": 0.25},
    }
    if entry_ranking is not None:
        cfg["entry_ranking"] = entry_ranking
    return cfg


def _first_entry(conn, strategy_cfg, *, attach_llm=False):
    instruments, bench = engine.load_instruments(conn, _universe(), "wig20tr")
    if attach_llm:
        instruments = engine.attach_llm_scores(conn, instruments)
    res = engine.run_backtest(instruments, bench, strategy_cfg, bt_config_no_gate())
    entries = [d for d in res.decisions if d["action"] == "ENTER"]
    assert entries, "fixture must produce an entry"
    return entries[0]


def test_without_ranking_lowest_id_takes_the_slot(conn):
    _seed_market(conn)
    assert _first_entry(conn, _one_slot_strategy())["ticker"] == "weak"


def test_momentum_ranking_gives_slot_to_strongest(conn):
    _seed_market(conn)
    strat = _one_slot_strategy([{"feature": "momentum_6m", "order": "desc"}])
    assert _first_entry(conn, strat)["ticker"] == "strong"


def test_llm_score_ranks_first_missing_scores_fall_back_to_momentum(conn):
    ids = _seed_market(conn)
    # `scored` has a verdict available from the very first bar; the others never
    # get one — they must rank after it (and never crash the loop).
    conn.execute(
        "INSERT INTO llm_features (instrument_id, as_of_date, llm_score, created_at)"
        " VALUES (?, '2018-01-01', 0.2, '2018-01-01T00:00:00')", (ids["scored"],))
    conn.commit()
    strat = _one_slot_strategy([{"feature": "llm_score", "order": "desc"},
                                {"feature": "momentum_6m", "order": "desc"}])
    assert _first_entry(conn, strat, attach_llm=True)["ticker"] == "scored"


def test_llm_score_published_after_decision_never_ranks_it(conn):
    """Point-in-time: a verdict materialized after T is invisible at T."""
    ids = _seed_market(conn)
    rows = synthetic_series(n=N, base=1, drift=0.0)
    first_entry_no_score = _first_entry(
        conn, _one_slot_strategy([{"feature": "llm_score", "order": "desc"},
                                  {"feature": "momentum_6m", "order": "desc"}]),
        attach_llm=True)
    # Materialize a top score for `scored` dated AFTER that decision date.
    later = next(d for d, *_ in rows if d > first_entry_no_score["decision_date"])
    conn.execute(
        "INSERT INTO llm_features (instrument_id, as_of_date, llm_score, created_at)"
        " VALUES (?, ?, 1.0, ?)", (ids["scored"], later, later + "T00:00:00"))
    conn.commit()
    strat = _one_slot_strategy([{"feature": "llm_score", "order": "desc"},
                                {"feature": "momentum_6m", "order": "desc"}])
    d = _first_entry(conn, strat, attach_llm=True)
    # The slot decision on the same first day must be unchanged: momentum winner.
    assert d["decision_date"] == first_entry_no_score["decision_date"]
    assert d["ticker"] == first_entry_no_score["ticker"] == "strong"


def test_malformed_ranking_fails_the_run(conn):
    _seed_market(conn)
    instruments, bench = engine.load_instruments(conn, _universe(), "wig20tr")
    strat = _one_slot_strategy([{"feature": "llm_score", "order": "best-first"}])
    with pytest.raises(ValueError, match="order must be one of"):
        engine.run_backtest(instruments, bench, strat, bt_config_no_gate())


# --- paper/backtest parity with ranking + scores active -----------------------


def test_paper_parity_with_ranking_and_llm_scores(conn):
    """The ranked entry pass is byte-compatible between engine and paper loop."""
    ids = _seed_market(conn)
    conn.execute(
        "INSERT INTO llm_features (instrument_id, as_of_date, llm_score, created_at)"
        " VALUES (?, '2018-01-01', 0.9, '2018-01-01T00:00:00')", (ids["scored"],))
    conn.commit()
    strat = _one_slot_strategy([{"feature": "llm_score", "order": "desc"},
                                {"feature": "momentum_6m", "order": "desc"}])
    strat["risk"]["max_open_positions"] = 2  # contention AND multiple entries
    bt_cfg = bt_config_no_gate()
    bt_cfg["paper"] = {"catchup_max_sessions": 100000}

    instruments, bench = engine.load_instruments(conn, _universe(), "wig20tr")
    instruments_llm = engine.attach_llm_scores(conn, instruments)
    result = engine.run_backtest(instruments_llm, bench, strat, bt_cfg)
    assert [d for d in result.decisions if d["action"] == "ENTER"], "need entries"

    calendar = engine._trading_calendar(instruments)
    user_id = store.paper_user_id(bt_cfg["user_id"])
    strategy_id = declog.register_strategy(conn, strat["name"],
                                           int(strat["version"]), "test")
    store.init_state(
        conn, user_id=user_id, initial_capital=float(bt_cfg["initial_capital"]),
        inception_date=calendar[0].date().isoformat(),
        last_settled_date=(calendar[0].date() - timedelta(days=1)).isoformat(),
        strategy_id=strategy_id,
        config_hash=loop.config_hash(strat, bt_cfg, _universe()),
    )
    conn.commit()
    now = datetime.fromisoformat(calendar[-1].date().isoformat()) + timedelta(hours=19)
    code, report = loop.run_signals(  # attaches llm scores itself (ranking-only use)
        conn, universe=_universe(), bt_cfg=bt_cfg, strategy_cfg=strat,
        now=now, send_fn=None,
    )
    assert code == 0, report.as_text()

    paper_trades = conn.execute(
        "SELECT t.trade_date, i.ticker, t.side, t.qty, t.price"
        " FROM trades t JOIN instruments i ON i.id = t.instrument_id"
        " WHERE t.user_id = ? ORDER BY t.id", (user_id,)).fetchall()
    engine_trades = [
        (d["fill_date"], d["ticker"], "BUY" if d["action"] == "ENTER" else "SELL",
         d["qty"], d["price"]) for d in result.decisions]
    assert len(paper_trades) == len(engine_trades)
    for got, want in zip(paper_trades, engine_trades):
        assert (got["trade_date"], got["ticker"], got["side"], got["qty"]) == want[:4]
        assert got["price"] == pytest.approx(want[4], abs=1e-12)

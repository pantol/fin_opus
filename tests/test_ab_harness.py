"""A/B harness + engine llm_score injection tests — fully offline & deterministic.

Covers:
  * point-in-time injection of llm_score into the engine snapshot (no look-ahead),
  * a positive llm_score PERMITS entries the baseline also takes (LLM as INPUT),
  * a negative llm_score BLOCKS entries (LLM can veto, never sizes money),
  * the A/B harness runs both variants on the same OOS window vs WIG20TR and
    reports an honest gate verdict.
"""
from __future__ import annotations

import pandas as pd

from app import config as cfg
from app.backtest import ab_harness, engine
from app.db import connect, init_db
from app.ingestion import stooq
from tests.conftest import make_stooq_csv, synthetic_series


def _build_db():
    conn = connect(":memory:")
    init_db(conn)

    def ing(t, rows, **kw):
        iid = stooq.upsert_instrument(conn, {"ticker": t, "name": t, **kw},
                                      is_index=kw.get("is_index", False))
        stooq.store_bars(conn, iid, stooq.parse_csv(make_stooq_csv(rows)))
        return iid

    ing("wig20tr", synthetic_series(n=900, base=2000, drift=0.0005), is_index=True)
    ing("aaa", synthetic_series(n=900, base=100, drift=0.0009), sector="tech",
        listed_from="2015-01-01")
    ing("bbb", synthetic_series(n=900, base=50, drift=0.0006), sector="banking",
        listed_from="2015-01-01")
    conn.commit()
    return conn


def _universe():
    return {
        "benchmark": {"ticker": "wig20tr", "is_index": True},
        "indices": [],
        "instruments": [{"ticker": "aaa", "sector": "tech"},
                        {"ticker": "bbb", "sector": "banking"}],
    }


def test_engine_injects_pit_llm_score_no_lookahead():
    conn = _build_db()
    insts, bench = engine.load_instruments(conn, _universe(), "wig20tr")
    inst = insts[0]
    dates = inst.features.index
    mid = dates[len(dates) // 2]
    # A score only becomes available at `mid`.
    inst.llm_scores = pd.Series([1.0], index=[mid])

    before = mid - pd.Timedelta(days=10)
    assert engine._llm_score_on(inst, before) is None      # not yet available
    assert engine._llm_score_on(inst, mid) == 1.0          # available on the day
    assert engine._llm_score_on(inst, dates[-1]) == 1.0    # carried forward


def test_negative_llm_score_blocks_entries_positive_permits():
    conn = _build_db()
    insts, bench = engine.load_instruments(conn, _universe(), "wig20tr")
    bt = cfg.load_backtest_config()
    llm_cfg = cfg.load_strategy("trend_momentum_llm")

    start = insts[0].features.index[0]

    # All-negative scores -> the llm_score gate fails -> no ENTER decisions.
    neg = [engine.Instrument(
        instrument_id=i.instrument_id, ticker=i.ticker, sector=i.sector,
        listed_from=i.listed_from, delisted_on=i.delisted_on, prices=i.prices,
        features=i.features,
        llm_scores=pd.Series([-1.0], index=[start]),
    ) for i in insts]
    res_neg = engine.run_walk_forward(neg, bench, llm_cfg, bt)
    assert not any(d["action"] == "ENTER" for d in res_neg.decisions)

    # All-positive scores -> gate passes -> entries occur (same as baseline allows).
    pos = [engine.Instrument(
        instrument_id=i.instrument_id, ticker=i.ticker, sector=i.sector,
        listed_from=i.listed_from, delisted_on=i.delisted_on, prices=i.prices,
        features=i.features,
        llm_scores=pd.Series([1.0], index=[start]),
    ) for i in insts]
    res_pos = engine.run_walk_forward(pos, bench, llm_cfg, bt)
    assert any(d["action"] == "ENTER" for d in res_pos.decisions)


def test_ab_harness_runs_and_reports():
    conn = _build_db()
    # Materialize positive llm_features for both instruments from the first bar.
    insts, _ = engine.load_instruments(conn, _universe(), "wig20tr")
    for inst in insts:
        d0 = inst.features.index[0].date().isoformat()
        conn.execute(
            "INSERT INTO llm_features (instrument_id, as_of_date, llm_score, created_at) VALUES (?,?,?,?)",
            (inst.instrument_id, d0, 1.0, "now"),
        )
    conn.commit()

    report = ab_harness.run_ab(
        conn, _universe(),
        cfg.load_strategy("trend_momentum"),
        cfg.load_strategy("trend_momentum_llm"),
        cfg.load_backtest_config(),
    )
    assert "baseline" in report.as_text().lower()
    assert isinstance(report.improved, bool)
    assert set(report.deltas) >= {"sharpe", "sortino", "max_drawdown"}
    # With permissive positive scores, the LLM variant takes the same entries as
    # baseline, so its metrics should match (delta ~ 0) -- a sanity invariant.
    assert abs(report.deltas["total_return"]) < 1e-9

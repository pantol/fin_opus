"""LLM feature-materialization pipeline tests — offline (injected transport).

Covers: point-in-time filing consumption (no look-ahead), filings marked
processed, llm_score persisted to llm_features, and deterministic score loading
for the backtest.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app.config import load_llm_config
from app.db import connect, init_db
from app.ingestion import filings_db
from app.llm import pipeline
from app.llm.client import LLMClient


def _client(conn, research_content, synthesis_content):
    """Transport returns research JSON first, then synthesis JSON, by role."""
    cfg = load_llm_config()

    def transport(url, headers, body, timeout):
        model = body["model"]
        is_synth = model == cfg["synthesis"]["model"]
        content = synthesis_content if is_synth else research_content
        return {
            "id": "gen",
            "model": model,
            "provider": "OpenAI",
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens_details": {"cached_tokens": 0}},
        }

    return LLMClient(conn, cfg, transport=transport, api_key="k")


def _research_json():
    return json.dumps({
        "sentiment": 0.7, "catalysts": ["contract"], "risks": [],
        "event_type": "contract", "confidence": 0.9,
        "evidence_quote": "signed a contract",
    })


def _synthesis_json(verdict="bullish", conviction=0.8):
    return json.dumps({"verdict": verdict, "conviction": conviction, "rationale": "ok"})


def _setup(conn):
    filings_db.ensure_schema(conn)
    iid = int(conn.execute(
        "INSERT INTO instruments (ticker, name, isin) VALUES ('pko','PKO','PLPKO0000016')"
    ).lastrowid)
    return iid


def _insert_filing(conn, iid, published_at, dedup_key, text="The company signed a contract."):
    item = {
        "source": "test", "issuer_isin": "PLPKO0000016", "issuer_name": "PKO",
        "instrument_id": iid, "espi_ebi_type": "ESPI", "report_number": "1/2024",
        "title": text, "published_at": published_at,
        "fetched_at": datetime.now(timezone.utc).isoformat(), "url": "http://x",
        "full_text": text, "content_hash": dedup_key, "dedup_key": dedup_key,
    }
    filings_db.insert_filing(conn, item)
    conn.commit()


def test_pipeline_persists_score_and_marks_processed():
    conn = connect(":memory:")
    init_db(conn)
    iid = _setup(conn)
    _insert_filing(conn, iid, "2024-05-01T09:00:00+02:00", "a")
    client = _client(conn, _research_json(), _synthesis_json(conviction=0.8))

    out = pipeline.compute_feature_for_date(
        conn, client, instrument_id=iid, ticker="pko",
        as_of_date="2024-05-02", quant_score=0.3,
    )
    assert out["llm_score"] == 0.8
    score = conn.execute(
        "SELECT llm_score FROM llm_features WHERE instrument_id=?", (iid,)
    ).fetchone()["llm_score"]
    assert score == 0.8
    # filing consumed
    processed = conn.execute("SELECT processed FROM filings WHERE dedup_key='a'").fetchone()[0]
    assert processed == 1


def test_pipeline_point_in_time_ignores_future_filing():
    conn = connect(":memory:")
    init_db(conn)
    iid = _setup(conn)
    # Filing published AFTER the decision date must not be consumed.
    _insert_filing(conn, iid, "2024-06-10T09:00:00+02:00", "future")
    client = _client(conn, _research_json(), _synthesis_json())
    out = pipeline.compute_feature_for_date(
        conn, client, instrument_id=iid, ticker="pko",
        as_of_date="2024-05-02", quant_score=None,
    )
    assert out is None  # no in-window filings
    assert conn.execute("SELECT COUNT(*) FROM llm_features").fetchone()[0] == 0
    assert conn.execute("SELECT processed FROM filings WHERE dedup_key='future'").fetchone()[0] == 0


def test_load_llm_scores_returns_pit_series():
    conn = connect(":memory:")
    init_db(conn)
    iid = _setup(conn)
    conn.execute(
        "INSERT INTO llm_features (instrument_id, as_of_date, llm_score, created_at) VALUES (?,?,?,?)",
        (iid, "2024-05-02", 0.8, "now"),
    )
    conn.execute(
        "INSERT INTO llm_features (instrument_id, as_of_date, llm_score, created_at) VALUES (?,?,?,?)",
        (iid, "2024-06-02", -0.5, "now"),
    )
    conn.commit()
    s = pipeline.load_llm_scores(conn, iid)
    assert list(s.values) == [0.8, -0.5]
    assert str(s.index[0].date()) == "2024-05-02"


def test_pipeline_rejected_research_still_marks_processed():
    conn = connect(":memory:")
    init_db(conn)
    iid = _setup(conn)
    _insert_filing(conn, iid, "2024-05-01T09:00:00+02:00", "bad")
    client = _client(conn, "not json", _synthesis_json())  # research malformed
    out = pipeline.compute_feature_for_date(
        conn, client, instrument_id=iid, ticker="pko",
        as_of_date="2024-05-02", quant_score=None,
    )
    assert out is None
    # consumed so we don't loop on a malformed filing forever
    assert conn.execute("SELECT processed FROM filings WHERE dedup_key='bad'").fetchone()[0] == 1

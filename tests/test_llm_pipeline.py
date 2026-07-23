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
from app.llm import synthesis as synthesis_mod
from app.llm.client import LLMClient


def _client(conn, research_content, synthesis_content):
    """Transport returns research JSON first, then synthesis JSON, by role."""
    cfg = load_llm_config()

    def transport(url, headers, body, timeout):
        model = body["model"]
        # Roles may share one model (e.g. flash for both) — discriminate by the
        # system prompt, which always differs between research and synthesis.
        is_synth = body["messages"][0].get("content") == synthesis_mod._SYSTEM
        content = synthesis_content if is_synth else research_content
        return {
            "id": "gen",
            "model": model,
            "provider": "OpenAI",
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens_details": {"cached_tokens": 0}},
        }

    def meta(url, headers, timeout):  # offline provider stub (no network)
        return {"data": {"provider_name": "OpenAI"}}

    return LLMClient(conn, cfg, transport=transport, meta_transport=meta, api_key="k")


def _research_json():
    return json.dumps({
        "sentiment": 0.7, "relevance": "relevant_interesting",
        "catalysts": ["contract"], "risks": [],
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


def test_pipeline_rejected_research_bumps_attempts_not_processed():
    # A malformed filing must NOT be marked processed (no feature exists for it);
    # instead its attempt counter is bumped so it can be retried, then retired.
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
    row = conn.execute(
        "SELECT processed, attempts FROM filings WHERE dedup_key='bad'"
    ).fetchone()
    assert row["processed"] == 0  # NOT hidden; no feature was persisted
    assert row["attempts"] == 1
    # no llm_features row was created for the malformed filing
    assert conn.execute("SELECT COUNT(*) FROM llm_features").fetchone()[0] == 0


def test_pipeline_retires_permanently_malformed_filing_after_max_attempts():
    # After MAX_ATTEMPTS failures the filing is given up on (marked processed)
    # so the pipeline stops re-reading it forever -- still WITHOUT a feature row.
    conn = connect(":memory:")
    init_db(conn)
    iid = _setup(conn)
    _insert_filing(conn, iid, "2024-05-01T09:00:00+02:00", "bad")
    client = _client(conn, "not json", _synthesis_json())  # always malformed
    for _ in range(pipeline.MAX_ATTEMPTS):
        out = pipeline.compute_feature_for_date(
            conn, client, instrument_id=iid, ticker="pko",
            as_of_date="2024-05-02", quant_score=None,
        )
        assert out is None
    row = conn.execute(
        "SELECT processed, attempts FROM filings WHERE dedup_key='bad'"
    ).fetchone()
    assert row["attempts"] == pipeline.MAX_ATTEMPTS
    assert row["processed"] == 1  # retired after exhausting retries
    # a subsequent run no longer sees it (max_attempts filter)
    out = pipeline.compute_feature_for_date(
        conn, client, instrument_id=iid, ticker="pko",
        as_of_date="2024-05-02", quant_score=None,
    )
    assert out is None
    assert conn.execute("SELECT COUNT(*) FROM llm_features").fetchone()[0] == 0


def test_prompt_identity_names_archive_instruments():
    # Curated ticker unchanged — its existing llm_cache entries stay valid.
    assert pipeline.prompt_identity("pko", "PKO BP", "PLPKO0000016") == "pko"
    # Archive-discovered: ticker IS the lowercase ISIN -> human-readable identity.
    assert (pipeline.prompt_identity("plgpw0000017", "GPW", "PLGPW0000017")
            == "GPW (PLGPW0000017)")
    # Missing name or ISIN -> fall back to the ticker (never guess).
    assert (pipeline.prompt_identity("plgpw0000017", None, "PLGPW0000017")
            == "plgpw0000017")
    assert pipeline.prompt_identity("abc", "ABC", None) == "abc"


def _add_instrument(conn, ticker, name, isin=None, is_index=0):
    return int(conn.execute(
        "INSERT INTO instruments (ticker, name, isin, is_index) VALUES (?,?,?,?)",
        (ticker, name, isin, is_index),
    ).lastrowid)


def _add_filing(conn, iid, published_at, dedup, attempts=0, processed=0):
    conn.execute(
        "INSERT INTO filings (source, title, published_at, fetched_at, full_text,"
        " content_hash, dedup_key, instrument_id, processed, attempts)"
        " VALUES ('test','t',?,?,'x',?,?,?,?,?)",
        (published_at, published_at, dedup, dedup, iid, processed, attempts),
    )
    conn.commit()


def test_discover_unprocessed_instruments_is_db_driven_and_point_in_time():
    """Targets come from filings in the DB (ISIN-resolved), not universe.yaml:
    point-in-time cutoff, processed/attempts filters, deterministic oldest-first
    order, and unmappable filings counted rather than silently dropped."""
    conn = connect(":memory:")
    init_db(conn)
    pko = _add_instrument(conn, "pko", "PKO", "PLPKO0000016")
    gpw = _add_instrument(conn, "plgpw0000017", "GPW", "PLGPW0000017")
    xyz = _add_instrument(conn, "plxyz0000019", "XYZ", "PLXYZ0000019")
    idx = _add_instrument(conn, "wig20tr", "WIG20TR", None, is_index=1)

    _add_filing(conn, gpw, "2024-05-01T09:00:00+02:00", "g1")  # oldest backlog
    _add_filing(conn, pko, "2024-05-01T10:00:00+02:00", "p1")
    _add_filing(conn, pko, "2024-05-02T08:00:00+02:00", "p2")
    _add_filing(conn, xyz, "2024-06-10T09:00:00+02:00", "x-future")  # beyond T
    _add_filing(conn, xyz, "2024-05-01T09:30:00+02:00", "x-exhausted",
                attempts=pipeline.MAX_ATTEMPTS)  # retries burned -> excluded
    _add_filing(conn, gpw, "2024-04-01T09:00:00+02:00", "g-done", processed=1)
    _add_filing(conn, None, "2024-05-01T11:00:00+02:00", "orphan")  # no ISIN match
    _add_filing(conn, idx, "2024-05-01T12:00:00+02:00", "on-index")

    targets, n_unmapped = pipeline.discover_unprocessed_instruments(conn, "2024-05-02")
    assert [t["ticker"] for t in targets] == ["plgpw0000017", "pko"]
    assert targets[0]["name"] == "GPW" and targets[0]["isin"] == "PLGPW0000017"
    assert targets[0]["n_filings"] == 1  # the processed g-done row is excluded
    assert targets[1]["n_filings"] == 2
    assert n_unmapped == 2  # the orphan + the index-mapped filing


def test_pipeline_warsaw_boundary_early_morning_filing_counts_for_local_day():
    # A filing at 01:30 CEST on 2024-05-03 is 2024-05-02T23:30Z. With a Warsaw
    # local end-of-day cutoff it belongs to May 3 (its OWN local day), and must
    # NOT leak into the May 2 decision.
    conn = connect(":memory:")
    init_db(conn)
    iid = _setup(conn)
    _insert_filing(conn, iid, "2024-05-03T01:30:00+02:00", "early")
    client = _client(conn, _research_json(), _synthesis_json())

    # Decision date May 2: the early-May-3 filing must be invisible (no leak).
    out_may2 = pipeline.compute_feature_for_date(
        conn, client, instrument_id=iid, ticker="pko",
        as_of_date="2024-05-02", quant_score=None,
    )
    assert out_may2 is None
    assert conn.execute("SELECT processed FROM filings WHERE dedup_key='early'").fetchone()[0] == 0

    # Decision date May 3: now in-window and consumed.
    out_may3 = pipeline.compute_feature_for_date(
        conn, client, instrument_id=iid, ticker="pko",
        as_of_date="2024-05-03", quant_score=0.1,
    )
    assert out_may3 is not None
    assert conn.execute("SELECT processed FROM filings WHERE dedup_key='early'").fetchone()[0] == 1

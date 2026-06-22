"""Synthesis/Judge tests — offline. Verdict mapping, malformed rejection,
point-in-time fundamentals fed into the prompt (no look-ahead)."""
from __future__ import annotations

import json

from app.config import load_llm_config
from app.db import connect, init_db
from app.features import fundamentals as fnd
from app.llm import synthesis as syn
from app.llm.client import LLMClient


class _Recorder:
    """Transport that records the body it was given and returns a fixed verdict."""

    def __init__(self, content):
        self.content = content
        self.bodies = []

    def __call__(self, url, headers, body, timeout):
        self.bodies.append(body)
        return {
            "id": "gen-s",
            "model": "openai/gpt-4o",
            "provider": "OpenAI",
            "choices": [{"message": {"content": self.content}}],
            "usage": {"prompt_tokens_details": {"cached_tokens": 0}},
        }


def _verdict_json(**over):
    base = {"verdict": "bullish", "conviction": 0.8, "rationale": "ok"}
    base.update(over)
    return json.dumps(base)


def _client(transport):
    conn = connect(":memory:")
    init_db(conn)
    return LLMClient(conn, load_llm_config(), transport=transport, api_key="k"), conn


def test_verdict_mapped_to_llm_score():
    assert syn.verdict_to_score("bullish", 0.8) == 0.8
    assert syn.verdict_to_score("bearish", 0.5) == -0.5
    assert syn.verdict_to_score("neutral", 0.9) == 0.0


def test_synthesize_returns_score():
    client, _ = _client(_Recorder(_verdict_json(verdict="bearish", conviction=0.6)))
    out = syn.synthesize(client, "pko", research={"sentiment": -0.5}, quant_score=0.1, fundamentals=None)
    assert out["verdict"] == "bearish"
    assert out["llm_score"] == -0.6


def test_malformed_synthesis_rejected():
    client, _ = _client(_Recorder("nope"))
    assert syn.synthesize(client, "pko", research=None, quant_score=None, fundamentals=None) is None


def test_quant_and_fundamentals_passed_as_context_text():
    rec = _Recorder(_verdict_json())
    client, _ = _client(rec)
    syn.synthesize(
        client, "pko",
        research={"sentiment": 0.3},
        quant_score=0.4242,
        fundamentals={"period": "2023Q4", "pe": 11.5, "roe": 0.18},
    )
    user_msg = rec.bodies[0]["messages"][-1]["content"]
    assert "0.4242" in user_msg            # quant score as text
    assert "pe=11.5" in user_msg           # fundamentals as text
    assert "2023Q4" in user_msg


def test_point_in_time_fundamentals_into_synthesis():
    conn = connect(":memory:")
    init_db(conn)
    iid = int(conn.execute(
        "INSERT INTO instruments (ticker, name) VALUES ('pko','PKO')"
    ).lastrowid)
    fnd.upsert_fundamental(conn, instrument_id=iid, as_of_date="2024-03-15", pe=12.0)
    fnd.upsert_fundamental(conn, instrument_id=iid, as_of_date="2024-08-20", pe=10.0)

    # As of 2024-05-01 only the March figure is known.
    snap = fnd.load_fundamentals_asof(conn, iid, "2024-05-01")
    rec = _Recorder(_verdict_json())
    client = LLMClient(conn, load_llm_config(), transport=rec, api_key="k")
    syn.synthesize(client, "pko", research=None, quant_score=None, fundamentals=snap)
    user_msg = rec.bodies[0]["messages"][-1]["content"]
    assert "pe=12.0" in user_msg     # the point-in-time figure
    assert "pe=10.0" not in user_msg  # the future figure must NOT leak in

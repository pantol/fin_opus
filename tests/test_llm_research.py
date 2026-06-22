"""Research Agent tests — offline. Evidence check + malformed rejection."""
from __future__ import annotations

import json

from app.config import load_llm_config
from app.db import connect, init_db
from app.llm import research as research_mod
from app.llm.client import LLMClient


def _make_client(content):
    conn = connect(":memory:")
    init_db(conn)

    def transport(url, headers, body, timeout):
        return {
            "id": "gen-1",
            "model": "openai/gpt-4o-mini",
            "provider": "OpenAI",
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens_details": {"cached_tokens": 0}},
        }

    return LLMClient(conn, load_llm_config(), transport=transport, api_key="k")


def _research_json(**over):
    base = {
        "sentiment": 0.6,
        "catalysts": ["new contract"],
        "risks": [],
        "event_type": "contract",
        "confidence": 0.9,
        "evidence_quote": "signed a major contract",
    }
    base.update(over)
    return json.dumps(base)


FILING = "The company signed a major contract worth 100 mln PLN today."


def test_supported_evidence_keeps_confidence():
    client = _make_client(_research_json())
    out = research_mod.analyze_filing(client, "pko", FILING)
    assert out["evidence_ok"] is True
    assert out["confidence"] == 0.9


def test_unsupported_evidence_lowers_confidence():
    client = _make_client(_research_json(evidence_quote="acquired a rival bank"))
    out = research_mod.analyze_filing(client, "pko", FILING)
    assert out["evidence_ok"] is False
    assert out["confidence"] == 0.9 * research_mod.EVIDENCE_PENALTY


def test_malformed_response_rejected_returns_none():
    client = _make_client("this is not json")
    assert research_mod.analyze_filing(client, "pko", FILING) is None


def test_schema_violation_rejected_returns_none():
    client = _make_client(_research_json(sentiment=5.0))  # out of range
    assert research_mod.analyze_filing(client, "pko", FILING) is None


def test_evidence_match_is_whitespace_and_case_insensitive():
    assert research_mod.evidence_supported("Signed   a MAJOR Contract", FILING) is True
    assert research_mod.evidence_supported("", FILING) is False

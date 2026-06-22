"""Schema-validation tests: valid passes, malformed is REJECTED (never guessed)."""
from __future__ import annotations

import json

import pytest

from app.llm.schemas import (
    LLMValidationError,
    validate_research,
    validate_synthesis,
)


def _research(**over):
    base = {
        "sentiment": 0.5,
        "catalysts": ["new contract"],
        "risks": ["fx"],
        "event_type": "contract",
        "confidence": 0.8,
        "evidence_quote": "signed a contract",
    }
    base.update(over)
    return json.dumps(base)


def _synthesis(**over):
    base = {"verdict": "bullish", "conviction": 0.7, "rationale": "strong momentum"}
    base.update(over)
    return json.dumps(base)


def test_valid_research_passes():
    out = validate_research(_research())
    assert out["sentiment"] == 0.5 and out["event_type"] == "contract"


def test_valid_synthesis_passes():
    out = validate_synthesis(_synthesis())
    assert out["verdict"] == "bullish"


def test_non_json_rejected():
    with pytest.raises(LLMValidationError):
        validate_research("not json at all")


def test_research_out_of_range_sentiment_rejected():
    with pytest.raises(LLMValidationError):
        validate_research(_research(sentiment=2.0))


def test_research_missing_field_rejected():
    payload = json.loads(_research())
    del payload["evidence_quote"]
    with pytest.raises(LLMValidationError):
        validate_research(json.dumps(payload))


def test_research_extra_field_rejected():
    with pytest.raises(LLMValidationError):
        validate_research(_research(price_target=123))


def test_synthesis_bad_verdict_enum_rejected():
    with pytest.raises(LLMValidationError):
        validate_synthesis(_synthesis(verdict="moon"))


def test_synthesis_conviction_out_of_range_rejected():
    with pytest.raises(LLMValidationError):
        validate_synthesis(_synthesis(conviction=1.5))

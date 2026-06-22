"""JSON schemas + validators for LLM outputs. Malformed -> REJECT, never guess.

Two contracts:
  * RESEARCH  (extraction agent): structured read of a filing's TEXT.
  * SYNTHESIS (judge): a verdict that becomes the ONLY input to the deterministic
    risk layer.

Validation is strict: the response must be parseable JSON AND match the schema,
or `validate_*` raises `LLMValidationError`. We do not repair or fabricate
fields (CLAUDE.md: malformed -> reject and log, do not guess).
"""
from __future__ import annotations

import json

from jsonschema import Draft7Validator

RESEARCH_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "sentiment",
        "catalysts",
        "risks",
        "event_type",
        "confidence",
        "evidence_quote",
    ],
    "properties": {
        "sentiment": {"type": "number", "minimum": -1, "maximum": 1},
        "catalysts": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "event_type": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence_quote": {"type": "string"},
    },
}

SYNTHESIS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "conviction", "rationale"],
    "properties": {
        "verdict": {"type": "string", "enum": ["bullish", "neutral", "bearish"]},
        "conviction": {"type": "number", "minimum": 0, "maximum": 1},
        "rationale": {"type": "string"},
    },
}

_RESEARCH_VALIDATOR = Draft7Validator(RESEARCH_SCHEMA)
_SYNTHESIS_VALIDATOR = Draft7Validator(SYNTHESIS_SCHEMA)


class LLMValidationError(ValueError):
    """Raised when an LLM response is not valid JSON or violates its schema."""


def _parse_and_validate(raw: str, validator: Draft7Validator, label: str) -> dict:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise LLMValidationError(f"{label}: response is not valid JSON: {exc}") from exc
    errors = sorted(validator.iter_errors(data), key=lambda e: e.path)
    if errors:
        msgs = "; ".join(e.message for e in errors)
        raise LLMValidationError(f"{label}: schema violation: {msgs}")
    return data


def validate_research(raw: str) -> dict:
    return _parse_and_validate(raw, _RESEARCH_VALIDATOR, "research")


def validate_synthesis(raw: str) -> dict:
    return _parse_and_validate(raw, _SYNTHESIS_VALIDATOR, "synthesis")

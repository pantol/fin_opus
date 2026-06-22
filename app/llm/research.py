"""Research Agent: reads a filing's TEXT and emits validated research JSON.

The LLM reads only TEXT (the filing title + full_text), never numbers. Output is
validated against RESEARCH_SCHEMA; malformed -> rejected and logged (no guessing).

Evidence check (anti-hallucination): the model must return an `evidence_quote`
that appears verbatim in the source filing. If it does not, we LOWER confidence
(do not trust an unsupported claim) and flag it — we never silently accept it.
"""
from __future__ import annotations

import logging

from app.llm.client import LLMClient
from app.llm.schemas import LLMValidationError, validate_research

log = logging.getLogger("llm.research")

_SYSTEM = (
    "You are a financial filings analyst for the Warsaw Stock Exchange (GPW). "
    "Read ONLY the provided filing text. Do not use outside knowledge or numbers "
    "not present in the text. Respond with a single JSON object matching the "
    "required schema. The evidence_quote MUST be copied verbatim from the filing."
)

EVIDENCE_PENALTY = 0.5  # multiply confidence by this when the quote is unsupported


def _normalize(text: str) -> str:
    return " ".join((text or "").split()).lower()


def build_messages(ticker: str, filing_text: str) -> list[dict]:
    user = (
        f"Ticker: {ticker}\n"
        f"Filing text:\n\"\"\"\n{filing_text}\n\"\"\"\n\n"
        "Return JSON with keys: sentiment (-1..1), catalysts (string[]), "
        "risks (string[]), event_type (string), confidence (0..1), "
        "evidence_quote (a short verbatim quote from the filing)."
    )
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user},
    ]


def evidence_supported(evidence_quote: str, source_text: str) -> bool:
    """True if the quote appears (normalized) in the source filing text."""
    quote = _normalize(evidence_quote)
    if not quote:
        return False
    return quote in _normalize(source_text)


def analyze_filing(client: LLMClient, ticker: str, filing_text: str) -> dict | None:
    """Run the research agent on one filing. Returns validated research dict
    (with an added `evidence_ok` flag and possibly reduced confidence), or None
    if the response was malformed (rejected + logged).
    """
    messages = build_messages(ticker, filing_text)
    result = client.complete_json("extraction", messages)
    try:
        research = validate_research(result.content)
    except LLMValidationError as exc:
        log.warning("research rejected for %s (gen=%s): %s", ticker, result.generation_id, exc)
        return None

    ok = evidence_supported(research["evidence_quote"], filing_text)
    research["evidence_ok"] = ok
    if not ok:
        log.warning(
            "research evidence_quote not found in source for %s (gen=%s); lowering confidence",
            ticker,
            result.generation_id,
        )
        research["confidence"] = round(research["confidence"] * EVIDENCE_PENALTY, 6)
    return research

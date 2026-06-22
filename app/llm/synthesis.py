"""Synthesis / Judge: research + quant + fundamentals -> verdict JSON -> llm_score.

TEXT vs NUMBERS boundary (CLAUDE.md): the quant score and the fundamentals are
NUMBERS computed by deterministic code. They are passed into the prompt as
CONTEXT TEXT only; the LLM must NOT recompute them. The LLM returns a verdict
JSON which deterministic code maps to a single numeric `llm_score` in [-1, 1].

That `llm_score` is the ONLY thing the strategy/risk layer ever sees from the
LLM — the LLM is always only an INPUT (rule 1).
"""
from __future__ import annotations

import json
import logging

from app.llm.client import LLMClient
from app.llm.schemas import LLMValidationError, validate_synthesis

log = logging.getLogger("llm.synthesis")

_SYSTEM = (
    "You are an investment judge for the Warsaw Stock Exchange (GPW). You are "
    "given (a) a research summary of recent filings, (b) a precomputed "
    "deterministic quant score, and (c) point-in-time fundamentals. The quant "
    "score and fundamentals are FACTS computed by code — use them as context, do "
    "NOT recompute or override them. Output a single JSON object with verdict "
    "(bullish/neutral/bearish), conviction (0..1), and a short rationale."
)

_VERDICT_SIGN = {"bullish": 1.0, "neutral": 0.0, "bearish": -1.0}


def _fmt_fundamentals(fundamentals: dict | None) -> str:
    if not fundamentals:
        return "Fundamentals: none available as of this date."
    parts = []
    for k in ("period", "pe", "pb", "roe", "debt_equity", "revenue_yoy"):
        if fundamentals.get(k) is not None:
            parts.append(f"{k}={fundamentals[k]}")
    return "Fundamentals (point-in-time): " + (", ".join(parts) if parts else "n/a")


def build_messages(
    ticker: str,
    research: dict | None,
    quant_score: float | None,
    fundamentals: dict | None,
) -> list[dict]:
    research_txt = json.dumps(research, sort_keys=True) if research else "none"
    quant_txt = "n/a" if quant_score is None else f"{quant_score:.4f}"
    user = (
        f"Ticker: {ticker}\n"
        f"Research summary (from filings): {research_txt}\n"
        f"Deterministic quant score (context, do not recompute): {quant_txt}\n"
        f"{_fmt_fundamentals(fundamentals)}\n\n"
        "Return JSON: verdict (bullish|neutral|bearish), conviction (0..1), rationale (string)."
    )
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user},
    ]


def verdict_to_score(verdict: str, conviction: float) -> float:
    """Map a verdict + conviction to llm_score in [-1, 1]. Deterministic."""
    score = _VERDICT_SIGN[verdict] * float(conviction)
    return max(-1.0, min(1.0, score))


def synthesize(
    client: LLMClient,
    ticker: str,
    *,
    research: dict | None,
    quant_score: float | None,
    fundamentals: dict | None,
) -> dict | None:
    """Run the judge. Returns {verdict, conviction, rationale, llm_score} or
    None if the response was malformed (rejected + logged)."""
    messages = build_messages(ticker, research, quant_score, fundamentals)
    result = client.complete_json("synthesis", messages)
    try:
        verdict = validate_synthesis(result.content)
    except LLMValidationError as exc:
        log.warning("synthesis rejected for %s (gen=%s): %s", ticker, result.generation_id, exc)
        return None
    verdict["llm_score"] = verdict_to_score(verdict["verdict"], verdict["conviction"])
    return verdict

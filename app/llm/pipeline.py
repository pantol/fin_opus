"""Materialize point-in-time LLM features from filings (research -> synthesis).

For each instrument and each decision date T, this:
  1. reads UNPROCESSED filings with published_at <= T (point-in-time),
  2. runs the Research Agent on their TEXT (validated JSON),
  3. loads the deterministic quant_score and point-in-time fundamentals
     (as_of_date <= T) -- NUMBERS, passed to synthesis as CONTEXT only,
  4. runs the Synthesis/Judge to get a verdict -> llm_score in [-1, 1],
  5. persists (instrument_id, as_of_date=T, llm_score, research, synthesis) into
     `llm_features`, and marks the consumed filings processed.

The backtest later reads `llm_features` deterministically (no LLM at backtest
time). The LLM is ALWAYS only an INPUT (CLAUDE.md rule 1).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import pandas as pd

from app.features import fundamentals as fnd
from app.ingestion import filings_db
from app.llm import research as research_mod
from app.llm import synthesis as syn

log = logging.getLogger("llm.pipeline")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _filing_text(row) -> str:
    title = row["title"] or ""
    body = row["full_text"] or ""
    return f"{title}\n{body}".strip()


def compute_feature_for_date(
    conn,
    client,
    *,
    instrument_id: int,
    ticker: str,
    as_of_date: str,
    quant_score: float | None,
) -> dict | None:
    """Compute and persist one llm_features row for (instrument, as_of_date).

    `as_of_date` is the decision date T (ISO date). Filings are read with a
    tz-aware end-of-day cutoff so any item published on T (Europe/Warsaw) counts.
    Returns the synthesis dict (with llm_score) or None if nothing to do / rejected.
    """
    cutoff = datetime.fromisoformat(f"{as_of_date}T23:59:59+00:00")
    filings = filings_db.select_filings_asof(
        conn, cutoff, instrument_id=instrument_id, only_unprocessed=True
    )
    if not filings:
        return None

    research_items = []
    consumed_ids = []
    for row in filings:
        consumed_ids.append(row["id"])
        r = research_mod.analyze_filing(client, ticker, _filing_text(row))
        if r is not None:
            research_items.append(r)

    # Even if all research was rejected, mark filings processed so we do not
    # reprocess malformed items forever.
    filings_db.mark_processed(conn, consumed_ids)
    if not research_items:
        return None

    # Aggregate research deterministically: highest-confidence item drives it.
    research = max(research_items, key=lambda x: x.get("confidence", 0.0))

    funds = fnd.load_fundamentals_asof(conn, instrument_id, as_of_date)
    verdict = syn.synthesize(
        client, ticker, research=research, quant_score=quant_score, fundamentals=funds
    )
    if verdict is None:
        return None

    conn.execute(
        """
        INSERT INTO llm_features
            (instrument_id, as_of_date, llm_score, research_json, synthesis_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(instrument_id, as_of_date) DO UPDATE SET
            llm_score=excluded.llm_score, research_json=excluded.research_json,
            synthesis_json=excluded.synthesis_json, created_at=excluded.created_at
        """,
        (
            instrument_id,
            as_of_date,
            verdict["llm_score"],
            json.dumps(research, sort_keys=True),
            json.dumps(verdict, sort_keys=True),
            _now(),
        ),
    )
    conn.commit()
    return verdict


def load_llm_scores(conn, instrument_id: int) -> pd.Series:
    """Load all materialized llm_score values for an instrument as a date-indexed
    Series (ascending). Empty Series if none. The backtest reads this
    deterministically and applies its own point-in-time `date <= T` cut.
    """
    rows = conn.execute(
        "SELECT as_of_date, llm_score FROM llm_features WHERE instrument_id = ? ORDER BY as_of_date ASC",
        (instrument_id,),
    ).fetchall()
    if not rows:
        return pd.Series(dtype=float)
    idx = pd.to_datetime([r["as_of_date"] for r in rows])
    return pd.Series([r["llm_score"] for r in rows], index=idx, dtype=float)

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
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from app.features import fundamentals as fnd
from app.ingestion import filings_db
from app.llm import research as research_mod
from app.llm import synthesis as syn

log = logging.getLogger("llm.pipeline")

WARSAW = ZoneInfo("Europe/Warsaw")

# A filing that fails research/synthesis this many times is given up on (marked
# processed) so the pipeline does not retry a permanently malformed item forever.
MAX_ATTEMPTS = 3


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

    `as_of_date` is the decision date T (ISO date). Filings are read with a cutoff
    of end-of-day T in Europe/Warsaw (the exchange's local day), so an item
    published just after midnight CEST counts for its OWN local day, not the
    previous UTC day. No look-ahead.

    Filings are marked processed ONLY after a feature is successfully persisted.
    A filing whose research/synthesis is rejected gets its attempt counter bumped
    (and is skipped once it exceeds MAX_ATTEMPTS) rather than being silently
    marked processed with no feature row.

    Returns the synthesis dict (with llm_score) or None if nothing to do / rejected.
    """
    cutoff = datetime.combine(datetime.fromisoformat(as_of_date).date(),
                              time(23, 59, 59), tzinfo=WARSAW)
    filings = filings_db.select_filings_asof(
        conn, cutoff, instrument_id=instrument_id, only_unprocessed=True,
        max_attempts=MAX_ATTEMPTS,
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

    if not research_items:
        # Nothing usable this run; record the attempt so a permanently malformed
        # filing is eventually given up on, but do NOT mark it processed (no
        # feature exists for it yet).
        filings_db.bump_attempts(conn, consumed_ids)
        _giveup_exhausted(conn, consumed_ids)
        return None

    # Aggregate research deterministically: highest-confidence item drives it.
    research = max(research_items, key=lambda x: x.get("confidence", 0.0))

    funds = fnd.load_fundamentals_asof(conn, instrument_id, as_of_date)
    verdict = syn.synthesize(
        client, ticker, research=research, quant_score=quant_score, fundamentals=funds
    )
    if verdict is None:
        filings_db.bump_attempts(conn, consumed_ids)
        _giveup_exhausted(conn, consumed_ids)
        return None

    conn.execute(
        """
        INSERT INTO llm_features
            (instrument_id, as_of_date, llm_score, relevance, research_json,
             synthesis_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(instrument_id, as_of_date) DO UPDATE SET
            llm_score=excluded.llm_score, relevance=excluded.relevance,
            research_json=excluded.research_json,
            synthesis_json=excluded.synthesis_json, created_at=excluded.created_at
        """,
        (
            instrument_id,
            as_of_date,
            verdict["llm_score"],
            research.get("relevance"),
            json.dumps(research, sort_keys=True),
            json.dumps(verdict, sort_keys=True),
            _now(),
        ),
    )
    conn.commit()
    # A feature now exists for these filings -> safe to mark them consumed.
    filings_db.mark_processed(conn, consumed_ids)
    return verdict


def _giveup_exhausted(conn, filing_ids) -> int:
    """Retire filings whose attempt counter has reached MAX_ATTEMPTS.

    A permanently malformed filing would otherwise be re-read on every run
    (it never produces a feature, so it is never marked processed). Once it
    has burned MAX_ATTEMPTS, mark it processed so the pipeline stops retrying
    it. No `llm_features` row is created -- the absence is intentional and the
    `attempts` column records why.
    """
    ids = [int(i) for i in filing_ids]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    exhausted = [
        r["id"]
        for r in conn.execute(
            f"SELECT id FROM filings WHERE id IN ({placeholders}) AND attempts >= ?",
            (*ids, MAX_ATTEMPTS),
        ).fetchall()
    ]
    if exhausted:
        filings_db.mark_processed(conn, exhausted)
    return len(exhausted)


# Deterministic numeric encoding of the discrete relevance label — the strategy
# rule engine consumes numbers only. The mapping is code, never the LLM.
RELEVANCE_TO_SCORE = {
    "relevant_interesting": 1.0,
    "relevant_uninteresting": 0.0,
    "irrelevant": -1.0,
}


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


def load_llm_relevance(conn, instrument_id: int) -> pd.Series:
    """Numeric llm_relevance series (RELEVANCE_TO_SCORE encoding), date-indexed.

    Rows without a relevance label (pre-Pack-D materializations) are skipped —
    a missing feature fails a strategy condition rather than being guessed.
    """
    rows = conn.execute(
        "SELECT as_of_date, relevance FROM llm_features"
        " WHERE instrument_id = ? AND relevance IS NOT NULL ORDER BY as_of_date ASC",
        (instrument_id,),
    ).fetchall()
    encoded = [(r["as_of_date"], RELEVANCE_TO_SCORE.get(r["relevance"]))
               for r in rows if r["relevance"] in RELEVANCE_TO_SCORE]
    if not encoded:
        return pd.Series(dtype=float)
    idx = pd.to_datetime([d for d, _ in encoded])
    return pd.Series([v for _, v in encoded], index=idx, dtype=float)

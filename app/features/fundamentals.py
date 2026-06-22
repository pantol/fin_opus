"""Minimal point-in-time fundamentals seam.

NUMBERS are deterministic code's job (CLAUDE.md): this module stores and reads
fundamental figures with an `as_of_date` = the date the figure became publicly
available (report publication), NOT the fiscal period it describes. The LLM
later receives the latest as-of snapshot as CONTEXT TEXT only and must never
recompute it.

This is a SEAM, not a data source: figures are loaded from a CSV / inserted by a
caller. No scraping/sourcing here (out of Phase-2 scope).
"""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

FIELDS = ("pe", "pb", "roe", "debt_equity", "revenue_yoy")


def upsert_fundamental(
    conn: sqlite3.Connection,
    *,
    instrument_id: int,
    as_of_date: str,
    period: str | None = None,
    pe: float | None = None,
    pb: float | None = None,
    roe: float | None = None,
    debt_equity: float | None = None,
    revenue_yoy: float | None = None,
) -> None:
    """Insert/replace one fundamentals snapshot keyed by (instrument, as_of_date)."""
    conn.execute(
        """
        INSERT INTO fundamentals
            (instrument_id, as_of_date, period, pe, pb, roe, debt_equity, revenue_yoy)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(instrument_id, as_of_date) DO UPDATE SET
            period=excluded.period, pe=excluded.pe, pb=excluded.pb, roe=excluded.roe,
            debt_equity=excluded.debt_equity, revenue_yoy=excluded.revenue_yoy
        """,
        (instrument_id, as_of_date, period, pe, pb, roe, debt_equity, revenue_yoy),
    )
    conn.commit()


def load_fundamentals_asof(
    conn: sqlite3.Connection, instrument_id: int, as_of: str
) -> dict | None:
    """Return the LATEST fundamentals snapshot with `as_of_date <= as_of`.

    Point-in-time: a snapshot published after the decision date is invisible, so
    no look-ahead. Returns None if nothing is available yet.
    """
    row = conn.execute(
        """
        SELECT as_of_date, period, pe, pb, roe, debt_equity, revenue_yoy
        FROM fundamentals
        WHERE instrument_id = ? AND as_of_date <= ?
        ORDER BY as_of_date DESC
        LIMIT 1
        """,
        (instrument_id, as_of),
    ).fetchone()
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def load_fundamentals_csv(conn: sqlite3.Connection, path: str | Path, ticker_to_id: dict) -> int:
    """Load fundamentals from a CSV with columns:
    ticker, as_of_date, period, pe, pb, roe, debt_equity, revenue_yoy.

    `ticker_to_id` maps ticker -> instruments(id). Rows with unknown tickers are
    skipped. Returns the number of rows loaded.
    """
    n = 0
    with open(path, "r", encoding="utf-8", newline="") as fh:
        for rec in csv.DictReader(fh):
            iid = ticker_to_id.get(rec["ticker"].strip().lower())
            if iid is None:
                continue
            kwargs = {f: (float(rec[f]) if rec.get(f) else None) for f in FIELDS}
            upsert_fundamental(
                conn,
                instrument_id=iid,
                as_of_date=rec["as_of_date"].strip(),
                period=(rec.get("period") or None),
                **kwargs,
            )
            n += 1
    return n

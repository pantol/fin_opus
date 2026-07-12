"""Price-row provenance: keep synthetic demo bars and real market bars apart.

Every `prices` row carries `source` ('gpw' | 'stooq' | 'demo'). Demo bars are
deterministic fakes for the REAL universe tickers, so once they sit in a
database they are indistinguishable from market data by value alone — and the
incremental ingest would silently extend fake history with real bars. The
guard here makes that impossible: a database holds either demo data or real
data, never both. 'gpw' and 'stooq' may coexist (both are real GPW prices;
Stooq is the documented fallback source).

This module is the single owner of the source vocabulary: the schema CHECK in
app.db is derived from VALID_SOURCES, and ingest paths tag rows with the named
constants below.
"""
from __future__ import annotations

import sqlite3

GPW_SOURCE = "gpw"
STOOQ_SOURCE = "stooq"
DEMO_SOURCE = "demo"
REAL_SOURCES = (GPW_SOURCE, STOOQ_SOURCE)
VALID_SOURCES = (*REAL_SOURCES, DEMO_SOURCE)


class DataMixingError(RuntimeError):
    """Raised when an ingest would mix demo and real price rows in one DB."""


def _db_path(conn) -> str:
    """Best-effort path of the main database, for error messages only."""
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        return row[2] or ":memory:"
    except Exception:  # noqa: BLE001 - cosmetic; never mask the real error
        return "<unknown>"


def demo_rows_present(conn) -> bool:
    """True if any demo price row exists (indexed existence probe)."""
    return conn.execute(
        "SELECT 1 FROM prices WHERE source = ? LIMIT 1", (DEMO_SOURCE,)
    ).fetchone() is not None


def real_rows_present(conn) -> bool:
    """True if any non-demo price row exists (indexed existence probe)."""
    return conn.execute(
        "SELECT 1 FROM prices WHERE source <> ? LIMIT 1", (DEMO_SOURCE,)
    ).fetchone() is not None


def stored_sources(conn) -> set[str]:
    """Distinct `source` values in prices. Full scan — error/report paths only."""
    rows = conn.execute("SELECT DISTINCT source FROM prices").fetchall()
    return {r[0] for r in rows}


def assert_no_mixing(conn, source: str) -> None:
    """Refuse an ingest that would mix demo and real rows in one database.

    Takes the database write lock (BEGIN IMMEDIATE) BEFORE checking, so two
    concurrent ingests cannot both pass the check on an empty table and then
    interleave demo and real writes: the second writer blocks here until the
    first one's initial commit makes its rows visible, and is then refused.
    The lock is released by the caller's first commit (or by rollback/close on
    a refusal, which leaves the database untouched). Safe to call again inside
    an already-guarded transaction (re-checks without re-locking).

    Raises DataMixingError with an actionable message.
    """
    if source not in VALID_SOURCES:
        raise ValueError(f"Unknown price source {source!r}; expected one of {VALID_SOURCES}")
    if not conn.in_transaction:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise DataMixingError(
                f"Cannot verify price provenance: database '{_db_path(conn)}' is "
                f"locked by another writer ({exc}). Retry after it finishes."
            ) from exc
    path = _db_path(conn)
    if source == DEMO_SOURCE:
        if real_rows_present(conn):
            real = sorted(stored_sources(conn) - {DEMO_SOURCE})
            raise DataMixingError(
                f"Refusing to write DEMO data: database '{path}' already holds real "
                f"price rows (source: {', '.join(real)}). Demo bars are synthetic "
                "fakes for the same tickers and must never share a database with "
                "real history. Use a separate database for demo runs, e.g. "
                "`make ingest-offline` (which targets data/demo.db) or "
                "`python -m app.cli --db data/demo.db ingest --offline`."
            )
    elif demo_rows_present(conn):
        raise DataMixingError(
            f"Refusing to ingest real data: database '{path}' holds DEMO price rows "
            "(deterministic synthetic bars, NOT real prices). Real bars must never "
            "extend demo history. Either keep demo data in its own database "
            "(`--db data/demo.db`) or purge it first: "
            "`python -m app.cli purge-demo`."
        )


def last_real_bar_date(conn) -> str | None:
    """MAX(date) over raw NON-demo bars, or None if there are none.

    The incremental ingest resumes from this date. Demo rows are excluded so
    synthetic history can never anchor (and thereby get extended by) a real
    backfill. One indexed MAX per real source (idx_prices_source) instead of a
    full-table scan.
    """
    dates = [
        conn.execute(
            "SELECT MAX(date) FROM prices WHERE source = ? AND adjusted = 0",
            (src,),
        ).fetchone()[0]
        for src in REAL_SOURCES
    ]
    known = [d for d in dates if d is not None]
    return max(known) if known else None

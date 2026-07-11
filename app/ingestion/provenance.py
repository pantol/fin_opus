"""Price-row provenance: keep synthetic demo bars and real market bars apart.

Every `prices` row carries `source` ('gpw' | 'stooq' | 'demo'). Demo bars are
deterministic fakes for the REAL universe tickers, so once they sit in a
database they are indistinguishable from market data by value alone — and the
incremental ingest would silently extend fake history with real bars. The
guard here makes that impossible: a database holds either demo data or real
data, never both. 'gpw' and 'stooq' may coexist (both are real GPW prices;
Stooq is the documented fallback source).
"""
from __future__ import annotations

DEMO_SOURCE = "demo"
REAL_SOURCES = ("gpw", "stooq")
VALID_SOURCES = REAL_SOURCES + (DEMO_SOURCE,)


class DataMixingError(RuntimeError):
    """Raised when an ingest would mix demo and real price rows in one DB."""


def _db_path(conn) -> str:
    """Best-effort path of the main database, for error messages only."""
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        return row[2] or ":memory:"
    except Exception:  # noqa: BLE001 - cosmetic; never mask the real error
        return "<unknown>"


def stored_sources(conn) -> set[str]:
    """Distinct `source` values currently present in the prices table."""
    rows = conn.execute("SELECT DISTINCT source FROM prices").fetchall()
    return {r[0] for r in rows}


def assert_no_mixing(conn, source: str) -> None:
    """Refuse an ingest that would mix demo and real rows in one database.

    Runs BEFORE any row is written, so a refused ingest leaves the database
    untouched. Raises DataMixingError with an actionable message.
    """
    if source not in VALID_SOURCES:
        raise ValueError(f"Unknown price source {source!r}; expected one of {VALID_SOURCES}")
    present = stored_sources(conn)
    path = _db_path(conn)
    if source == DEMO_SOURCE:
        real = sorted(present - {DEMO_SOURCE})
        if real:
            raise DataMixingError(
                f"Refusing to write DEMO data: database '{path}' already holds real "
                f"price rows (source: {', '.join(real)}). Demo bars are synthetic "
                "fakes for the same tickers and must never share a database with "
                "real history. Use a separate database for demo runs, e.g. "
                "`python -m app.cli --db data/demo.db ingest --offline`."
            )
    elif DEMO_SOURCE in present:
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
    backfill — even on a database that predates the mixing guard.
    """
    row = conn.execute(
        "SELECT MAX(date) FROM prices WHERE adjusted = 0 AND source <> ?",
        (DEMO_SOURCE,),
    ).fetchone()
    return row[0] if row else None

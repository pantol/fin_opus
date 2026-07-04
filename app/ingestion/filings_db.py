"""Storage layer for company filings / news items (the `filings` table).

The collector OWNS this table and creates it itself (idempotent migration), so
it can run on a VPS before the rest of the app exists. Schema uses clean,
portable types (no SQLite-only hacks) so migration to Postgres/TimescaleDB is
trivial.

APPEND-ONLY guarantee (point-in-time integrity): rows are inserted with
`ON CONFLICT(dedup_key) DO NOTHING`. An existing row is NEVER updated, so
`published_at` / `fetched_at` of a stored filing can never change. Cross-source
duplicates are resolved BEFORE insert (earliest published_at wins), so we never
overwrite a timestamp to enforce that rule.

ZERO LLM here — pure SQL plumbing.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

# Owned by the collector; created on demand. `instrument_id` references
# instruments(id) when that table exists, but is nullable and resolvable later.
FILINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS filings (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,            -- feed name from config
    issuer_isin   TEXT,                     -- ISO 6166 code (nullable)
    issuer_name   TEXT,
    instrument_id INTEGER,                  -- resolved from instruments by ISIN; nullable
    espi_ebi_type TEXT,                     -- ESPI / EBI / NULL
    report_number TEXT,                     -- e.g. "12/2024"
    title         TEXT NOT NULL,
    published_at  TEXT NOT NULL,            -- POINT-IN-TIME ANCHOR; tz-aware ISO (from feed pubDate, Europe/Warsaw)
    fetched_at    TEXT NOT NULL,            -- tz-aware ISO UTC; when this run first saw the item
    url           TEXT,
    full_text     TEXT,
    content_hash  TEXT NOT NULL,            -- sha256 of canonical content
    dedup_key     TEXT NOT NULL,            -- guid/link, fallback content_hash
    processed     INTEGER NOT NULL DEFAULT 0,  -- boolean (0/1); set only after a feature persists
    attempts      INTEGER NOT NULL DEFAULT 0,  -- LLM-pipeline attempt count (retry/giveup tracking)
    UNIQUE (dedup_key)
);
CREATE INDEX IF NOT EXISTS idx_filings_report
    ON filings(issuer_isin, report_number, espi_ebi_type);
CREATE INDEX IF NOT EXISTS idx_filings_published ON filings(published_at);
CREATE INDEX IF NOT EXISTS idx_filings_processed ON filings(processed);

-- Single-row health beacon so an external monitor can alert on staleness.
CREATE TABLE IF NOT EXISTS collector_health (
    id                   INTEGER PRIMARY KEY CHECK (id = 1),
    last_successful_run  TEXT,
    last_cycle_new_items INTEGER,
    last_error           TEXT
);

-- Golden evaluation set (Pack D): human relevance labels over collected
-- filings. One label per filing (relabeling upserts); joined against eval
-- predictions (and later trade outcomes).
CREATE TABLE IF NOT EXISTS eval_labels (
    filing_id  INTEGER PRIMARY KEY,
    label      TEXT NOT NULL CHECK (label IN
                  ('relevant_interesting', 'relevant_uninteresting', 'irrelevant')),
    labeled_by TEXT NOT NULL,
    labeled_at TEXT NOT NULL,      -- ISO datetime, UTC
    notes      TEXT
);

-- Prompt-regression history: one row per `make eval-llm` run. README rule:
-- no prompt/model change ships if it regresses on the golden set.
CREATE TABLE IF NOT EXISTS eval_runs (
    id              INTEGER PRIMARY KEY,
    created_at      TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,     -- research prompt content fingerprint
    requested_model TEXT NOT NULL,
    served_provider TEXT,
    n_labels        INTEGER NOT NULL,
    n_rejected      INTEGER NOT NULL DEFAULT 0,
    accuracy        REAL NOT NULL,
    f1_json         TEXT NOT NULL      -- per-class F1 (JSON object)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the filings + health tables if absent. Idempotent.

    Additive column migrations run for pre-existing databases (CREATE TABLE IF
    NOT EXISTS does not add new columns to an existing table).
    """
    conn.executescript(FILINGS_SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(filings)").fetchall()}
    if "attempts" not in cols:
        conn.execute("ALTER TABLE filings ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
    conn.commit()


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def filing_exists(conn: sqlite3.Connection, dedup_key: str) -> bool:
    row = conn.execute("SELECT 1 FROM filings WHERE dedup_key = ?", (dedup_key,)).fetchone()
    return row is not None


def find_by_report_key(
    conn: sqlite3.Connection,
    issuer_isin: str | None,
    report_number: str | None,
    espi_ebi_type: str | None,
) -> sqlite3.Row | None:
    """Return an existing filing matching the business key, or None.

    Used for cross-source dedup (same report on GPW + bankier). Only meaningful
    when all three components are present.
    """
    if not (issuer_isin and report_number and espi_ebi_type):
        return None
    return conn.execute(
        """
        SELECT * FROM filings
        WHERE issuer_isin = ? AND report_number = ? AND espi_ebi_type = ?
        ORDER BY published_at ASC LIMIT 1
        """,
        (issuer_isin, report_number, espi_ebi_type),
    ).fetchone()


def resolve_instrument_id(conn: sqlite3.Connection, issuer_isin: str | None) -> int | None:
    """Map an ISIN to instruments(id) if the table + a matching row exist.

    Returns None when the instruments table is absent (collector runs alone) or
    no instrument carries this ISIN — the filing is still stored, resolvable
    later once the instrument is known.
    """
    if not issuer_isin:
        return None
    try:
        row = conn.execute(
            "SELECT id FROM instruments WHERE isin = ? LIMIT 1", (issuer_isin,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None  # no instruments table yet
    return int(row[0]) if row else None


def resolve_instrument_id_by_name(conn: sqlite3.Connection,
                                  issuer: str | None) -> int | None:
    """Map an issuer name (from a filing title prefix) to instruments(id).

    Fallback for feeds that carry no ISIN (bankier/stockwatch/PAP RSS).
    Deterministic and conservative: case-insensitive EXACT match on ticker or
    name only, and None on ambiguity (two different instruments matching) —
    never a fuzzy guess.
    """
    if not issuer:
        return None
    key = issuer.strip().upper()
    if not key:
        return None
    try:
        rows = conn.execute(
            "SELECT DISTINCT id FROM instruments "
            "WHERE UPPER(ticker) = ? OR UPPER(name) = ? LIMIT 2",
            (key, key),
        ).fetchall()
    except sqlite3.OperationalError:
        return None  # no instruments table yet
    return int(rows[0][0]) if len(rows) == 1 else None


def insert_filing(conn: sqlite3.Connection, item: dict) -> bool:
    """Append a filing. Returns True if a NEW row was inserted, False if it was
    a duplicate (existing row left untouched — append-only).

    `item` keys: source, issuer_isin, issuer_name, instrument_id, espi_ebi_type,
    report_number, title, published_at, fetched_at, url, full_text,
    content_hash, dedup_key.
    """
    cur = conn.execute(
        """
        INSERT INTO filings
            (source, issuer_isin, issuer_name, instrument_id, espi_ebi_type,
             report_number, title, published_at, fetched_at, url, full_text,
             content_hash, dedup_key, processed)
        VALUES
            (:source, :issuer_isin, :issuer_name, :instrument_id, :espi_ebi_type,
             :report_number, :title, :published_at, :fetched_at, :url, :full_text,
             :content_hash, :dedup_key, 0)
        ON CONFLICT(dedup_key) DO NOTHING
        """,
        {
            "source": item["source"],
            "issuer_isin": item.get("issuer_isin"),
            "issuer_name": item.get("issuer_name"),
            "instrument_id": item.get("instrument_id"),
            "espi_ebi_type": item.get("espi_ebi_type"),
            "report_number": item.get("report_number"),
            "title": item["title"],
            "published_at": item["published_at"],
            "fetched_at": item.get("fetched_at") or _now_utc_iso(),
            "url": item.get("url"),
            "full_text": item.get("full_text"),
            "content_hash": item["content_hash"],
            "dedup_key": item["dedup_key"],
        },
    )
    return cur.rowcount > 0


def mark_run_success(conn: sqlite3.Connection, new_items: int) -> None:
    conn.execute(
        """
        INSERT INTO collector_health (id, last_successful_run, last_cycle_new_items, last_error)
        VALUES (1, ?, ?, NULL)
        ON CONFLICT(id) DO UPDATE SET
            last_successful_run=excluded.last_successful_run,
            last_cycle_new_items=excluded.last_cycle_new_items,
            last_error=NULL
        """,
        (_now_utc_iso(), new_items),
    )
    conn.commit()


def mark_run_error(conn: sqlite3.Connection, error: str) -> None:
    """Record a cycle-level error WITHOUT touching last_successful_run."""
    conn.execute(
        """
        INSERT INTO collector_health (id, last_successful_run, last_cycle_new_items, last_error)
        VALUES (1, NULL, NULL, ?)
        ON CONFLICT(id) DO UPDATE SET last_error=excluded.last_error
        """,
        (error,),
    )
    conn.commit()


def get_health(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM collector_health WHERE id = 1").fetchone()


def select_filings_asof(
    conn: sqlite3.Connection,
    as_of: str | datetime,
    *,
    instrument_id: int | None = None,
    only_unprocessed: bool = False,
    max_attempts: int | None = None,
) -> list[sqlite3.Row]:
    """Point-in-time read: only filings with published_at <= `as_of`.

    `as_of` is a tz-aware ISO string or datetime. `published_at` is stored
    tz-aware (Europe/Warsaw); comparison is done on PARSED, tz-aware datetimes
    (converted to a common instant), so a differing stored offset can never
    leak a future item past the cutoff. No look-ahead.

    Optional filters: restrict to one `instrument_id`, only `processed = 0`,
    and/or `attempts < max_attempts` (skip items that keep failing the LLM).
    """
    cutoff = as_of if isinstance(as_of, datetime) else datetime.fromisoformat(as_of)
    if cutoff.tzinfo is None:
        raise ValueError("as_of must be timezone-aware (point-in-time safety)")
    sql = "SELECT * FROM filings"
    clauses, params = [], []
    if instrument_id is not None:
        clauses.append("instrument_id = ?")
        params.append(instrument_id)
    if only_unprocessed:
        clauses.append("processed = 0")
    if max_attempts is not None:
        clauses.append("attempts < ?")
        params.append(max_attempts)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY published_at ASC"
    rows = conn.execute(sql, params).fetchall()
    return [r for r in rows if datetime.fromisoformat(r["published_at"]) <= cutoff]


def mark_processed(conn: sqlite3.Connection, filing_ids) -> int:
    """Flag filings as consumed by the LLM pipeline (idempotent).

    Only flips `processed` 0 -> 1; never mutates point-in-time timestamps.
    Call this ONLY after the derived feature has been persisted. Returns the
    number of rows newly flipped.
    """
    ids = [int(i) for i in filing_ids]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    cur = conn.execute(
        f"UPDATE filings SET processed = 1 WHERE id IN ({placeholders}) AND processed = 0",
        ids,
    )
    conn.commit()
    return cur.rowcount


def bump_attempts(conn: sqlite3.Connection, filing_ids) -> int:
    """Increment the LLM-pipeline attempt counter for filings (failure tracking).

    Used when a filing was read but no feature could be persisted (malformed
    research/synthesis), so a bounded number of retries happens before giving up
    WITHOUT marking it processed (which would hide a never-featured filing).
    """
    ids = [int(i) for i in filing_ids]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    cur = conn.execute(
        f"UPDATE filings SET attempts = attempts + 1 WHERE id IN ({placeholders})",
        ids,
    )
    conn.commit()
    return cur.rowcount

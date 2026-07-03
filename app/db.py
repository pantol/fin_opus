"""SQLite access + schema bootstrap.

Schema is written with clean, portable types so migration to
Postgres/TimescaleDB is trivial (no SQLite-only hacks). Every
decisions/positions/trades/equity row carries a `user_id` (multi-tenant seam).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from app.config import DEFAULT_DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS instruments (
    id           INTEGER PRIMARY KEY,
    ticker       TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    market       TEXT NOT NULL DEFAULT 'GPW',
    sector       TEXT,
    isin         TEXT,                          -- ISO 6166 code, used to map filings
    is_index     INTEGER NOT NULL DEFAULT 0,   -- boolean (0/1)
    listed_from  TEXT,                          -- ISO date
    delisted_on  TEXT                           -- ISO date, NULL if active
);

-- as_of_date = the date the row became publicly available (NOT the period it describes).
-- raw vs adjusted prices are stored separately and flagged via `adjusted`.
CREATE TABLE IF NOT EXISTS prices (
    instrument_id INTEGER NOT NULL REFERENCES instruments(id),
    date          TEXT NOT NULL,    -- bar date (ISO)
    as_of_date    TEXT NOT NULL,    -- availability date (ISO)
    open          REAL,
    high          REAL,
    low           REAL,
    close         REAL,
    volume        REAL,
    adjusted      INTEGER NOT NULL DEFAULT 0,   -- boolean (0/1)
    PRIMARY KEY (instrument_id, date, adjusted)
);
CREATE INDEX IF NOT EXISTS idx_prices_asof ON prices(instrument_id, as_of_date, adjusted);

CREATE TABLE IF NOT EXISTS strategies (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    version     INTEGER NOT NULL,
    config_yaml TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (name, version)
);

CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY,
    user_id       TEXT NOT NULL,
    strategy_id   INTEGER REFERENCES strategies(id),
    instrument_id INTEGER NOT NULL REFERENCES instruments(id),
    decision_date TEXT NOT NULL,
    action        TEXT NOT NULL,        -- ENTER / EXIT / HOLD
    features_json TEXT NOT NULL,        -- full feature snapshot (reproducibility)
    params_json   TEXT,                 -- strategy params snapshot
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_user ON decisions(user_id, decision_date);

CREATE TABLE IF NOT EXISTS positions (
    id            INTEGER PRIMARY KEY,
    user_id       TEXT NOT NULL,
    instrument_id INTEGER NOT NULL REFERENCES instruments(id),
    qty           REAL NOT NULL,
    entry_date    TEXT NOT NULL,
    entry_price   REAL NOT NULL,
    stop_price    REAL,
    exit_date     TEXT,
    exit_price    REAL,
    status        TEXT NOT NULL          -- OPEN / CLOSED
);
CREATE INDEX IF NOT EXISTS idx_positions_user ON positions(user_id, status);

CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY,
    user_id       TEXT NOT NULL,
    instrument_id INTEGER NOT NULL REFERENCES instruments(id),
    side          TEXT NOT NULL,         -- BUY / SELL
    qty           REAL NOT NULL,
    price         REAL NOT NULL,         -- fill price (incl. spread+slippage)
    fee           REAL NOT NULL,
    slippage      REAL NOT NULL,
    trade_date    TEXT NOT NULL,
    decision_id   INTEGER REFERENCES decisions(id)
);
CREATE INDEX IF NOT EXISTS idx_trades_user ON trades(user_id, trade_date);

CREATE TABLE IF NOT EXISTS equity_curve (
    user_id  TEXT NOT NULL,
    date     TEXT NOT NULL,
    equity   REAL NOT NULL,
    cash     REAL NOT NULL,
    exposure REAL NOT NULL,
    PRIMARY KEY (user_id, date)
);

-- Point-in-time index membership (e.g. WIG20 revisions). The backtest universe
-- for date T = members as of T — former members stay so history is unbiased.
CREATE TABLE IF NOT EXISTS index_membership (
    index_name    TEXT NOT NULL,
    instrument_id INTEGER NOT NULL REFERENCES instruments(id),
    date_from     TEXT NOT NULL,   -- first session of membership (ISO)
    date_to       TEXT,            -- last session of membership (ISO), NULL = current member
    source        TEXT,
    PRIMARY KEY (index_name, instrument_id, date_from)
);
CREATE INDEX IF NOT EXISTS idx_index_membership ON index_membership(index_name, date_from);

-- Corporate actions keyed by ex-date. Used to (a) derive the adjusted price
-- series and (b) shield stops: a gap explained by an action is not a market move.
CREATE TABLE IF NOT EXISTS corporate_actions (
    instrument_id  INTEGER NOT NULL REFERENCES instruments(id),
    action_type    TEXT NOT NULL CHECK (action_type IN ('dividend', 'split', 'rights_issue')),
    ex_date        TEXT NOT NULL,   -- first session the price trades ex (ISO)
    value_or_ratio REAL NOT NULL,   -- dividend: PLN/share; split: new shares per old; rights_issue: price factor
    source         TEXT,
    PRIMARY KEY (instrument_id, action_type, ex_date)
);
CREATE INDEX IF NOT EXISTS idx_corporate_actions_ex ON corporate_actions(instrument_id, ex_date);

-- Append-only journal of manual deviations from system signals. Rows are only
-- ever inserted (no UPDATE/DELETE path exists in code).
CREATE TABLE IF NOT EXISTS overrides (
    id           INTEGER PRIMARY KEY,
    user_id      TEXT NOT NULL,
    timestamp    TEXT NOT NULL,     -- ISO datetime, UTC
    decision_id  INTEGER REFERENCES decisions(id),
    action_taken TEXT NOT NULL,
    reason       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_overrides_user ON overrides(user_id, timestamp);

-- ---------------------------------------------------------------------------
-- Phase 2: LLM FEATURES layer. The LLM is ALWAYS only an INPUT to the
-- deterministic risk layer (CLAUDE.md rule 1). Nothing here computes money.
-- ---------------------------------------------------------------------------

-- Point-in-time fundamentals. as_of_date = date the figure became public
-- (report publication), NOT the fiscal period it describes. Numbers are
-- computed/sourced by deterministic code; the LLM only receives them as text.
CREATE TABLE IF NOT EXISTS fundamentals (
    instrument_id INTEGER NOT NULL REFERENCES instruments(id),
    as_of_date    TEXT NOT NULL,    -- availability date (ISO)
    period        TEXT,             -- fiscal period label, e.g. 2023Q4
    pe            REAL,
    pb            REAL,
    roe           REAL,
    debt_equity   REAL,
    revenue_yoy   REAL,
    PRIMARY KEY (instrument_id, as_of_date)
);
CREATE INDEX IF NOT EXISTS idx_fundamentals_asof ON fundamentals(instrument_id, as_of_date);

-- Audit trail for every LLM call (reproducibility, CLAUDE.md rule 8):
-- served provider + model + generation id + cache hit are logged on EVERY call.
CREATE TABLE IF NOT EXISTS llm_calls (
    id            INTEGER PRIMARY KEY,
    created_at    TEXT NOT NULL,
    role          TEXT NOT NULL,        -- extraction / synthesis
    requested_model TEXT NOT NULL,
    served_model  TEXT,                 -- model the provider actually served
    served_provider TEXT,              -- provider name from response
    generation_id TEXT,                 -- OpenRouter generation id
    input_hash    TEXT NOT NULL,        -- sha256(model+params+prompt)
    cached_tokens INTEGER,              -- usage.prompt_tokens_details.cached_tokens
    cache_hit     INTEGER NOT NULL DEFAULT 0   -- local cache hit (no network)
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_hash ON llm_calls(input_hash);

-- Local content-addressed cache (cache by input hash). A hit returns stored
-- JSON WITHOUT any network call, keeping backtests deterministic on replay.
CREATE TABLE IF NOT EXISTS llm_cache (
    input_hash  TEXT PRIMARY KEY,
    response_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- Materialized point-in-time LLM features per (instrument, as_of_date). The
-- backtest reads these deterministically; llm_score is the ONLY value injected
-- into the strategy feature snapshot.
CREATE TABLE IF NOT EXISTS llm_features (
    instrument_id INTEGER NOT NULL REFERENCES instruments(id),
    as_of_date    TEXT NOT NULL,    -- decision date the feature is valid for
    llm_score     REAL,             -- [-1, 1], derived from synthesis verdict/conviction
    research_json TEXT,
    synthesis_json TEXT,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (instrument_id, as_of_date)
);
CREATE INDEX IF NOT EXISTS idx_llm_features_asof ON llm_features(instrument_id, as_of_date);
"""


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with FK enforcement and row access by name."""
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables if they do not exist + run lightweight column migrations.

    Column migrations run BEFORE indexes that depend on added columns, so a
    pre-existing database (e.g. an `instruments` table without `isin`) upgrades
    cleanly.
    """
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_instruments_isin ON instruments(isin)")
    conn.commit()


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """True if `column` exists on `table` (and the table exists)."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent additive migrations for pre-existing databases.

    `CREATE TABLE IF NOT EXISTS` does not add new columns to a table that
    already exists, so add them explicitly when missing. Additive only — never
    drops or rewrites existing data.
    """
    if not column_exists(conn, "instruments", "isin"):
        conn.execute("ALTER TABLE instruments ADD COLUMN isin TEXT")

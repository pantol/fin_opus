"""Point-in-time fundamentals seam tests (no look-ahead)."""
from __future__ import annotations

from app.db import connect, init_db
from app.features import fundamentals as fnd


def _seed_instrument(conn, ticker="pko"):
    cur = conn.execute(
        "INSERT INTO instruments (ticker, name) VALUES (?, ?)", (ticker, ticker.upper())
    )
    return int(cur.lastrowid)


def test_latest_snapshot_on_or_before_asof():
    conn = connect(":memory:")
    init_db(conn)
    iid = _seed_instrument(conn)
    fnd.upsert_fundamental(conn, instrument_id=iid, as_of_date="2024-03-15", period="2023Q4", pe=12.0)
    fnd.upsert_fundamental(conn, instrument_id=iid, as_of_date="2024-08-20", period="2024Q2", pe=10.0)

    # Between the two reports -> only the March snapshot is known.
    snap = fnd.load_fundamentals_asof(conn, iid, "2024-05-01")
    assert snap["as_of_date"] == "2024-03-15" and snap["pe"] == 12.0

    # After the second report -> the newer snapshot wins.
    snap2 = fnd.load_fundamentals_asof(conn, iid, "2024-09-01")
    assert snap2["as_of_date"] == "2024-08-20" and snap2["pe"] == 10.0


def test_no_lookahead_before_first_publication():
    conn = connect(":memory:")
    init_db(conn)
    iid = _seed_instrument(conn)
    fnd.upsert_fundamental(conn, instrument_id=iid, as_of_date="2024-03-15", pe=12.0)
    # Decision date before the figure was published -> invisible.
    assert fnd.load_fundamentals_asof(conn, iid, "2024-01-01") is None


def test_upsert_replaces_same_key():
    conn = connect(":memory:")
    init_db(conn)
    iid = _seed_instrument(conn)
    fnd.upsert_fundamental(conn, instrument_id=iid, as_of_date="2024-03-15", pe=12.0)
    fnd.upsert_fundamental(conn, instrument_id=iid, as_of_date="2024-03-15", pe=99.0)
    rows = conn.execute("SELECT COUNT(*) FROM fundamentals").fetchone()[0]
    assert rows == 1
    assert fnd.load_fundamentals_asof(conn, iid, "2024-04-01")["pe"] == 99.0

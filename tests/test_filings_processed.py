"""Tests for the filings `processed` flag + point-in-time unprocessed selection."""
from __future__ import annotations

from datetime import datetime, timezone

from app.db import connect, init_db
from app.ingestion import filings_db


def _insert(conn, *, isin, published_at, title="t", dedup_key=None, instrument_id=None):
    item = {
        "source": "test",
        "issuer_isin": isin,
        "issuer_name": "X",
        "instrument_id": instrument_id,
        "espi_ebi_type": "ESPI",
        "report_number": "1/2024",
        "title": title,
        "published_at": published_at,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "url": "http://x",
        "full_text": "body",
        "content_hash": dedup_key or title,
        "dedup_key": dedup_key or title,
    }
    filings_db.insert_filing(conn, item)
    conn.commit()
    return conn.execute("SELECT id FROM filings WHERE dedup_key=?", (item["dedup_key"],)).fetchone()[0]


def test_mark_processed_is_idempotent_and_counts_only_new_flips():
    conn = connect(":memory:")
    init_db(conn)
    filings_db.ensure_schema(conn)
    a = _insert(conn, isin="PLPKO0000016", published_at="2024-05-01T09:00:00+02:00", dedup_key="a")
    b = _insert(conn, isin="PLPKO0000016", published_at="2024-05-02T09:00:00+02:00", dedup_key="b")

    assert filings_db.mark_processed(conn, [a, b]) == 2
    # second call flips nothing (already processed) -> idempotent
    assert filings_db.mark_processed(conn, [a, b]) == 0
    assert filings_db.mark_processed(conn, []) == 0
    rows = conn.execute("SELECT processed FROM filings ORDER BY id").fetchall()
    assert all(r["processed"] == 1 for r in rows)


def test_only_unprocessed_filter_and_point_in_time():
    conn = connect(":memory:")
    init_db(conn)
    filings_db.ensure_schema(conn)
    a = _insert(conn, isin="X", published_at="2024-05-01T09:00:00+02:00", dedup_key="a")
    _insert(conn, isin="X", published_at="2024-06-01T09:00:00+02:00", dedup_key="b")

    cutoff = datetime(2024, 5, 15, tzinfo=timezone.utc)
    pit = filings_db.select_filings_asof(conn, cutoff, only_unprocessed=True)
    assert len(pit) == 1  # only the May filing is published by the cutoff

    filings_db.mark_processed(conn, [a])
    pit2 = filings_db.select_filings_asof(conn, cutoff, only_unprocessed=True)
    assert pit2 == []  # the only in-window filing is now processed

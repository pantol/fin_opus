"""Tests for the standalone ESPI/EBI + news collector.

All offline: RSS text and full-text are injected (no network). Covers the
Definition of Done: cross-source dedup (earliest published_at), idempotency,
point-in-time / timezone, ISIN mapping, and per-feed resilience.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.ingestion import filings_db, news_collector

WARSAW = ZoneInfo("Europe/Warsaw")


# --- helpers -----------------------------------------------------------------

def _rss(items: list[dict]) -> str:
    """Build a minimal RSS 2.0 document from item dicts.

    item keys: title, link, guid, pubDate (RFC-822-ish), description.
    """
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0"><channel><title>test</title>',
    ]
    for it in items:
        parts.append("<item>")
        parts.append(f"<title>{it['title']}</title>")
        if it.get("link"):
            parts.append(f"<link>{it['link']}</link>")
        if it.get("guid"):
            parts.append(f"<guid>{it['guid']}</guid>")
        if it.get("pubDate"):
            parts.append(f"<pubDate>{it['pubDate']}</pubDate>")
        if it.get("description"):
            parts.append(f"<description>{it['description']}</description>")
        parts.append("</item>")
    parts.append("</channel></rss>")
    return "".join(parts)


def _no_full_text(url, *, user_agent, timeout):
    return ""


def _config(feeds, **overrides):
    base = {
        "db_path": ":memory:",
        "poll_interval_minutes": 10,
        "request_timeout_seconds": 5,
        "fetch_full_text": False,
        "user_agent": "test-agent",
        "feeds": feeds,
    }
    base.update(overrides)
    return base


def _count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0]


# --- 1. cross-source dedup ---------------------------------------------------

def test_cross_source_dedup_keeps_earliest(conn):
    """Same report on two feeds -> stored once, with the EARLIEST published_at."""
    isin = "PLPKO0000016"
    # GPW publishes earlier; bankier mirrors it later.
    gpw = _rss([{
        "title": f"Raport ESPI nr 12/2024 {isin} PKO BP",
        "link": "https://gpw.example/espi-12-2024",
        "guid": "gpw-12-2024",
        "pubDate": "Mon, 06 May 2024 09:00:00 +0200",
    }])
    bankier = _rss([{
        "title": f"PKO BP Raport ESPI 12/2024 {isin}",
        "link": "https://bankier.example/pko-espi-12",
        "guid": "bankier-pko-12",
        "pubDate": "Mon, 06 May 2024 10:30:00 +0200",  # later
    }])

    feeds = {
        "gpw_espi": gpw,
        "bankier_espi_ebi": bankier,
    }

    def fetch_feed(url, *, user_agent, timeout):
        return feeds[url]

    cfg = _config([
        {"name": "gpw_espi", "type": "ESPI", "url": "gpw_espi"},
        {"name": "bankier_espi_ebi", "type": None, "url": "bankier_espi_ebi"},
    ])
    news_collector.run_cycle(conn, cfg, fetch_feed=fetch_feed, fetch_full_text=_no_full_text)

    assert _count(conn) == 1
    row = conn.execute("SELECT * FROM filings").fetchone()
    # earliest published_at (09:00 Warsaw), from the GPW feed
    assert row["source"] == "gpw_espi"
    pub = datetime.fromisoformat(row["published_at"])
    assert pub == datetime(2024, 5, 6, 9, 0, tzinfo=WARSAW)


# --- 2. idempotency ----------------------------------------------------------

def test_idempotent_rerun_no_dupes_no_timestamp_change(conn):
    rss = _rss([{
        "title": "Raport ESPI nr 5/2024 PLPKO0000016",
        "link": "https://gpw.example/espi-5",
        "guid": "g-5",
        "pubDate": "Tue, 07 May 2024 08:00:00 +0200",
    }])

    def fetch_feed(url, *, user_agent, timeout):
        return rss

    cfg = _config([{"name": "gpw_espi", "type": "ESPI", "url": "x"}])

    news_collector.run_cycle(conn, cfg, fetch_feed=fetch_feed, fetch_full_text=_no_full_text)
    first = conn.execute("SELECT published_at, fetched_at FROM filings").fetchone()
    assert _count(conn) == 1

    # run the same snapshot again
    news_collector.run_cycle(conn, cfg, fetch_feed=fetch_feed, fetch_full_text=_no_full_text)
    assert _count(conn) == 1
    second = conn.execute("SELECT published_at, fetched_at FROM filings").fetchone()
    assert second["published_at"] == first["published_at"]
    assert second["fetched_at"] == first["fetched_at"]  # row never rewritten


# --- 3. point-in-time / timezone --------------------------------------------

def test_published_at_from_feed_not_fetch_and_tz_aware(conn):
    rss = _rss([{
        "title": "Raport ESPI nr 1/2020 PLPKO0000016",
        "link": "https://gpw.example/espi-1-2020",
        "guid": "g-1-2020",
        "pubDate": "Thu, 02 Jan 2020 09:15:00 +0100",  # winter -> +01:00 Warsaw
    }])

    def fetch_feed(url, *, user_agent, timeout):
        return rss

    cfg = _config([{"name": "gpw_espi", "type": "ESPI", "url": "x"}])
    news_collector.run_cycle(conn, cfg, fetch_feed=fetch_feed, fetch_full_text=_no_full_text)

    row = conn.execute("SELECT * FROM filings").fetchone()
    pub = datetime.fromisoformat(row["published_at"])
    fetched = datetime.fromisoformat(row["fetched_at"])
    # published_at is the feed instant, tz-aware, NOT the fetch time
    assert pub.tzinfo is not None
    assert pub == datetime(2020, 1, 2, 9, 15, tzinfo=WARSAW)
    assert pub != fetched
    assert pub.year == 2020 and fetched.year >= 2024

    # point-in-time query: a cutoff before publication excludes the item
    before = datetime(2020, 1, 1, 0, 0, tzinfo=WARSAW)
    after = datetime(2020, 1, 3, 0, 0, tzinfo=WARSAW)
    assert filings_db.select_filings_asof(conn, before) == []
    assert len(filings_db.select_filings_asof(conn, after)) == 1


def test_select_asof_never_includes_future_item(conn):
    rss = _rss([
        {"title": "Raport ESPI nr 1/2024 PLPKO0000016", "guid": "a",
         "link": "u1", "pubDate": "Wed, 01 May 2024 09:00:00 +0200"},
        {"title": "Raport ESPI nr 2/2024 PLPEKAO00016", "guid": "b",
         "link": "u2", "pubDate": "Fri, 31 May 2024 09:00:00 +0200"},
    ])

    def fetch_feed(url, *, user_agent, timeout):
        return rss

    cfg = _config([{"name": "gpw_espi", "type": "ESPI", "url": "x"}])
    news_collector.run_cycle(conn, cfg, fetch_feed=fetch_feed, fetch_full_text=_no_full_text)

    cutoff = datetime(2024, 5, 15, 0, 0, tzinfo=WARSAW)
    visible = filings_db.select_filings_asof(conn, cutoff)
    assert len(visible) == 1
    assert all(datetime.fromisoformat(r["published_at"]) <= cutoff for r in visible)


# --- 4. ISIN mapping ---------------------------------------------------------

def test_isin_maps_to_instrument_id_when_known(conn):
    # seed an instrument carrying a known ISIN
    conn.execute(
        "INSERT INTO instruments (ticker, name, isin) VALUES (?, ?, ?)",
        ("pko", "PKO BP", "PLPKO0000016"),
    )
    conn.commit()

    rss = _rss([{
        "title": "Raport ESPI nr 9/2024 PLPKO0000016 PKO BP",
        "link": "u", "guid": "g9", "pubDate": "Mon, 06 May 2024 09:00:00 +0200",
    }])

    def fetch_feed(url, *, user_agent, timeout):
        return rss

    cfg = _config([{"name": "gpw_espi", "type": "ESPI", "url": "x"}])
    news_collector.run_cycle(conn, cfg, fetch_feed=fetch_feed, fetch_full_text=_no_full_text)

    row = conn.execute("SELECT * FROM filings").fetchone()
    iid = conn.execute("SELECT id FROM instruments WHERE isin='PLPKO0000016'").fetchone()[0]
    assert row["issuer_isin"] == "PLPKO0000016"
    assert row["instrument_id"] == iid


def test_unknown_isin_stored_with_null_instrument_id(conn):
    rss = _rss([{
        "title": "Raport EBI nr 3/2024 PLUNKNOWN001 Some SA",
        "link": "u", "guid": "g3", "pubDate": "Mon, 06 May 2024 09:00:00 +0200",
    }])

    def fetch_feed(url, *, user_agent, timeout):
        return rss

    cfg = _config([{"name": "newconnect", "type": "EBI", "url": "x"}])
    news_collector.run_cycle(conn, cfg, fetch_feed=fetch_feed, fetch_full_text=_no_full_text)

    row = conn.execute("SELECT * FROM filings").fetchone()
    assert row["issuer_isin"] == "PLUNKNOWN001"
    assert row["instrument_id"] is None


# --- 5. resilience -----------------------------------------------------------

def test_one_failing_feed_does_not_stop_others(conn):
    good = _rss([{
        "title": "Raport ESPI nr 7/2024 PLPKO0000016", "link": "u7", "guid": "g7",
        "pubDate": "Mon, 06 May 2024 09:00:00 +0200",
    }])

    def fetch_feed(url, *, user_agent, timeout):
        if url == "bad":
            raise RuntimeError("feed down (500)")
        return good

    cfg = _config([
        {"name": "broken", "type": "ESPI", "url": "bad"},
        {"name": "gpw_espi", "type": "ESPI", "url": "good"},
    ])
    stats = news_collector.run_cycle(conn, cfg, fetch_feed=fetch_feed, fetch_full_text=_no_full_text)

    assert stats.feeds_failed == 1
    assert stats.feeds_polled == 1
    assert _count(conn) == 1  # the healthy feed's item was still stored
    health = filings_db.get_health(conn)
    assert health["last_successful_run"] is not None  # cycle completed


def test_placeholder_url_is_skipped_not_fetched(conn):
    calls = {"n": 0}

    def fetch_feed(url, *, user_agent, timeout):
        calls["n"] += 1
        return _rss([])

    cfg = _config([
        {"name": "gpw_espi", "type": "ESPI", "url": "PLACEHOLDER_GPW_ESPI_RSS_URL"},
    ])
    stats = news_collector.run_cycle(conn, cfg, fetch_feed=fetch_feed, fetch_full_text=_no_full_text)
    assert calls["n"] == 0  # placeholder never fetched
    assert stats.feeds_polled == 0
    assert stats.feeds_failed == 0

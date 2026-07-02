"""Tests for the standalone ESPI/EBI + news collector.

All offline: RSS text and full-text are injected (no network). Covers the
Definition of Done: cross-source dedup (earliest published_at), idempotency,
point-in-time / timezone, ISIN mapping, and per-feed resilience.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.ingestion import collect_news, filings_db, news_collector

WARSAW = ZoneInfo("Europe/Warsaw")
UTC = timezone.utc


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
    # One feed down -> cycle is unhealthy; health records an error and does NOT
    # mark a successful run (so VPS monitoring can alert).
    assert stats.healthy is False
    health = filings_db.get_health(conn)
    assert health["last_successful_run"] is None
    assert health["last_error"] is not None


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
    assert stats.feeds_skipped == 1
    # A feed left as a placeholder makes the cycle unhealthy (so the operator is
    # nudged to paste the real URL) and is recorded as an error, not a success.
    assert stats.healthy is False
    health = filings_db.get_health(conn)
    assert health["last_successful_run"] is None


# --- 6. global earliest-wins regardless of feed config order -----------------

def test_earliest_wins_regardless_of_feed_order(conn):
    """Dedup must keep the earliest published_at even if the LATER source is
    listed FIRST in the config (global sort, not config-order)."""
    isin = "PLPKO0000016"
    early = _rss([{
        "title": f"Raport ESPI nr 20/2024 {isin}", "link": "u-early", "guid": "early",
        "pubDate": "Mon, 06 May 2024 09:00:00 +0200",  # earlier
    }])
    late = _rss([{
        "title": f"Raport ESPI nr 20/2024 {isin}", "link": "u-late", "guid": "late",
        "pubDate": "Mon, 06 May 2024 11:00:00 +0200",  # later
    }])
    feeds = {"early": early, "late": late}

    def fetch_feed(url, *, user_agent, timeout):
        return feeds[url]

    # mirror (later) listed FIRST, primary (earlier) listed SECOND
    cfg = _config([
        {"name": "mirror_late", "type": "ESPI", "url": "late"},
        {"name": "primary_early", "type": "ESPI", "url": "early"},
    ])
    news_collector.run_cycle(conn, cfg, fetch_feed=fetch_feed, fetch_full_text=_no_full_text)

    assert _count(conn) == 1
    row = conn.execute("SELECT * FROM filings").fetchone()
    assert row["source"] == "primary_early"
    assert datetime.fromisoformat(row["published_at"]) == datetime(2024, 5, 6, 9, 0, tzinfo=WARSAW)


# --- 7. run_once exit code reflects health -----------------------------------

def test_run_once_returns_nonzero_when_unhealthy(conn, monkeypatch):
    """A degraded cycle (feed down) must make run_once exit non-zero so VPS
    cron/monitoring detects it even though the process did not crash."""
    good = _rss([{
        "title": "Raport ESPI nr 7/2024 PLPKO0000016", "link": "u7", "guid": "g7",
        "pubDate": "Mon, 06 May 2024 09:00:00 +0200",
    }])

    def fetch_feed(url, *, user_agent, timeout):
        if url == "bad":
            raise RuntimeError("feed down")
        return good

    cfg = _config([
        {"name": "broken", "type": "ESPI", "url": "bad"},
        {"name": "gpw_espi", "type": "ESPI", "url": "good"},
    ])

    # patch connect so run_once uses our in-memory test conn, and inject feeds
    monkeypatch.setattr(collect_news, "connect", lambda path: conn)
    orig_run_cycle = news_collector.run_cycle
    monkeypatch.setattr(
        collect_news.news_collector, "run_cycle",
        lambda c, config, **kw: orig_run_cycle(
            c, config, fetch_feed=fetch_feed, fetch_full_text=_no_full_text
        ),
    )

    rc = collect_news.run_once(cfg)
    assert rc != 0


def test_run_once_returns_zero_when_healthy(conn, monkeypatch):
    good = _rss([{
        "title": "Raport ESPI nr 8/2024 PLPKO0000016", "link": "u8", "guid": "g8",
        "pubDate": "Mon, 06 May 2024 09:00:00 +0200",
    }])

    def fetch_feed(url, *, user_agent, timeout):
        return good

    cfg = _config([{"name": "gpw_espi", "type": "ESPI", "url": "good"}])

    monkeypatch.setattr(collect_news, "connect", lambda path: conn)
    orig_run_cycle = news_collector.run_cycle
    monkeypatch.setattr(
        collect_news.news_collector, "run_cycle",
        lambda c, config, **kw: orig_run_cycle(
            c, config, fetch_feed=fetch_feed, fetch_full_text=_no_full_text
        ),
    )

    rc = collect_news.run_once(cfg)
    assert rc == 0


# --- 8. timestamp parsing across formats -------------------------------------

def test_parse_datetime_rfc_with_numeric_offset():
    dt = news_collector.parse_datetime("Mon, 06 May 2024 09:00:00 +0200")
    assert dt.astimezone(UTC) == datetime(2024, 5, 6, 7, 0, tzinfo=UTC)


def test_parse_datetime_rfc_without_offset_assumes_warsaw():
    # No offset/zone at all -> interpreted as Europe/Warsaw local time.
    # 06 May is CEST (+02:00) -> 09:00 Warsaw == 07:00 UTC.
    dt = news_collector.parse_datetime("Mon, 06 May 2024 09:00:00")
    assert dt.astimezone(UTC) == datetime(2024, 5, 6, 7, 0, tzinfo=UTC)


def test_parse_datetime_iso_naive_assumes_warsaw():
    # Winter date (02 Jan) -> CET (+01:00) -> 09:00 Warsaw == 08:00 UTC.
    dt = news_collector.parse_datetime("2024-01-02T09:00:00")
    assert dt.astimezone(UTC) == datetime(2024, 1, 2, 8, 0, tzinfo=UTC)


def test_parse_datetime_iso_with_offset():
    dt = news_collector.parse_datetime("2024-01-02T09:15:00+01:00")
    assert dt.astimezone(UTC) == datetime(2024, 1, 2, 8, 15, tzinfo=UTC)


def test_parse_datetime_named_cet():
    # CET is +01:00 -> 09:00 CET == 08:00 UTC.
    dt = news_collector.parse_datetime("Tue, 02 Jan 2024 09:00:00 CET")
    assert dt.astimezone(UTC) == datetime(2024, 1, 2, 8, 0, tzinfo=UTC)


def test_parse_datetime_named_cest():
    # CEST is +02:00 -> 09:00 CEST == 07:00 UTC.
    dt = news_collector.parse_datetime("Mon, 06 May 2024 09:00:00 CEST")
    assert dt.astimezone(UTC) == datetime(2024, 5, 6, 7, 0, tzinfo=UTC)


def test_parse_datetime_gmt():
    dt = news_collector.parse_datetime("Mon, 06 May 2024 09:00:00 GMT")
    assert dt.astimezone(UTC) == datetime(2024, 5, 6, 9, 0, tzinfo=UTC)


def test_parse_datetime_naive_space_separated_assumes_warsaw():
    # stockwatch.pl format: naive "YYYY-MM-DD HH:MM" (Warsaw local).
    # 02 Jul is CEST (+02:00) -> 07:54 Warsaw == 05:54 UTC.
    dt = news_collector.parse_datetime("2026-07-02 07:54")
    assert dt.astimezone(UTC) == datetime(2026, 7, 2, 5, 54, tzinfo=UTC)


def test_parse_datetime_tz_override_discards_mislabeled_offset():
    # bankier.pl/gpw.pl label Warsaw wall-clock with "+0100" year-round.
    # In July the true zone is CEST: 07:21 Warsaw == 05:21 UTC.
    raw = "Thu, 2 Jul 2026 07:21:00 +0100"
    with_override = news_collector.parse_datetime(
        raw, tz_override=news_collector.WARSAW)
    assert with_override.astimezone(UTC) == datetime(2026, 7, 2, 5, 21, tzinfo=UTC)
    # Without the override the stated offset is trusted (1h later instant).
    without = news_collector.parse_datetime(raw)
    assert without.astimezone(UTC) == datetime(2026, 7, 2, 6, 21, tzinfo=UTC)


def test_parse_datetime_tz_override_agrees_in_winter():
    # In winter Warsaw IS +0100, so override and stated offset agree.
    raw = "Tue, 02 Jan 2024 09:00:00 +0100"
    with_override = news_collector.parse_datetime(
        raw, tz_override=news_collector.WARSAW)
    assert with_override.astimezone(UTC) == datetime(2024, 1, 2, 8, 0, tzinfo=UTC)


def test_extract_issuer_from_title_prefixes():
    ex = news_collector.extract_issuer
    assert ex("PCC ROKITA S.A.: zawarcie umowy") == "PCC ROKITA"        # bankier
    assert ex("PRYMUS: Transakcje osob blisko zwiazanych") == "PRYMUS"  # stockwatch
    assert ex("QUERCUS TFI S.A. (14/2026) Informacja o wycenie") == "QUERCUS TFI"  # PAP
    assert ex("Apollo Capital ASI S.A.: raport") == "Apollo Capital"
    # Plain news headline without an issuer prefix -> None, never a guess.
    assert ex("Ceny miedzi w Londynie na stabilnym poziomie") is None
    assert ex("") is None and ex(None) is None


def test_resolve_instrument_by_name_exact_and_ambiguous(conn):
    conn.execute("INSERT INTO instruments (ticker, name) VALUES ('prm', 'Prymus')")
    conn.execute("INSERT INTO instruments (ticker, name) VALUES ('abc', 'ABC')")
    conn.execute("INSERT INTO instruments (ticker, name) VALUES ('xyz', 'abc')")
    conn.commit()
    from app.ingestion import filings_db
    iid = conn.execute("SELECT id FROM instruments WHERE ticker='prm'").fetchone()[0]
    assert filings_db.resolve_instrument_id_by_name(conn, "PRYMUS") == iid
    assert filings_db.resolve_instrument_id_by_name(conn, "prymus") == iid
    # Ambiguous (ticker of one row, name of another) -> None, no guessing.
    assert filings_db.resolve_instrument_id_by_name(conn, "ABC") is None
    assert filings_db.resolve_instrument_id_by_name(conn, "nomatch") is None
    assert filings_db.resolve_instrument_id_by_name(conn, None) is None


def test_store_items_maps_instrument_by_title_prefix(conn):
    conn.execute("INSERT INTO instruments (ticker, name) VALUES ('prm', 'Prymus')")
    conn.commit()
    iid = conn.execute("SELECT id FROM instruments WHERE ticker='prm'").fetchone()[0]
    feed = {"name": "stockwatch_espi_ebi", "type": None, "url": "https://x/rss.aspx"}
    rss = _rss([{
        "title": "PRYMUS: Zawarcie znaczacej umowy",
        "link": "https://x/prymus,espi,20260702_075422_0000353478",
        "guid": "sw-map-1",
        "pubDate": "2026-07-02 07:54",
    }])
    news_collector.run_cycle(conn, _config([feed]),
                             fetch_feed=lambda url, *, user_agent, timeout: rss,
                             fetch_full_text=_no_full_text)
    row = conn.execute(
        "SELECT instrument_id, issuer_name FROM filings WHERE dedup_key='sw-map-1'"
    ).fetchone()
    assert row["instrument_id"] == iid  # no ISIN in feed; resolved by name
    assert row["issuer_name"] == "PRYMUS"


def test_detect_type_from_stockwatch_link_slug():
    # stockwatch titles carry no ESPI/EBI marker; the link slug does.
    espi_url = ("https://www.stockwatch.pl/komunikaty-spolek/"
                "prymus,espi,20260702_075422_0000353478")
    ebi_url = ("https://www.stockwatch.pl/komunikaty-spolek/"
               "apollo,ebi,20260701_234425_0000012345")
    assert news_collector.detect_type(None, "PRYMUS: umowa", "", espi_url) == "ESPI"
    assert news_collector.detect_type(None, "APOLLO: raport", "", ebi_url) == "EBI"


def test_feed_timezone_override_applied_end_to_end(conn):
    # A bankier-style feed with the mislabeled +0100 July offset: the stored
    # published_at must reflect Warsaw wall-clock (05:21 UTC), not the offset.
    feed = {
        "name": "bankier_espi_ebi", "type": None,
        "timezone_override": "Europe/Warsaw",
        "url": "https://www.bankier.pl/rss/espi.xml",
    }
    rss = _rss([{
        "title": "SPOLKA S.A.: zawarcie umowy",
        "link": "https://www.bankier.pl/wiadomosc/x",
        "guid": "bank-1",
        "pubDate": "Thu, 2 Jul 2026 07:21:00 +0100",
    }])

    def fetch_feed(url, *, user_agent, timeout):
        return rss

    news_collector.run_cycle(conn, _config([feed]),
                             fetch_feed=fetch_feed, fetch_full_text=_no_full_text)
    row = conn.execute("SELECT published_at FROM filings").fetchone()
    stored = datetime.fromisoformat(row["published_at"])
    assert stored.astimezone(UTC) == datetime(2026, 7, 2, 5, 21, tzinfo=UTC)


def test_stockwatch_style_feed_end_to_end(conn):
    # Naive "YYYY-MM-DD HH:MM" pubDate + type from the link slug.
    feed = {"name": "stockwatch_espi_ebi", "type": None,
            "url": "https://www.stockwatch.pl/komunikaty-spolek/rss.aspx?type=&c=&t="}
    rss = _rss([{
        "title": "PRYMUS: Zawarcie znaczacej umowy",
        "link": ("https://www.stockwatch.pl/komunikaty-spolek/"
                 "prymus,espi,20260702_075422_0000353478"),
        "guid": "sw-1",
        "pubDate": "2026-07-02 07:54",
    }])

    def fetch_feed(url, *, user_agent, timeout):
        return rss

    stats = news_collector.run_cycle(conn, _config([feed]),
                                     fetch_feed=fetch_feed,
                                     fetch_full_text=_no_full_text)
    assert stats.healthy and stats.new_items == 1
    row = conn.execute(
        "SELECT published_at, espi_ebi_type FROM filings").fetchone()
    assert row["espi_ebi_type"] == "ESPI"
    stored = datetime.fromisoformat(row["published_at"])
    assert stored.astimezone(UTC) == datetime(2026, 7, 2, 5, 54, tzinfo=UTC)

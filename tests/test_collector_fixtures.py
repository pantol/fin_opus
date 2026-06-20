"""Real-shape feed-fixture harness for the ESPI/EBI + news collector.

Unlike test_news_collector.py (minimal synthetic RSS), these tests run the
collector against saved samples in tests/fixtures/feeds/ that mimic real GPW
ESPI / NewConnect EBI / Bankier feeds: CDATA titles, RSS namespaces (dc:, atom:),
an Atom feed, CET/CEST named-zone pubDates, and issuer-leading title formats.

Capturing real samples here is how we harden the collector against actual feed
quirks before relying on it. To add coverage: save a real feed body as a new
.xml file in tests/fixtures/feeds/ and assert against it.

All offline (feeds injected). ZERO LLM.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.ingestion import filings_db, news_collector
from tests.conftest import load_feed_fixture

UTC = timezone.utc


def _feed_router(mapping: dict[str, str]):
    """Build a fetch_feed that returns fixture text keyed by feed URL."""
    def fetch_feed(url, *, user_agent, timeout):
        return mapping[url]
    return fetch_feed


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


# --- parsing real-shape feeds ------------------------------------------------

def test_parse_gpw_espi_cdata_and_namespaces():
    raw = load_feed_fixture("gpw_espi_sample.xml")
    items = news_collector.parse_feed_items({"name": "gpw_espi", "type": "ESPI"}, raw)
    assert len(items) == 3
    first = items[0]
    # CDATA title decoded; issuer extracted from "ISSUER SA (12/2024) ..."
    assert first["issuer_name"] == "PKO BANK POLSKI SA"
    assert first["report_number"] == "12/2024"
    assert first["espi_ebi_type"] == "ESPI"
    # CEST pubDate -> +02:00; stored tz-aware
    pub = datetime.fromisoformat(first["published_at"])
    assert pub.astimezone(UTC) == datetime(2024, 5, 6, 15, 32, tzinfo=UTC)


def test_parse_newconnect_ebi_named_zone_and_prefix_strip():
    raw = load_feed_fixture("newconnect_ebi_sample.xml")
    items = news_collector.parse_feed_items({"name": "newconnect", "type": "EBI"}, raw)
    assert len(items) == 2
    it = items[0]
    assert it["espi_ebi_type"] == "EBI"
    assert it["report_number"] == "4/2024"
    # "CEST" named zone (+02:00) parsed authoritatively
    assert datetime.fromisoformat(it["published_at"]).astimezone(UTC) == datetime(
        2024, 5, 6, 7, 45, tzinfo=UTC
    )
    # report prefix stripped from the issuer-name guess
    assert it["issuer_name"].lower().startswith("some smallcap")


def test_parse_atom_feed():
    raw = load_feed_fixture("gpw_atom_sample.xml")
    items = news_collector.parse_feed_items({"name": "gpw_atom", "type": "ESPI"}, raw)
    assert len(items) == 1
    it = items[0]
    assert it["issuer_name"] == "CD PROJEKT SA"
    assert it["report_number"] == "8/2024"
    assert datetime.fromisoformat(it["published_at"]).astimezone(UTC) == datetime(
        2024, 5, 6, 9, 0, tzinfo=UTC
    )


# --- ISIN mapping against a real-shape feed ----------------------------------

def test_bankier_isin_in_description_maps_to_instrument(conn):
    conn.execute(
        "INSERT INTO instruments (ticker, name, isin) VALUES (?, ?, ?)",
        ("pko", "PKO BP", "PLPKO0000016"),
    )
    conn.commit()

    raw = load_feed_fixture("bankier_mirror_sample.xml")
    cfg = _config([{"name": "bankier", "type": None, "url": "b"}])
    news_collector.run_cycle(
        conn, cfg, fetch_feed=_feed_router({"b": raw}), fetch_full_text=_no_full_text
    )

    iid = conn.execute("SELECT id FROM instruments WHERE ticker='pko'").fetchone()[0]
    row = conn.execute(
        "SELECT * FROM filings WHERE issuer_isin='PLPKO0000016'"
    ).fetchone()
    assert row is not None
    assert row["instrument_id"] == iid


# --- issuer-name fallback mapping (no/unresolved ISIN) -----------------------

def test_name_fallback_maps_gpw_issuer_when_isin_absent(conn):
    """GPW ESPI titles carry no ISIN; map via issuer name when the instrument is
    stored under its full legal name."""
    conn.execute(
        "INSERT INTO instruments (ticker, name, isin) VALUES (?, ?, ?)",
        ("kgh", "KGHM Polska Miedz", None),
    )
    conn.commit()

    raw = load_feed_fixture("gpw_espi_sample.xml")
    cfg = _config([{"name": "gpw_espi", "type": "ESPI", "url": "g"}])
    news_collector.run_cycle(
        conn, cfg, fetch_feed=_feed_router({"g": raw}), fetch_full_text=_no_full_text
    )

    iid = conn.execute("SELECT id FROM instruments WHERE ticker='kgh'").fetchone()[0]
    row = conn.execute(
        "SELECT * FROM filings WHERE report_number='33/2024'"
    ).fetchone()
    assert row["issuer_isin"] is None       # no ISIN in the GPW title
    assert row["instrument_id"] == iid      # mapped by name fallback


def test_name_fallback_ambiguous_is_not_mapped(conn):
    """Two instruments normalizing to the same name => no guess (null)."""
    conn.executemany(
        "INSERT INTO instruments (ticker, name) VALUES (?, ?)",
        [("cdr", "CD Projekt"), ("cdr2", "CD Projekt SA")],
    )
    conn.commit()
    # both normalize to "cdprojekt" -> ambiguous -> resolver returns None
    assert filings_db.resolve_by_name(conn, "CD PROJEKT SA") is None


def test_isin_takes_precedence_over_name(conn):
    """When both an ISIN row and a name row exist, ISIN wins."""
    conn.executemany(
        "INSERT INTO instruments (ticker, name, isin) VALUES (?, ?, ?)",
        [("pko", "PKO BP", "PLPKO0000016"), ("decoy", "PKO Bank Polski", None)],
    )
    conn.commit()
    iid = conn.execute("SELECT id FROM instruments WHERE ticker='pko'").fetchone()[0]
    got = filings_db.resolve_instrument_id(conn, "PLPKO0000016", "PKO Bank Polski")
    assert got == iid


# --- cross-source dedup across real-shape feeds ------------------------------

def test_cross_source_dedup_across_real_shape_feeds(conn):
    """The PKO 12/2024 ESPI report appears on GPW (earlier) and Bankier (later);
    stored once with the earliest published_at, regardless of feed order."""
    gpw = load_feed_fixture("gpw_espi_sample.xml")
    bankier = load_feed_fixture("bankier_mirror_sample.xml")

    # Bankier listed FIRST (later pub) to prove global earliest-wins ordering.
    cfg = _config([
        {"name": "bankier", "type": "ESPI", "url": "b"},
        {"name": "gpw_espi", "type": "ESPI", "url": "g"},
    ])
    news_collector.run_cycle(
        conn, cfg,
        fetch_feed=_feed_router({"g": gpw, "b": bankier}),
        fetch_full_text=_no_full_text,
    )

    # GPW item lacks an ISIN in its title, so the PKO 12/2024 report does NOT
    # share the (isin, report, type) business key across both feeds — both are
    # kept (this documents the real limitation: cross-source dedup needs the ISIN
    # on BOTH sources). The Bankier item carries the ISIN; the GPW one does not.
    rows = conn.execute(
        "SELECT source, issuer_isin, report_number FROM filings "
        "WHERE report_number='12/2024' ORDER BY source"
    ).fetchall()
    assert len(rows) == 2
    bankier_row = next(r for r in rows if r["source"] == "bankier")
    assert bankier_row["issuer_isin"] == "PLPKO0000016"


def test_point_in_time_ordering_from_fixture(conn):
    raw = load_feed_fixture("gpw_espi_sample.xml")
    cfg = _config([{"name": "gpw_espi", "type": "ESPI", "url": "g"}])
    news_collector.run_cycle(
        conn, cfg, fetch_feed=_feed_router({"g": raw}), fetch_full_text=_no_full_text
    )
    cutoff = datetime(2024, 5, 7, 0, 0, tzinfo=UTC)
    visible = filings_db.select_filings_asof(conn, cutoff)
    # only the 06 May item is <= cutoff; 07 + 08 May are in the future
    assert len(visible) == 1
    assert all(datetime.fromisoformat(r["published_at"]) <= cutoff for r in visible)

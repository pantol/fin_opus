"""ESPI/EBI + news collector — core cycle logic.

Standalone plumbing that polls RSS feeds, captures each new company filing/news
item at PUBLICATION time (point-in-time anchor), maps it to an issuer by ISIN,
fetches full text, and stores it append-only + idempotently into `filings`.

Point-in-time discipline (see SKILL): `published_at` is taken from the feed's
pubDate (Europe/Warsaw, stored tz-aware) — NEVER from `fetched_at`. The LLM
(Phase 2) only reads what is stored here. ZERO LLM in this module.

Network calls (RSS fetch + full-text fetch) are injectable so tests run offline.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import feedparser
import requests

from app.ingestion import filings_db

logger = logging.getLogger("news_collector")

WARSAW = ZoneInfo("Europe/Warsaw")

# ISIN: 2-letter country code + 9 alphanumerics + 1 check digit.
_ISIN_RE = re.compile(r"\b([A-Z]{2}[A-Z0-9]{9}[0-9])\b")
# Report number like "12/2024" optionally prefixed by ESPI/EBI/raport.
_REPORT_RE = re.compile(r"\b(\d{1,4}\s*/\s*\d{4})\b")
_ESPI_RE = re.compile(r"\bESPI\b", re.IGNORECASE)
_EBI_RE = re.compile(r"\bEBI\b", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")

_PLACEHOLDER_PREFIX = "PLACEHOLDER_"


@dataclass
class CycleStats:
    """Per-cycle summary for structured logging + health."""
    feeds_polled: int = 0
    feeds_failed: int = 0
    items_seen: int = 0
    new_items: int = 0
    errors: list[str] = field(default_factory=list)


# --- network seams (injectable in tests) -------------------------------------

def default_fetch_feed(url: str, *, user_agent: str, timeout: float) -> str:
    """Fetch raw RSS/Atom text. Network call (not used in tests)."""
    resp = requests.get(url, headers={"User-Agent": user_agent}, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def default_fetch_full_text(url: str, *, user_agent: str, timeout: float) -> str:
    """Fetch the filing/article body. Network call (not used in tests).

    Best-effort: strips HTML tags to plain text. Returns "" on any failure so a
    missing body never blocks storing the item (the url + title are enough to
    re-fetch later).
    """
    try:
        resp = requests.get(url, headers={"User-Agent": user_agent}, timeout=timeout)
        resp.raise_for_status()
        return strip_html(resp.text)
    except requests.RequestException as exc:
        logger.warning("full-text fetch failed for %s: %s", url, exc)
        return ""


# --- pure parsing helpers ----------------------------------------------------

def strip_html(html: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", html or "")).strip()


def content_hash(*parts: str | None) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update((p or "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def extract_isin(*texts: str | None) -> str | None:
    for t in texts:
        if not t:
            continue
        m = _ISIN_RE.search(t)
        if m:
            return m.group(1)
    return None


def extract_report_number(*texts: str | None) -> str | None:
    for t in texts:
        if not t:
            continue
        m = _REPORT_RE.search(t)
        if m:
            return re.sub(r"\s*", "", m.group(1))
    return None


def detect_type(default_type: str | None, *texts: str | None) -> str | None:
    """Detect ESPI vs EBI from item text; fall back to the feed's default."""
    blob = " ".join(t for t in texts if t)
    if _ESPI_RE.search(blob):
        return "ESPI"
    if _EBI_RE.search(blob):
        return "EBI"
    return default_type


def parse_published_at(entry) -> str:
    """Return a tz-aware ISO timestamp for the item's publication moment.

    Feeds expose pubDate; feedparser parses it into `published_parsed` (a UTC
    struct_time). We treat a naive feed wall-clock as Europe/Warsaw per the
    Polish sources, but if feedparser already resolved an explicit offset we
    honour that instant. NEVER derived from fetch time.
    """
    # feedparser sets *_parsed to a UTC time.struct_time when an offset/zone is
    # present in the source; use it as the authoritative instant.
    tm = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if tm is not None:
        dt_utc = datetime(*tm[:6], tzinfo=timezone.utc)
        # Express in Warsaw local time (explicit tz) for storage/readability.
        return dt_utc.astimezone(WARSAW).isoformat()
    # Fallback: parse the raw string as a Warsaw-local naive datetime.
    raw = getattr(entry, "published", None) or getattr(entry, "updated", None)
    if raw:
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=WARSAW)
            return dt.astimezone(WARSAW).isoformat()
        except ValueError:
            pass
    raise ValueError("entry has no parseable pubDate (cannot anchor point-in-time)")


def dedup_key_for(entry, fallback_hash: str) -> str:
    """guid/id -> link -> content hash."""
    key = getattr(entry, "id", None) or getattr(entry, "guid", None) or getattr(entry, "link", None)
    return key or fallback_hash


def _entry_text(entry) -> str:
    summary = getattr(entry, "summary", "") or ""
    return strip_html(summary)


# --- one feed --------------------------------------------------------------

def parse_feed_items(feed_cfg: dict, raw_text: str) -> list[dict]:
    """Parse one feed's raw RSS text into candidate filing dicts (no I/O).

    Items are returned sorted by published_at ASCENDING so that, within a cycle,
    the earliest occurrence of a cross-source duplicate is stored first
    (earliest-published wins).
    """
    parsed = feedparser.parse(raw_text)
    items: list[dict] = []
    default_type = feed_cfg.get("type")
    for entry in parsed.entries:
        title = (getattr(entry, "title", "") or "").strip()
        if not title:
            continue
        summary = _entry_text(entry)
        url = getattr(entry, "link", None)
        try:
            published_at = parse_published_at(entry)
        except ValueError as exc:
            logger.warning("skipping item without pubDate in %s: %s", feed_cfg["name"], exc)
            continue
        chash = content_hash(title, summary, url)
        items.append({
            "source": feed_cfg["name"],
            "title": title,
            "summary": summary,
            "url": url,
            "published_at": published_at,
            "espi_ebi_type": detect_type(default_type, title, summary),
            "issuer_isin": extract_isin(title, summary),
            "issuer_name": title,  # best-effort; refined later by the LLM phase
            "report_number": extract_report_number(title, summary),
            "content_hash": chash,
            "dedup_key": dedup_key_for(entry, chash),
        })
    items.sort(key=lambda it: it["published_at"])
    return items


def _is_placeholder(url: str | None) -> bool:
    return (not url) or url.startswith(_PLACEHOLDER_PREFIX)


# --- the cycle -------------------------------------------------------------

def run_cycle(
    conn,
    config: dict,
    *,
    fetch_feed=default_fetch_feed,
    fetch_full_text=default_fetch_full_text,
) -> CycleStats:
    """Run ONE collection cycle over all enabled feeds.

    Reliability: each feed is wrapped in try/except — one feed being down (or a
    placeholder URL) never blocks the others or crashes the cycle. The health
    beacon records success only if the cycle completes; per-feed failures are
    logged and counted but do not abort.
    """
    filings_db.ensure_schema(conn)
    stats = CycleStats()

    user_agent = config.get("user_agent", "gpw-decision-system news collector")
    timeout = float(config.get("request_timeout_seconds", 20))
    want_full_text = bool(config.get("fetch_full_text", True))

    for feed_cfg in config.get("feeds", []):
        if not feed_cfg.get("enabled", True):
            continue
        name = feed_cfg.get("name", "<unnamed>")
        url = feed_cfg.get("url")
        if _is_placeholder(url):
            logger.warning("feed '%s' has a placeholder URL — skipping (paste the real feed URL)", name)
            continue
        try:
            raw = fetch_feed(url, user_agent=user_agent, timeout=timeout)
            items = parse_feed_items(feed_cfg, raw)
            stats.feeds_polled += 1
            stats.items_seen += len(items)
            new_here = _store_items(
                conn, items, want_full_text=want_full_text,
                fetch_full_text=fetch_full_text, user_agent=user_agent, timeout=timeout,
            )
            stats.new_items += new_here
            logger.info("feed '%s': %d items, %d new", name, len(items), new_here)
        except Exception as exc:  # noqa: BLE001 - one bad feed must not kill the cycle
            stats.feeds_failed += 1
            msg = f"feed '{name}' failed: {exc}"
            stats.errors.append(msg)
            logger.error(msg)

    conn.commit()
    filings_db.mark_run_success(conn, stats.new_items)
    logger.info(
        "cycle done: %d feeds polled, %d failed, %d items seen, %d new",
        stats.feeds_polled, stats.feeds_failed, stats.items_seen, stats.new_items,
    )
    return stats


def _store_items(conn, items, *, want_full_text, fetch_full_text, user_agent, timeout) -> int:
    """Resolve, dedup, and append a feed's items. Returns count of NEW rows."""
    new_count = 0
    fetched_at = datetime.now(timezone.utc).isoformat()
    for it in items:
        # Primary idempotency gate: already stored under this dedup key.
        if filings_db.filing_exists(conn, it["dedup_key"]):
            continue
        # Cross-source dedup: same (isin, report_number, type) already stored
        # (earlier published_at) -> skip; never overwrite the earlier row.
        existing = filings_db.find_by_report_key(
            conn, it["issuer_isin"], it["report_number"], it["espi_ebi_type"]
        )
        if existing is not None:
            logger.info(
                "cross-source duplicate for %s %s/%s — keeping earliest published_at",
                it["issuer_isin"], it["espi_ebi_type"], it["report_number"],
            )
            continue

        instrument_id = filings_db.resolve_instrument_id(conn, it["issuer_isin"])
        full_text = ""
        if want_full_text and it.get("url"):
            full_text = fetch_full_text(it["url"], user_agent=user_agent, timeout=timeout)

        inserted = filings_db.insert_filing(conn, {
            "source": it["source"],
            "issuer_isin": it["issuer_isin"],
            "issuer_name": it["issuer_name"],
            "instrument_id": instrument_id,
            "espi_ebi_type": it["espi_ebi_type"],
            "report_number": it["report_number"],
            "title": it["title"],
            "published_at": it["published_at"],
            "fetched_at": fetched_at,
            "url": it.get("url"),
            "full_text": full_text,
            "content_hash": it["content_hash"],
            "dedup_key": it["dedup_key"],
        })
        if inserted:
            new_count += 1
    return new_count

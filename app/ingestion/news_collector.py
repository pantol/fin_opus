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
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import feedparser
import requests

from app.ingestion import filings_db

logger = logging.getLogger("news_collector")

WARSAW = ZoneInfo("Europe/Warsaw")

# Named timezone abbreviations that appear in Polish/EU feeds. feedparser does
# NOT reliably convert these (it leaves CET/CEST wall-clock untouched), so we
# resolve them explicitly to fixed offsets when parsing the raw pubDate string.
_TZ_ABBREV = {
    "UTC": timezone.utc, "GMT": timezone.utc, "Z": timezone.utc,
    "CET": timezone(timedelta(hours=1)),
    "CEST": timezone(timedelta(hours=2)),
}
_TRAILING_ABBREV_RE = re.compile(r"\b(UTC|GMT|CET|CEST)\b\s*$", re.IGNORECASE)

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
    feeds_configured: int = 0     # enabled feeds in config
    feeds_polled: int = 0         # fetched + parsed successfully
    feeds_failed: int = 0         # raised during fetch/parse
    feeds_skipped: int = 0        # placeholder / missing URL
    items_seen: int = 0
    new_items: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        """A cycle is healthy only if every configured feed was polled OK.

        Any failed OR skipped (placeholder) feed makes the cycle unhealthy, so
        VPS monitoring can alert instead of trusting a green `last_successful_run`
        produced by a collector that actually fetched nothing.
        """
        return (
            self.feeds_configured > 0
            and self.feeds_failed == 0
            and self.feeds_skipped == 0
            and self.feeds_polled == self.feeds_configured
        )


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


def parse_datetime(raw: str) -> datetime:
    """Parse a feed pubDate string into a tz-AWARE datetime, normalized to Warsaw.

    Handles, authoritatively (we do NOT trust feedparser's struct_time, which
    leaves CET/CEST wall-clock unconverted):
      - RFC-822 with numeric offset:  "Mon, 06 May 2024 09:00:00 +0200"
      - RFC-822 with GMT/UTC:         "... 09:00:00 GMT"
      - RFC-822 with CET/CEST names:  "... 09:00:00 CET" / "... CEST"
      - RFC-822 with NO offset:       "Mon, 06 May 2024 09:00:00"  -> Warsaw
      - ISO-8601 with offset:         "2024-01-02T09:15:00+01:00"
      - ISO-8601 naive:               "2024-05-06T09:00:00"        -> Warsaw

    A naive timestamp (no offset/zone) is interpreted as Europe/Warsaw local
    time, which is the convention of the Polish sources. Never UTC-by-default.
    """
    s = raw.strip()

    # ISO-8601 first (RFC parser would reject it).
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return _as_warsaw(dt)
    except ValueError:
        pass

    # Named-abbreviation zones the RFC parser can't resolve (CET/CEST) or maps
    # to a naive datetime (GMT handled by parser, but be explicit/robust).
    m = _TRAILING_ABBREV_RE.search(s)
    if m:
        tz = _TZ_ABBREV[m.group(1).upper()]
        base = s[: m.start()].strip()
        dt = parsedate_to_datetime(base)  # naive (offset stripped with the name)
        dt = dt.replace(tzinfo=tz)
        return _as_warsaw(dt)

    # RFC-822 with a numeric offset, or no offset at all.
    dt = parsedate_to_datetime(s)  # raises ValueError/TypeError on garbage
    return _as_warsaw(dt)


def _as_warsaw(dt: datetime) -> datetime:
    """Attach Warsaw tz to a naive datetime; convert aware ones to Warsaw."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=WARSAW)
    return dt.astimezone(WARSAW)


def parse_published_at(entry) -> str:
    """Return a tz-aware ISO timestamp (Warsaw) for the item's publication moment.

    Parsed from the raw feed pubDate/updated string (the point-in-time anchor),
    NEVER derived from fetch time.
    """
    raw = getattr(entry, "published", None) or getattr(entry, "updated", None)
    if raw:
        try:
            return parse_datetime(raw).isoformat()
        except (ValueError, TypeError):
            logger.warning("unparseable pubDate %r", raw)
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

    Each candidate carries `_instant` (a tz-aware UTC datetime parsed from the
    feed pubDate) so the caller can GLOBALLY sort candidates from ALL feeds by
    true publication instant before storing — this is what guarantees
    earliest-published-wins regardless of feed order in the config.
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
            "_instant": datetime.fromisoformat(published_at).astimezone(timezone.utc),
            "espi_ebi_type": detect_type(default_type, title, summary),
            "issuer_isin": extract_isin(title, summary),
            "issuer_name": title,  # best-effort; refined later by the LLM phase
            "report_number": extract_report_number(title, summary),
            "content_hash": chash,
            "dedup_key": dedup_key_for(entry, chash),
        })
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

    Cross-source earliest-wins: candidates from ALL feeds are collected first,
    then sorted GLOBALLY by true publication instant, and only then stored. This
    makes earliest-published win regardless of feed order in the config (a later
    Bankier mirror can no longer beat an earlier GPW primary just because it is
    listed first).

    Reliability: each feed fetch/parse is wrapped in try/except — one feed being
    down never blocks the others or crashes the cycle. The health beacon records
    SUCCESS only for a genuinely healthy cycle (every configured feed polled OK,
    none failed or skipped); otherwise it records an error so VPS monitoring can
    alert. Per-feed failures are logged and counted but do not abort.
    """
    filings_db.ensure_schema(conn)
    stats = CycleStats()

    user_agent = config.get("user_agent", "gpw-decision-system news collector")
    timeout = float(config.get("request_timeout_seconds", 20))
    want_full_text = bool(config.get("fetch_full_text", True))

    # --- 1. gather candidates from every feed (no DB writes yet) ---
    candidates: list[dict] = []
    for feed_cfg in config.get("feeds", []):
        if not feed_cfg.get("enabled", True):
            continue
        stats.feeds_configured += 1
        name = feed_cfg.get("name", "<unnamed>")
        url = feed_cfg.get("url")
        if _is_placeholder(url):
            stats.feeds_skipped += 1
            msg = f"feed '{name}' has a placeholder/missing URL — skipped"
            stats.errors.append(msg)
            logger.warning("%s (paste the real feed URL)", msg)
            continue
        try:
            raw = fetch_feed(url, user_agent=user_agent, timeout=timeout)
            items = parse_feed_items(feed_cfg, raw)
            stats.feeds_polled += 1
            stats.items_seen += len(items)
            candidates.extend(items)
            logger.info("feed '%s': %d items", name, len(items))
        except Exception as exc:  # noqa: BLE001 - one bad feed must not kill the cycle
            stats.feeds_failed += 1
            msg = f"feed '{name}' failed: {exc}"
            stats.errors.append(msg)
            logger.error(msg)

    # --- 2. global sort by true instant (earliest first) ---
    candidates.sort(key=lambda it: it["_instant"])

    # --- 3. store with idempotency + cross-source dedup (earliest wins) ---
    stats.new_items = _store_items(
        conn, candidates, want_full_text=want_full_text,
        fetch_full_text=fetch_full_text, user_agent=user_agent, timeout=timeout,
    )

    conn.commit()

    # --- 4. health beacon: success ONLY if the whole cycle was healthy ---
    if stats.healthy:
        filings_db.mark_run_success(conn, stats.new_items)
    else:
        detail = "; ".join(stats.errors) or "no feeds polled"
        filings_db.mark_run_error(
            conn,
            f"unhealthy cycle: {stats.feeds_polled}/{stats.feeds_configured} polled, "
            f"{stats.feeds_failed} failed, {stats.feeds_skipped} skipped — {detail}",
        )

    logger.info(
        "cycle done: %d/%d feeds polled, %d failed, %d skipped, %d items seen, %d new, healthy=%s",
        stats.feeds_polled, stats.feeds_configured, stats.feeds_failed,
        stats.feeds_skipped, stats.items_seen, stats.new_items, stats.healthy,
    )
    return stats


def _store_items(conn, items, *, want_full_text, fetch_full_text, user_agent, timeout) -> int:
    """Resolve, dedup, and append a globally-sorted batch. Returns NEW row count.

    `items` MUST be pre-sorted by publication instant ascending so the earliest
    occurrence of any cross-source duplicate is stored first. Dedup is two-layer:
      1. idempotency: skip if dedup_key already in the DB;
      2. cross-source: skip if (isin, report_number, type) already stored OR
         already seen earlier in THIS batch — the earlier (earliest) row wins and
         is never overwritten.
    """
    new_count = 0
    fetched_at = datetime.now(timezone.utc).isoformat()
    seen_report_keys: set[tuple[str, str, str]] = set()
    for it in items:
        # Primary idempotency gate: already stored under this dedup key.
        if filings_db.filing_exists(conn, it["dedup_key"]):
            continue

        report_key = None
        if it["issuer_isin"] and it["report_number"] and it["espi_ebi_type"]:
            report_key = (it["issuer_isin"], it["report_number"], it["espi_ebi_type"])

        # Cross-source dedup against the DB and against earlier items in this batch.
        if report_key is not None:
            already = report_key in seen_report_keys or filings_db.find_by_report_key(
                conn, *report_key
            ) is not None
            if already:
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
            if report_key is not None:
                seen_report_keys.add(report_key)
    return new_count

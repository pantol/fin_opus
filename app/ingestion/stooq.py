"""EOD ingestion from Stooq.

Stooq daily CSV endpoint:
    https://stooq.pl/q/d/l/?s=<TICKER>&i=d

CSV columns (PL header):
    Data,Otwarcie,Najwyzszy,Najnizszy,Zamkniecie,Wolumen

Point-in-time rule: an EOD bar for date D is only publicly available after the
close of D, so we set `as_of_date = date` (= D). No future row is ever used to
compute a feature for an earlier decision date.

We store RAW prices (adjusted=0). Stooq also serves an adjusted series; when an
adjusted source is available it is stored separately with adjusted=1. The two
series are flagged and never mixed (see SKILL: keep raw + adjusted separate).
"""
from __future__ import annotations

import csv
import io
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable

import requests

from app.ingestion import provenance

STOOQ_URLS = (
    "https://stooq.pl/q/d/l/?s={ticker}&i=d",
    "https://stooq.com/q/d/l/?s={ticker}&i=d",
)
STOOQ_URL = STOOQ_URLS[0]  # kept for backward compatibility


class StooqUnavailableError(RuntimeError):
    """Raised when Stooq returns a non-CSV (e.g. JS bot-check) response."""

# Stooq PL header -> canonical field
_HEADER_MAP = {
    "Data": "date",
    "Otwarcie": "open",
    "Najwyzszy": "high",
    "Najnizszy": "low",
    "Zamkniecie": "close",
    "Wolumen": "volume",
    # Stooq sometimes serves EN headers
    "Date": "date",
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Volume": "volume",
}


@dataclass(frozen=True)
class Bar:
    date: str       # ISO date string
    as_of_date: str  # availability date (== date for EOD)
    open: float
    high: float
    low: float
    close: float
    volume: float


# Plain-text bodies Stooq serves instead of CSV when it refuses a request.
_BLOCK_MARKERS = (
    "access denied",
    "przekroczona dzienna liczba",    # daily call limit (PL)
    "exceeded the daily hits limit",  # daily call limit (EN)
)


def _failure_reason(text: str) -> str | None:
    """Return why a response body is not CSV data, or None if it looks like CSV."""
    head = text.lstrip()[:400].lower()
    if head.startswith(("<!doctype", "<html")) or "__verify" in head:
        return "JS bot-check page"
    for marker in _BLOCK_MARKERS:
        if marker in head:
            return f"blocked: {text.strip().splitlines()[0][:80]!r}"
    return None


def fetch_csv(ticker: str, timeout: float = 30.0, retries: int = 2,
              backoff_seconds: float = 2.0) -> str:
    """Fetch raw CSV text for a ticker from Stooq. Network call (not used in tests).

    Tries the .pl then .com endpoint, retrying transient network errors with
    backoff. Raises StooqUnavailableError when Stooq refuses the request
    (JS bot-check page, "Access denied", daily call limit), so callers get a
    clear, actionable failure instead of a parse error.
    """
    headers = {"User-Agent": "Mozilla/5.0 (compatible; gpw-decision-system)"}
    last_reason = "no endpoint reached"
    for template in STOOQ_URLS:
        url = template.format(ticker=ticker.lower())
        for attempt in range(retries + 1):
            try:
                resp = requests.get(url, timeout=timeout, headers=headers)
                resp.raise_for_status()
            except requests.RequestException as exc:
                last_reason = f"network error: {exc}"
                if attempt < retries:
                    time.sleep(backoff_seconds * (attempt + 1))
                    continue
                break
            reason = _failure_reason(resp.text)
            if reason is None:
                return resp.text
            last_reason = reason
            break  # a refusal is not transient for this URL; try the other domain
    raise StooqUnavailableError(
        f"Stooq unavailable for '{ticker}': {last_reason}"
    )


def parse_csv(text: str) -> list[Bar]:
    """Parse Stooq CSV text into Bars. Pure function (deterministic, no network)."""
    reader = csv.reader(io.StringIO(text.strip()))
    rows = list(reader)
    if not rows:
        return []
    header = [_HEADER_MAP.get(h.strip(), h.strip()) for h in rows[0]]
    idx = {name: i for i, name in enumerate(header)}
    required = ("date", "open", "high", "low", "close")
    if not all(c in idx for c in required):
        raise ValueError(f"Unexpected Stooq header: {rows[0]}")

    bars: list[Bar] = []
    for row in rows[1:]:
        if not row or not row[idx["date"]].strip():
            continue
        d = row[idx["date"]].strip()
        try:
            o = float(row[idx["open"]])
            h = float(row[idx["high"]])
            lo = float(row[idx["low"]])
            c = float(row[idx["close"]])
            v = float(row[idx["volume"]]) if "volume" in idx and row[idx["volume"]].strip() else 0.0
        except ValueError:
            # rows with non-numeric placeholders ("N/D") are skipped
            continue
        # as_of_date == bar date: EOD bar is available only after that day's close.
        bars.append(Bar(date=d, as_of_date=d, open=o, high=h, low=lo, close=c, volume=v))
    return bars


def upsert_instrument(conn, inst: dict, is_index: bool = False) -> int:
    """Insert or update an instrument; return its id."""
    conn.execute(
        """
        INSERT INTO instruments (ticker, name, market, sector, isin, is_index, listed_from, delisted_on)
        VALUES (:ticker, :name, :market, :sector, :isin, :is_index, :listed_from, :delisted_on)
        ON CONFLICT(ticker) DO UPDATE SET
            name=excluded.name, sector=excluded.sector, isin=excluded.isin, is_index=excluded.is_index,
            listed_from=excluded.listed_from, delisted_on=excluded.delisted_on
        """,
        {
            "ticker": inst["ticker"].lower(),
            "name": inst.get("name", inst["ticker"]),
            "market": inst.get("market", "GPW"),
            "sector": inst.get("sector"),
            "isin": inst.get("isin"),
            "is_index": 1 if is_index else 0,
            "listed_from": _iso(inst.get("listed_from")),
            "delisted_on": _iso(inst.get("delisted_on")),
        },
    )
    row = conn.execute(
        "SELECT id FROM instruments WHERE ticker = ?", (inst["ticker"].lower(),)
    ).fetchone()
    return int(row[0])


def store_bars(conn, instrument_id: int, bars: Iterable[Bar], adjusted: bool = False,
               *, source: str) -> int:
    """Insert bars for an instrument. Idempotent on (instrument_id, date, adjusted).

    `source` records provenance ('gpw' | 'stooq' | 'demo') and is REQUIRED:
    a forgotten default would silently mint fake provenance, so omission is a
    TypeError and an unknown value a ValueError. A re-ingest of an existing bar
    updates it to the latest writer. This is the write layer only — demo/real
    separation is enforced by the callers via provenance.assert_no_mixing
    BEFORE any bar is written.
    """
    if source not in provenance.VALID_SOURCES:
        raise ValueError(
            f"Unknown price source {source!r}; expected one of {provenance.VALID_SOURCES}")
    n = 0
    for b in bars:
        conn.execute(
            """
            INSERT INTO prices (instrument_id, date, as_of_date, open, high, low, close, volume, adjusted, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(instrument_id, date, adjusted) DO UPDATE SET
                as_of_date=excluded.as_of_date, open=excluded.open, high=excluded.high,
                low=excluded.low, close=excluded.close, volume=excluded.volume,
                source=excluded.source
            """,
            (
                instrument_id,
                b.date,
                b.as_of_date,
                b.open,
                b.high,
                b.low,
                b.close,
                b.volume,
                1 if adjusted else 0,
                source,
            ),
        )
        n += 1
    return n


@dataclass
class IngestReport:
    """Outcome of a universe ingest: per-ticker bar counts and failures."""
    counts: dict[str, int] = field(default_factory=dict)    # ticker -> bars stored
    failures: dict[str, str] = field(default_factory=dict)  # ticker -> reason
    # Session-file days successfully fetched; None for sources without the
    # concept (Stooq/demo). 0 with no fetch errors means the requested window
    # simply had no trading days.
    sessions: int | None = None

    @property
    def ok(self) -> bool:
        return not self.failures


def ingest_universe(conn, universe: dict, fetcher=fetch_csv,
                    delay_seconds: float = 0.0,
                    source: str = provenance.STOOQ_SOURCE) -> IngestReport:
    """Ingest indices + instruments described by the universe config.

    `fetcher` is injectable so tests can supply cached CSV without network.
    One ticker failing (Stooq refusal, network error, bad CSV, empty history)
    does not abort the rest: successes are committed per ticker (upserts are
    idempotent, so partial ingests are safe to re-run) and failures are
    collected in the report. `delay_seconds` sleeps between fetches to stay
    polite to the live endpoint. `source` tags row provenance (the demo
    generator routes through here with source='demo'); mixing demo and real
    rows in one database raises DataMixingError before anything is written.
    """
    provenance.assert_no_mixing(conn, source)
    report = IngestReport()

    index_entries = list(universe.get("indices", []))
    bench = universe.get("benchmark")
    if bench:
        index_entries.append(bench)

    entries = [(e, True) for e in index_entries]
    entries += [(e, False) for e in universe.get("instruments", [])]

    for i, (entry, is_index) in enumerate(entries):
        ticker = entry["ticker"]
        inst_id = upsert_instrument(conn, entry, is_index=is_index)
        if i and delay_seconds > 0:
            time.sleep(delay_seconds)
        try:
            bars = parse_csv(fetcher(ticker))
        except (StooqUnavailableError, requests.RequestException, ValueError) as exc:
            report.failures[ticker] = str(exc)
            continue
        if not bars:
            report.failures[ticker] = "no rows parsed (empty history?)"
            continue
        report.counts[ticker] = store_bars(conn, inst_id, bars, adjusted=False,
                                           source=source)
        conn.commit()

    conn.commit()
    return report


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    return str(value)

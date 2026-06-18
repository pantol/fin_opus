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
from dataclasses import dataclass
from datetime import date
from typing import Iterable

import requests

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


def _looks_like_csv(text: str) -> bool:
    head = text.lstrip()[:64].lower()
    return not head.startswith(("<!doctype", "<html"))


def fetch_csv(ticker: str, timeout: float = 30.0) -> str:
    """Fetch raw CSV text for a ticker from Stooq. Network call (not used in tests).

    Tries the .pl then .com endpoint. Raises StooqUnavailableError if Stooq
    serves an HTML/JS bot-check page instead of CSV (common when rate-limited or
    behind certain networks), so callers get a clear, actionable failure.
    """
    headers = {"User-Agent": "Mozilla/5.0 (compatible; gpw-decision-system)"}
    last_text = ""
    for template in STOOQ_URLS:
        url = template.format(ticker=ticker.lower())
        resp = requests.get(url, timeout=timeout, headers=headers)
        resp.raise_for_status()
        if _looks_like_csv(resp.text):
            return resp.text
        last_text = resp.text
    raise StooqUnavailableError(
        f"Stooq returned a non-CSV response for '{ticker}' "
        f"(likely a JS bot-check / rate limit). First bytes: {last_text[:80]!r}"
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
        INSERT INTO instruments (ticker, name, market, sector, is_index, listed_from, delisted_on)
        VALUES (:ticker, :name, :market, :sector, :is_index, :listed_from, :delisted_on)
        ON CONFLICT(ticker) DO UPDATE SET
            name=excluded.name, sector=excluded.sector, is_index=excluded.is_index,
            listed_from=excluded.listed_from, delisted_on=excluded.delisted_on
        """,
        {
            "ticker": inst["ticker"].lower(),
            "name": inst.get("name", inst["ticker"]),
            "market": inst.get("market", "GPW"),
            "sector": inst.get("sector"),
            "is_index": 1 if is_index else 0,
            "listed_from": _iso(inst.get("listed_from")),
            "delisted_on": _iso(inst.get("delisted_on")),
        },
    )
    row = conn.execute(
        "SELECT id FROM instruments WHERE ticker = ?", (inst["ticker"].lower(),)
    ).fetchone()
    return int(row[0])


def store_bars(conn, instrument_id: int, bars: Iterable[Bar], adjusted: bool = False) -> int:
    """Insert bars for an instrument. Idempotent on (instrument_id, date, adjusted)."""
    n = 0
    for b in bars:
        conn.execute(
            """
            INSERT INTO prices (instrument_id, date, as_of_date, open, high, low, close, volume, adjusted)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(instrument_id, date, adjusted) DO UPDATE SET
                as_of_date=excluded.as_of_date, open=excluded.open, high=excluded.high,
                low=excluded.low, close=excluded.close, volume=excluded.volume
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
            ),
        )
        n += 1
    return n


def ingest_universe(conn, universe: dict, fetcher=fetch_csv) -> dict[str, int]:
    """Ingest indices + instruments described by the universe config.

    `fetcher` is injectable so tests can supply cached CSV without network.
    Returns {ticker: bars_stored}.
    """
    counts: dict[str, int] = {}

    index_entries = list(universe.get("indices", []))
    bench = universe.get("benchmark")
    if bench:
        index_entries.append(bench)

    for entry in index_entries:
        inst_id = upsert_instrument(conn, entry, is_index=True)
        bars = parse_csv(fetcher(entry["ticker"]))
        counts[entry["ticker"]] = store_bars(conn, inst_id, bars, adjusted=False)

    for entry in universe.get("instruments", []):
        inst_id = upsert_instrument(conn, entry, is_index=False)
        bars = parse_csv(fetcher(entry["ticker"]))
        counts[entry["ticker"]] = store_bars(conn, inst_id, bars, adjusted=False)

    conn.commit()
    return counts


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    return str(value)

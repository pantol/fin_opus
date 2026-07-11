"""EOD ingestion from GPW's official quotes archive + GPW Benchmark indices.

Primary REAL-data source (Stooq's CSV endpoint is login-gated as of 2026-07;
Bossa's public archives were removed):

  * equities — https://www.gpw.pl/archiwum-notowan?fetch=1&type=10&date=DD-MM-YYYY
    One legacy .xls per SESSION with the full market snapshot: every instrument
    listed on that day, keyed by ISIN. Historical files include companies that
    have since been delisted, so a date-range backfill is survivorship-bias-free
    BY CONSTRUCTION (verified back to 1995).
  * indices — https://gpwbenchmark.pl/chart-json.php?req=[{isin,mode:RANGE,...}]
    Full daily history for an index ISIN (e.g. WIG20TR) in ONE request. Index
    ISINs are discovered from GPW's own indices session file (type=1), never
    hardcoded.

Both hosts reset bare TLS clients (WAF); network fetches use curl_cffi browser
impersonation. All network calls are injectable seams so tests run offline.

Point-in-time: as_of_date = session date (an EOD bar is public only after that
day's close) — the same convention as the Stooq path, enforced downstream by
`WHERE as_of_date <= T`.
"""
from __future__ import annotations

import json
import time
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import xlrd

from app.ingestion import provenance
from app.ingestion.stooq import Bar, IngestReport, store_bars, upsert_instrument

WARSAW = ZoneInfo("Europe/Warsaw")

ARCHIVE_URL = ("https://www.gpw.pl/archiwum-notowan"
               "?fetch=1&type={kind}&instrument=&date={date}")
CHART_JSON_URL = "https://gpwbenchmark.pl/chart-json.php?req={req}"
KIND_EQUITIES = 10
KIND_INDICES = 1

# .xls magic (OLE2). A session-less date returns an HTML page instead.
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0"


class GpwArchiveError(RuntimeError):
    """Raised when the GPW archive serves an unusable response."""


@dataclass(frozen=True)
class SessionRow:
    """One instrument's bar inside a session snapshot file."""
    date: str      # ISO session date
    name: str      # GPW short name, e.g. "PKOBP"
    isin: str
    currency: str
    open: float
    high: float
    low: float
    close: float
    volume: float


# --- network seams (injectable in tests) -------------------------------------

def _impersonated_get(url: str, timeout: float) -> "curl_cffi.requests.Response":
    # Imported lazily so offline tests never need curl_cffi's native lib.
    from curl_cffi import requests as cr
    return cr.get(url, impersonate="chrome", timeout=timeout)


def fetch_session_xls(session_date: date, kind: int = KIND_EQUITIES,
                      timeout: float = 60.0) -> bytes | None:
    """Fetch one session's .xls snapshot. Returns None when there was no
    session that day (GPW answers 200 with an HTML page instead of a file)."""
    url = ARCHIVE_URL.format(kind=kind, date=session_date.strftime("%d-%m-%Y"))
    resp = _impersonated_get(url, timeout)
    if resp.status_code != 200:
        raise GpwArchiveError(f"GPW archive HTTP {resp.status_code} for {session_date}")
    if not resp.content.startswith(_OLE2_MAGIC):
        return None  # no session (weekend/holiday): HTML page, no attachment
    return resp.content


def default_fetch_session_rows(session_date: date,
                               timeout: float = 60.0) -> list[SessionRow] | None:
    """Fetch + parse one equities session snapshot. None = no session."""
    content = fetch_session_xls(session_date, KIND_EQUITIES, timeout)
    if content is None:
        return None
    return rows_from_grid(_xls_to_grid(content))


def default_fetch_index_bars(index_name: str, start: date, end: date,
                             timeout: float = 60.0) -> list[Bar]:
    """Fetch an index's full daily history (by NAME, e.g. "WIG20TR").

    The ISIN is discovered from GPW's own indices session file — never
    hardcoded or guessed.
    """
    isin = _resolve_index_isin(index_name, end, timeout)
    req = json.dumps([{"isin": isin, "mode": "RANGE",
                       "from": start.isoformat(), "to": end.isoformat()}])
    url = CHART_JSON_URL.format(req=urllib.parse.quote(req))
    resp = _impersonated_get(url, timeout)
    if resp.status_code != 200:
        raise GpwArchiveError(f"chart-json HTTP {resp.status_code} for {index_name}")
    return bars_from_chart_json(resp.json())


def _resolve_index_isin(index_name: str, near: date, timeout: float) -> str:
    """Find an index ISIN by name in the most recent indices session file."""
    day = near
    for _ in range(14):  # walk back past weekends/holidays
        content = fetch_session_xls(day, KIND_INDICES, timeout)
        if content is not None:
            for row in rows_from_grid(_xls_to_grid(content)):
                if row.name.upper() == index_name.upper():
                    return row.isin
            raise GpwArchiveError(
                f"index '{index_name}' not present in GPW indices file for {day}")
        day -= timedelta(days=1)
    raise GpwArchiveError(f"no GPW indices session file found near {near}")


# --- pure parsing (deterministic, no I/O) -------------------------------------

def _xls_to_grid(content: bytes) -> list[list]:
    book = xlrd.open_workbook(file_contents=content)
    sheet = book.sheet_by_index(0)
    return [[sheet.cell_value(r, c) for c in range(sheet.ncols)]
            for r in range(sheet.nrows)]

# Header (PL) -> canonical index. Layout verified on live files (1995..2026):
# Data, Nazwa, ISIN, Waluta, Kurs otwarcia, Kurs max, Kurs min,
# Kurs zamkniecia, Zmiana, Wolumen, ...
_REQUIRED_HEADERS = ("data", "nazwa", "isin", "waluta")


def rows_from_grid(grid: list[list]) -> list[SessionRow]:
    """Parse a session file's cell grid into SessionRows. Pure function.

    Rows with unparseable/absent prices (suspended instruments) are skipped.
    """
    if not grid:
        return []
    header = [str(h).strip().lower() for h in grid[0]]
    if not all(any(req in h for h in header) for req in _REQUIRED_HEADERS):
        raise GpwArchiveError(f"unexpected GPW session file header: {grid[0]!r}")

    rows: list[SessionRow] = []
    for raw in grid[1:]:
        if len(raw) < 10:
            continue
        try:
            o, h, lo, c = (float(raw[4]), float(raw[5]), float(raw[6]), float(raw[7]))
            v = float(raw[9]) if raw[9] not in ("", None) else 0.0
        except (TypeError, ValueError):
            continue  # suspended / no-trade row
        if c <= 0:
            continue
        rows.append(SessionRow(
            date=str(raw[0]).strip(),
            name=str(raw[1]).strip(),
            isin=str(raw[2]).strip().upper(),
            currency=str(raw[3]).strip().upper(),
            open=o, high=h, low=lo, close=c, volume=v,
        ))
    return rows


def bars_from_chart_json(payload) -> list[Bar]:
    """Convert a gpwbenchmark chart-json payload into Bars. Pure function.

    `t` is a unix timestamp at Warsaw midnight of the session date. The very
    first point of a series can carry open=0 (index base quirk); such bars are
    repaired to open=high=low=close so close-based features stay usable.
    """
    if not (isinstance(payload, list) and payload and "data" in payload[0]):
        raise GpwArchiveError(f"unexpected chart-json payload: {str(payload)[:120]!r}")
    bars: list[Bar] = []
    for p in payload[0]["data"]:
        d = datetime.fromtimestamp(int(p["t"]), tz=WARSAW).date().isoformat()
        c = float(p["c"])
        if c <= 0:
            continue
        o, h, lo = (float(p.get("o") or 0), float(p.get("h") or 0), float(p.get("l") or 0))
        if o <= 0 or h <= 0 or lo <= 0:
            o = h = lo = c
        # as_of_date == session date: EOD value public only after the close.
        bars.append(Bar(date=d, as_of_date=d, open=o, high=h, low=lo, close=c,
                        volume=0.0))
    return bars


# --- ingestion ----------------------------------------------------------------

def ingest_range(
    conn,
    universe: dict,
    start: date,
    end: date,
    *,
    full_market: bool = False,
    delay_seconds: float = 1.0,
    fetch_session_rows=default_fetch_session_rows,
    fetch_index_bars=default_fetch_index_bars,
) -> IngestReport:
    """Ingest GPW session snapshots for [start, end] + index benchmark history.

    Universe instruments are matched by ISIN (entries without an ISIN cannot
    match and are reported as failures once). With `full_market=True` every
    other PLN instrument found in the files is stored too, keyed by
    ticker=isin (lowercase) — this is what makes a deep backfill
    survivorship-bias-free: dead companies appear in the files of the days
    they traded.

    Indices (benchmark + `indices` entries) come from GPW Benchmark's
    chart-json in one request each, covering [start, end].

    Resilient like the Stooq path: one failed session day or index does not
    abort the rest; successes commit per session; re-runs are idempotent.
    Refuses (before any write or network call) a database holding demo rows.
    """
    provenance.assert_no_mixing(conn, "gpw")
    report = IngestReport()

    # -- indices / benchmark (one request each, full range) --
    index_entries = list(universe.get("indices", []))
    if universe.get("benchmark"):
        index_entries.append(universe["benchmark"])
    for entry in index_entries:
        ticker = entry["ticker"]
        # GPW files use short index names ("WIG20TR"), configs may carry long
        # ones ("WIG20 Total Return") — try the name, fall back to the ticker.
        candidates = [n for n in (entry.get("name"), ticker) if n]
        bars, last_exc = None, None
        for name in candidates:
            try:
                bars = fetch_index_bars(name, start, end)
                break
            except Exception as exc:  # noqa: BLE001 - try next candidate
                last_exc = exc
        if bars is None:
            report.failures[ticker] = str(last_exc)
            continue
        bars = [b for b in bars if start.isoformat() <= b.date <= end.isoformat()]
        if not bars:
            report.failures[ticker] = "no index history in range"
            continue
        inst_id = upsert_instrument(conn, entry, is_index=True)
        report.counts[ticker] = store_bars(conn, inst_id, bars, adjusted=False,
                                           source="gpw")
        conn.commit()

    # -- equities: one session file per trading day --
    by_isin: dict[str, dict] = {}
    for entry in universe.get("instruments", []):
        if entry.get("isin"):
            by_isin[str(entry["isin"]).upper()] = entry
        else:
            report.failures[entry["ticker"]] = (
                "no ISIN in universe config (GPW files are ISIN-keyed)")

    inst_ids: dict[str, int] = {}   # isin -> instruments.id
    tickers: dict[str, str] = {}    # isin -> report key
    sessions = 0
    day = start
    while day <= end:
        if day.weekday() < 5:
            try:
                rows = fetch_session_rows(day)
            except Exception as exc:  # noqa: BLE001 - one bad day must not abort
                report.failures[f"session:{day.isoformat()}"] = str(exc)
                rows = None
            if rows is not None:
                sessions += 1
                for row in rows:
                    if row.currency != "PLN":
                        continue
                    entry = by_isin.get(row.isin)
                    if entry is None and not full_market:
                        continue
                    if row.isin not in inst_ids:
                        meta = entry or {"ticker": row.isin.lower(), "name": row.name,
                                         "isin": row.isin}
                        inst_ids[row.isin] = upsert_instrument(conn, meta, is_index=False)
                        tickers[row.isin] = meta["ticker"]
                    bar = Bar(date=row.date, as_of_date=row.date, open=row.open,
                              high=row.high, low=row.low, close=row.close,
                              volume=row.volume)
                    tk = tickers[row.isin]
                    report.counts[tk] = report.counts.get(tk, 0) + store_bars(
                        conn, inst_ids[row.isin], [bar], adjusted=False,
                        source="gpw")
                conn.commit()
            if delay_seconds > 0:
                time.sleep(delay_seconds)
        day += timedelta(days=1)

    # A universe instrument whose ISIN never appeared in any session file gets
    # a failure entry (silence would read as "covered" when it wasn't) —
    # unless its absence is EXPECTED because the whole window lies outside its
    # [listed_from, delisted_on] lifetime (e.g. incremental runs after a
    # delisting must not fail forever).
    for isin, entry in by_isin.items():
        if isin in inst_ids or entry["ticker"] in report.failures:
            continue
        delisted = entry.get("delisted_on")
        listed = entry.get("listed_from")
        if delisted and str(delisted) < start.isoformat():
            continue
        if listed and str(listed) > end.isoformat():
            continue
        report.failures[entry["ticker"]] = (
            f"ISIN {isin} not found in any session file "
            f"{start.isoformat()}..{end.isoformat()} ({sessions} sessions)")
    return report

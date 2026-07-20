"""Delayed intraday bar recorder (day-trading groundwork; ZERO decisions).

Records ~15-min-delayed 5-minute OHLCV bars for the live universe from
Yahoo Finance's chart endpoint into `prices_intraday`, append-only, with
`as_of_ts` = the moment WE first observed each bar — the point-in-time rule
extended to intraday timestamps. GPW intraday history is expensive to buy;
this recorder accumulates the dataset that any future intraday strategy
will need for an honest backtest.

Boundaries (non-negotiable):
- INFORMATIONAL tier only. Nothing here is read by the EOD decision path,
  and nothing here writes to decisions / positions / paper_orders / trades.
- The feed is DELAYED — never treat these bars as an execution reference.

One-shot (single cycle: record + monitor, then exit):
    python -m app.ingestion.intraday
    make intraday

Scheduler loop (every N minutes inside the session window):
    python -m app.ingestion.intraday --loop
    make intraday-loop
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

WARSAW = ZoneInfo("Europe/Warsaw")

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


class IntradayError(RuntimeError):
    pass


@dataclass
class IntradayBar:
    bar_start: str      # ISO datetime with offset (Warsaw)
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class RecordReport:
    counts: dict[str, int] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)


def yahoo_symbol(ticker: str) -> str:
    """Map a universe ticker (stooq-style, lowercase) to Yahoo's WSE symbol."""
    return f"{ticker.upper()}.WA"


def default_fetch_chart(symbol: str, interval_min: int, timeout: float = 20.0) -> dict:
    """Fetch one day of intraday candles for `symbol`. Returns parsed JSON.

    Yahoo 429s bare TLS clients (fingerprint-based, like the GPW WAF) while
    accepting browser fingerprints — so this uses curl_cffi impersonation,
    the same convention as gpw_archive. Imported lazily so offline tests
    never need curl_cffi's native lib.
    """
    from curl_cffi import requests as cr

    resp = cr.get(
        CHART_URL.format(symbol=symbol),
        params={"interval": f"{interval_min}m", "range": "1d"},
        impersonate="chrome",
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise IntradayError(f"chart HTTP {resp.status_code} for {symbol}")
    return resp.json()


def bars_from_chart(payload: dict, *, drop_last: bool = True) -> list[IntradayBar]:
    """Convert a Yahoo chart payload into IntradayBars. Pure function.

    Entries with missing OHLC values (no trades in the interval) are skipped.
    The LAST bar of the response is dropped by default: it may still be
    forming, and `prices_intraday` is append-only (a stored bar must be
    final — first write wins, no updates).
    """
    try:
        result = payload["chart"]["result"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise IntradayError(f"unexpected chart payload: {str(payload)[:120]!r}") from exc
    timestamps = result.get("timestamp") or []
    if not timestamps:
        return []  # no trades yet today (pre-open / suspended)
    quote = result["indicators"]["quote"][0]
    bars: list[IntradayBar] = []
    for i, ts in enumerate(timestamps):
        o, h = quote["open"][i], quote["high"][i]
        lo, c = quote["low"][i], quote["close"][i]
        if o is None or h is None or lo is None or c is None:
            continue
        v = quote["volume"][i] or 0.0
        bars.append(IntradayBar(
            bar_start=datetime.fromtimestamp(int(ts), tz=WARSAW).isoformat(),
            open=float(o), high=float(h), low=float(lo), close=float(c),
            volume=float(v),
        ))
    if drop_last and bars:
        bars.pop()
    return bars


def store_intraday_bars(conn, instrument_id: int, bars: list[IntradayBar], *,
                        interval_min: int, source: str, as_of_ts: str) -> int:
    """Append-only insert; a bar already stored is never modified."""
    stored = 0
    for b in bars:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO prices_intraday
                (instrument_id, bar_start, interval_min,
                 open, high, low, close, volume, as_of_ts, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (instrument_id, b.bar_start, interval_min,
             b.open, b.high, b.low, b.close, b.volume, as_of_ts, source),
        )
        stored += cur.rowcount
    return stored


def _live_equities(conn, universe: dict,
                   skip: set[str] | None = None) -> list[tuple[int, str]]:
    """(instrument_id, ticker) for currently listed universe equities.

    Delisted names stay in the EOD universe (anti-survivorship), but a
    recorder can only record what trades TODAY. `skip` holds tickers the
    feed is known not to cover (documented gaps, not failures).
    """
    out: list[tuple[int, str]] = []
    for entry in universe.get("instruments", []):
        if entry.get("delisted_on") or (skip and entry["ticker"] in skip):
            continue
        row = conn.execute("SELECT id FROM instruments WHERE ticker = ?",
                           (entry["ticker"],)).fetchone()
        if row is not None:
            out.append((int(row["id"]), entry["ticker"]))
    return out


def record_cycle(
    conn,
    universe: dict,
    *,
    interval_min: int = 5,
    source: str = "yahoo_delayed",
    fetch_chart=default_fetch_chart,
    delay_seconds: float = 0.4,
    now: datetime | None = None,
    skip: set[str] | None = None,
) -> RecordReport:
    """Fetch + store one round of delayed intraday bars for the live universe.

    Per-ticker failures never abort the cycle (same resilience contract as
    the EOD ingesters). Idempotent: re-runs insert only unseen bars.
    """
    report = RecordReport()
    as_of = (now or datetime.now(WARSAW)).isoformat()
    for inst_id, ticker in _live_equities(conn, universe, skip):
        try:
            payload = fetch_chart(yahoo_symbol(ticker), interval_min)
            bars = bars_from_chart(payload)
            report.counts[ticker] = store_intraday_bars(
                conn, inst_id, bars, interval_min=interval_min, source=source,
                as_of_ts=as_of)
            conn.commit()
        except Exception as exc:  # noqa: BLE001 — one ticker must not abort the rest
            report.failures[ticker] = str(exc)
        if delay_seconds > 0:
            time.sleep(delay_seconds)
    return report


def session_is_open(cfg_intraday: dict, now: datetime | None = None) -> bool:
    """True inside the configured recording window (Warsaw time)."""
    now = now or datetime.now(WARSAW)
    session = cfg_intraday.get("session") or {}
    days = session.get("days", [0, 1, 2, 3, 4])
    if now.weekday() not in days:
        return False
    hhmm = now.strftime("%H:%M")
    return str(session.get("start", "09:05")) <= hhmm <= str(session.get("end", "17:30"))


# --- entrypoints ---------------------------------------------------------------

def run_once(db_path: str | None = None, *, monitor: bool = True) -> int:
    from app import config as cfg
    from app.alerts import monitor as monitor_mod
    from app.db import connect, init_db

    icfg = cfg.load_intraday_config()
    universe = cfg.load_universe()
    conn = connect(db_path)
    init_db(conn)
    log = logging.getLogger("intraday")
    try:
        report = record_cycle(
            conn, universe,
            interval_min=int(icfg.get("interval_minutes", 5)),
            source=str(icfg.get("source", "yahoo_delayed")),
            delay_seconds=float(icfg.get("fetch_delay_seconds", 0.4)),
            skip=set(icfg.get("skip_tickers") or []),
        )
        total = sum(report.counts.values())
        log.info("stored %d new bar(s) across %d ticker(s)", total, len(report.counts))
        for tk, reason in sorted(report.failures.items()):
            log.warning("FAILED %s: %s", tk, reason)
        if monitor:
            warnings = monitor_mod.check_positions(
                conn, near_pct=float((icfg.get("monitor") or {})
                                     .get("near_stop_pct", 0.02)))
            for w in warnings:
                log.info("monitor: %s %s @ %.2f (stop %.2f)",
                         w["state"], w["ticker"], w["price"], w["stop_price"])
        # Failures on every single ticker = the feed is down/blocked; surface
        # a non-zero exit so cron monitoring notices. Partial failures are OK.
        if report.failures and not report.counts:
            return 2
        return 0
    finally:
        conn.close()


def run_loop(db_path: str | None = None) -> int:
    from apscheduler.schedulers.blocking import BlockingScheduler

    from app import config as cfg

    icfg = cfg.load_intraday_config()
    interval = int(icfg.get("loop_interval_minutes", 5))
    log = logging.getLogger("intraday")
    log.info("starting recorder loop: every %d minute(s) inside the session window",
             interval)

    def _tick() -> None:
        if not session_is_open(icfg):
            log.debug("session closed — skipping cycle")
            return
        run_once(db_path)

    scheduler = BlockingScheduler(timezone="Europe/Warsaw")
    scheduler.add_job(_tick, "interval", minutes=interval,
                      next_run_time=datetime.now(WARSAW))
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("recorder loop stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None, help="database path (default: data/gpw.db)")
    parser.add_argument("--loop", action="store_true",
                        help="run forever on the configured schedule")
    parser.add_argument("--no-monitor", action="store_true",
                        help="record only; skip the stop-monitor pass")
    args = parser.parse_args(argv)
    if args.loop:
        return run_loop(args.db)
    return run_once(args.db, monitor=not args.no_monitor)


if __name__ == "__main__":
    sys.exit(main())

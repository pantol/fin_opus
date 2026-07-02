"""Deterministic synthetic demo data generator.

For development / CI / when the live Stooq endpoint is unreachable (it serves a
JS bot-check from some networks). This is CLEARLY-LABELLED FAKE DATA, NOT for
any real evaluation. Real runs use `app.ingestion.stooq` against live Stooq.

It produces Stooq-format CSV per ticker (deterministic by a per-ticker seed) so
the exact same end-to-end pipeline (ingest -> features -> walk-forward) runs.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import math

from app.ingestion import stooq


def _seed_for(ticker: str) -> int:
    return int(hashlib.sha256(ticker.encode()).hexdigest(), 16) % (2 ** 32)


def _synthetic_csv(ticker: str, n_days: int, base: float, drift: float) -> str:
    """Generate a deterministic OHLCV CSV (business days) for a ticker."""
    seed = _seed_for(ticker)
    lines = ["Data,Otwarcie,Najwyzszy,Najnizszy,Zamkniecie,Wolumen"]
    day = dt.date(2015, 1, 1)
    price = base
    added = 0
    i = 0
    while added < n_days:
        if day.weekday() < 5:
            # deterministic pseudo-noise from seed + index (no RNG state)
            noise = math.sin((seed % 1000 + i) / 17.0) * 0.4 + math.cos((seed % 333 + i) / 9.0) * 0.3
            price = max(1.0, price * (1 + drift) + noise * 0.05 * base / 100.0)
            o = round(price * 0.998, 4)
            h = round(price * 1.012, 4)
            lo = round(price * 0.988, 4)
            c = round(price, 4)
            v = 200000 + (i % 60) * 3000
            lines.append(f"{day.isoformat()},{o},{h},{lo},{c},{float(v)}")
            added += 1
        day += dt.timedelta(days=1)
        i += 1
    return "\n".join(lines)


def ingest_offline(conn, universe: dict, n_days: int = 1200) -> stooq.IngestReport:
    """Populate the DB with deterministic demo data for the whole universe."""
    base_map_default = 100.0

    def fetcher(ticker: str) -> str:
        # indices get a higher base, instruments vary by ticker hash for variety
        seed = _seed_for(ticker)
        base = 2000.0 if ticker in _index_tickers(universe) else base_map_default + (seed % 80)
        drift = 0.0004 + (seed % 7) * 0.00005
        return _synthetic_csv(ticker, n_days=n_days, base=base, drift=drift)

    return stooq.ingest_universe(conn, universe, fetcher=fetcher)


def _index_tickers(universe: dict) -> set[str]:
    tickers = {i["ticker"] for i in universe.get("indices", [])}
    if universe.get("benchmark"):
        tickers.add(universe["benchmark"]["ticker"])
    return tickers

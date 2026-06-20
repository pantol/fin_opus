"""Shared fixtures: synthetic deterministic price data (no network)."""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from app.db import connect, init_db

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FEEDS_DIR = FIXTURES_DIR / "feeds"


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db(c)
    yield c
    c.close()


def load_feed_fixture(name: str) -> str:
    """Read a saved real-shape RSS/Atom feed sample from tests/fixtures/feeds."""
    return (FEEDS_DIR / name).read_text(encoding="utf-8")


@pytest.fixture
def feeds_dir() -> Path:
    return FEEDS_DIR


def make_stooq_csv(rows: list[tuple[str, float, float, float, float, float]]) -> str:
    """Build a Stooq-format CSV string from (date,o,h,l,c,v) rows."""
    lines = ["Data,Otwarcie,Najwyzszy,Najnizszy,Zamkniecie,Wolumen"]
    for d, o, h, l, c, v in rows:
        lines.append(f"{d},{o},{h},{l},{c},{v}")
    return "\n".join(lines)


def synthetic_series(start="2018-01-01", n=400, base=100.0, drift=0.001):
    """Deterministic trending series for feature/strategy tests.

    Business-dayish dates (skip weekends crudely) with a gentle upward drift plus
    a smooth oscillation so SMA/ATR/momentum are well-defined.
    """
    import datetime as dt

    rows = []
    day = dt.date.fromisoformat(start)
    price = base
    added = 0
    while added < n:
        if day.weekday() < 5:  # Mon-Fri
            osc = math.sin(added / 20.0) * 0.5
            price = price * (1 + drift) + osc * 0.1
            o = price * 0.999
            h = price * 1.01
            lo = price * 0.99
            c = price
            v = 100000 + (added % 50) * 1000
            rows.append((day.isoformat(), round(o, 4), round(h, 4), round(lo, 4), round(c, 4), float(v)))
            added += 1
        day += dt.timedelta(days=1)
    return rows

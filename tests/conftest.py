"""Shared fixtures: synthetic deterministic price data (no network)."""
from __future__ import annotations

import math

import pytest

from app.db import connect, init_db


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db(c)
    yield c
    c.close()


def bt_config_no_gate() -> dict:
    """The real backtest.yaml minus the full-market entry gate and cost tiers.

    For tests that exercise OTHER mechanics (fills, corporate actions, paper
    plumbing, accounting) on SHORT synthetic histories: the production
    liquidity gate needs a full 63-session turnover window and would push
    every entry past the scenario, and the cost tiers would re-price fills the
    assertions pin. The production gate/tiers have their own dedicated tests
    (tests/test_full_universe.py).
    """
    from app import config as cfg

    bt = dict(cfg.load_backtest_config())
    uni = dict(bt.get("universe") or {})
    uni["mode"] = "config"
    uni.pop("liquidity", None)
    bt["universe"] = uni
    costs = dict(bt["costs"])
    costs.pop("liquidity_tiers", None)
    bt["costs"] = costs
    return bt


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

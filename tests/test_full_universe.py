"""Full-market universe: DB-driven loading, the point-in-time liquidity entry
gate, liquidity-tiered costs, the fast feature views, the paper coverage
denominator and the full-market ingest watermark. Fully offline/deterministic.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from app.backtest import engine, mc_benchmark
from app.backtest import fills as fillmod
from app.features import compute
from app.ingestion import gpw_archive, stooq
from app.paper import loop as paper_loop
from tests.conftest import make_stooq_csv


def _rows(n, price, volume, start="2019-01-01", drift=0.0005):
    """Deterministic gently-trending OHLCV rows with controllable turnover."""
    rows, day, px, added = [], dt.date.fromisoformat(start), float(price), 0
    while added < n:
        if day.weekday() < 5:
            px *= (1 + drift)
            rows.append((day.isoformat(), round(px * 0.999, 6), round(px * 1.01, 6),
                         round(px * 0.99, 6), round(px, 6), float(volume)))
            added += 1
        day += dt.timedelta(days=1)
    return rows


def _ingest(conn, ticker, rows, is_index=False, **inst):
    iid = stooq.upsert_instrument(conn, {"ticker": ticker, "name": ticker, **inst},
                                  is_index=is_index)
    stooq.store_bars(conn, iid, stooq.parse_csv(make_stooq_csv(rows)), source="stooq")
    conn.commit()
    return iid


def _mk_instrument(ticker, rows, sector=None):
    df = pd.DataFrame(rows, columns=["date"] + compute.PRICE_COLS)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    return engine.Instrument(
        instrument_id=abs(hash(ticker)) % 10_000, ticker=ticker, sector=sector,
        listed_from=None, delisted_on=None, prices=df,
        features=compute.compute_features(df),
    )


def _enter_always_strategy():
    return {
        "name": "enteralways", "version": 1,
        "entry": {"all": [{"feature": "close", "op": "gt", "value": 0}]},
        "exit": {"any": [{"feature": "close", "op": "lt", "value": -1}]},
        "risk": {"risk_per_trade": 0.01, "atr_mult_stop": 2.5,
                 "max_open_positions": 8, "max_exposure_per_name": 0.20,
                 "max_total_exposure": 1.0, "drawdown_circuit_breaker": 0.9},
    }


def _bt(liquidity=None, tiers=None):
    costs = {"commission_bps": 38.0, "commission_min": 3.0, "spread_bps": 20.0,
             "slippage_bps": 10.0, "max_volume_participation": 0.10}
    if tiers is not None:
        costs["liquidity_tiers"] = tiers
    uni = {"mode": "full", "index": None}
    if liquidity is not None:
        uni["liquidity"] = liquidity
    return {"initial_capital": 100000.0, "seed": 42, "user_id": "default",
            "costs": costs, "execution": {"signal_to_fill_lag_days": 1},
            "universe": uni,
            "walk_forward": {"in_sample_months": 1, "out_sample_months": 1,
                             "benchmark": "wig20tr"}}


TIERS = [
    {"min_median_turnover_pln": 20_000_000, "spread_bps": 20.0, "slippage_bps": 10.0},
    {"min_median_turnover_pln": 1_000_000, "spread_bps": 60.0, "slippage_bps": 25.0},
    {"min_median_turnover_pln": 0, "spread_bps": 250.0, "slippage_bps": 80.0},
]


# --- universe.mode ------------------------------------------------------------

def test_full_mode_loads_every_db_instrument_config_mode_stays_whitelisted(conn):
    _ingest(conn, "wig20tr", _rows(50, 2000, 0), is_index=True)
    _ingest(conn, "aaa", _rows(50, 100, 100000))
    _ingest(conn, "plxyz0000011", _rows(50, 5, 2000))  # archive-discovered, not in YAML
    universe = {"benchmark": {"ticker": "wig20tr", "is_index": True}, "indices": [],
                "instruments": [{"ticker": "aaa"}]}

    cfg_insts, _ = engine.load_instruments(conn, universe, "wig20tr", mode="config")
    full_insts, _ = engine.load_instruments(conn, universe, "wig20tr", mode="full")

    assert [i.ticker for i in cfg_insts] == ["aaa"]
    assert [i.ticker for i in full_insts] == ["aaa", "plxyz0000011"]  # no indices
    # bulk loader returns the same frames the per-instrument path would
    assert full_insts[0].prices.equals(cfg_insts[0].prices)


def test_universe_mode_validation():
    assert engine.universe_mode({"universe": {"mode": "full"}}) == "full"
    assert engine.universe_mode({}) == "config"
    with pytest.raises(ValueError):
        engine.universe_mode({"universe": {"mode": "everything"}})


# --- liquidity entry gate -----------------------------------------------------

def test_liquidity_gate_blocks_illiquid_entries():
    liquid = _mk_instrument("liq", _rows(200, 100, 100_000))      # ~10M PLN/day
    illiquid = _mk_instrument("ill", _rows(200, 1.0, 1_000))      # ~1k PLN/day
    bt = _bt(liquidity={"min_median_turnover_pln": 250_000,
                        "require_fresh_bar": True})
    res = engine.run_backtest([liquid, illiquid], pd.Series(dtype=float),
                              _enter_always_strategy(), bt)
    entered = {d["ticker"] for d in res.decisions if d["action"] == "ENTER"}
    assert "liq" in entered
    assert "ill" not in entered


def test_liquidity_gate_is_point_in_time():
    """Raising FUTURE volume must not unlock entries in the past, and no entry
    may be decided before the rolling median first clears the floor."""
    base = _rows(80, 100, 1_000) + _rows(120, 100, 100_000,
                                         start="2019-04-25")
    inst = _mk_instrument("ramp", base)
    floor = 250_000.0
    bt = _bt(liquidity={"min_median_turnover_pln": floor,
                        "require_fresh_bar": True})
    res = engine.run_backtest([inst], pd.Series(dtype=float),
                              _enter_always_strategy(), bt)
    enters = [d for d in res.decisions if d["action"] == "ENTER"]
    assert enters, "the instrument becomes liquid and must eventually be entered"

    med = inst.features["turnover_med_63"]
    first_ok = med[med >= floor].index[0]
    assert min(pd.Timestamp(d["decision_date"]) for d in enters) >= first_ok

    # Look-ahead probe: multiply volume AFTER the first entry's decision date
    # by 1000x — every decision on/before that date must be unchanged.
    cut = min(pd.Timestamp(d["decision_date"]) for d in enters)
    boosted_rows = [(d, o, h, l, c, v * 1000 if pd.Timestamp(d) > cut else v)
                    for d, o, h, l, c, v in base]
    boosted = _mk_instrument("ramp", boosted_rows)
    res2 = engine.run_backtest([boosted], pd.Series(dtype=float),
                               _enter_always_strategy(), bt)
    upto = lambda r: [d for d in r.decisions  # noqa: E731
                      if pd.Timestamp(d["decision_date"]) <= cut]
    assert upto(res) == upto(res2)


def test_fresh_bar_gate_blocks_suspended_names():
    """A name that stops printing bars must produce no NEW entry decisions
    after its last bar (stale quotes are not entry candidates)."""
    active = _mk_instrument("act", _rows(200, 100, 100_000))
    suspended = _mk_instrument("susp", _rows(150, 100, 100_000))  # ends earlier
    bt = _bt(liquidity={"min_median_turnover_pln": 0, "require_fresh_bar": True})
    res = engine.run_backtest([active, suspended], pd.Series(dtype=float),
                              _enter_always_strategy(), bt)
    last_susp_bar = suspended.prices.index[-1]
    stale_enters = [d for d in res.decisions
                    if d["action"] == "ENTER" and d["ticker"] == "susp"
                    and pd.Timestamp(d["decision_date"]) > last_susp_bar]
    assert stale_enters == []


# --- liquidity-tiered costs ---------------------------------------------------

def test_resolve_costs_tiers():
    costs = {"commission_bps": 38.0, "commission_min": 3.0, "spread_bps": 20.0,
             "slippage_bps": 10.0, "max_volume_participation": 0.10,
             "liquidity_tiers": TIERS}
    assert fillmod.resolve_costs(costs, 30_000_000)["spread_bps"] == 20.0
    assert fillmod.resolve_costs(costs, 20_000_000)["spread_bps"] == 20.0  # boundary
    assert fillmod.resolve_costs(costs, 5_000_000)["spread_bps"] == 60.0
    assert fillmod.resolve_costs(costs, 100)["spread_bps"] == 250.0
    # unknown liquidity -> most expensive bucket, never optimistic
    assert fillmod.resolve_costs(costs, None)["spread_bps"] == 250.0
    assert fillmod.resolve_costs(costs, float("nan"))["slippage_bps"] == 80.0
    # commission is never tiered; flat config passes through untouched
    assert fillmod.resolve_costs(costs, 100)["commission_bps"] == 38.0
    flat = {k: v for k, v in costs.items() if k != "liquidity_tiers"}
    assert fillmod.resolve_costs(flat, 100) is flat


def test_engine_fills_pay_the_instruments_tier():
    """The same illiquid instrument fills more expensively under tiers than
    under the flat model (spread 250 vs 20 bps)."""
    thin = _mk_instrument("thin", _rows(200, 10.0, 20_000))  # ~200k PLN/day
    gate = {"min_median_turnover_pln": 0, "require_fresh_bar": True}
    flat = engine.run_backtest([thin], pd.Series(dtype=float),
                               _enter_always_strategy(), _bt(liquidity=gate))
    tiered = engine.run_backtest([thin], pd.Series(dtype=float),
                                 _enter_always_strategy(),
                                 _bt(liquidity=gate, tiers=TIERS))
    f_first = next(d for d in flat.decisions if d["action"] == "ENTER")
    t_first = next(d for d in tiered.decisions if d["action"] == "ENTER")
    assert f_first["decision_date"] == t_first["decision_date"]
    assert t_first["price"] > f_first["price"]  # worse spread+slippage tier
    assert t_first["slippage"] > f_first["slippage"]


# --- fast feature views == pandas reference ----------------------------------

def test_view_snapshot_matches_features_at():
    inst = _mk_instrument("ref", _rows(120, 100, 50_000))
    view = engine.build_feature_view(inst.features)
    probe_days = (list(inst.features.index[:3])            # NaN-heavy head
                  + list(inst.features.index[60:70])
                  + [inst.features.index[0] - pd.Timedelta(days=1),   # before data
                     inst.features.index[40] + pd.Timedelta(days=1),  # weekend gap
                     inst.features.index[-1] + pd.Timedelta(days=30)])  # after end
    for day in probe_days:
        ref = compute.features_at(inst.features, day.date().isoformat())
        idx = engine._view_asof_idx(view, int(pd.Timestamp(day).value))
        fast = engine._view_snapshot(view, idx) if idx >= 0 else None
        assert fast == ref, f"snapshot mismatch at {day}"


# --- MC benchmark obeys the same gate ----------------------------------------

def test_mc_entry_matrix_respects_liquidity_gate():
    liquid = _mk_instrument("liq", _rows(120, 100, 100_000))
    illiquid = _mk_instrument("ill", _rows(120, 1.0, 1_000))
    calendar = engine._trading_calendar([liquid, illiquid])
    arrays = mc_benchmark._prepare([liquid, illiquid], calendar, None)
    bt = _bt(liquidity={"min_median_turnover_pln": 250_000,
                        "require_fresh_bar": True})
    ok = mc_benchmark._entry_ok_matrix(arrays, len(calendar), bt)
    assert ok[:, 1].sum() == 0            # illiquid never eligible
    assert ok[70:-1, 0].all()             # liquid eligible once medians exist
    # without a gate the illiquid name becomes eligible (legacy behavior)
    ok_nogate = mc_benchmark._entry_ok_matrix(arrays, len(calendar), _bt())
    assert ok_nogate[70:-1, 1].all()


# --- paper loop: coverage denominator in full mode ----------------------------

def _never_enter_strategy():
    s = _enter_always_strategy()
    s["entry"] = {"all": [{"feature": "close", "op": "lt", "value": -1}]}
    return s


def test_paper_coverage_ignores_long_dead_names_in_full_mode(conn):
    _ingest(conn, "wig20tr", _rows(250, 2000, 0), is_index=True)
    _ingest(conn, "aaa", _rows(250, 100, 100_000))
    _ingest(conn, "bbb", _rows(250, 50, 100_000))
    _ingest(conn, "pldead0000015", _rows(60, 10, 50_000))  # last bar months ago
    universe = {"benchmark": {"ticker": "wig20tr", "is_index": True},
                "indices": [], "instruments": []}
    latest = pd.Timestamp(_rows(250, 100, 1)[-1][0])

    def bt_with(activity_window):
        bt = _bt(liquidity={"min_median_turnover_pln": 250_000,
                            "require_fresh_bar": True})
        bt["paper"] = {"initial_capital": 100000.0, "max_staleness_days": 4,
                       "min_session_coverage": 0.75, "catchup_max_sessions": 3,
                       "activity_window_sessions": activity_window}
        return bt

    from datetime import datetime, timedelta
    now = datetime.combine(latest.date() + timedelta(days=1),
                           datetime.min.time(), tzinfo=paper_loop.WARSAW)

    # Dead name outside the activity window -> excluded -> coverage 2/2 -> ok.
    code, report = paper_loop.run_signals(
        conn, universe=universe, bt_cfg=bt_with(15),
        strategy_cfg=_never_enter_strategy(), now=now, send_fn=None)
    assert (code, report.status) == (paper_loop.EXIT_OK, "ok"), report.reason

    # A window covering the dead name's bars puts it back in the denominator:
    # 2/3 < 0.75 -> refused. Proves the denominator is what changed above.
    code2, report2 = paper_loop.run_signals(
        conn, universe=universe, bt_cfg=bt_with(100_000),
        strategy_cfg=_never_enter_strategy(), now=now, send_fn=None)
    assert code2 == paper_loop.EXIT_REFUSED
    assert "printed a bar" in report2.reason


# --- full-market ingest watermark ---------------------------------------------

def test_last_full_market_session_watermark(conn):
    assert gpw_archive.last_full_market_session(conn) is None
    ids = [_ingest(conn, t, _rows(1, 10, 100)) for t in ("t1", "t2", "t3")]
    assert len(set(ids)) == 3
    # one further day where ONLY t1 got a bar (universe-only incremental run)
    day2 = "2019-01-02"
    stooq.store_bars(conn, ids[0], [stooq.Bar(date=day2, as_of_date=day2, open=10,
                                              high=10, low=10, close=10, volume=1)],
                     source="stooq")
    conn.commit()
    # the full-market watermark stays on the 3-instrument day
    assert gpw_archive.last_full_market_session(conn, min_instruments=3) == "2019-01-01"
    # a demo database never yields a real watermark
    assert gpw_archive.last_full_market_session(conn, min_instruments=999) is None

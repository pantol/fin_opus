"""Regression tests for backtest accounting fixes.

Covers:
- cash applied atomically on fill day (equity = cash + holdings, fees only loss)
- partial (volume-capped) sells do not discard shares
- load_instruments restricts to the configured universe
- pending BUY orders prevent duplicate entries
- equity curve carries real cash + exposure
"""
import pandas as pd
import pytest

from app import config as cfg
from app.backtest import engine
from app.backtest import fills as fillmod
from app.ingestion import stooq

from tests.conftest import make_stooq_csv, synthetic_series


def _ingest(conn, ticker, rows, **inst):
    iid = stooq.upsert_instrument(conn, {"ticker": ticker, "name": ticker, **inst},
                                  is_index=inst.get("is_index", False))
    stooq.store_bars(conn, iid, stooq.parse_csv(make_stooq_csv(rows)))
    return iid


def _seed(conn):
    _ingest(conn, "wig20tr", synthetic_series(n=900, base=2000, drift=0.0005), is_index=True)
    _ingest(conn, "aaa", synthetic_series(n=900, base=100, drift=0.0009), sector="tech",
            listed_from="2015-01-01")
    _ingest(conn, "bbb", synthetic_series(n=900, base=50, drift=0.0006), sector="banking",
            listed_from="2015-01-01")
    conn.commit()


def _universe():
    return {
        "benchmark": {"ticker": "wig20tr", "is_index": True},
        "indices": [],
        "instruments": [{"ticker": "aaa", "sector": "tech"},
                        {"ticker": "bbb", "sector": "banking"}],
    }


def test_load_instruments_restricts_to_universe(conn):
    _seed(conn)
    # extra ticker in the DB but NOT in the universe config must be ignored
    _ingest(conn, "zzz", synthetic_series(n=900, base=10, drift=0.001), sector="misc",
            listed_from="2015-01-01")
    conn.commit()

    uni = _universe()  # only aaa, bbb
    instruments, _bench = engine.load_instruments(conn, uni, "wig20tr")
    tickers = {i.ticker for i in instruments}
    assert tickers == {"aaa", "bbb"}
    assert "zzz" not in tickers


def test_equity_curve_has_cash_and_exposure(conn):
    _seed(conn)
    uni = _universe()
    bt_cfg = cfg.load_backtest_config()
    strat = cfg.load_strategy("trend_momentum")
    instruments, bench = engine.load_instruments(conn, uni, "wig20tr")
    res = engine.run_walk_forward(instruments, bench, strat, bt_cfg)

    assert not res.cash_curve.empty
    assert not res.exposure_curve.empty
    assert res.cash_curve.index.equals(res.equity_curve.index)
    # exposure ratio is bounded [0, ~1] (no leverage)
    assert (res.exposure_curve >= -1e-9).all()
    assert (res.exposure_curve <= 1.0 + 1e-6).all()
    # equity == cash + exposure*equity within rounding on every recorded day
    for d in res.equity_curve.index:
        eq = res.equity_curve[d]
        cash = res.cash_curve[d]
        holdings = res.exposure_curve[d] * eq
        assert abs(eq - (cash + holdings)) < 1e-3


def test_cash_never_goes_negative_meaningfully(conn):
    _seed(conn)
    uni = _universe()
    bt_cfg = cfg.load_backtest_config()
    strat = cfg.load_strategy("trend_momentum")
    instruments, bench = engine.load_instruments(conn, uni, "wig20tr")
    res = engine.run_walk_forward(instruments, bench, strat, bt_cfg)
    # small negative cash from fees/rounding is tolerable; large overdraft is not
    assert res.cash_curve.min() > -1000.0


def test_partial_sell_preserves_remaining_shares(conn):
    """A volume-capped SELL must reduce the position, not delete it."""
    # Low-volume bars (200 shares) so a 10% cap = 20 fillable shares << 100 held.
    rows = [(d, o, h, l, c, 200.0)
            for (d, o, h, l, c, _v) in synthetic_series(n=10, base=100, drift=0.0)]
    _ingest(conn, "lq", rows, sector="x", listed_from="2015-01-01")
    prices = engine.compute.load_prices_asof(conn, 1, as_of="9999-12-31")
    inst = engine.Instrument(
        instrument_id=1, ticker="lq", sector="x", listed_from="2015-01-01",
        delisted_on=None, prices=prices,
        features=engine.compute.compute_features(prices),
    )
    positions = {"lq": engine.Position(ticker="lq", sector="x", instrument_id=1,
                                       qty=100, entry_price=100.0,
                                       entry_date="2015-01-01", stop_price=90.0)}
    trade_pnls, decisions = [], []
    day = inst.prices.index[1]
    bar_vol = float(inst.prices.at[day, "volume"])
    costs = dict(cfg.load_backtest_config()["costs"])

    max_fillable = fillmod.apply_volume_cap(100, bar_vol, costs)
    assert 0 < max_fillable < 100  # genuinely partial (regression guard)

    order = {"side": "SELL", "ticker": "lq", "qty": 100,
             "decision_date": "x", "features": {}}
    cash_delta, buy_notional, unfilled = engine._execute_order(
        order, day, {"lq": inst}, costs, positions, trade_pnls, decisions)

    # remainder preserved, not discarded
    assert "lq" in positions
    assert positions["lq"].qty == 100 - max_fillable
    assert unfilled == 100 - max_fillable
    assert len(trade_pnls) == 1
    assert cash_delta > 0  # sale produced proceeds


def test_suspended_position_marks_at_last_close_not_zero(conn):
    """A4: a held name printing no bar is a stale mark, not a -100% move.

    Zero-marking created phantom drawdowns (falsely tripping the 25% circuit
    breaker) and freed the exposure caps for over-allocation.
    """
    rows = synthetic_series(n=10, base=100, drift=0.0)
    _ingest(conn, "sus", rows[:5] + rows[7:], sector="x", listed_from="2015-01-01")
    conn.commit()
    prices = engine.compute.load_prices_asof(conn, 1, as_of="9999-12-31")
    inst = engine.Instrument(
        instrument_id=1, ticker="sus", sector="x", listed_from="2015-01-01",
        delisted_on=None, prices=prices,
        features=engine.compute.compute_features(prices))
    positions = {"sus": engine.Position(ticker="sus", sector="x", instrument_id=1,
                                        qty=100, entry_price=100.0,
                                        entry_date="2015-01-01", stop_price=90.0)}
    gap_day = pd.Timestamp(rows[5][0])            # suspended session
    last_close = float(prices["close"].iloc[4])   # last bar before the gap

    state, equity, holdings, _peak = engine.build_day_state(
        day=gap_day, positions=positions, pending_buys={},
        inst_by_ticker={"sus": inst}, cash=1000.0, peak_equity=100000.0,
        atr_mult=2.5)
    assert holdings == 100 * last_close                  # stale mark, not zero
    assert state.exposure_by_name["sus"] == 100 * last_close  # caps still see it
    assert equity == 1000.0 + 100 * last_close

    # once DELISTED the position is written off (no market left to sell into)
    dead = engine.Instrument(
        instrument_id=1, ticker="sus", sector="x", listed_from="2015-01-01",
        delisted_on=rows[4][0], prices=prices,
        features=engine.compute.compute_features(prices))
    state, equity, holdings, _peak = engine.build_day_state(
        day=gap_day, positions=dict(positions), pending_buys={},
        inst_by_ticker={"sus": dead}, cash=1000.0, peak_equity=100000.0,
        atr_mult=2.5)
    assert holdings == 0.0 and equity == 1000.0


def test_equity_is_continuous_across_a_suspension(conn):
    """End to end: a mid-span suspension must not crater the equity curve."""
    rows = synthetic_series(n=60, base=100, drift=0.0)
    _ingest(conn, "wig20tr", synthetic_series(n=60, base=2000, drift=0.0),
            is_index=True)
    _ingest(conn, "aaa", rows[:30] + rows[40:], sector="tech",
            listed_from="2015-01-01")
    # a second name trading every day keeps the gap days on the union calendar
    _ingest(conn, "bbb", synthetic_series(n=60, base=50, drift=0.0),
            sector="fin", listed_from="2015-01-01")
    conn.commit()
    uni = {"benchmark": {"ticker": "wig20tr", "is_index": True}, "indices": [],
           "instruments": [{"ticker": "aaa", "sector": "tech"},
                           {"ticker": "bbb", "sector": "fin"}]}
    strat = {  # always-enter toy: in position well before the gap
        "name": "toy", "version": 1,
        "entry": {"all": [{"feature": "close", "op": "gt", "value": 0.0}]},
        "exit": {"any": [{"feature": "close", "op": "lt", "value": 0.0}]},
        "risk": dict(cfg.load_strategy("trend_momentum")["risk"]),
    }
    instruments, bench = engine.load_instruments(conn, uni, "wig20tr")
    res = engine.run_backtest(instruments, bench, strat,
                              cfg.load_backtest_config())
    gap_days = [pd.Timestamp(r[0]) for r in rows[30:40]]
    before = res.equity_curve[pd.Timestamp(rows[29][0])]
    # The 'aaa' position (~20% of equity) is stale-marked through the gap:
    # equity may wiggle with 'bbb', but the pre-fix phantom write-off of the
    # whole aaa position value must never reappear.
    for d in gap_days:
        assert abs(res.equity_curve[d] - before) < 0.01 * before


def test_position_held_into_delisting_is_written_off(conn):
    """A held name that delists realizes its -100% in the trade ledger and
    frees the max_open_positions slot — no zombie positions blocking the book
    while win_rate/profit_factor never see the loss."""
    n = 60
    rows = synthetic_series(n=n, base=100, drift=0.0)
    _ingest(conn, "wig20tr", synthetic_series(n=n, base=2000, drift=0.0),
            is_index=True)
    _ingest(conn, "dead", rows[:30], sector="x", listed_from="2015-01-01",
            delisted_on=rows[29][0])
    _ingest(conn, "live", synthetic_series(n=n, base=50, drift=0.0), sector="y",
            listed_from="2015-01-01")
    conn.commit()
    uni = {"benchmark": {"ticker": "wig20tr", "is_index": True}, "indices": [],
           "instruments": [{"ticker": "dead", "sector": "x"},
                           {"ticker": "live", "sector": "y"}]}
    strat = {
        "name": "toy", "version": 1,
        "entry": {"all": [{"feature": "close", "op": "gt", "value": 0.0}]},
        "exit": {"any": [{"feature": "close", "op": "lt", "value": 0.0}]},
        "risk": {**cfg.load_strategy("trend_momentum")["risk"],
                 "max_open_positions": 1},
    }
    instruments, bench = engine.load_instruments(conn, uni, "wig20tr")
    res = engine.run_backtest(instruments, bench, strat,
                              cfg.load_backtest_config())

    write_offs = [d for d in res.decisions
                  if d["action"] == "EXIT" and d["price"] == 0.0]
    assert [d["ticker"] for d in write_offs] == ["dead"]
    assert write_offs[0]["features"] == {"forced": "delisted_write_off"}
    assert min(res.trade_pnls) < 0  # the -100% reached the trade statistics
    # the freed slot lets 'live' enter AFTER the delisting (max_open_positions=1)
    live_entries = [d for d in res.decisions
                    if d["ticker"] == "live" and d["action"] == "ENTER"]
    assert live_entries, "write-off must free the book slot"
    assert live_entries[0]["fill_date"] > rows[29][0]


def test_no_duplicate_pending_buys_for_same_ticker(conn):
    """With lag>1, the same ticker must not be queued twice before filling."""
    _seed(conn)
    uni = _universe()
    bt_cfg = dict(cfg.load_backtest_config())
    bt_cfg["execution"] = {"signal_to_fill_lag_days": 3}
    strat = cfg.load_strategy("trend_momentum")
    instruments, bench = engine.load_instruments(conn, uni, "wig20tr")
    res = engine.run_walk_forward(instruments, bench, strat, bt_cfg)

    # An ENTER for a ticker must be followed by an EXIT before the next ENTER.
    seen_open = {}
    for d in sorted(res.decisions, key=lambda x: (x["fill_date"], x["ticker"])):
        tk = d["ticker"]
        if d["action"] == "ENTER":
            assert not seen_open.get(tk), f"double ENTER for {tk} without EXIT"
            seen_open[tk] = True
        elif d["action"] == "EXIT":
            seen_open[tk] = False

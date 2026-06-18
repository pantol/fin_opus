"""End-to-end integration + reproducibility on synthetic data (no network)."""
import pandas as pd

from app import config as cfg
from app.backtest import engine
from app.ingestion import stooq

from tests.conftest import make_stooq_csv, synthetic_series


def _seed_db(conn):
    """Ingest a small synthetic universe incl. a delisted ticker + benchmark."""
    # benchmark (index, not traded)
    bench_id = stooq.upsert_instrument(
        conn, {"ticker": "wig20tr", "name": "WIG20TR"}, is_index=True
    )
    stooq.store_bars(conn, bench_id,
                     stooq.parse_csv(make_stooq_csv(synthetic_series(n=900, base=2000, drift=0.0005))))

    # tradable instruments
    rows_a = synthetic_series(n=900, base=100.0, drift=0.0008)
    a_id = stooq.upsert_instrument(conn, {"ticker": "aaa", "name": "AAA", "sector": "tech",
                                          "listed_from": rows_a[0][0]})
    stooq.store_bars(conn, a_id, stooq.parse_csv(make_stooq_csv(rows_a)))

    rows_b = synthetic_series(n=900, base=50.0, drift=0.0006)
    b_id = stooq.upsert_instrument(conn, {"ticker": "bbb", "name": "BBB", "sector": "banking",
                                          "listed_from": rows_b[0][0]})
    stooq.store_bars(conn, b_id, stooq.parse_csv(make_stooq_csv(rows_b)))

    # delisted ticker (anti-survivorship): stops trading partway through
    rows_c = synthetic_series(n=400, base=30.0, drift=0.0003)
    c_id = stooq.upsert_instrument(conn, {"ticker": "ccc", "name": "CCC", "sector": "energy",
                                          "listed_from": rows_c[0][0],
                                          "delisted_on": rows_c[-1][0]})
    stooq.store_bars(conn, c_id, stooq.parse_csv(make_stooq_csv(rows_c)))
    conn.commit()


def _universe():
    return {
        "benchmark": {"ticker": "wig20tr", "name": "WIG20TR", "is_index": True},
        "indices": [],
        "instruments": [
            {"ticker": "aaa", "sector": "tech"},
            {"ticker": "bbb", "sector": "banking"},
            {"ticker": "ccc", "sector": "energy"},
        ],
    }


def test_end_to_end_walk_forward_runs(conn):
    _seed_db(conn)
    universe = _universe()
    bt_cfg = cfg.load_backtest_config()
    strat = cfg.load_strategy("trend_momentum")

    instruments, bench = engine.load_instruments(conn, universe, "wig20tr")
    assert len(instruments) == 3  # incl. delisted ccc (anti-survivorship)

    result = engine.run_walk_forward(instruments, bench, strat, bt_cfg)

    assert not result.equity_curve.empty
    assert len(result.benchmark_curve) == len(result.equity_curve)
    for key in ("cagr", "sharpe", "max_drawdown", "calmar", "n_trades"):
        assert key in result.metrics
    # benchmark present (vs WIG20TR, not SPY)
    assert "cagr" in result.benchmark_metrics


def test_delisted_instrument_not_traded_after_delisting(conn):
    _seed_db(conn)
    universe = _universe()
    bt_cfg = cfg.load_backtest_config()
    strat = cfg.load_strategy("trend_momentum")
    instruments, bench = engine.load_instruments(conn, universe, "wig20tr")
    result = engine.run_walk_forward(instruments, bench, strat, bt_cfg)

    ccc = next(i for i in instruments if i.ticker == "ccc")
    delisted = pd.to_datetime(ccc.delisted_on)
    for d in result.decisions:
        if d["ticker"] == "ccc":
            assert pd.to_datetime(d["decision_date"]) <= delisted


def test_reproducibility_same_seed_same_result(conn):
    _seed_db(conn)
    universe = _universe()
    bt_cfg = cfg.load_backtest_config()
    strat = cfg.load_strategy("trend_momentum")
    instruments, bench = engine.load_instruments(conn, universe, "wig20tr")

    r1 = engine.run_walk_forward(instruments, bench, strat, bt_cfg)
    r2 = engine.run_walk_forward(instruments, bench, strat, bt_cfg)

    assert r1.metrics == r2.metrics
    assert r1.trade_pnls == r2.trade_pnls
    pd.testing.assert_series_equal(r1.equity_curve, r2.equity_curve)


def test_walk_forward_windows_roll_forward():
    cal = pd.bdate_range("2015-01-01", "2021-01-01")
    windows = engine.make_walk_forward_windows(cal, is_months=24, oos_months=6)
    assert len(windows) >= 2
    for w in windows:
        assert w.is_start < w.is_end == w.oos_start < w.oos_end
    # OOS segments advance in time
    for a, b in zip(windows, windows[1:]):
        assert b.oos_start > a.oos_start

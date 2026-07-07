"""A3: the benchmark curve must never fabricate pre-history.

WIG20TR only exists from ~2004; instrument history can go back to 1995. A
back-filled benchmark would show a flat zero-return segment made of FUTURE
values, understating benchmark CAGR/Sharpe over the whole run and inflating
every strategy-vs-WIG20TR verdict. The engine instead clamps the measured
span to the benchmark's first bar and flags the clamp in metrics.
"""
import pandas as pd
import pytest

from app import config as cfg
from app.backtest import engine
from app.ingestion import stooq

from tests.conftest import make_stooq_csv, synthetic_series


def _ingest(conn, ticker, rows, **inst):
    iid = stooq.upsert_instrument(conn, {"ticker": ticker, "name": ticker, **inst},
                                  is_index=inst.get("is_index", False))
    stooq.store_bars(conn, iid, stooq.parse_csv(make_stooq_csv(rows)))
    return iid


def _universe():
    return {"benchmark": {"ticker": "wig20tr", "is_index": True}, "indices": [],
            "instruments": [{"ticker": "aaa", "sector": "tech"}]}


def test_backtest_clamps_calendar_to_benchmark_start(conn):
    # instrument history starts ~250 sessions BEFORE the benchmark exists
    inst_rows = synthetic_series(n=500, base=100, drift=0.0008)
    bench_rows = synthetic_series(n=500, base=2000, drift=0.0005)[250:]
    _ingest(conn, "aaa", inst_rows, sector="tech", listed_from="2018-01-01")
    _ingest(conn, "wig20tr", bench_rows, is_index=True)
    conn.commit()

    instruments, bench = engine.load_instruments(conn, _universe(), "wig20tr")
    bt_cfg = cfg.load_backtest_config()
    strat = cfg.load_strategy("trend_momentum")
    result = engine.run_backtest(instruments, bench, strat, bt_cfg)

    first_bench = bench.index[0]
    assert result.equity_curve.index[0] >= first_bench
    assert result.metrics["oos_start_clamped_to_benchmark"] == \
        first_bench.date().isoformat()
    # the aligned benchmark curve holds no fabricated flat prefix: its first
    # value is the real first bar, normalized to capital
    assert result.benchmark_curve.iloc[0] == pytest.approx(100000.0)
    assert result.benchmark_curve.index.equals(result.equity_curve.index)


def test_no_clamp_flag_when_benchmark_covers_span(conn):
    _ingest(conn, "aaa", synthetic_series(n=300, base=100, drift=0.0008),
            sector="tech", listed_from="2018-01-01")
    _ingest(conn, "wig20tr", synthetic_series(n=300, base=2000, drift=0.0005),
            is_index=True)
    conn.commit()
    instruments, bench = engine.load_instruments(conn, _universe(), "wig20tr")
    result = engine.run_backtest(instruments, bench,
                                 cfg.load_strategy("trend_momentum"),
                                 cfg.load_backtest_config())
    assert "oos_start_clamped_to_benchmark" not in result.metrics


def test_benchmark_helper_refuses_leading_gap():
    bench = pd.Series([100.0, 101.0],
                      index=pd.DatetimeIndex(["2020-01-10", "2020-01-11"]))
    span = pd.DatetimeIndex(["2020-01-01", "2020-01-10", "2020-01-11"])
    with pytest.raises(ValueError, match="benchmark history starts after"):
        engine._benchmark_buy_and_hold(bench, span, 100000.0)

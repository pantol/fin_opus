"""Feature correctness on known inputs."""
import numpy as np
import pandas as pd

from app.features import compute


def _df(closes, highs=None, lows=None):
    n = len(closes)
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    highs = highs or [c * 1.0 for c in closes]
    lows = lows or [c * 1.0 for c in closes]
    return pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes, "volume": [1000] * n},
        index=idx,
    )


def test_returns_known_value():
    df = _df([100.0, 110.0, 121.0])
    r = compute.returns(df["close"], 1)
    assert r.iloc[1] == pytest_approx(0.10)
    assert r.iloc[2] == pytest_approx(0.10)


def test_sma_known_value():
    df = _df([1.0, 2.0, 3.0, 4.0, 5.0])
    s = compute.sma(df["close"], 3)
    assert np.isnan(s.iloc[1])      # not enough data
    assert s.iloc[2] == pytest_approx(2.0)
    assert s.iloc[4] == pytest_approx(4.0)


def test_atr_known_value():
    # constant 2.0 high-low range, no gaps -> ATR == 2.0
    closes = [10.0] * 20
    highs = [11.0] * 20
    lows = [9.0] * 20
    df = _df(closes, highs, lows)
    a = compute.atr(df, window=14)
    assert a.iloc[-1] == pytest_approx(2.0)


def test_close_vs_sma_sign():
    closes = list(range(1, 260))  # strictly rising
    df = _df([float(c) for c in closes])
    feats = compute.compute_features(df)
    # rising series: close above its SMA200 -> positive distance
    assert feats["close_vs_sma200"].iloc[-1] > 0
    assert feats["momentum_6m"].iloc[-1] > 0


def test_rel_strength_vs_benchmark():
    closes = [100.0 * (1.01 ** i) for i in range(260)]   # strong up
    df = _df(closes)
    bench = pd.Series([100.0 * (1.005 ** i) for i in range(260)], index=df.index)
    feats = compute.compute_features(df, benchmark_close=bench)
    assert feats["rel_strength_6m"].iloc[-1] > 0  # outperforming benchmark


# tiny local approx helper to avoid extra import noise
def pytest_approx(value, tol=1e-9):
    import pytest

    return pytest.approx(value, abs=tol)

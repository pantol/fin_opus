"""Deterministic, point-in-time quant features.

All features are pure functions of a price history DataFrame indexed by date and
sorted ascending. Rolling windows use only PAST and CURRENT bars (pandas rolling
is backward-looking), so a feature value at row T depends only on rows <= T.

Point-in-time loading (`load_prices_asof`) additionally enforces, at the SQL
level, that no row with `as_of_date > T` is ever read.

Approximate trading-day counts for GPW horizons:
    1M ~ 21, 3M ~ 63, 6M ~ 126, 12M ~ 252.
"""
from __future__ import annotations

import pandas as pd

TD_1M = 21
TD_3M = 63
TD_6M = 126
TD_12M = 252

SMA_FAST = 50
SMA_SLOW = 200
ATR_WINDOW = 14
VOL_WINDOW = 21  # realized volatility window (~1M)

PRICE_COLS = ["open", "high", "low", "close", "volume"]


def load_prices_asof(conn, instrument_id: int, as_of: str, adjusted: bool = False) -> pd.DataFrame:
    """Load a price history for an instrument using ONLY rows available by `as_of`.

    Enforces point-in-time correctness at the data boundary: WHERE as_of_date <= as_of.
    Returns a DataFrame indexed by date (ascending), or empty if none.
    """
    rows = conn.execute(
        """
        SELECT date, open, high, low, close, volume
        FROM prices
        WHERE instrument_id = ? AND adjusted = ? AND as_of_date <= ?
        ORDER BY date ASC
        """,
        (instrument_id, 1 if adjusted else 0, as_of),
    ).fetchall()
    if not rows:
        return pd.DataFrame(columns=PRICE_COLS, index=pd.DatetimeIndex([], name="date"))
    df = pd.DataFrame(rows, columns=["date"] + PRICE_COLS)
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date").sort_index()


def returns(close: pd.Series, window: int) -> pd.Series:
    """Simple return over `window` bars: close_t / close_{t-window} - 1."""
    return close / close.shift(window) - 1.0


def sma(close: pd.Series, window: int) -> pd.Series:
    return close.rolling(window, min_periods=window).mean()


def atr(df: pd.DataFrame, window: int = ATR_WINDOW) -> pd.Series:
    """Average True Range (Wilder-style via simple rolling mean of true range)."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(window, min_periods=window).mean()


def realized_vol(close: pd.Series, window: int = VOL_WINDOW) -> pd.Series:
    """Annualized realized volatility of daily log-ish (simple) returns."""
    daily = close.pct_change()
    return daily.rolling(window, min_periods=window).std() * (252 ** 0.5)


def compute_features(df: pd.DataFrame, benchmark_close: pd.Series | None = None) -> pd.DataFrame:
    """Compute the full feature panel for one instrument.

    `benchmark_close` (aligned by date) enables relative strength vs WIG/benchmark.
    Every column at row T is a function of rows <= T only (no look-ahead).
    """
    out = pd.DataFrame(index=df.index)
    close = df["close"]

    out["close"] = close
    out["ret_1m"] = returns(close, TD_1M)
    out["ret_3m"] = returns(close, TD_3M)
    out["ret_6m"] = returns(close, TD_6M)
    out["ret_12m"] = returns(close, TD_12M)

    out["momentum_6m"] = returns(close, TD_6M)  # alias used by strategy config

    out["sma50"] = sma(close, SMA_FAST)
    out["sma200"] = sma(close, SMA_SLOW)
    out["close_vs_sma50"] = close / out["sma50"] - 1.0
    out["close_vs_sma200"] = close / out["sma200"] - 1.0

    out["atr"] = atr(df, ATR_WINDOW)
    out["realized_vol"] = realized_vol(close, VOL_WINDOW)

    if benchmark_close is not None and not benchmark_close.empty:
        bench = benchmark_close.reindex(df.index).ffill()
        # Relative strength: 6M instrument return minus 6M benchmark return.
        bench_mom_6m = returns(bench, TD_6M)
        out["rel_strength_6m"] = out["ret_6m"] - bench_mom_6m
    else:
        out["rel_strength_6m"] = pd.NA

    return out


def features_at(features: pd.DataFrame, decision_date: str) -> dict | None:
    """Return the feature row for the last available bar on/before `decision_date`.

    Returns None if no bar is available or the row has unresolved core features.
    """
    ts = pd.to_datetime(decision_date)
    eligible = features.loc[features.index <= ts]
    if eligible.empty:
        return None
    row = eligible.iloc[-1]
    snapshot = {k: (None if pd.isna(v) else float(v)) for k, v in row.items()}
    snapshot["bar_date"] = eligible.index[-1].date().isoformat()
    return snapshot

"""Point-in-time guarantee: a feature computed for T uses no as_of_date > T."""
import pandas as pd

from app.features import compute
from app.ingestion import stooq

from tests.conftest import make_stooq_csv, synthetic_series


def _ingest(conn, ticker, rows):
    inst_id = stooq.upsert_instrument(conn, {"ticker": ticker, "name": ticker})
    bars = stooq.parse_csv(make_stooq_csv(rows))
    stooq.store_bars(conn, inst_id, bars)
    conn.commit()
    return inst_id


def test_load_prices_asof_excludes_future_rows(conn):
    rows = synthetic_series(n=300)
    inst_id = _ingest(conn, "tst", rows)

    cutoff = rows[150][0]  # a date in the middle
    df = compute.load_prices_asof(conn, inst_id, as_of=cutoff)

    # no bar later than the cutoff is loaded
    assert df.index.max() <= pd.to_datetime(cutoff)
    assert len(df) == 151  # rows 0..150 inclusive


def test_feature_at_T_unaffected_by_future_data(conn):
    """Compute a feature using full history vs history-as-of-T; values at T match.

    This is the core anti-look-ahead assertion: adding FUTURE bars must not change
    a feature value computed at an earlier decision date.
    """
    rows = synthetic_series(n=300)
    inst_id = _ingest(conn, "tst", rows)

    cutoff = rows[250][0]

    # (a) features from data available only up to cutoff
    df_asof = compute.load_prices_asof(conn, inst_id, as_of=cutoff)
    feats_asof = compute.compute_features(df_asof)
    snap_asof = compute.features_at(feats_asof, cutoff)

    # (b) features from the FULL history (includes future bars 251..299)
    df_full = compute.load_prices_asof(conn, inst_id, as_of=rows[-1][0])
    feats_full = compute.compute_features(df_full)
    snap_full = compute.features_at(feats_full, cutoff)

    assert snap_asof["bar_date"] == snap_full["bar_date"]
    for key in ("close", "sma200", "momentum_6m", "atr", "close_vs_sma200"):
        a, b = snap_asof[key], snap_full[key]
        if a is None and b is None:
            continue
        assert a == b, f"look-ahead leak in {key}: {a} != {b}"


def test_features_at_returns_none_before_any_bar(conn):
    rows = synthetic_series(n=50)
    inst_id = _ingest(conn, "tst", rows)
    df = compute.load_prices_asof(conn, inst_id, as_of=rows[-1][0])
    feats = compute.compute_features(df)
    assert compute.features_at(feats, "2010-01-01") is None

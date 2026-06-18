"""Smoke tests: scaffold imports, config loads, DB bootstraps."""
from app import config
from app.db import connect, init_db


def test_db_bootstrap_creates_all_tables():
    conn = connect(":memory:")
    init_db(conn)
    tables = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    expected = {
        "instruments",
        "prices",
        "strategies",
        "decisions",
        "positions",
        "trades",
        "equity_curve",
    }
    assert expected.issubset(tables)


def test_configs_load_and_have_user_id_seam():
    bt = config.load_backtest_config()
    assert "user_id" in bt  # multi-tenant seam
    assert bt["initial_capital"] > 0
    strat = config.load_strategy("trend_momentum")
    assert strat["name"] == "trend_momentum"
    assert "risk" in strat


def test_universe_includes_delisted_tickers():
    uni = config.load_universe()
    delisted = [i for i in uni["instruments"] if i.get("delisted_on")]
    assert len(delisted) >= 3  # anti-survivorship

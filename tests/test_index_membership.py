"""Pack A.2: point-in-time index membership.

The backtest universe for date T = members as of T. A stock that joined the
index in year Y must be absent from the tradable universe before Y; former
members keep their historical ranges (anti-survivorship).
"""
import pandas as pd

from app import config as cfg
from app.backtest import engine
from app.ingestion import refdata, stooq

from tests.conftest import make_stooq_csv, synthetic_series


def _ingest(conn, ticker, rows, **inst):
    iid = stooq.upsert_instrument(conn, {"ticker": ticker, "name": ticker, **inst},
                                  is_index=inst.get("is_index", False))
    stooq.store_bars(conn, iid, stooq.parse_csv(make_stooq_csv(rows)), source="stooq")
    return iid


def test_membership_loader_upserts_and_reports_unknown_tickers(conn):
    _ingest(conn, "aaa", synthetic_series(n=10), sector="tech")
    conn.commit()
    membership_cfg = {"indices": {"wig20": [
        {"ticker": "aaa", "date_from": "2019-06-01", "source": "test"},
        {"ticker": "ghost", "date_from": "2019-06-01", "source": "test"},
    ]}}
    report = refdata.RefdataReport()
    refdata.load_index_membership(conn, membership_cfg, report)
    assert report.membership_rows == 1
    assert any("ghost" in f for f in report.failures)

    # idempotent: reloading does not duplicate
    report2 = refdata.RefdataReport()
    refdata.load_index_membership(conn, membership_cfg, report2)
    n = conn.execute("SELECT COUNT(*) FROM index_membership").fetchone()[0]
    assert n == 1


def test_member_on_ranges():
    ranges = [(pd.Timestamp("2019-06-01"), pd.Timestamp("2021-03-19")),
              (pd.Timestamp("2023-01-02"), None)]
    assert not engine._member_on(ranges, pd.Timestamp("2019-05-31"))
    assert engine._member_on(ranges, pd.Timestamp("2019-06-01"))
    assert engine._member_on(ranges, pd.Timestamp("2021-03-19"))
    assert not engine._member_on(ranges, pd.Timestamp("2022-01-01"))
    assert engine._member_on(ranges, pd.Timestamp("2024-01-01"))
    assert not engine._member_on(None, pd.Timestamp("2024-01-01"))
    assert not engine._member_on([], pd.Timestamp("2024-01-01"))


def test_stock_absent_from_universe_before_membership(conn):
    """No ENTER before date_from; entries do happen after it."""
    rows = synthetic_series(n=400, base=100, drift=0.0009, start="2018-01-01")
    _ingest(conn, "wig20tr", synthetic_series(n=400, base=2000, drift=0.0005,
                                              start="2018-01-01"), is_index=True)
    iid = _ingest(conn, "aaa", rows, sector="tech", listed_from="2018-01-01")
    conn.commit()

    join_date = rows[200][0]  # joins the index halfway through the sample
    conn.execute(
        "INSERT INTO index_membership (index_name, instrument_id, date_from, date_to, source)"
        " VALUES ('wig20', ?, ?, NULL, 'test')", (iid, join_date))
    conn.commit()

    uni = {"benchmark": {"ticker": "wig20tr", "is_index": True}, "indices": [],
           "instruments": [{"ticker": "aaa", "sector": "tech"}]}
    strat = {
        "name": "always", "version": 1,
        "entry": {"all": [{"feature": "close", "op": "gt", "value": 0}]},
        "exit": {"any": [{"type": "atr_stop", "atr_mult": 2.5}]},
        "risk": {"risk_per_trade": 0.01, "atr_mult_stop": 2.5,
                 "max_open_positions": 8, "max_exposure_per_name": 0.20,
                 "max_total_exposure": 1.0, "drawdown_circuit_breaker": 0.25},
    }
    bt_cfg = cfg.load_backtest_config()
    instruments, bench = engine.load_instruments(conn, uni, "wig20tr")
    membership = engine.load_membership_map(conn, "wig20")

    gated = engine.run_backtest(instruments, bench, strat, bt_cfg,
                                membership=membership)
    enters = [d for d in gated.decisions if d["action"] == "ENTER"]
    assert enters, "expected entries after the join date"
    assert all(d["decision_date"] >= join_date for d in enters), (
        "an instrument was traded before it joined the index"
    )

    # control: without the membership gate the first entry is much earlier
    ungated = engine.run_backtest(instruments, bench, strat, bt_cfg)
    first_ungated = min(d["decision_date"] for d in ungated.decisions
                        if d["action"] == "ENTER")
    assert first_ungated < join_date

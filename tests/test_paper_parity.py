"""Paper loop vs backtest engine: byte-level parity on the same data.

THE drift alarm for the live loop: the paper loop must produce the same
trades, equity, cash and final book as engine.run_backtest over an identical
span, because it IS the same money code driven session-by-session. Any future
engine change not reflected in the loop (or vice versa) turns this red.

Fixture deliberately exercises the hairy paths: a volume-capped instrument
(partial SELL remainders re-queued across sessions), an instrument with a
mid-span suspension gap (no-bar marking, union calendar), dividends and a
split landing while positions are held.

Documented divergences (excluded from assertions):
- the engine suppresses NEW signals on the last calendar day (no future bar to
  fill on) and force-closes the book at the end; the live loop queues signals
  as PENDING orders and never force-closes — so equity/cash are compared
  through the second-to-last session and trades via decisions_log only (the
  forced close never enters decisions_log).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import config as cfg
from app.backtest import engine
from app.ingestion import stooq
from app.logging import decisions as declog
from app.paper import loop, store

from tests.conftest import make_stooq_csv, synthetic_series

N = 450  # > SMA200 + momentum_6m warmup, small enough to stay fast


def _ingest(conn, ticker, rows, **inst):
    iid = stooq.upsert_instrument(conn, {"ticker": ticker, "name": ticker, **inst},
                                  is_index=inst.get("is_index", False))
    stooq.store_bars(conn, iid, stooq.parse_csv(make_stooq_csv(rows)), source="stooq")
    return iid


def _two_phase(n=N, base=100.0, drift=0.0012, turn_at=330, down=-0.004,
               volume=None):
    """Trend up, then decline after `turn_at` so trend-break/ATR exits fire."""
    rows = synthetic_series(n=n, base=base, drift=drift)
    out = list(rows[:turn_at])
    price = rows[turn_at - 1][4]
    for i, (d, _o, _h, _l, _c, v) in enumerate(rows[turn_at:]):
        price = price * (1 + down)
        out.append((d, round(price * 0.999, 4), round(price * 1.01, 4),
                    round(price * 0.99, 4), round(price, 4), v))
    if volume is not None:
        out = [(d, o, h, l, c, volume(i)) for i, (d, o, h, l, c, _v) in enumerate(out)]
    return out


def _seed_market(conn):
    """Benchmark + 3 instruments: plain trend, low-volume, suspension gap."""
    _ingest(conn, "wig20tr", synthetic_series(n=N, base=2000, drift=0.0005),
            is_index=True)
    up_id = _ingest(conn, "up", synthetic_series(n=N, base=100, drift=0.0012),
                    sector="tech", listed_from="2018-01-01")
    # Alternating thin volume (10% participation cap => 90/30 shares per fill)
    # plus a 2:1 split while held: the post-split exit quantity exceeds the
    # cap, forcing partial SELLs with re-queued remainders.
    lowvol = _two_phase(base=60, drift=0.0010, turn_at=330,
                        volume=lambda i: 900.0 if i % 2 == 0 else 300.0)
    lowvol_id = _ingest(conn, "lowvol", lowvol, sector="banking",
                        listed_from="2018-01-01")
    # Suspension: no bars for ~10 sessions mid-span while held; declines later.
    gappy = _two_phase(base=80, drift=0.0011, turn_at=380)
    gappy = gappy[:260] + gappy[270:]
    _ingest(conn, "gappy", gappy, sector="mining", listed_from="2018-01-01")
    # Volume shaped to force both engine requeue paths deterministically
    # (10% participation cap; 5.0 volume => 0 fillable shares):
    #   - thin during [200, 250): the first BUY attempts (signals start right
    #     after the ~200-session warmup) zero-fill => LAPSED, until the entry
    #     fills once volume returns at 250;
    #   - thin again from the decline at 340: the exit SELL zero-fills and
    #     re-queues session after session => REQUEUED chain.
    thinvol = _two_phase(base=40, drift=0.0011, turn_at=340,
                         volume=lambda i: 5.0 if (200 <= i < 250 or i >= 340)
                         else 900.0)
    _ingest(conn, "thinvol", thinvol, sector="retail", listed_from="2018-01-01")

    # Corporate actions landing well after the ~200-session warmup, while the
    # positions are held: dividends on the trend name, a split on lowvol.
    all_dates = [r[0] for r in synthetic_series(n=N, base=1, drift=0.0)]
    for ex in (all_dates[240], all_dates[300], all_dates[360]):
        conn.execute(
            "INSERT INTO corporate_actions (instrument_id, action_type, ex_date,"
            " value_or_ratio, source) VALUES (?, 'dividend', ?, 1.5, 'test')",
            (up_id, ex))
    conn.execute(
        "INSERT INTO corporate_actions (instrument_id, action_type, ex_date,"
        " value_or_ratio, source) VALUES (?, 'split', ?, 2.0, 'test')",
        (lowvol_id, all_dates[300]))
    conn.commit()


def _universe():
    return {
        "benchmark": {"ticker": "wig20tr", "is_index": True},
        "indices": [],
        "instruments": [{"ticker": "up", "sector": "tech"},
                        {"ticker": "lowvol", "sector": "banking"},
                        {"ticker": "gappy", "sector": "mining"},
                        {"ticker": "thinvol", "sector": "retail"}],
    }


@pytest.fixture
def market(conn):
    _seed_market(conn)
    bt_cfg = dict(cfg.load_backtest_config())
    bt_cfg["paper"] = {"catchup_max_sessions": 100000}
    strat = cfg.load_strategy("trend_momentum")
    instruments, bench = engine.load_instruments(conn, _universe(), "wig20tr")
    return conn, bt_cfg, strat, instruments, bench


def _run_both(market):
    conn, bt_cfg, strat, instruments, bench = market
    calendar = engine._trading_calendar(instruments)

    result = engine.run_backtest(instruments, bench, strat, bt_cfg)
    assert result.decisions, "fixture must generate trades or the test proves nothing"
    sells = [d for d in result.decisions if d["action"] == "EXIT"]
    partial_tickers = {d["ticker"] for d in sells if d["ticker"] == "lowvol"}
    assert partial_tickers, "fixture must exercise volume-capped sells"

    # Paper side: seed the watermark before the first session, replay everything.
    user_id = store.paper_user_id(bt_cfg["user_id"])
    strategy_id = declog.register_strategy(conn, strat["name"],
                                           int(strat["version"]), "test")
    store.init_state(
        conn, user_id=user_id, initial_capital=float(bt_cfg["initial_capital"]),
        inception_date=calendar[0].date().isoformat(),
        last_settled_date=(calendar[0].date() - timedelta(days=1)).isoformat(),
        strategy_id=strategy_id,
        config_hash=loop.config_hash(strat, bt_cfg, _universe()),
    )
    conn.commit()
    now = datetime.fromisoformat(calendar[-1].date().isoformat()) + timedelta(hours=19)
    code, report = loop.run_signals(
        conn, universe=_universe(), bt_cfg=bt_cfg, strategy_cfg=strat,
        now=now, send_fn=None,
    )
    assert code == 0, report.as_text()
    assert len(report.sessions) == len(calendar)
    return conn, result, calendar, user_id


def test_trades_are_identical(market):
    conn, result, calendar, user_id = _run_both(market)
    paper_trades = conn.execute(
        "SELECT t.trade_date, i.ticker, t.side, t.qty, t.price, t.fee, t.slippage"
        " FROM trades t JOIN instruments i ON i.id = t.instrument_id"
        " WHERE t.user_id = ? ORDER BY t.id", (user_id,)).fetchall()
    engine_trades = [
        (d["fill_date"], d["ticker"], "BUY" if d["action"] == "ENTER" else "SELL",
         d["qty"], d["price"], d["fee"], d["slippage"])
        for d in result.decisions
    ]
    assert len(paper_trades) == len(engine_trades)
    for got, want in zip(paper_trades, engine_trades):
        assert (got["trade_date"], got["ticker"], got["side"]) == want[:3]
        assert got["qty"] == want[3]
        assert got["price"] == pytest.approx(want[4], abs=1e-12)
        assert got["fee"] == pytest.approx(want[5], abs=1e-12)
        assert got["slippage"] == pytest.approx(want[6], abs=1e-12)


def test_equity_cash_exposure_are_identical(market):
    conn, result, calendar, user_id = _run_both(market)
    rows = conn.execute(
        "SELECT date, equity, cash, exposure FROM equity_curve"
        " WHERE user_id = ? ORDER BY date", (user_id,)).fetchall()
    assert len(rows) == len(result.equity_curve)
    # Exclude the last session: the engine's end-of-run forced close rewrites
    # its final equity/cash record; the live book has no such event.
    for row in rows[:-1]:
        day = row["date"]
        ts = engine.pd.to_datetime(day)
        assert row["equity"] == pytest.approx(result.equity_curve[ts], abs=1e-9)
        assert row["cash"] == pytest.approx(result.cash_curve[ts], abs=1e-9)
        assert row["exposure"] == pytest.approx(result.exposure_curve[ts], abs=1e-9)


def test_final_book_matches_engine_net(market):
    conn, result, calendar, user_id = _run_both(market)
    net: dict[str, int] = {}
    for d in result.decisions:
        sign = 1 if d["action"] == "ENTER" else -1
        net[d["ticker"]] = net.get(d["ticker"], 0) + sign * int(d["qty"])
    # engine corporate-action rebasing (the split) also rescaled held qty; the
    # paper book applied the same rebase, so compare against the DB directly:
    open_rows = conn.execute(
        "SELECT i.ticker, p.qty, p.stop_price FROM positions p"
        " JOIN instruments i ON i.id = p.instrument_id"
        " WHERE p.user_id = ? AND p.status = 'OPEN' ORDER BY p.id",
        (user_id,)).fetchall()
    open_tickers = {r["ticker"] for r in open_rows}
    engine_open = {tk for tk, q in net.items() if q > 0}
    assert open_tickers == engine_open
    for r in open_rows:
        assert r["qty"] > 0
        assert r["stop_price"] > 0
        if r["ticker"] != "lowvol":  # lowvol's held qty was rebased by the split
            assert r["qty"] == net[r["ticker"]]


def test_lapsed_and_partial_orders_are_recorded(market):
    conn, result, calendar, user_id = _run_both(market)
    statuses = {r["status"] for r in conn.execute(
        "SELECT status FROM paper_orders WHERE user_id = ?", (user_id,)).fetchall()}
    # lowvol forces PARTIAL sells; thinvol forces BUY volume-lapses and
    # zero-fill SELL requeues; the final session's signals stay PENDING
    assert "PARTIAL" in statuses
    assert "FILLED" in statuses
    assert "LAPSED" in statuses
    assert "REQUEUED" in statuses

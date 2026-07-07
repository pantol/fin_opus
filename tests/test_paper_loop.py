"""Operational behavior of the daily paper loop (fast, tiny fixtures).

Covers: bootstrap, idempotent re-runs, staleness/coverage/config-change/catch-up
refusals, lapse on a missing fill bar, dry-run rollback, split-across-evenings
equivalence, namespace isolation, and alert delivery bookkeeping.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import config as cfg
from app.backtest import engine
from app.ingestion import stooq
from app.paper import loop, store

from tests.conftest import make_stooq_csv, synthetic_series


def _ingest(conn, ticker, rows, **inst):
    iid = stooq.upsert_instrument(conn, {"ticker": ticker, "name": ticker, **inst},
                                  is_index=inst.get("is_index", False))
    stooq.store_bars(conn, iid, stooq.parse_csv(make_stooq_csv(rows)))
    return iid


def _universe(tickers=("aaa", "bbb")):
    return {
        "benchmark": {"ticker": "wig20tr", "is_index": True},
        "indices": [],
        "instruments": [{"ticker": t, "sector": "tech"} for t in tickers],
    }


def _strategy():
    """Always-enter / never-exit toy strategy: fires from the first bar."""
    return {
        "name": "paper_toy", "version": 1,
        "entry": {"all": [{"feature": "close", "op": "gt", "value": 0.0}]},
        "exit": {"any": [{"feature": "close", "op": "lt", "value": 0.0}]},
        "risk": {
            "risk_per_trade": 0.01, "atr_mult_stop": 2.5, "max_open_positions": 8,
            "max_exposure_per_name": 0.20, "max_exposure_per_sector": 0.40,
            "max_total_exposure": 1.0, "drawdown_circuit_breaker": 0.25,
        },
    }


def _bt_cfg(**paper):
    c = dict(cfg.load_backtest_config())
    c["paper"] = paper
    return c


def _seed(conn, n=60):
    _ingest(conn, "wig20tr", synthetic_series(n=n, base=2000, drift=0.0), is_index=True)
    _ingest(conn, "aaa", synthetic_series(n=n, base=100, drift=0.0008),
            sector="tech", listed_from="2018-01-01")
    _ingest(conn, "bbb", synthetic_series(n=n, base=50, drift=0.0006),
            sector="tech", listed_from="2018-01-01")
    conn.commit()
    instruments, _ = engine.load_instruments(conn, _universe(), "wig20tr")
    return engine._trading_calendar(instruments)


def _now_at(day):
    return datetime.fromisoformat(day.date().isoformat()) + timedelta(hours=19)


def _run(conn, *, calendar=None, day=None, sent=None, **kw):
    day = day if day is not None else calendar[-1]
    kw.setdefault("universe", _universe())
    kw.setdefault("bt_cfg", _bt_cfg())
    kw.setdefault("strategy_cfg", _strategy())
    kw.setdefault("now", _now_at(day))
    kw.setdefault("session_end", day.date().isoformat())
    send_fn = None if sent is None else sent.append
    return loop.run_signals(conn, send_fn=send_fn, **kw)


def test_bootstrap_processes_only_latest_session(conn):
    calendar = _seed(conn)
    sent = []
    code, rep = _run(conn, calendar=calendar, sent=sent)
    assert code == 0 and rep.status == "ok"
    assert [s.date for s in rep.sessions] == [calendar[-1].date().isoformat()]
    # ENTER signals queued for both instruments, nothing filled yet
    assert {o["ticker"] for o in rep.sessions[0].new_orders} == {"aaa", "bbb"}
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    state = store.load_state(conn, "paper:default")
    assert state["last_settled_date"] == calendar[-1].date().isoformat()
    # 2 signal cards + summary
    assert len(sent) == 3


def test_rerun_same_evening_is_noop(conn):
    calendar = _seed(conn)
    _run(conn, calendar=calendar)
    n_orders = conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0]
    code, rep = _run(conn, calendar=calendar)
    assert code == 0 and rep.status == "noop"
    assert conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0] == n_orders
    assert conn.execute(
        "SELECT COUNT(*) FROM equity_curve WHERE user_id='paper:default'"
    ).fetchone()[0] == 1


def test_next_evening_settles_at_open_and_updates_positions(conn):
    calendar = _seed(conn)
    _run(conn, calendar=calendar, day=calendar[-2])
    code, rep = _run(conn, calendar=calendar, day=calendar[-1])
    assert code == 0
    fills = rep.sessions[0].fills
    assert {f["ticker"] for f in fills} == {"aaa", "bbb"}
    rows = conn.execute(
        "SELECT * FROM positions WHERE user_id='paper:default' AND status='OPEN'"
    ).fetchall()
    assert len(rows) == 2
    # decisions + trades persisted and linked
    for t in conn.execute("SELECT * FROM trades WHERE user_id='paper:default'"):
        assert t["decision_id"] is not None
        assert t["side"] == "BUY"


def test_staleness_gate_refuses(conn):
    calendar = _seed(conn)
    code, rep = _run(conn, calendar=calendar,
                     now=_now_at(calendar[-1]) + timedelta(days=30))
    assert code == 2 and rep.status == "refused"
    assert "days old" in rep.reason
    assert store.load_state(conn, "paper:default") is None  # no writes


def test_coverage_gate_refuses_partial_ingest(conn):
    n = 60
    _ingest(conn, "wig20tr", synthetic_series(n=n, base=2000, drift=0.0), is_index=True)
    # aaa printed the last session; bbb and ccc stop 1 session earlier -> 1/3
    _ingest(conn, "aaa", synthetic_series(n=n, base=100, drift=0.0008),
            sector="tech", listed_from="2018-01-01")
    short = synthetic_series(n=n, base=50, drift=0.0006)[:-1]
    _ingest(conn, "bbb", short, sector="tech", listed_from="2018-01-01")
    _ingest(conn, "ccc", short, sector="tech", listed_from="2018-01-01")
    conn.commit()
    uni = _universe(("aaa", "bbb", "ccc"))
    instruments, _ = engine.load_instruments(conn, uni, "wig20tr")
    calendar = engine._trading_calendar(instruments)
    code, rep = _run(conn, calendar=calendar, universe=uni)
    assert code == 2 and rep.status == "refused"
    assert "printed a bar" in rep.reason


def test_config_change_refused_then_accepted(conn):
    calendar = _seed(conn)
    _run(conn, calendar=calendar, day=calendar[-2])
    changed = _strategy()
    changed["risk"]["risk_per_trade"] = 0.02
    code, rep = _run(conn, calendar=calendar, strategy_cfg=changed)
    assert code == 2 and "config changed" in rep.reason
    code, rep = _run(conn, calendar=calendar, strategy_cfg=changed,
                     accept_config_change=True)
    assert code == 0 and rep.status == "ok"
    assert any("config change accepted" in w for w in rep.warnings)


def test_catchup_cap_refuses_big_gaps(conn):
    calendar = _seed(conn)
    _run(conn, calendar=calendar, day=calendar[0],
         bt_cfg=_bt_cfg(catchup_max_sessions=5))
    code, rep = _run(conn, calendar=calendar,
                     bt_cfg=_bt_cfg(catchup_max_sessions=5))
    assert code == 2 and "catchup_max_sessions" in rep.reason


def test_catchup_equals_daily_runs(conn):
    """Processing 3 sessions in one call == processing them evening by evening."""
    calendar = _seed(conn)
    days = [calendar[-4], calendar[-3], calendar[-2], calendar[-1]]
    # book A: one catch-up call
    _run(conn, calendar=calendar, day=days[0])
    code, rep = _run(conn, calendar=calendar, day=days[-1])
    assert code == 0 and len(rep.sessions) == 3
    a = conn.execute(
        "SELECT date, equity, cash FROM equity_curve WHERE user_id='paper:default'"
        " ORDER BY date").fetchall()
    a_state = dict(store.load_state(conn, "paper:default"))

    # book B (fresh DB): one call per evening
    from app.db import connect, init_db
    conn2 = connect(":memory:")
    init_db(conn2)
    _seed(conn2)
    for d in days:
        code, _ = _run(conn2, calendar=calendar, day=d)
        assert code == 0
    b = conn2.execute(
        "SELECT date, equity, cash FROM equity_curve WHERE user_id='paper:default'"
        " ORDER BY date").fetchall()
    b_state = dict(store.load_state(conn2, "paper:default"))
    assert [(r["date"], r["equity"], r["cash"]) for r in a] == \
           [(r["date"], r["equity"], r["cash"]) for r in b]
    assert a_state["cash"] == b_state["cash"]
    assert a_state["peak_equity"] == b_state["peak_equity"]
    conn2.close()


def test_order_lapses_when_fill_bar_missing(conn):
    n = 60
    _ingest(conn, "wig20tr", synthetic_series(n=n, base=2000, drift=0.0), is_index=True)
    _ingest(conn, "aaa", synthetic_series(n=n, base=100, drift=0.0008),
            sector="tech", listed_from="2018-01-01")
    # bbb is suspended on the final session (no bar to fill on)
    _ingest(conn, "bbb", synthetic_series(n=n, base=50, drift=0.0006)[:-1],
            sector="tech", listed_from="2018-01-01")
    conn.commit()
    instruments, _ = engine.load_instruments(conn, _universe(), "wig20tr")
    calendar = engine._trading_calendar(instruments)
    sent = []
    _run(conn, calendar=calendar, day=calendar[-2], sent=sent)
    code, rep = _run(conn, calendar=calendar, day=calendar[-1], sent=sent)
    assert code == 0
    lapses = rep.sessions[0].lapses
    assert [(l["ticker"], l["reason"]) for l in lapses] == [("bbb", "no_bar")]
    row = conn.execute(
        "SELECT status, lapse_reason FROM paper_orders o JOIN instruments i"
        " ON i.id = o.instrument_id WHERE i.ticker='bbb'").fetchone()
    assert (row["status"], row["lapse_reason"]) == ("LAPSED", "no_bar")
    assert any("NIE ZREALIZOWANO" in c for c in sent)


def test_dry_run_rolls_back_everything(conn):
    calendar = _seed(conn)
    sent = []
    code, rep = _run(conn, calendar=calendar, dry_run=True, sent=sent)
    assert code == 0 and rep.status == "ok" and rep.dry_run
    assert rep.sessions and rep.sessions[0].new_orders
    for table in ("paper_state", "paper_orders", "decisions", "trades"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert sent == []  # no alerts on dry-run


def test_paper_namespace_is_isolated(conn):
    calendar = _seed(conn)
    _run(conn, calendar=calendar)
    users_o = {r[0] for r in conn.execute("SELECT DISTINCT user_id FROM paper_orders")}
    users_e = {r[0] for r in conn.execute("SELECT DISTINCT user_id FROM equity_curve")}
    assert users_o == users_e == {"paper:default"}
    # the backtest persist path refuses to write into the namespace
    from app.cli import _persist_results
    with pytest.raises(ValueError, match="paper"):
        _persist_results(conn, "paper:default", None)


def test_failed_alert_is_retried_next_run(conn):
    calendar = _seed(conn)

    def broken(_text):
        raise ConnectionError("telegram down")

    code, rep = loop.run_signals(conn, universe=_universe(), bt_cfg=_bt_cfg(),
                                 strategy_cfg=_strategy(),
                                 now=_now_at(calendar[-1]),
                                 session_end=calendar[-1].date().isoformat(),
                                 send_fn=broken)
    assert code == 0  # alert failure never fails the money path
    assert any("alert delivery failed" in w for w in rep.warnings)
    unsent = conn.execute("SELECT COUNT(*) FROM paper_orders"
                          " WHERE signal_alerted_at IS NULL").fetchone()[0]
    assert unsent == 2
    # next (noop) run delivers the backlog
    sent = []
    code, rep = _run(conn, calendar=calendar, sent=sent)
    assert code == 0 and rep.status == "noop"
    assert len(sent) == 2
    assert conn.execute("SELECT COUNT(*) FROM paper_orders"
                        " WHERE signal_alerted_at IS NULL").fetchone()[0] == 0


def test_catchup_does_not_send_stale_signal_cards(conn):
    """An order created at S and settled at S+1 within one catch-up run must
    not get a 'fills at next session' card after it already filled."""
    calendar = _seed(conn)
    _run(conn, calendar=calendar, day=calendar[-4])
    sent = []
    code, rep = _run(conn, calendar=calendar, day=calendar[-1], sent=sent,
                     bt_cfg=_bt_cfg(catchup_max_sessions=10))
    assert code == 0 and len(rep.sessions) == 3
    filled = conn.execute(
        "SELECT COUNT(*) FROM paper_orders WHERE status='FILLED'").fetchone()[0]
    assert filled == 2  # the two entries queued at calendar[-4] settled at [-3]
    # outcome cards were sent; no stale pending-signal card for filled orders
    assert sum("ZREALIZOWANO" in c for c in sent) == 2
    assert not any("Realizacja: otwarcie nastepnej sesji" in c
                   and "KUP AAA" in c for c in sent)
    # every order is marked signal-alerted (stale ones silently, filled or not)
    assert conn.execute("SELECT COUNT(*) FROM paper_orders"
                        " WHERE signal_alerted_at IS NULL").fetchone()[0] == 0


def test_dry_run_respects_catchup_cap(conn):
    calendar = _seed(conn)
    _run(conn, calendar=calendar, day=calendar[0],
         bt_cfg=_bt_cfg(catchup_max_sessions=5))
    code, rep = _run(conn, calendar=calendar, dry_run=True,
                     bt_cfg=_bt_cfg(catchup_max_sessions=5))
    assert code == 2 and "catchup_max_sessions" in rep.reason
    assert not conn.in_transaction  # refusal rolled the dry-run txn back


def test_lag_other_than_one_is_refused(conn):
    calendar = _seed(conn)
    bt = _bt_cfg()
    bt["execution"] = {"signal_to_fill_lag_days": 2}
    code, rep = _run(conn, calendar=calendar, bt_cfg=bt)
    assert code == 2 and "not supported live" in rep.reason

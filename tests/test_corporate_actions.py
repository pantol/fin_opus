"""Pack A.3: corporate actions.

An overnight gap explained by a corporate action must NOT trigger the ATR stop
as if it were a market move; the identical gap WITHOUT a matching action must
still fire it. Splits also re-base position quantity/entry so the equity curve
stays continuous, dividends credit cash. The back-adjusted price series is
derived deterministically from raw bars + actions.
"""
import pandas as pd
import pytest

from app import config as cfg
from app.backtest import engine
from app.ingestion import refdata, stooq
from app.ingestion.stooq import Bar

from tests.conftest import make_stooq_csv


def _flat_rows(n, base, start="2019-01-01"):
    """Flat deterministic series: close == base every session."""
    import datetime as dt
    rows = []
    day = dt.date.fromisoformat(start)
    added = 0
    while added < n:
        if day.weekday() < 5:
            rows.append((day.isoformat(), round(base * 0.999, 4), round(base * 1.01, 4),
                         round(base * 0.99, 4), float(base), 100000.0))
            added += 1
        day += dt.timedelta(days=1)
    return rows


def _ingest(conn, ticker, rows, **inst):
    iid = stooq.upsert_instrument(conn, {"ticker": ticker, "name": ticker, **inst},
                                  is_index=inst.get("is_index", False))
    stooq.store_bars(conn, iid, stooq.parse_csv(make_stooq_csv(rows)))
    return iid


def _stop_only_strategy():
    return {
        "name": "stoponly", "version": 1,
        "entry": {"all": [{"feature": "close", "op": "gt", "value": 0}]},
        "exit": {"any": [{"type": "atr_stop", "atr_mult": 2.5}]},
        "risk": {"risk_per_trade": 0.01, "atr_mult_stop": 2.5,
                 "max_open_positions": 8, "max_exposure_per_name": 0.20,
                 "max_total_exposure": 1.0, "drawdown_circuit_breaker": 0.9},
    }


def _gap_series(n_before, n_after, base, gapped):
    """Flat at `base`, then a one-day gap down to `gapped` and flat after."""
    rows = _flat_rows(n_before + n_after, base)
    out = []
    for i, (d, o, h, l, c, v) in enumerate(rows):
        if i >= n_before:
            scale = gapped / base
            out.append((d, round(o * scale, 4), round(h * scale, 4),
                        round(l * scale, 4), round(c * scale, 4), v))
        else:
            out.append((d, o, h, l, c, v))
    return out, rows[n_before][0]  # (series, ex_date)


def _run_gap_case(conn, ticker, gapped, base, action=None, volume_scale_after=1.0):
    n_before, n_after = 40, 30
    rows, ex_date = _gap_series(n_before, n_after, base, gapped)
    if volume_scale_after != 1.0:
        rows = [(d, o, h, l, c, v * volume_scale_after if i >= n_before else v)
                for i, (d, o, h, l, c, v) in enumerate(rows)]
    _ingest(conn, "wig20tr", _flat_rows(n_before + n_after, 2000), is_index=True)
    iid = _ingest(conn, ticker, rows, sector="x", listed_from="2019-01-01")
    if action is not None:
        conn.execute(
            "INSERT INTO corporate_actions (instrument_id, action_type, ex_date, value_or_ratio, source)"
            " VALUES (?, ?, ?, ?, 'test')", (iid, action[0], ex_date, action[1]))
    conn.commit()

    uni = {"benchmark": {"ticker": "wig20tr", "is_index": True}, "indices": [],
           "instruments": [{"ticker": ticker, "sector": "x"}]}
    instruments, bench = engine.load_instruments(conn, uni, "wig20tr")
    res = engine.run_backtest(instruments, bench, _stop_only_strategy(),
                              cfg.load_backtest_config())
    return res, ex_date


def _exits_on(res, ex_date):
    return [d for d in res.decisions
            if d["action"] == "EXIT" and d["decision_date"] == ex_date]


def test_split_gap_without_action_fires_stop(conn):
    # 5:1 split-shaped gap (100 -> 20) with NO action row: treated as a crash.
    res, ex_date = _run_gap_case(conn, "nosplit", gapped=20.0, base=100.0)
    assert _exits_on(res, ex_date), "an unexplained -80% gap must breach the stop"


def test_split_with_action_shields_stop_and_equity(conn):
    res, ex_date = _run_gap_case(conn, "split", gapped=20.0, base=100.0,
                                 action=("split", 5.0), volume_scale_after=5.0)
    assert not _exits_on(res, ex_date), (
        "a gap explained by a split must not trigger the ATR stop"
    )
    # equity continuity: qty was multiplied by the ratio, so the -80% price gap
    # does not appear as a portfolio loss
    eq = res.equity_curve
    ex_ts = pd.Timestamp(ex_date)
    prev = eq[eq.index < ex_ts].iloc[-1]
    ratio = eq[ex_ts] / prev
    assert ratio == pytest.approx(1.0, rel=0.05)


def test_dividend_without_action_fires_stop(conn):
    # 100 -> 80 gap (a 20 PLN "dividend" without the action row) = market crash.
    res, ex_date = _run_gap_case(conn, "nodiv", gapped=80.0, base=100.0)
    assert _exits_on(res, ex_date), "an unexplained -20% gap must breach the stop"


def test_dividend_with_action_shields_stop_and_credits_cash(conn):
    res, ex_date = _run_gap_case(conn, "div", gapped=80.0, base=100.0,
                                 action=("dividend", 20.0))
    assert not _exits_on(res, ex_date), (
        "a gap explained by a dividend must not trigger the ATR stop"
    )
    # cash was credited qty * dividend on the ex-date
    ex_ts = pd.Timestamp(ex_date)
    cash_before = res.cash_curve[res.cash_curve.index < ex_ts].iloc[-1]
    cash_on_ex = res.cash_curve[ex_ts]
    assert cash_on_ex > cash_before
    # equity continuity: price drop offset by the cash credit
    eq = res.equity_curve
    prev = eq[eq.index < ex_ts].iloc[-1]
    assert eq[ex_ts] / prev == pytest.approx(1.0, rel=0.05)
    # per-trade PnL is total-return-consistent: entry was re-based by the
    # dividend, so the forced close near 80 books ~costs-only, not a -20% loss
    assert res.trade_pnls, "expected the forced end-of-run close"
    assert res.trade_pnls[0] > -1000.0, (
        f"mechanical ex-dividend gap leaked into trade PnL: {res.trade_pnls[0]}"
    )


def test_weekend_ex_date_bridges_to_next_session(conn):
    """An ex-date recorded on a Saturday still shields the Monday gap."""
    # session 39 of the 2019-01-01-anchored flat calendar is a Monday, so the
    # Saturday recording sits inside the (Friday, Monday] bridging window
    n_before, n_after = 39, 30
    rows, session_ex_date = _gap_series(n_before, n_after, 100.0, 80.0)
    _ingest(conn, "wig20tr", _flat_rows(n_before + n_after, 2000), is_index=True)
    iid = _ingest(conn, "wknd", rows, sector="x", listed_from="2019-01-01")
    # record the dividend on the SATURDAY before the gap session (a common
    # data-entry slip: record/payment dates are often non-session days)
    import datetime as dt
    ex = dt.date.fromisoformat(session_ex_date)
    assert ex.weekday() == 0, "test setup: the gap session must be a Monday"
    saturday = ex - dt.timedelta(days=2)
    assert saturday.weekday() == 5
    conn.execute(
        "INSERT INTO corporate_actions (instrument_id, action_type, ex_date, value_or_ratio, source)"
        " VALUES (?, 'dividend', ?, 20.0, 'test')", (iid, saturday.isoformat()))
    conn.commit()

    uni = {"benchmark": {"ticker": "wig20tr", "is_index": True}, "indices": [],
           "instruments": [{"ticker": "wknd", "sector": "x"}]}
    instruments, bench = engine.load_instruments(conn, uni, "wig20tr")
    res = engine.run_backtest(instruments, bench, _stop_only_strategy(),
                              cfg.load_backtest_config())
    assert not _exits_on(res, session_ex_date), (
        "a weekend-dated action must bridge to the next session, not vanish"
    )


def test_reverse_split_scales_pending_buy_qty(conn):
    """A BUY straddling a reverse split must not buy 10x the intended notional."""
    n_before = 20  # signal at session 19 (listed_from gate), fill on ex session 20
    rows, ex_date = _gap_series(n_before, 30, 100.0, 1000.0)  # 1:10 consolidation
    rows = [(d, o, h, l, c, v / 10.0 if i >= n_before else v)
            for i, (d, o, h, l, c, v) in enumerate(rows)]
    _ingest(conn, "wig20tr", _flat_rows(n_before + 30, 2000), is_index=True)
    iid = _ingest(conn, "rsplit", rows, sector="x", listed_from=rows[19][0])
    conn.execute(
        "INSERT INTO corporate_actions (instrument_id, action_type, ex_date, value_or_ratio, source)"
        " VALUES (?, 'split', ?, 0.1, 'test')", (iid, ex_date))
    conn.commit()

    uni = {"benchmark": {"ticker": "wig20tr", "is_index": True}, "indices": [],
           "instruments": [{"ticker": "rsplit", "sector": "x"}]}
    instruments, bench = engine.load_instruments(conn, uni, "wig20tr")
    res = engine.run_backtest(instruments, bench, _stop_only_strategy(),
                              cfg.load_backtest_config())

    enters = [d for d in res.decisions if d["action"] == "ENTER"]
    assert enters, "expected the straddling BUY to fill"
    first = enters[0]
    # sized ~200 shares at cum price 100 -> re-based to ~20 shares at ~1000
    assert first["qty"] <= 25, f"BUY qty not re-based across the split: {first['qty']}"
    notional = first["qty"] * first["price"]
    assert notional < 30000, f"reverse split bought {notional:.0f} PLN on 100k capital"
    # and crucially: no phantom margin loan
    assert res.cash_curve.min() > -1000.0


def test_apply_corporate_action_floor_and_cash_in_lieu():
    """Reverse-split rounding pays cash-in-lieu instead of minting equity."""
    pos = engine.Position(ticker="t", sector=None, instrument_id=1, qty=15,
                          entry_price=10.0, entry_date="2024-01-01", stop_price=9.0)
    # 1:10 consolidation: 15 * 0.1 = 1.5 -> 1 share + 0.5 * post-price cash
    delta = engine._apply_corporate_action(
        pos, {"action_type": "split", "value_or_ratio": 0.1}, prev_close=10.0)
    assert pos.qty == 1
    assert pos.entry_price == pytest.approx(100.0)
    assert pos.stop_price == pytest.approx(90.0)
    assert delta == pytest.approx(0.5 * (10.0 / 0.1))  # 0.5 shares @ 100 = 50
    # value conserved exactly: 15*10 == 1*100 + 50
    assert 15 * 10.0 == pytest.approx(pos.qty * 100.0 + delta)


def test_apply_corporate_action_consolidation_below_one_share():
    """qty rounding to 0 pays out the full position; caller removes it."""
    pos = engine.Position(ticker="t", sector=None, instrument_id=1, qty=4,
                          entry_price=10.0, entry_date="2024-01-01", stop_price=9.0)
    delta = engine._apply_corporate_action(
        pos, {"action_type": "split", "value_or_ratio": 0.1}, prev_close=10.0)
    assert pos.qty == 0
    assert delta == pytest.approx(4 * 10.0)  # full cash-in-lieu


def test_actions_loader_replaces_stale_rows_on_reload(conn):
    """Correcting an ex-date in the YAML must not leave both rows live."""
    _ingest(conn, "aaa", _flat_rows(5, 100), sector="x")
    conn.commit()
    typo = {"actions": [{"ticker": "aaa", "action_type": "dividend",
                         "ex_date": "2024-06-03", "value_or_ratio": 4.0}]}
    fixed = {"actions": [{"ticker": "aaa", "action_type": "dividend",
                          "ex_date": "2024-06-10", "value_or_ratio": 4.0}]}
    refdata.load_corporate_actions(conn, typo, refdata.RefdataReport())
    refdata.load_corporate_actions(conn, fixed, refdata.RefdataReport())
    rows = conn.execute("SELECT ex_date FROM corporate_actions").fetchall()
    assert [r["ex_date"] for r in rows] == ["2024-06-10"]


def test_membership_loader_replaces_stale_rows_on_reload(conn):
    _ingest(conn, "aaa", _flat_rows(5, 100), sector="x")
    conn.commit()
    v1 = {"indices": {"wig20": [{"ticker": "aaa", "date_from": "2019-06-01"}]}}
    v2 = {"indices": {"wig20": [{"ticker": "aaa", "date_from": "2019-09-23"}]}}
    refdata.load_index_membership(conn, v1, refdata.RefdataReport())
    refdata.load_index_membership(conn, v2, refdata.RefdataReport())
    rows = conn.execute("SELECT date_from FROM index_membership").fetchall()
    assert [r["date_from"] for r in rows] == ["2019-09-23"]


def test_actions_loader_upserts_and_validates(conn):
    _ingest(conn, "aaa", _flat_rows(5, 100), sector="x")
    conn.commit()
    actions_cfg = {"actions": [
        {"ticker": "aaa", "action_type": "dividend", "ex_date": "2024-06-10",
         "value_or_ratio": 4.15, "source": "test"},
        {"ticker": "aaa", "action_type": "bogus", "ex_date": "2024-06-10",
         "value_or_ratio": 1.0},
        {"ticker": "ghost", "action_type": "split", "ex_date": "2024-06-10",
         "value_or_ratio": 2.0},
    ]}
    report = refdata.RefdataReport()
    refdata.load_corporate_actions(conn, actions_cfg, report)
    assert report.action_rows == 1
    assert len(report.failures) == 2  # bogus type + unknown ticker
    # idempotent
    report2 = refdata.RefdataReport()
    refdata.load_corporate_actions(conn, actions_cfg, report2)
    assert conn.execute("SELECT COUNT(*) FROM corporate_actions").fetchone()[0] == 1


def _bar(date, price, volume=1000.0):
    return Bar(date=date, as_of_date=date, open=price, high=price, low=price,
               close=price, volume=volume)


def test_back_adjust_split():
    bars = [_bar("2024-01-01", 100.0), _bar("2024-01-02", 100.0),
            _bar("2024-01-03", 50.0), _bar("2024-01-04", 50.0)]
    actions = [{"action_type": "split", "ex_date": "2024-01-03", "value_or_ratio": 2.0}]
    adj = refdata.back_adjust(bars, actions)
    assert [b.close for b in adj] == [50.0, 50.0, 50.0, 50.0]
    # volume scales inversely for splits (share count doubled)
    assert [b.volume for b in adj] == [2000.0, 2000.0, 1000.0, 1000.0]
    # the LAST bar always equals raw
    assert adj[-1].close == bars[-1].close


def test_back_adjust_dividend():
    bars = [_bar("2024-01-01", 100.0), _bar("2024-01-02", 100.0),
            _bar("2024-01-03", 90.0), _bar("2024-01-04", 90.0)]
    actions = [{"action_type": "dividend", "ex_date": "2024-01-03", "value_or_ratio": 10.0}]
    adj = refdata.back_adjust(bars, actions)
    # factor = (100 - 10) / 100 = 0.9 applied strictly before the ex-date
    assert [b.close for b in adj] == [90.0, 90.0, 90.0, 90.0]
    # dividends do not rescale volume
    assert [b.volume for b in adj] == [1000.0] * 4


def test_derive_adjusted_series_writes_adjusted_rows_only_with_actions(conn):
    iid = _ingest(conn, "aaa", _flat_rows(6, 100), sector="x")
    conn.commit()
    # no actions -> no adjusted series (avoid inviting raw/adjusted mixing)
    assert refdata.derive_adjusted_series(conn, iid) == 0
    conn.execute(
        "INSERT INTO corporate_actions (instrument_id, action_type, ex_date, value_or_ratio, source)"
        " VALUES (?, 'dividend', ?, 5.0, 'test')",
        (iid, _flat_rows(6, 100)[3][0]))
    conn.commit()
    n = refdata.derive_adjusted_series(conn, iid)
    assert n == 6
    raw = conn.execute("SELECT COUNT(*) FROM prices WHERE instrument_id=? AND adjusted=0",
                       (iid,)).fetchone()[0]
    adj = conn.execute("SELECT COUNT(*) FROM prices WHERE instrument_id=? AND adjusted=1",
                       (iid,)).fetchone()[0]
    assert raw == 6 and adj == 6
    # adjusted closes before the ex-date carry the dividend factor (95/100)
    row = conn.execute(
        "SELECT close FROM prices WHERE instrument_id=? AND adjusted=1 ORDER BY date LIMIT 1",
        (iid,)).fetchone()
    assert row["close"] == pytest.approx(95.0)


def test_build_features_bridges_split_gap():
    """A1: features must not read a split gap as a market move.

    A 2:1 split halves the raw close; on the raw panel that is a -50% "return"
    poisoning momentum for a full lookback window and a huge true-range spike
    poisoning ATR (so the stop distance explodes). The action-aware panel
    bridges the gap, while close/atr stay in RAW units for execution.
    """
    rows = _flat_rows(80, 100)
    raw = []
    for i, (d, o, h, l, c, v) in enumerate(rows):
        f = 0.5 if i >= 40 else 1.0  # 2:1 split at bar 40
        raw.append((d, o * f, h * f, l * f, c * f, v))
    df = pd.DataFrame(
        {"open": [r[1] for r in raw], "high": [r[2] for r in raw],
         "low": [r[3] for r in raw], "close": [r[4] for r in raw],
         "volume": [r[5] for r in raw]},
        index=pd.DatetimeIndex([r[0] for r in raw]))
    actions = {rows[40][0]: [{"action_type": "split", "value_or_ratio": 2.0}]}

    plain = engine.compute.compute_features(df)
    fixed = engine.build_features(df, actions, None)
    probe = 45  # ret_1m window (21 bars) straddles the ex-date

    assert plain["ret_1m"].iloc[probe] == pytest.approx(-0.5, abs=0.02)  # the bug
    assert fixed["ret_1m"].iloc[probe] == pytest.approx(0.0, abs=0.02)   # bridged
    # execution stays in raw units: close is the raw post-split price
    assert fixed["close"].iloc[probe] == df["close"].iloc[probe]
    # ATR no longer carries the 50-PLN mechanical gap
    assert plain["atr"].iloc[probe] > 3.0
    assert fixed["atr"].iloc[probe] < 2.0
    # no actions -> identical to the plain panel
    same = engine.build_features(df, None, None)
    assert same["ret_1m"].equals(plain["ret_1m"])


def test_shipped_corporate_actions_fixture_loads(conn):
    """The repo YAML (PZU + DNP 1:10 splits) loads without failures."""
    _ingest(conn, "pzu", _flat_rows(6, 100), sector="fin")
    _ingest(conn, "dnp", _flat_rows(6, 100), sector="retail")
    conn.commit()
    report = refdata.load_refdata(conn, {"indices": {}}, cfg.load_corporate_actions())
    assert report.ok, report.failures
    assert report.action_rows == 2
    rows = conn.execute(
        "SELECT i.ticker, c.action_type, c.value_or_ratio FROM corporate_actions c"
        " JOIN instruments i ON i.id = c.instrument_id ORDER BY c.ex_date").fetchall()
    assert [(r["ticker"], r["action_type"], r["value_or_ratio"]) for r in rows] == \
        [("pzu", "split", 10.0), ("dnp", "split", 10.0)]

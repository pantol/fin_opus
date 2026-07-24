"""Read-only web dashboard: rendering, per-user isolation, read-only guarantee."""
from __future__ import annotations

import sqlite3

import pytest

from app.db import connect, init_db
from app.web.server import connect_ro, create_app

NOW = "2026-07-22T20:00:00+00:00"


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "web.db"
    conn = connect(path)
    init_db(conn)

    conn.execute(
        "INSERT INTO instruments (ticker, name, market, sector, is_index)"
        " VALUES ('pko', 'PKO BP', 'GPW', 'banking', 0)")
    conn.execute(
        "INSERT INTO instruments (ticker, name, market, is_index)"
        " VALUES ('wig20tr', 'WIG20 Total Return', 'GPW', 1)")
    pko = conn.execute("SELECT id FROM instruments WHERE ticker='pko'").fetchone()[0]
    tr = conn.execute("SELECT id FROM instruments WHERE ticker='wig20tr'").fetchone()[0]

    for date, pko_c, tr_c in [("2026-07-20", 100.0, 7000.0),
                              ("2026-07-21", 104.0, 7070.0),
                              ("2026-07-22", 110.0, 7140.0)]:
        for iid, close in [(pko, pko_c), (tr, tr_c)]:
            conn.execute(
                "INSERT INTO prices (instrument_id, date, as_of_date, open, high,"
                " low, close, volume, adjusted, source)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 1000, 0, 'gpw')",
                (iid, date, date, close, close, close, close))

    conn.execute(
        "INSERT INTO strategies (name, version, config_yaml, created_at)"
        " VALUES ('trend_momentum', 1, '{}', ?)", (NOW,))
    sid = conn.execute("SELECT id FROM strategies").fetchone()[0]

    conn.execute(
        "INSERT INTO paper_state (user_id, cash, peak_equity, initial_capital,"
        " inception_date, last_settled_date, strategy_id, config_hash, updated_at)"
        " VALUES ('paper:default', 5000, 102000, 100000,"
        " '2026-07-20', '2026-07-22', ?, 'h', ?)", (sid, NOW))
    conn.execute(
        "INSERT INTO paper_state (user_id, cash, peak_equity, initial_capital,"
        " inception_date, last_settled_date, strategy_id, config_hash, updated_at)"
        " VALUES ('paper:llm', 100000, 100000, 100000,"
        " '2026-07-22', '2026-07-22', ?, 'h', ?)", (sid, NOW))

    conn.execute(
        "INSERT INTO positions (user_id, instrument_id, qty, entry_date,"
        " entry_price, stop_price, status)"
        " VALUES ('paper:default', ?, 100, '2026-07-21', 95.0, 90.0, 'OPEN')",
        (pko,))
    conn.execute(
        "INSERT INTO positions (user_id, instrument_id, qty, entry_date,"
        " entry_price, exit_date, exit_price, status)"
        " VALUES ('paper:default', ?, 0, '2026-07-20', 80.0, '2026-07-21',"
        " 104.0, 'CLOSED')", (pko,))

    conn.execute(
        "INSERT INTO paper_orders (user_id, instrument_id, side, qty, stop_price,"
        " decision_date, features_json, status, created_at)"
        " VALUES ('paper:default', ?, 'BUY', 50, 99.0, '2026-07-22', '{}',"
        " 'PENDING', ?)", (pko, NOW))
    conn.execute(
        "INSERT INTO paper_orders (user_id, instrument_id, side, qty, stop_price,"
        " decision_date, features_json, status, fill_date, fill_qty, fill_price,"
        " created_at)"
        " VALUES ('paper:default', ?, 'BUY', 100, 91.0, '2026-07-20', '{}',"
        " 'FILLED', '2026-07-21', 100, 95.0, ?)", (pko, NOW))

    for date, eq, cash, exp in [("2026-07-20", 100000, 100000, 0.0),
                                ("2026-07-21", 101000, 5000, 0.94),
                                ("2026-07-22", 102000, 5000, 0.95)]:
        conn.execute(
            "INSERT INTO equity_curve (user_id, date, equity, cash, exposure)"
            " VALUES ('paper:default', ?, ?, ?, ?)", (date, eq, cash, exp))
    conn.execute(
        "INSERT INTO equity_curve (user_id, date, equity, cash, exposure)"
        " VALUES ('paper:llm', '2026-07-22', 100000, 100000, 0.0)")

    conn.execute(
        "INSERT INTO trades (user_id, instrument_id, side, qty, price, fee,"
        " slippage, trade_date) VALUES"
        " ('paper:default', ?, 'BUY', 100, 95.0, 3.7, 0.1, '2026-07-21')", (pko,))
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def client(db_path):
    app = create_app(db_path, benchmark_ticker="wig20tr")
    app.testing = True
    return app.test_client()


def test_index_lists_every_user(client):
    resp = client.get("/")
    assert resp.status_code == 200   # two books -> list, no redirect
    html = resp.get_data(as_text=True)
    assert "paper:default" in html
    assert "paper:llm" in html
    assert 'class="spark"' in html   # per-user equity sparkline
    assert "102 000,00" in html     # latest equity, Polish formatting (NBSP)
    assert "trend_momentum" in html


def test_single_user_index_still_shows_the_picker(tmp_path):
    # The picker is ALWAYS the entry page (onboarding flow): even a one-book
    # database must offer user selection + the new-user survey path.
    path = tmp_path / "solo.db"
    conn = connect(path)
    init_db(conn)
    conn.execute(
        "INSERT INTO paper_state (user_id, cash, peak_equity, initial_capital,"
        " inception_date, last_settled_date, config_hash, updated_at)"
        " VALUES ('paper:solo', 1000, 1000, 1000,"
        " '2026-07-20', '2026-07-22', 'h', ?)", (NOW,))
    conn.commit()
    conn.close()
    app = create_app(path, benchmark_ticker="wig20tr")
    app.testing = True
    c = app.test_client()
    resp = c.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "paper:solo" in html
    assert "Nowy użytkownik" in html


def test_user_dashboard_renders_book(client):
    resp = client.get("/u/paper:default")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "PKO" in html
    assert "KUP" in html                  # pending BUY badge
    assert "eqchart" in html              # SVG chart present
    assert "WIG20TR" in html              # benchmark series labelled
    assert "1 500,00" in html        # unrealized: (110-95)*100 (NBSP)
    assert "+15,79%" in html              # unrealized pct
    assert "+2,00%" in html               # benchmark return 7000 -> 7140
    assert "+30,00%" in html              # closed position 80 -> 104
    assert "+0,99%" in html               # day delta 101000 -> 102000
    assert "18,2%" in html                # stop buffer: 1 - 90/110
    assert "10,8%" in html                # weight: 100*110 / 102000


def test_user_isolation(client):
    html = client.get("/u/paper:llm").get_data(as_text=True)
    # the llm book is empty: nothing from paper:default may leak in
    assert "Brak otwartych pozycji" in html
    assert "Brak zleceń oczekujących" in html
    assert "1 500,00" not in html


def test_single_point_chart_renders(client):
    resp = client.get("/u/paper:llm")
    assert resp.status_code == 200
    assert "eqchart" in resp.get_data(as_text=True)


def test_unknown_user_is_404(client):
    resp = client.get("/u/nie-ma-takiego")
    assert resp.status_code == 404
    assert "Nie znaleziono" in resp.get_data(as_text=True)


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


def test_missing_db_yields_503(tmp_path):
    app = create_app(tmp_path / "missing.db", benchmark_ticker="wig20tr")
    app.testing = True
    resp = app.test_client().get("/")
    assert resp.status_code == 503


def test_connection_is_read_only(db_path):
    conn = connect_ro(db_path)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("INSERT INTO equity_curve (user_id, date, equity, cash,"
                     " exposure) VALUES ('x', '2026-01-01', 1, 1, 0)")
    conn.close()

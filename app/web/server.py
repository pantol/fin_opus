"""Per-user web dashboard served straight from SQLite opened READ-ONLY.

One page per paper user (multi-tenant: everything is keyed by user_id).
This is a display layer only: it renders the book the paper loop wrote —
it never computes or writes money state, and the read-only connection makes
that structural (an INSERT/UPDATE raises at the SQLite level).

UI strings are Polish (end-user surface, same convention as Telegram cards);
code and comments stay English.
"""
from __future__ import annotations

import math
import sqlite3
from pathlib import Path

from flask import Flask, abort, render_template
from markupsafe import Markup, escape

# Chart geometry (SVG user units; the element scales responsively via viewBox).
CHART_W = 860
CHART_H = 300
PAD_L, PAD_R, PAD_T, PAD_B = 68, 84, 14, 30  # right pad fits direct labels


def connect_ro(db_path: str | Path) -> sqlite3.Connection:
    """Read-only connection; raises sqlite3.OperationalError if the file is absent."""
    conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# --- Polish number formatting (space thousands, comma decimals) ---------------

def fmt_pln(x, decimals: int = 2) -> str:
    if x is None:
        return "—"
    s = f"{float(x):,.{decimals}f}"
    return s.replace(",", "\u00a0").replace(".", ",")  # NBSP thousands (Polish, no wrap)


def fmt_pct(x, signed: bool = True) -> str:
    if x is None:
        return "—"
    sign = "+" if signed else ""
    return f"{float(x) * 100:{sign}.2f}%".replace(".", ",")


def fmt_qty(x) -> str:
    if x is None:
        return "—"
    return f"{int(x):,}".replace(",", "\u00a0")  # NBSP thousands


# --- queries -------------------------------------------------------------------

_STATE_SQL = """
SELECT s.user_id, s.cash, s.peak_equity, s.initial_capital, s.inception_date,
       s.last_settled_date,
       (SELECT equity FROM equity_curve e
         WHERE e.user_id = s.user_id ORDER BY e.date DESC LIMIT 1) AS equity,
       (SELECT exposure FROM equity_curve e
         WHERE e.user_id = s.user_id ORDER BY e.date DESC LIMIT 1) AS exposure,
       (SELECT COUNT(*) FROM positions p
         WHERE p.user_id = s.user_id AND p.status = 'OPEN') AS n_open,
       (SELECT COUNT(*) FROM paper_orders o
         WHERE o.user_id = s.user_id AND o.status = 'PENDING') AS n_pending,
       st.name AS strategy_name, st.version AS strategy_version
FROM paper_state s
LEFT JOIN strategies st ON st.id = s.strategy_id
"""


def users_overview(conn) -> list[dict]:
    rows = conn.execute(_STATE_SQL + " ORDER BY s.user_id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["equity"] = d["equity"] if d["equity"] is not None else d["cash"]
        d["ret"] = (d["equity"] / d["initial_capital"] - 1.0) if d["initial_capital"] else None
        out.append(d)
    return out


def _last_close(conn, instrument_id: int):
    row = conn.execute(
        "SELECT date, close FROM prices WHERE instrument_id = ? AND close IS NOT NULL"
        " ORDER BY date DESC, adjusted DESC LIMIT 1",
        (instrument_id,),
    ).fetchone()
    return (row["date"], float(row["close"])) if row else (None, None)


def _bench_series(conn, ticker: str, d_from: str, d_to: str) -> list[tuple[str, float]]:
    inst = conn.execute(
        "SELECT id FROM instruments WHERE ticker = ?", (ticker,)
    ).fetchone()
    if inst is None:
        return []
    rows = conn.execute(
        """
        SELECT date, close FROM prices p
        WHERE instrument_id = ? AND close IS NOT NULL AND date >= ? AND date <= ?
          AND adjusted = (SELECT MAX(adjusted) FROM prices
                          WHERE instrument_id = p.instrument_id AND date = p.date)
        ORDER BY date
        """,
        (inst["id"], d_from, d_to),
    ).fetchall()
    return [(r["date"], float(r["close"])) for r in rows]


def dashboard_data(conn, user_id: str, bench_ticker: str) -> dict | None:
    state = conn.execute(_STATE_SQL + " WHERE s.user_id = ?", (user_id,)).fetchone()
    if state is None:
        return None
    d = dict(state)
    d["equity"] = d["equity"] if d["equity"] is not None else d["cash"]
    initial = float(d["initial_capital"]) if d["initial_capital"] else None
    d["ret"] = (d["equity"] / initial - 1.0) if initial else None
    d["drawdown"] = (1.0 - d["equity"] / d["peak_equity"]) if d["peak_equity"] else None

    eq_rows = conn.execute(
        "SELECT date, equity, cash, exposure FROM equity_curve"
        " WHERE user_id = ? ORDER BY date",
        (user_id,),
    ).fetchall()
    equity_series = [(r["date"], float(r["equity"])) for r in eq_rows]

    bench_series: list[tuple[str, float]] = []
    d["bench_ret"] = None
    if equity_series:
        raw = _bench_series(conn, bench_ticker,
                            equity_series[0][0], equity_series[-1][0])
        if raw:
            base = raw[0][1]
            start_value = float(equity_series[0][1])
            bench_series = [(dt, start_value * c / base) for dt, c in raw]
            d["bench_ret"] = raw[-1][1] / base - 1.0

    open_rows = conn.execute(
        """
        SELECT p.qty, p.entry_date, p.entry_price, p.stop_price,
               i.id AS instrument_id, i.ticker, i.name
        FROM positions p JOIN instruments i ON i.id = p.instrument_id
        WHERE p.user_id = ? AND p.status = 'OPEN'
        ORDER BY p.entry_date, i.ticker
        """,
        (user_id,),
    ).fetchall()
    open_positions, unrealized_total = [], 0.0
    for r in open_rows:
        pos = dict(r)
        last_date, last = _last_close(conn, r["instrument_id"])
        pos["last_date"], pos["last_close"] = last_date, last
        if last is not None:
            pos["pnl"] = (last - r["entry_price"]) * r["qty"]
            pos["pnl_pct"] = last / r["entry_price"] - 1.0 if r["entry_price"] else None
            pos["to_stop_pct"] = (r["stop_price"] / last - 1.0) if r["stop_price"] else None
            unrealized_total += pos["pnl"]
        else:
            pos["pnl"] = pos["pnl_pct"] = pos["to_stop_pct"] = None
        open_positions.append(pos)
    d["unrealized_total"] = unrealized_total if open_positions else None

    pending = [dict(r) for r in conn.execute(
        """
        SELECT o.side, o.qty, o.stop_price, o.decision_date, i.ticker, i.name
        FROM paper_orders o JOIN instruments i ON i.id = o.instrument_id
        WHERE o.user_id = ? AND o.status = 'PENDING' ORDER BY o.id
        """,
        (user_id,),
    ).fetchall()]

    closed = []
    for r in conn.execute(
        """
        SELECT p.entry_date, p.entry_price, p.exit_date, p.exit_price,
               i.ticker, i.name
        FROM positions p JOIN instruments i ON i.id = p.instrument_id
        WHERE p.user_id = ? AND p.status = 'CLOSED'
        ORDER BY p.exit_date DESC, p.id DESC LIMIT 20
        """,
        (user_id,),
    ).fetchall():
        c = dict(r)
        c["change_pct"] = (
            r["exit_price"] / r["entry_price"] - 1.0
            if r["exit_price"] is not None and r["entry_price"] else None
        )
        closed.append(c)

    trades = [dict(r) for r in conn.execute(
        """
        SELECT t.trade_date, t.side, t.qty, t.price, t.fee, i.ticker
        FROM trades t JOIN instruments i ON i.id = t.instrument_id
        WHERE t.user_id = ? ORDER BY t.trade_date DESC, t.id DESC LIMIT 20
        """,
        (user_id,),
    ).fetchall()]

    chart = build_chart(equity_series, bench_series, bench_ticker.upper())
    return {
        "state": d, "open_positions": open_positions, "pending": pending,
        "closed": closed, "trades": trades, "chart": chart,
        "equity_rows": [
            {"date": dt, "equity": v,
             "bench": dict(bench_series).get(dt)} for dt, v in equity_series
        ],
    }


# --- SVG line chart (server-rendered; palette lives in CSS custom properties) --

def _y_ticks(lo: float, hi: float, n: int = 4) -> list[float]:
    """Round tick values (1/2/2.5/5 x 10^k steps) inside [lo, hi]."""
    raw = (hi - lo) / max(n - 1, 1)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    step = next((m * mag for m in (1, 2, 2.5, 5, 10) if raw <= m * mag), 10 * mag)
    first = math.ceil(lo / step) * step
    ticks = []
    t = first
    while t <= hi + step * 1e-6:
        ticks.append(t)
        t += step
    return ticks or [lo, hi]


def build_chart(equity: list[tuple[str, float]],
                bench: list[tuple[str, float]],
                bench_label: str) -> dict | None:
    """Layout for the equity-vs-benchmark line chart.

    Returns geometry the template drops into an inline SVG plus the point data
    the hover script needs; None when there is nothing to plot yet.
    """
    if not equity:
        return None
    dates = sorted({dt for dt, _ in equity} | {dt for dt, _ in bench})
    x_of = {dt: i for i, dt in enumerate(dates)}
    span = max(len(dates) - 1, 1)

    values = [v for _, v in equity] + [v for _, v in bench]
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:  # flat/single-point book: give the axis some air
        pad = max(abs(hi) * 0.01, 1.0)
        lo, hi = lo - pad, hi + pad
    else:
        margin = (hi - lo) * 0.06
        lo, hi = lo - margin, hi + margin

    plot_w = CHART_W - PAD_L - PAD_R
    plot_h = CHART_H - PAD_T - PAD_B

    def px(dt: str) -> float:
        return round(PAD_L + plot_w * x_of[dt] / span, 2)

    def py(v: float) -> float:
        return round(PAD_T + plot_h * (1.0 - (v - lo) / (hi - lo)), 2)

    def series(pts: list[tuple[str, float]], name: str, css_var: str) -> dict:
        by_date = dict(pts)
        return {
            "name": name,
            "var": css_var,
            "points": " ".join(f"{px(dt)},{py(v)}" for dt, v in pts),
            "single": len(pts) == 1,
            "cx": px(pts[-1][0]), "cy": py(pts[-1][1]),
            "ys": [py(by_date[dt]) if dt in by_date else None for dt in dates],
            "values": [by_date.get(dt) for dt in dates],
        }

    all_series = [series(equity, "Portfel", "--series-1")]
    if bench:
        all_series.append(series(bench, bench_label, "--series-2"))
    # Direct labels sit at the line ends; nudge one label when they collide.
    if len(all_series) == 2 and abs(all_series[0]["cy"] - all_series[1]["cy"]) < 14:
        lower = max(all_series, key=lambda s: s["cy"])
        lower["label_y"] = lower["cy"] + 14
    for s in all_series:
        s.setdefault("label_y", s["cy"])

    n_x = min(len(dates), 3)
    x_label_idx = sorted({0, len(dates) // 2, len(dates) - 1})[:n_x]
    return {
        "w": CHART_W, "h": CHART_H,
        "pad_l": PAD_L, "pad_r": PAD_R, "pad_t": PAD_T, "pad_b": PAD_B,
        "grid": [{"y": py(t), "label": fmt_pln(t, 0)} for t in _y_ticks(lo, hi)],
        "x_labels": [{"x": px(dates[i]), "label": dates[i]} for i in x_label_idx],
        "series": all_series,
        "hover": {"xs": [px(dt) for dt in dates], "dates": dates,
                  "series": [{"name": s["name"], "var": s["var"],
                              "ys": s["ys"], "values": s["values"]}
                             for s in all_series]},
    }


# --- app factory ---------------------------------------------------------------

def create_app(db_path: str | Path | None = None,
               benchmark_ticker: str | None = None) -> Flask:
    from app.config import DEFAULT_DB_PATH

    if benchmark_ticker is None:
        try:
            from app import config as cfg
            benchmark_ticker = (cfg.load_universe().get("benchmark") or {}).get(
                "ticker", "wig20tr")
        except Exception:  # config unavailable: chart simply omits the benchmark
            benchmark_ticker = "wig20tr"

    app = Flask(__name__)
    app.config["DB_PATH"] = str(db_path or DEFAULT_DB_PATH)
    app.config["BENCH_TICKER"] = benchmark_ticker
    app.jinja_env.filters["pln"] = fmt_pln
    app.jinja_env.filters["pct"] = fmt_pct
    app.jinja_env.filters["qty"] = fmt_qty

    def _open():
        try:
            return connect_ro(app.config["DB_PATH"])
        except sqlite3.OperationalError:
            return None

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "db": app.config["DB_PATH"]}

    @app.get("/")
    def index():
        conn = _open()
        if conn is None:
            return render_template("no_db.html", db=app.config["DB_PATH"]), 503
        try:
            users = users_overview(conn)
        finally:
            conn.close()
        return render_template("index.html", users=users, db=app.config["DB_PATH"])

    @app.get("/u/<user_id>")
    def user_dashboard(user_id: str):
        conn = _open()
        if conn is None:
            return render_template("no_db.html", db=app.config["DB_PATH"]), 503
        try:
            data = dashboard_data(conn, user_id, app.config["BENCH_TICKER"])
        finally:
            conn.close()
        if data is None:
            abort(404)
        return render_template("user.html", db=app.config["DB_PATH"],
                               bench_label=app.config["BENCH_TICKER"].upper(),
                               **data)

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("not_found.html", db=app.config["DB_PATH"]), 404

    return app

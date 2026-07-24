"""Per-user web dashboard served straight from SQLite opened READ-ONLY.

One page per paper user (multi-tenant: everything is keyed by user_id).
This is a display layer only: it renders the book the paper loop wrote —
it never computes or writes money state, and the read-only connection makes
that structural (an INSERT/UPDATE raises at the SQLite level).

The ONE deliberate exception is onboarding (`/onboarding/...`): those
endpoints open a separate read-write connection whose writes are limited to
`user_profiles` plus the LLM audit/cache/cost tables the LLMClient itself
maintains (`llm_calls`, `llm_cache`, `llm_costs`). Money tables (positions /
trades / decisions / paper_*) are never written by the web layer — starting
a book stays a deliberate CLI act (`signals --user X`). See
app/web/onboarding.py for the LLM boundary (language layer only; the profile
is computed by deterministic code).

UI strings are Polish (end-user surface, same convention as Telegram cards);
code and comments stay English.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, abort, redirect, render_template, request, url_for
from markupsafe import Markup

from app.web import onboarding as onb

WARSAW = ZoneInfo("Europe/Warsaw")

# Chart geometry (SVG user units; the element scales responsively via viewBox).
CHART_W = 860
CHART_H = 280
PAD_L, PAD_R, PAD_T, PAD_B = 68, 116, 14, 30  # right pad fits name+value labels

NEAR_STOP_BUFFER = 0.03  # flag a position whose close sits <3% above its stop


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
    return s.replace(",", " ").replace(".", ",")  # NBSP thousands (Polish, no wrap)


def fmt_pct(x, signed: bool = True, decimals: int = 2) -> str:
    if x is None:
        return "—"
    sign = "+" if signed else ""
    return f"{float(x) * 100:{sign}.{decimals}f}%".replace(".", ",")


def fmt_qty(x) -> str:
    if x is None:
        return "—"
    return f"{int(x):,}".replace(",", " ")  # NBSP thousands


def fmt_pp(x) -> str:
    """Fraction difference -> percentage points, e.g. -0.0315 -> '-3,15 p.p.'"""
    if x is None:
        return "—"
    return f"{float(x) * 100:+.2f}".replace(".", ",") + " p.p."


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


def _day_change(equity_series: list[tuple[str, float]], initial: float | None):
    """Last-session change: vs previous session, or vs initial capital for a
    one-point book. Returns (delta, delta_pct, reference_label) or Nones."""
    if len(equity_series) >= 2:
        (prev_dt, prev), (_cur_dt, cur) = equity_series[-2], equity_series[-1]
        if prev:
            return cur - prev, cur / prev - 1.0, f"vs sesja {prev_dt}"
    if len(equity_series) == 1 and initial:
        cur = equity_series[-1][1]
        return cur - initial, cur / initial - 1.0, "od startu księgi"
    return None, None, None


def users_overview(conn) -> list[dict]:
    rows = conn.execute(_STATE_SQL + " ORDER BY s.user_id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["equity"] = d["equity"] if d["equity"] is not None else d["cash"]
        d["ret"] = (d["equity"] / d["initial_capital"] - 1.0) if d["initial_capital"] else None
        eq = [float(x[0]) for x in conn.execute(
            "SELECT equity FROM (SELECT equity, date FROM equity_curve"
            " WHERE user_id = ? ORDER BY date DESC LIMIT 40) ORDER BY date",
            (d["user_id"],)).fetchall()]
        d["spark"] = build_sparkline(eq)
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


def _days_between(d_from: str | None, d_to: str | None) -> int | None:
    if not d_from or not d_to:
        return None
    return (date.fromisoformat(d_to) - date.fromisoformat(d_from)).days


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
    d["day_delta"], d["day_delta_pct"], d["day_ref_label"] = _day_change(
        equity_series, initial)

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
    d["diff_pp"] = (d["ret"] - d["bench_ret"]
                    if d["ret"] is not None and d["bench_ret"] is not None else None)

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
            pos["value"] = last * r["qty"]
            pos["weight"] = pos["value"] / d["equity"] if d["equity"] else None
            pos["pnl"] = (last - r["entry_price"]) * r["qty"]
            pos["pnl_pct"] = last / r["entry_price"] - 1.0 if r["entry_price"] else None
            # Distance DOWN to the stop as a positive buffer; small buffer = risk.
            pos["stop_buffer"] = (1.0 - r["stop_price"] / last) if r["stop_price"] else None
            pos["near_stop"] = (pos["stop_buffer"] is not None
                                and pos["stop_buffer"] < NEAR_STOP_BUFFER)
            unrealized_total += pos["pnl"]
        else:
            pos["value"] = pos["weight"] = None
            pos["pnl"] = pos["pnl_pct"] = pos["stop_buffer"] = None
            pos["near_stop"] = False
        open_positions.append(pos)
    open_positions.sort(key=lambda p: p["value"] or 0.0, reverse=True)
    d["unrealized_total"] = unrealized_total if open_positions else None

    pending = []
    for r in conn.execute(
        """
        SELECT o.side, o.qty, o.stop_price, o.decision_date,
               i.id AS instrument_id, i.ticker, i.name
        FROM paper_orders o JOIN instruments i ON i.id = o.instrument_id
        WHERE o.user_id = ? AND o.status = 'PENDING' ORDER BY o.id
        """,
        (user_id,),
    ).fetchall():
        o = dict(r)
        _dt, last = _last_close(conn, r["instrument_id"])
        o["est_value"] = last * r["qty"] if last is not None else None
        pending.append(o)

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
        c["days"] = _days_between(r["entry_date"], r["exit_date"])
        closed.append(c)

    trades = []
    for r in conn.execute(
        """
        SELECT t.trade_date, t.side, t.qty, t.price, t.fee, i.ticker
        FROM trades t JOIN instruments i ON i.id = t.instrument_id
        WHERE t.user_id = ? ORDER BY t.trade_date DESC, t.id DESC LIMIT 20
        """,
        (user_id,),
    ).fetchall():
        t = dict(r)
        t["value"] = t["qty"] * t["price"]
        trades.append(t)

    chart = build_chart(equity_series, bench_series, bench_ticker.upper(),
                        start_ref=initial)
    return {
        "state": d, "open_positions": open_positions, "pending": pending,
        "closed": closed, "trades": trades, "chart": chart,
        "equity_rows": [
            {"date": dt, "equity": v,
             "bench": dict(bench_series).get(dt)} for dt, v in equity_series
        ],
    }


# --- SVG charts (server-rendered; palette lives in CSS custom properties) ------

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


def build_sparkline(points: list[float], w: int = 120, h: int = 30) -> Markup | None:
    """Tiny inline equity sparkline for the user list (no axes, no labels)."""
    if not points:
        return None
    lo, hi = min(points), max(points)
    if hi - lo < 1e-9:
        lo, hi = lo - 1.0, hi + 1.0
    n = len(points)

    def sx(i: int) -> float:
        return 3 + (w - 6) * (i / (n - 1)) if n > 1 else w / 2

    def sy(v: float) -> float:
        return 3 + (h - 6) * (1.0 - (v - lo) / (hi - lo))

    line = ""
    if n > 1:
        pts = " ".join(f"{sx(i):.1f},{sy(v):.1f}" for i, v in enumerate(points))
        line = (f'<polyline points="{pts}" fill="none" stroke="var(--series-1)"'
                f' stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
    return Markup(
        f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}"'
        f' aria-hidden="true">{line}<circle cx="{sx(n - 1):.1f}"'
        f' cy="{sy(points[-1]):.1f}" r="2.5" fill="var(--series-1)"/></svg>')


def build_chart(equity: list[tuple[str, float]],
                bench: list[tuple[str, float]],
                bench_label: str,
                start_ref: float | None = None) -> dict | None:
    """Layout for the equity-vs-benchmark line chart.

    Returns geometry the template drops into an inline SVG plus the point data
    the hover script needs; None when there is nothing to plot yet. start_ref
    (initial capital) always sits inside the y-domain and gets a dotted
    reference line, so gains/losses read against a stable anchor.
    """
    if not equity:
        return None
    dates = sorted({dt for dt, _ in equity} | {dt for dt, _ in bench})
    x_of = {dt: i for i, dt in enumerate(dates)}
    span = max(len(dates) - 1, 1)

    values = [v for _, v in equity] + [v for _, v in bench]
    if start_ref is not None:
        values = values + [start_ref]
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
        # A one-session book renders as a centered dot, not one glued to the axis.
        pos = 0.5 if len(dates) == 1 else x_of[dt] / span
        return round(PAD_L + plot_w * pos, 2)

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
            "last_value": fmt_pln(pts[-1][1], 0),
            "ys": [py(by_date[dt]) if dt in by_date else None for dt in dates],
            "values": [by_date.get(dt) for dt in dates],
        }

    all_series = [series(equity, "Portfel", "--series-1")]
    if bench:
        all_series.append(series(bench, bench_label, "--series-2"))
    # Two-line direct labels (name + value) need ~26px; nudge on collision.
    # sorted() keeps insertion order on ties, so equal-cy series (a fresh flat
    # book) still split into a distinct upper and lower label.
    if len(all_series) == 2 and abs(all_series[0]["cy"] - all_series[1]["cy"]) < 30:
        upper, lower = sorted(all_series, key=lambda s: s["cy"])
        mid = (all_series[0]["cy"] + all_series[1]["cy"]) / 2
        upper["label_y"] = mid - 15
        lower["label_y"] = mid + 15
    for s in all_series:
        s.setdefault("label_y", s["cy"])

    n_x = min(len(dates), 3)
    x_label_idx = sorted({0, len(dates) // 2, len(dates) - 1})[:n_x]
    ref = None
    if start_ref is not None:
        ref = {"y": py(start_ref), "label": fmt_pln(start_ref, 0)}
    return {
        "w": CHART_W, "h": CHART_H,
        "pad_l": PAD_L, "pad_r": PAD_R, "pad_t": PAD_T, "pad_b": PAD_B,
        "grid": [{"y": py(t), "label": fmt_pln(t, 0)} for t in _y_ticks(lo, hi)],
        "x_labels": [{"x": px(dates[i]), "label": dates[i]} for i in x_label_idx],
        "series": all_series,
        "ref": ref,
        "single_session": len(dates) == 1,
        "hover": {"xs": [px(dt) for dt in dates], "dates": dates,
                  "series": [{"name": s["name"], "var": s["var"],
                              "ys": s["ys"], "values": s["values"]}
                             for s in all_series]},
    }


# --- app factory ---------------------------------------------------------------

def create_app(db_path: str | Path | None = None,
               benchmark_ticker: str | None = None,
               llm_transport=None, llm_meta_transport=None) -> Flask:
    """`llm_transport` / `llm_meta_transport`: injectable HTTP seams for the
    onboarding chat's LLMClient — tests run fully offline through them."""
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
    app.config["LLM_TRANSPORT"] = llm_transport
    app.config["LLM_META_TRANSPORT"] = llm_meta_transport
    app.jinja_env.filters["pln"] = fmt_pln
    app.jinja_env.filters["pct"] = fmt_pct
    app.jinja_env.filters["qty"] = fmt_qty
    app.jinja_env.filters["pp"] = fmt_pp

    def _open():
        try:
            return connect_ro(app.config["DB_PATH"])
        except sqlite3.OperationalError:
            return None

    def _page(template: str, **ctx):
        ctx.setdefault("db", app.config["DB_PATH"])
        ctx.setdefault("generated",
                       datetime.now(WARSAW).strftime("%Y-%m-%d %H:%M"))
        return render_template(template, **ctx)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "db": app.config["DB_PATH"]}

    def _profiles(conn) -> dict[str, dict]:
        """{user_id: profile-row dict} — RO read; absent table reads empty."""
        try:
            return {r["user_id"]: dict(r) for r in conn.execute(
                "SELECT user_id, display_name, risk_tolerance, strategy "
                "FROM user_profiles ORDER BY user_id")}
        except sqlite3.OperationalError:
            return {}

    def _open_rw():
        """The onboarding write path: user_profiles + LLM audit tables ONLY
        (see module docstring). Opened per request, closed immediately."""
        from app.db import connect as connect_rw, init_db

        conn = connect_rw(app.config["DB_PATH"])
        init_db(conn)
        return conn

    def _llm_client(conn):
        from app import config as cfg
        from app.llm.client import LLMClient

        # An injected transport means offline/test mode: satisfy the client's
        # key check with a dummy so no env var is needed; production keeps
        # reading OPENROUTER_API_KEY from the environment.
        return LLMClient(conn, cfg.load_llm_config(),
                         transport=app.config["LLM_TRANSPORT"],
                         meta_transport=app.config["LLM_META_TRANSPORT"],
                         api_key=("offline-test"
                                  if app.config["LLM_TRANSPORT"] is not None
                                  else None))

    def _llm_available() -> bool:
        import os
        return (app.config["LLM_TRANSPORT"] is not None
                or bool(os.environ.get("OPENROUTER_API_KEY")))

    @app.get("/")
    def index():
        """User picker — ALWAYS the entry page: pick a book, resume an
        onboarding, or create a new user (survey first, book later)."""
        conn = _open()
        if conn is None:
            return _page("no_db.html"), 503
        try:
            users = users_overview(conn)
            profiles = _profiles(conn)
        finally:
            conn.close()
        base_of = {u["user_id"]: u["user_id"].split(":", 1)[-1] for u in users}
        for u in users:
            u["profile"] = profiles.get(base_of[u["user_id"]])
            u["base_user"] = base_of[u["user_id"]]
            u["can_onboard"] = onb.valid_user_slug(base_of[u["user_id"]])
        books_bases = set(base_of.values())
        profiles_only = [p for uid, p in profiles.items()
                         if uid not in books_bases]
        return _page("index.html", users=users, profiles_only=profiles_only)

    @app.get("/u/<user_id>")
    def user_dashboard(user_id: str):
        conn = _open()
        if conn is None:
            return _page("no_db.html"), 503
        try:
            data = dashboard_data(conn, user_id, app.config["BENCH_TICKER"])
        finally:
            conn.close()
        if data is None:
            abort(404)
        return _page("user.html",
                     bench_label=app.config["BENCH_TICKER"].upper(), **data)

    # --- onboarding (survey chat; the ONLY web write surface) -----------------

    @app.post("/onboarding/new")
    def onboarding_new():
        user = (request.form.get("user") or "").strip().lower()
        if not onb.valid_user_slug(user):
            return _page("onboarding.html", user=None, error=(
                "Nazwa uzytkownika: 1-32 znakow [a-z0-9_-], nie 'default'."),
                llm_available=_llm_available(), opening=onb.OPENING_MESSAGE,
                survey=_survey_questions()), 400
        return redirect(url_for("onboarding_page", user=user))

    def _survey_questions():
        from app.users.profiles import SURVEY
        return SURVEY

    @app.get("/onboarding/<user>")
    def onboarding_page(user: str):
        if not onb.valid_user_slug(user):
            abort(404)
        conn = _open()
        existing = None
        if conn is not None:
            try:
                existing = _profiles(conn).get(user)
            finally:
                conn.close()
        return _page("onboarding.html", user=user, error=None,
                     existing=existing, llm_available=_llm_available(),
                     opening=onb.OPENING_MESSAGE, survey=_survey_questions())

    @app.post("/api/onboarding/<user>/chat")
    def onboarding_chat(user: str):
        from app.llm.client import LLMBudgetExceededError

        if not onb.valid_user_slug(user):
            abort(404)
        if not _llm_available():
            return {"fallback": True,
                    "reason": "Brak klucza OPENROUTER_API_KEY — uzyj "
                              "formularza ponizej."}, 503
        payload = request.get_json(silent=True) or {}
        transcript = payload.get("transcript") or []
        if not isinstance(transcript, list):
            return {"error": "transcript must be a list"}, 400
        conn = _open_rw()
        try:
            try:
                turn = onb.chat_turn(_llm_client(conn), transcript)
            except LLMBudgetExceededError:
                return {"fallback": True,
                        "reason": "Miesieczny budzet LLM wyczerpany — uzyj "
                                  "formularza ponizej."}, 503
            except onb.LLMValidationError:
                # Malformed model output: rejected, never repaired/guessed.
                return {"fallback": True,
                        "reason": "Model zwrocil niepoprawna odpowiedz "
                                  "(odrzucona). Uzyj formularza ponizej."}, 422
        finally:
            conn.close()
        out = {"reply": turn["reply"], "collected": turn["collected"],
               "complete": onb.answers_complete(turn["collected"])}
        if out["complete"]:
            from app import config as cfg
            try:
                out["preview"] = onb.preview_profile(
                    user, turn["collected"], cfg.load_profiles_config())
            except ValueError:
                out["complete"] = False  # model enums failed the strict clean
        return out

    @app.post("/api/onboarding/<user>/save")
    def onboarding_save(user: str):
        if not onb.valid_user_slug(user):
            abort(404)
        from app import config as cfg
        from app.users import profiles as prof

        payload = request.get_json(silent=True) or {}
        try:
            answers = onb.clean_answers(payload.get("answers") or {})
        except ValueError as exc:
            return {"error": str(exc)}, 400
        answers["source"] = ("llm_chat" if payload.get("source") == "llm_chat"
                             else "form")
        profile = prof.build_profile(
            user, answers, cfg.load_profiles_config(),
            display_name=(payload.get("display_name") or user)[:64])
        conn = _open_rw()
        try:
            prof.save_profile(conn, profile)
            has_book = conn.execute(
                "SELECT 1 FROM paper_state WHERE user_id = ?",
                (f"paper:{user}",)).fetchone() is not None
        finally:
            conn.close()
        return {"ok": True,
                "redirect": (url_for("user_dashboard", user_id=f"paper:{user}")
                             if has_book else url_for("index")),
                "book_started": has_book,
                "profile": {k: profile[k] for k in
                            ("user_id", "risk_tolerance", "strategy",
                             "risk_multiplier", "max_drawdown_pct",
                             "excluded_sectors")}}

    @app.errorhandler(404)
    def not_found(_e):
        return _page("not_found.html"), 404

    return app

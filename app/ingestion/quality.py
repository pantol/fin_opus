"""Data-quality monitor (`make check-data`).

Read-only, deterministic sanity checks over ingested prices:
  - missing sessions vs the exchange calendar (benchmark index bar dates),
  - zero/negative volume on equity bars,
  - close-to-close jumps above the config threshold with NO corporate action
    on that ex-date to explain them,
  - stale tickers: alive per listing dates but not printing bars.

The monitor only reports; it never mutates data. Alerting is the caller's job.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.ingestion.refdata import price_factor


@dataclass
class Issue:
    category: str   # missing_sessions / bad_volume / unexplained_jump / stale_ticker / no_data
    ticker: str
    detail: str


@dataclass
class QualityReport:
    issues: list[Issue] = field(default_factory=list)
    checked_instruments: int = 0
    calendar_sessions: int = 0

    @property
    def ok(self) -> bool:
        return not self.issues

    def counts_by_category(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for issue in self.issues:
            out[issue.category] = out.get(issue.category, 0) + 1
        return out


def _bar_dates(conn, instrument_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT date FROM prices WHERE instrument_id = ? AND adjusted = 0 ORDER BY date",
        (instrument_id,),
    ).fetchall()
    return [r["date"] for r in rows]


def exchange_calendar(conn, benchmark_ticker: str) -> list[str]:
    """Exchange session dates = the benchmark index's bar dates.

    The benchmark (WIG20TR) prints a bar on every real session, so its date set
    is the de facto GPW calendar for the ingested range. Weekday holidays are
    naturally absent -- no hardcoded holiday table needed.
    """
    row = conn.execute(
        "SELECT id FROM instruments WHERE ticker = ?", (benchmark_ticker.lower(),)
    ).fetchone()
    if not row:
        return []
    return _bar_dates(conn, int(row["id"]))


def _actions_by_date(conn, instrument_id: int) -> dict[str, list[dict]]:
    rows = conn.execute(
        "SELECT action_type, ex_date, value_or_ratio FROM corporate_actions"
        " WHERE instrument_id = ?",
        (instrument_id,),
    ).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["ex_date"], []).append(
            {"action_type": r["action_type"], "value_or_ratio": float(r["value_or_ratio"])}
        )
    return out


def run_checks(conn, universe: dict, dq_cfg: dict) -> QualityReport:
    """Run all checks for the configured (non-index) universe instruments."""
    max_jump = float(dq_cfg.get("max_jump_pct", 0.25))
    stale_sessions = int(dq_cfg.get("stale_sessions", 5))
    max_examples = int(dq_cfg.get("max_examples", 10))

    report = QualityReport()
    benchmark_ticker = universe["benchmark"]["ticker"]
    calendar = exchange_calendar(conn, benchmark_ticker)
    report.calendar_sessions = len(calendar)
    if not calendar:
        # A missing calendar silently blinds the missing-session and staleness
        # checks -- that is itself a data-quality failure, never a clean bill.
        report.issues.append(Issue(
            "no_data", benchmark_ticker,
            "benchmark has no price bars — exchange calendar unavailable; "
            "missing-session and staleness checks are DISABLED until it is ingested",
        ))
    newest_equity_bar = ""

    for entry in universe.get("instruments", []):
        ticker = entry["ticker"].lower()
        row = conn.execute(
            "SELECT id, listed_from, delisted_on FROM instruments WHERE ticker = ?",
            (ticker,),
        ).fetchone()
        if row is None:
            report.issues.append(Issue("no_data", ticker, "not in instruments table (never ingested)"))
            continue
        inst_id = int(row["id"])
        report.checked_instruments += 1

        bars = conn.execute(
            "SELECT date, close, volume FROM prices"
            " WHERE instrument_id = ? AND adjusted = 0 ORDER BY date",
            (inst_id,),
        ).fetchall()
        if not bars:
            report.issues.append(Issue("no_data", ticker, "no price bars stored"))
            continue

        dates = [b["date"] for b in bars]
        date_set = set(dates)
        newest_equity_bar = max(newest_equity_bar, dates[-1])

        # --- missing sessions: calendar days between first and last bar ---
        expected = [d for d in calendar if dates[0] <= d <= dates[-1]]
        missing = [d for d in expected if d not in date_set]
        if missing:
            shown = ", ".join(missing[:max_examples])
            more = f" (+{len(missing) - max_examples} more)" if len(missing) > max_examples else ""
            report.issues.append(Issue(
                "missing_sessions", ticker,
                f"{len(missing)} calendar sessions without a bar: {shown}{more}",
            ))

        # --- zero / negative volume ---
        bad_vol = [b["date"] for b in bars if b["volume"] is None or b["volume"] <= 0]
        if bad_vol:
            shown = ", ".join(bad_vol[:max_examples])
            more = f" (+{len(bad_vol) - max_examples} more)" if len(bad_vol) > max_examples else ""
            report.issues.append(Issue(
                "bad_volume", ticker,
                f"{len(bad_vol)} bars with volume <= 0: {shown}{more}",
            ))

        # --- unexplained jumps: |close/prev - 1| > threshold beyond what any
        # recorded corporate action on that date explains (an action absolves
        # only its own magnitude -- a crash landing on an ex-date still flags) ---
        actions_by_date = _actions_by_date(conn, inst_id)
        jumps = []
        prev_close = None
        for b in bars:
            close = b["close"]
            if prev_close and close and prev_close > 0:
                expected = 1.0
                for action in actions_by_date.get(b["date"], []):
                    expected *= price_factor(action, prev_close)
                actual = close / prev_close
                residual = (actual / expected - 1.0) if expected > 0 else actual - 1.0
                if abs(residual) > max_jump:
                    note = " beyond the recorded action" if b["date"] in actions_by_date else ""
                    jumps.append(f"{b['date']} ({actual - 1.0:+.1%}{note})")
            if close:
                prev_close = close
        if jumps:
            shown = ", ".join(jumps[:max_examples])
            more = f" (+{len(jumps) - max_examples} more)" if len(jumps) > max_examples else ""
            report.issues.append(Issue(
                "unexplained_jump", ticker,
                f"{len(jumps)} close-to-close jumps > {max_jump:.0%} with no corporate action: {shown}{more}",
            ))

        # --- stale ticker: alive but not printing bars ---
        delisted_on = row["delisted_on"]
        alive = delisted_on is None or (calendar and delisted_on > calendar[-1])
        if alive and calendar:
            sessions_behind = len([d for d in calendar if d > dates[-1]])
            if sessions_behind > stale_sessions:
                report.issues.append(Issue(
                    "stale_ticker", ticker,
                    f"last bar {dates[-1]} is {sessions_behind} sessions behind the calendar "
                    f"({calendar[-1]}) but the instrument is not delisted",
                ))

    # --- stale benchmark: equities printing newer bars than the calendar means
    # the calendar itself lags and the staleness checks above under-report ---
    if calendar and newest_equity_bar > calendar[-1]:
        report.issues.append(Issue(
            "stale_ticker", benchmark_ticker,
            f"benchmark last bar {calendar[-1]} is older than the newest equity bar "
            f"({newest_equity_bar}) — the exchange calendar is lagging",
        ))

    return report


def format_report(report: QualityReport) -> str:
    """Plain-text report for the CLI."""
    lines = [
        f"Data quality: {report.checked_instruments} instruments checked over "
        f"{report.calendar_sessions} calendar sessions.",
    ]
    if report.ok:
        lines.append("No issues found.")
        return "\n".join(lines)
    lines.append(f"{len(report.issues)} issue(s) found:")
    for issue in report.issues:
        lines.append(f"  [{issue.category}] {issue.ticker}: {issue.detail}")
    return "\n".join(lines)


def format_alert_pl(report: QualityReport) -> str:
    """Polish-language Telegram summary (end-user string)."""
    counts = report.counts_by_category()
    label_pl = {
        "missing_sessions": "brakujace sesje",
        "bad_volume": "zly wolumen",
        "unexplained_jump": "niewyjasnione skoki cen",
        "stale_ticker": "nieaktualne notowania",
        "no_data": "brak danych",
    }
    lines = ["⚠️ Kontrola danych GPW: wykryto problemy"]
    for category, n in sorted(counts.items()):
        lines.append(f"- {label_pl.get(category, category)}: {n}")
    lines.append("Szczegoly: make check-data")
    return "\n".join(lines)

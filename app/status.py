"""`make status` — one command to verify the whole deployment is alive.

Checks the freshness of prices, the collector health beacon, unprocessed
filings, and the newest local backup snapshot. Prints a report, sends ONE
Polish Telegram alert when something is stale (dry-run without a token), and
exits non-zero so cron/monitoring can react.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.backup import SNAPSHOT_RE
from app.ingestion import filings_db


@dataclass
class StatusReport:
    lines: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)  # short Polish labels for the alert

    @property
    def ok(self) -> bool:
        return not self.stale

    def as_text(self) -> str:
        out = list(self.lines)
        out.append("STATUS: " + ("OK" if self.ok else f"STALE ({len(self.stale)} problem(s))"))
        return "\n".join(out)

    def alert_pl(self) -> str:
        card = ["⚠️ Status GPW: wykryto przestoje"]
        card.extend(f"- {item}" for item in self.stale)
        card.append("Szczegoly: make status")
        return "\n".join(card)


def _parse_dt(raw: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def run_status(conn, backup_cfg: dict, *, now: datetime | None = None) -> StatusReport:
    now = now or datetime.now(timezone.utc)
    status_cfg = backup_cfg.get("status") or {}
    collector_stale_minutes = int(status_cfg.get("collector_stale_minutes", 120))
    backup_stale_hours = int(status_cfg.get("backup_stale_hours", 36))
    report = StatusReport()

    # --- prices freshness (informational; ingest cadence is manual/daily) ---
    row = conn.execute(
        "SELECT MAX(date) AS d, COUNT(*) AS n, "
        "SUM(CASE WHEN source = 'demo' THEN 1 ELSE 0 END) AS demo_n "
        "FROM prices WHERE adjusted = 0"
    ).fetchone()
    if row and row["d"]:
        line = f"prices: {row['n']} bars, last session {row['d']}"
        if row["demo_n"]:
            # Demo bars in the MONITORED database are always anomalous (demo
            # runs live in data/demo.db) — page the operator, don't just log.
            line += f" [WARNING: {row['demo_n']} DEMO bars — synthetic, NOT real prices]"
            report.stale.append(
                f"baza zawiera {row['demo_n']} syntetycznych barow DEMO "
                "(to nie sa prawdziwe ceny) — uruchom purge-demo")
        report.lines.append(line)
    else:
        report.lines.append("prices: EMPTY — run `make ingest`")
        report.stale.append("brak danych cenowych")

    # --- collector heartbeat ---
    health = filings_db.get_health(conn)
    last_run = _parse_dt(health["last_successful_run"]) if (
        health and health["last_successful_run"]) else None
    if last_run is None:
        report.lines.append("collector: no successful run recorded")
        report.stale.append("kolektor nigdy nie zakonczyl cyklu")
    else:
        age_min = (now - last_run).total_seconds() / 60.0
        report.lines.append(
            f"collector: last successful run {health['last_successful_run']} "
            f"({age_min:.0f} min ago), last_error={health['last_error'] or 'none'}")
        if age_min > collector_stale_minutes:
            report.stale.append(
                f"kolektor nieaktywny od {age_min / 60.0:.1f} h "
                f"(prog: {collector_stale_minutes} min)")

    # --- filings backlog (informational) ---
    try:
        unprocessed = conn.execute(
            "SELECT COUNT(*) FROM filings WHERE processed = 0").fetchone()[0]
        report.lines.append(f"filings: {unprocessed} unprocessed")
    except Exception:  # noqa: BLE001 — filings table may predate unification
        report.lines.append("filings: table missing (collector never ran)")

    # --- paper-trading loop ---
    paper_stale_sessions = int(status_cfg.get("paper_stale_sessions", 2))
    state = conn.execute(
        "SELECT user_id, last_settled_date FROM paper_state ORDER BY user_id"
    ).fetchall()
    if not state:
        report.lines.append("paper: not started — run `make signals`")
    for s in state:
        n_open = conn.execute(
            "SELECT COUNT(*) FROM positions WHERE user_id = ? AND status = 'OPEN'",
            (s["user_id"],)).fetchone()[0]
        n_pending = conn.execute(
            "SELECT COUNT(*) FROM paper_orders WHERE user_id = ? AND status = 'PENDING'",
            (s["user_id"],)).fetchone()[0]
        n_unsent = conn.execute(
            "SELECT COUNT(*) FROM paper_orders WHERE user_id = ?"
            " AND (signal_alerted_at IS NULL OR (fill_alerted_at IS NULL"
            "      AND status IN ('FILLED', 'PARTIAL', 'LAPSED')))",
            (s["user_id"],)).fetchone()[0]
        report.lines.append(
            f"paper[{s['user_id']}]: settled {s['last_settled_date']}, "
            f"{n_open} open, {n_pending} pending, {n_unsent} unsent alert(s)")
        # Behind = stored sessions the loop has not processed. One is normal
        # (status may run between ingest and the evening signals run).
        behind = conn.execute(
            "SELECT COUNT(DISTINCT date) FROM prices WHERE adjusted = 0 AND date > ?",
            (s["last_settled_date"],)).fetchone()[0]
        if behind >= paper_stale_sessions:
            report.stale.append(
                f"petla paper {behind} sesje w tyle "
                f"(ostatnia rozliczona: {s['last_settled_date']})")

    # --- newest local backup snapshot ---
    backups_dir = Path(backup_cfg.get("backups_dir", "data/backups"))
    names = sorted(p.name for p in backups_dir.glob("gpw-*.db")
                   if SNAPSHOT_RE.match(p.name)) if backups_dir.exists() else []
    if not names:
        report.lines.append(f"backup: NONE in {backups_dir} — run `make backup`")
        report.stale.append("brak kopii zapasowych")
    else:
        latest = names[-1]
        m = SNAPSHOT_RE.match(latest)
        taken = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc)
        age_h = (now - taken).total_seconds() / 3600.0
        report.lines.append(f"backup: latest {latest} ({age_h:.1f} h old)")
        if age_h > backup_stale_hours:
            report.stale.append(
                f"kopia zapasowa przestarzala ({age_h:.0f} h, prog: {backup_stale_hours} h)")

    return report

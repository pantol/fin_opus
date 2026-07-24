"""Working-window scheduler — the system's clock (Stage 1, ZERO decision changes).

One process (`make daemon`) reads `config/schedule.yaml` and fires the
EXISTING jobs (collect / intraday / digest / evening decision chain / health)
at their slots, journaling every fired slot into `schedule_runs`. The jobs
themselves are unchanged one-shot commands run as subprocesses — the same
commands a crontab would run — so removing the daemon and going back to cron
loses nothing.

Boundary rules:
  - The ONLY decision point stays the evening `signals` run at its slot.
  - The scheduler NEVER passes `--accept-config-change`: a config-fingerprint
    break must be acknowledged by a human, not by the clock.
  - `llm` is an optional chain step: its failure never blocks `signals`
    (missing scores fail LLM-gated entries closed — deterministic behavior).

Idempotency: `schedule_runs` PRIMARY KEY (job, scheduled_for). A slot claimed
is a slot spent — a crashed run is journaled `aborted` on the next startup and
is NOT retried (every job is idempotent and catches up at its next slot; the
journal shows the gap honestly instead of hiding it behind silent retries).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time as _time
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app import config as cfg
from app.db import connect, init_db

_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# Job name -> argv appended to `python -m`. Every job is an existing one-shot
# entrypoint; the scheduler adds nothing to their behavior.
JOB_COMMANDS: dict[str, list[str]] = {
    "collect": ["app.ingestion.collect_news"],
    "intraday": ["app.ingestion.intraday"],
    "ingest": ["app.cli", "ingest"],
    "llm": ["app.cli", "llm"],
    "signals": ["app.cli", "signals"],
    "status": ["app.cli", "status"],
    "backup": ["app.cli", "backup"],
}

# Chain steps whose failure does NOT stop the chain. `llm` produces optional
# INPUT features; the deterministic decision must never wait on it.
OPTIONAL_CHAIN_STEPS = {"llm"}

# Hard per-step timeout. Generous: the evening ingest of a missed week plus a
# full-market backfill session stays far below this.
STEP_TIMEOUT_S = 1800


@dataclass
class SlotResult:
    job: str
    scheduled_for: str
    status: str  # ok / failed / skipped
    detail: str = ""


def parse_hhmm(raw: str) -> time:
    hh, mm = str(raw).split(":")
    return time(int(hh), int(mm))


def _tz(schedule_cfg: dict) -> ZoneInfo:
    return ZoneInfo(str((schedule_cfg.get("window") or {}).get("tz", "Europe/Warsaw")))


def _active_weekdays(schedule_cfg: dict) -> set[int]:
    days = (schedule_cfg.get("window") or {}).get("days") or ["mon", "tue", "wed", "thu", "fri"]
    return {_WEEKDAYS[str(d).lower()] for d in days}


def job_slots_for_day(job: dict, day: datetime, schedule_cfg: dict) -> list[datetime]:
    """All slot datetimes for `job` on `day` (a tz-aware datetime's date).

    `at` jobs yield one slot at their wall-clock time — deliberately NOT
    clamped to the window (the evening decision chain sits outside it).
    `every_min` jobs yield slots inside the global window intersected with the
    per-job window. Weekday gating applies to both kinds.
    """
    tz = _tz(schedule_cfg)
    if day.weekday() not in _active_weekdays(schedule_cfg):
        return []
    base = day.astimezone(tz) if day.tzinfo else day.replace(tzinfo=tz)
    d = base.date()

    if "at" in job:
        t = parse_hhmm(job["at"])
        return [datetime.combine(d, t, tzinfo=tz)]

    every = int(job["every_min"])
    win = schedule_cfg.get("window") or {}
    start, end = parse_hhmm(win.get("start", "07:00")), parse_hhmm(win.get("end", "19:00"))
    if job.get("window"):
        jstart, jend = (parse_hhmm(x) for x in job["window"])
        start, end = max(start, jstart), min(end, jend)
    slots = []
    cur = datetime.combine(d, start, tzinfo=tz)
    enddt = datetime.combine(d, end, tzinfo=tz)
    while cur <= enddt:
        slots.append(cur)
        cur += timedelta(minutes=every)
    return slots


def due_slots(conn, schedule_cfg: dict, now: datetime) -> list[tuple[dict, datetime, bool]]:
    """(job, slot, should_run) for every past, unclaimed slot of today.

    should_run=False marks an OLDER missed slot of an every_min job: it is
    journaled `skipped` (honest gap) while only the latest missed slot runs.
    `at` jobs keep should_run=True for the whole day (grace until midnight);
    slots from previous days are never resurrected — jobs are idempotent and
    catch up by themselves.
    """
    out: list[tuple[dict, datetime, bool]] = []
    for job in schedule_cfg.get("jobs") or []:
        past = [s for s in job_slots_for_day(job, now, schedule_cfg) if s <= now]
        unclaimed = [s for s in past if not _claimed(conn, job["name"], s)]
        if not unclaimed:
            continue
        if "every_min" in job:
            out.extend((job, s, False) for s in unclaimed[:-1])
            out.append((job, unclaimed[-1], True))
        else:
            out.extend((job, s, True) for s in unclaimed)
    return out


def _claimed(conn, job_name: str, slot: datetime) -> bool:
    row = conn.execute(
        "SELECT 1 FROM schedule_runs WHERE job = ? AND scheduled_for = ?",
        (job_name, slot.isoformat()),
    ).fetchone()
    return row is not None


def _claim(conn, job_name: str, slot: datetime, now: datetime, status: str) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO schedule_runs (job, scheduled_for, started_at, status) "
        "VALUES (?, ?, ?, ?)",
        (job_name, slot.isoformat(), now.isoformat(), status),
    )
    conn.commit()
    return cur.rowcount == 1


def _finish(conn, job_name: str, slot: datetime, now: datetime, status: str, detail: str) -> None:
    conn.execute(
        "UPDATE schedule_runs SET finished_at = ?, status = ?, detail = ? "
        "WHERE job = ? AND scheduled_for = ?",
        (now.isoformat(), status, detail[:2000], job_name, slot.isoformat()),
    )
    conn.commit()


def abort_stale_running(conn, now: datetime) -> int:
    """Mark rows a dead daemon left as 'running' -> 'aborted' (startup only).

    Single-daemon assumption (enforced by the pidfile): at startup nothing can
    legitimately be running, so every 'running' row is a corpse. Aborted slots
    are terminal — the job catches up at its next slot.
    """
    cur = conn.execute(
        "UPDATE schedule_runs SET status = 'aborted', finished_at = ?, "
        "detail = COALESCE(detail, '') || ' [daemon died mid-run]' "
        "WHERE status = 'running'",
        (now.isoformat(),),
    )
    conn.commit()
    return cur.rowcount


def run_step(step: str, db_path: str | None, *, timeout_s: int = STEP_TIMEOUT_S) -> tuple[int, str]:
    """Run one job step as a subprocess of the SAME interpreter. Returns
    (exit_code, output_tail). Never raises: a scheduler that dies on a job
    error stops the whole clock."""
    argv = [sys.executable, "-m", *JOB_COMMANDS[step]]
    if db_path and JOB_COMMANDS[step][0] == "app.cli":
        argv = [sys.executable, "-m", JOB_COMMANDS[step][0], "--db", db_path,
                *JOB_COMMANDS[step][1:]]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        tail = (proc.stdout + proc.stderr)[-500:]
        return proc.returncode, tail
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout_s}s"
    except Exception as exc:  # noqa: BLE001 — the clock must keep ticking
        return 1, f"launcher error: {exc}"


def execute_job(job: dict, db_path: str | None, *, run_step_fn=run_step,
                digest_fn=None) -> tuple[str, str]:
    """Execute one job (or its chain). Returns (status, detail).

    Chain semantics: steps run in order; a REQUIRED step's failure stops the
    chain (deciding after a failed ingest would decide on yesterday's data —
    `signals` would refuse anyway, but the chain makes the policy explicit).
    OPTIONAL steps (`llm`) log their failure and let the chain continue.
    """
    name = job["name"]
    if name == "digest":
        detail = (digest_fn or _run_digest)(db_path)
        return "ok", detail
    steps = job.get("chain") or [name]
    parts: list[str] = []
    for step in steps:
        if step not in JOB_COMMANDS:
            return "failed", f"unknown step '{step}'"
        code, tail = run_step_fn(step, db_path)
        parts.append(f"{step}: exit {code}")
        if code != 0:
            if step in OPTIONAL_CHAIN_STEPS:
                parts[-1] += " (optional — chain continues)"
                continue
            parts[-1] += f" | {tail.strip()[-200:]}"
            return "failed", "; ".join(parts)
    return "ok", "; ".join(parts)


def _run_digest(db_path: str | None) -> str:
    from app.alerts import digest as digest_mod
    from app.alerts import telegram

    conn = connect(db_path)
    try:
        init_db(conn)
        card = digest_mod.build_digest(conn)
    finally:
        conn.close()
    if card is None:
        return "digest: nothing to report"
    telegram.send_text(card)
    return "digest: sent"


def fire_due(conn, schedule_cfg: dict, now: datetime, db_path: str | None,
             *, run_step_fn=run_step, digest_fn=None) -> list[SlotResult]:
    """One scheduler pass: claim + run everything due at `now`."""
    results: list[SlotResult] = []
    for job, slot, should_run in due_slots(conn, schedule_cfg, now):
        if not should_run:
            if _claim(conn, job["name"], slot, now, "skipped"):
                _finish(conn, job["name"], slot, now, "skipped",
                        "older missed slot — only the latest missed slot runs")
                results.append(SlotResult(job["name"], slot.isoformat(), "skipped"))
            continue
        if not _claim(conn, job["name"], slot, now, "running"):
            continue  # another (earlier) process claimed it — idempotency
        status, detail = execute_job(job, db_path, run_step_fn=run_step_fn,
                                     digest_fn=digest_fn)
        _finish(conn, job["name"], slot, now, status, detail)
        results.append(SlotResult(job["name"], slot.isoformat(), status, detail))
    return results


# --- daemon ------------------------------------------------------------------

def _pidfile_path(db_path: str | None) -> Path:
    base = Path(db_path).parent if db_path else Path("data")
    return base / "daemon.pid"


def _acquire_pidfile(path: Path) -> bool:
    """True if we own the pidfile; False if another daemon is alive."""
    if path.exists():
        try:
            pid = int(path.read_text().strip())
            os.kill(pid, 0)  # raises if dead
            return False
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # stale or unreadable — take over
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()))
    return True


def run_daemon(db_path: str | None = None, *, schedule_cfg: dict | None = None,
               now_fn=None, sleep_fn=_time.sleep, run_step_fn=run_step,
               once: bool = False) -> int:
    """The clock. `once=True` does a single pass (cron/smoke-test mode)."""
    schedule_cfg = schedule_cfg or cfg.load_schedule_config()
    tz = _tz(schedule_cfg)
    now_fn = now_fn or (lambda: datetime.now(tz))
    tick = int(schedule_cfg.get("tick_seconds", 30))

    pidfile = _pidfile_path(db_path)
    if not _acquire_pidfile(pidfile):
        print(f"another daemon is already running (pidfile {pidfile}); refusing "
              "to start — two clocks would double-fire every job")
        return 1
    conn = connect(db_path)
    try:
        init_db(conn)
        aborted = abort_stale_running(conn, now_fn())
        if aborted:
            print(f"marked {aborted} stale 'running' slot(s) as aborted")
        print(f"scheduler up: window {schedule_cfg.get('window')}, "
              f"{len(schedule_cfg.get('jobs') or [])} job(s), tick {tick}s")
        while True:
            for res in fire_due(conn, schedule_cfg, now_fn(), db_path,
                                run_step_fn=run_step_fn):
                print(f"[{res.scheduled_for}] {res.job}: {res.status}"
                      + (f" — {res.detail}" if res.detail else ""))
            if once:
                return 0
            sleep_fn(tick)
    finally:
        conn.close()
        try:
            if pidfile.exists() and pidfile.read_text().strip() == str(os.getpid()):
                pidfile.unlink()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    cfg.load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None, help="database path (default: data/gpw.db)")
    parser.add_argument("--once", action="store_true",
                        help="single pass: fire everything due now, then exit")
    args = parser.parse_args(argv)
    return run_daemon(args.db, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())

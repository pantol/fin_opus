"""SQLite backups: online snapshot, R2 upload, retention, restore verification.

Snapshots are taken with `VACUUM INTO` — SQLite's online backup path — NEVER by
copying a live DB file (a copy taken mid-write is silently corrupt). Snapshots
are pushed to S3-compatible object storage (Cloudflare R2) when credentials are
present in the environment; without them the snapshot stays local-only.

A backup that was never restored is not a backup: `make restore-test` pulls the
latest snapshot, opens it, runs PRAGMA integrity_check, verifies the expected
tables, and sanity-compares row counts against the live DB.

The `filings` history is irreplaceable (RSS has no backfill) — this module is
what protects it. ZERO LLM anywhere near here.
"""
from __future__ import annotations

import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.alerts import healthcheck

SNAPSHOT_RE = re.compile(r"^gpw-(\d{8})-(\d{6})\.db$")
HEALTHCHECK_ENV = "HEALTHCHECK_URL_BACKUP"

# Tables every healthy snapshot must contain (core schema + collector schema).
EXPECTED_TABLES = (
    "instruments", "prices", "decisions", "trades", "equity_curve",
    "filings", "collector_health",
)

# Sentinel: "resolve the R2 client from the environment" (tests inject fakes).
_FROM_ENV = object()


def snapshot_name(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"gpw-{now:%Y%m%d-%H%M%S}.db"


def make_snapshot(db_path: str | Path, backups_dir: str | Path,
                  *, now: datetime | None = None) -> Path:
    """Online snapshot of the live DB via VACUUM INTO (compact + consistent).

    Writes to a `.part` file and renames only after a passing quick_check, so
    a crash/OOM-kill/disk-full mid-copy can never leave a garbage file that
    matches the snapshot pattern (retention and `make status` trust the
    pattern). Stale `.part` leftovers from previous crashes are swept first.
    """
    backups_dir = Path(backups_dir)
    backups_dir.mkdir(parents=True, exist_ok=True)
    for stale in backups_dir.glob("*.part"):
        stale.unlink()
    target = backups_dir / snapshot_name(now)
    part = backups_dir / (target.name + ".part")
    if target.exists():
        target.unlink()  # same-second rerun: VACUUM INTO refuses to overwrite
    src = sqlite3.connect(str(db_path))
    try:
        src.execute("VACUUM INTO ?", (str(part),))
        check = sqlite3.connect(f"file:{part}?mode=ro", uri=True)
        try:
            row = check.execute("PRAGMA quick_check").fetchone()
            if not row or row[0] != "ok":
                raise RuntimeError(f"snapshot failed quick_check: {row}")
        finally:
            check.close()
        part.rename(target)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    finally:
        src.close()
    return target


def classify_keep(names: list[str], keep_daily: int, keep_monthly: int) -> set[str]:
    """Snapshot names to KEEP under the retention policy.

    Keep the newest `keep_daily` snapshots plus the EARLIEST snapshot of each
    of the newest `keep_monthly` calendar months (the monthly archive). Names
    not matching the snapshot pattern are ignored (and thus never deleted).
    """
    valid = sorted(n for n in names if SNAPSHOT_RE.match(n))
    keep: set[str] = set(valid[-keep_daily:]) if keep_daily > 0 else set()
    earliest_by_month: dict[str, str] = {}
    for name in valid:  # sorted ascending -> first seen per month is earliest
        month = name[4:10]  # YYYYMM
        earliest_by_month.setdefault(month, name)
    if keep_monthly > 0:
        for month in sorted(earliest_by_month)[-keep_monthly:]:
            keep.add(earliest_by_month[month])
    return keep


def apply_local_retention(backups_dir: str | Path, keep_daily: int,
                          keep_monthly: int,
                          protect: set[str] | None = None) -> list[str]:
    """Delete local snapshots outside the retention policy. Returns deletions.

    `protect` names are never deleted regardless of policy (the snapshot just
    taken must survive even a zero/misconfigured retention).
    """
    backups_dir = Path(backups_dir)
    names = [p.name for p in backups_dir.glob("gpw-*.db")]
    keep = classify_keep(names, keep_daily, keep_monthly) | (protect or set())
    deleted = []
    for name in sorted(names):
        if SNAPSHOT_RE.match(name) and name not in keep:
            (backups_dir / name).unlink()
            deleted.append(name)
    return deleted


# --- R2 (S3-compatible) ------------------------------------------------------

def r2_client():
    """boto3 S3 client for Cloudflare R2 from env vars, or None without creds.

    Env: R2_ENDPOINT_URL, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY (bucket and
    prefix live in config/backup.yaml — only secrets come from the env).
    All-or-nothing: a PARTIAL credential set is a misconfiguration (typo'd env
    file), not local-only mode — it raises instead of silently degrading.
    """
    env_vars = ("R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
    values = {v: os.environ.get(v) for v in env_vars}
    present = [v for v in env_vars if values[v]]
    if not present:
        return None
    if len(present) < len(env_vars):
        missing = [v for v in env_vars if not values[v]]
        raise RuntimeError(
            f"partial R2 credentials: {', '.join(present)} set but "
            f"{', '.join(missing)} missing — fix the env or unset all three"
        )
    try:
        import boto3
    except ImportError as exc:  # boto3 is an optional extra
        raise RuntimeError(
            "R2 credentials are set but boto3 is not installed; "
            "run: pip install -e '.[backup]'"
        ) from exc
    return boto3.client(
        "s3", endpoint_url=values["R2_ENDPOINT_URL"],
        aws_access_key_id=values["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=values["R2_SECRET_ACCESS_KEY"],
    )


def _remote_key(prefix: str, name: str) -> str:
    return f"{prefix.rstrip('/')}/{name}" if prefix else name


def list_remote_snapshots(client, bucket: str, prefix: str) -> list[str]:
    """Snapshot NAMES directly under bucket/prefix (paginated).

    Lists with the exact directory prefix (trailing slash) and requires the
    remainder of the key to be a bare snapshot name, so sibling prefixes
    (gpw-staging/, gpw2/) can never leak into retention math or restore-test —
    that leak would delete still-needed production monthlies.
    """
    dir_prefix = f"{prefix.rstrip('/')}/" if prefix else ""
    names: list[str] = []
    token = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": dir_prefix}
        if token:
            kwargs["ContinuationToken"] = token
        resp = client.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            remainder = obj["Key"][len(dir_prefix):]
            if "/" not in remainder and SNAPSHOT_RE.match(remainder):
                names.append(remainder)
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    return sorted(names)


def apply_remote_retention(client, bucket: str, prefix: str, keep_daily: int,
                           keep_monthly: int,
                           protect: set[str] | None = None) -> list[str]:
    names = list_remote_snapshots(client, bucket, prefix)
    keep = classify_keep(names, keep_daily, keep_monthly) | (protect or set())
    deleted = []
    for name in names:
        if name not in keep:
            client.delete_object(Bucket=bucket, Key=_remote_key(prefix, name))
            deleted.append(name)
    return deleted


# --- backup run ---------------------------------------------------------------

@dataclass
class BackupReport:
    snapshot: Path
    uploaded_key: str | None = None
    deleted_local: list[str] = field(default_factory=list)
    deleted_remote: list[str] = field(default_factory=list)
    pinged: bool = False

    def as_text(self) -> str:
        lines = [f"Snapshot: {self.snapshot}"]
        if self.uploaded_key:
            lines.append(f"Uploaded to R2: {self.uploaded_key}")
        else:
            lines.append("R2 upload skipped (no R2_* credentials in env) — snapshot is LOCAL ONLY.")
        if self.deleted_local:
            lines.append(f"Local retention: deleted {len(self.deleted_local)} old snapshot(s).")
        if self.deleted_remote:
            lines.append(f"Remote retention: deleted {len(self.deleted_remote)} old snapshot(s).")
        return "\n".join(lines)


def run_backup(db_path: str | Path, backup_cfg: dict, *, client=_FROM_ENV,
               now: datetime | None = None) -> BackupReport:
    """Snapshot -> upload (if creds) -> retention (local + remote) -> ping.

    The ping is the LAST step and fires only when everything configured
    succeeded — a backup that silently degraded must starve the dead-man's
    switch, not feed it. Set `r2.required: true` on the VPS so a lost env file
    fails the run instead of quietly downgrading to local-only.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"live DB not found: {db_path}")
    backups_dir = Path(backup_cfg.get("backups_dir", "data/backups"))
    r2_cfg = backup_cfg.get("r2") or {}
    retention = backup_cfg.get("retention") or {}
    # A backup run must never delete the snapshot it just produced, so the
    # daily floor is 1 and the new snapshot is additionally protected below.
    keep_daily = max(1, int(retention.get("daily", 14)))
    keep_monthly = max(0, int(retention.get("monthly", 12)))

    if client is _FROM_ENV:
        client = r2_client()  # raises on partial creds / missing boto3
    if client is None and r2_cfg.get("required"):
        raise RuntimeError(
            "r2.required is true in config/backup.yaml but no R2 credentials "
            "are present in the environment — refusing to degrade to local-only"
        )

    report = BackupReport(snapshot=make_snapshot(db_path, backups_dir, now=now))
    protect = {report.snapshot.name}

    if client is not None:
        bucket = r2_cfg["bucket"]
        prefix = r2_cfg.get("prefix", "")
        client.upload_file(str(report.snapshot), bucket,
                           _remote_key(prefix, report.snapshot.name))
        report.uploaded_key = _remote_key(prefix, report.snapshot.name)
        report.deleted_remote = apply_remote_retention(
            client, bucket, prefix, keep_daily, keep_monthly, protect=protect)

    report.deleted_local = apply_local_retention(
        backups_dir, keep_daily, keep_monthly, protect=protect)
    report.pinged = healthcheck.ping(HEALTHCHECK_ENV)
    return report


# --- restore verification -----------------------------------------------------

@dataclass
class RestoreReport:
    source: str
    integrity_ok: bool = False
    missing_tables: list[str] = field(default_factory=list)
    counts: dict[str, tuple[int, int]] = field(default_factory=dict)  # table -> (snapshot, live)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.integrity_ok and not self.missing_tables and not self.problems

    def as_text(self) -> str:
        lines = [f"Restore test on: {self.source}",
                 f"integrity_check: {'ok' if self.integrity_ok else 'FAILED'}"]
        for table, (snap, live) in sorted(self.counts.items()):
            lines.append(f"  {table:<18} snapshot={snap:<10} live={live}")
        for table in self.missing_tables:
            lines.append(f"  MISSING TABLE in snapshot: {table}")
        for problem in self.problems:
            lines.append(f"  PROBLEM: {problem}")
        lines.append("RESULT: " + ("OK — snapshot is restorable." if self.ok
                                   else "FAILED — this backup cannot be trusted."))
        return "\n".join(lines)


def _table_count(conn: sqlite3.Connection, table: str) -> int | None:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.Error:
        return None


def verify_snapshot(snapshot_path: str | Path, live_db_path: str | Path,
                    source_label: str | None = None) -> RestoreReport:
    """Open a snapshot, integrity-check it, and compare row counts vs live."""
    report = RestoreReport(source=source_label or str(snapshot_path))
    try:
        snap = sqlite3.connect(f"file:{Path(snapshot_path)}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        report.problems.append(f"cannot open snapshot: {exc}")
        return report
    # Read-only: a plain connect() would CREATE an empty DB at a wrong/missing
    # live path, silently disable every row-count comparison, and leave a stray
    # file that the next `make backup` would happily snapshot.
    try:
        live = sqlite3.connect(f"file:{Path(live_db_path)}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        snap.close()
        report.problems.append(f"cannot open live DB {live_db_path}: {exc}")
        return report
    try:
        try:
            row = snap.execute("PRAGMA integrity_check").fetchone()
            report.integrity_ok = bool(row) and row[0] == "ok"
        except sqlite3.Error as exc:
            report.problems.append(f"integrity_check failed to run: {exc}")
            return report
        for table in EXPECTED_TABLES:
            snap_count = _table_count(snap, table)
            if snap_count is None:
                report.missing_tables.append(table)
                continue
            live_count = _table_count(live, table)
            report.counts[table] = (snap_count, live_count if live_count is not None else -1)
            if live_count is not None and snap_count > live_count:
                report.problems.append(
                    f"{table}: snapshot has MORE rows ({snap_count}) than the live DB "
                    f"({live_count}) — live data loss or wrong snapshot?"
                )
    finally:
        snap.close()
        live.close()
    return report


def run_restore_test(db_path: str | Path, backup_cfg: dict,
                     *, client=_FROM_ENV) -> RestoreReport:
    """Pull the LATEST snapshot (R2 when creds are set, else local) and verify it."""
    backups_dir = Path(backup_cfg.get("backups_dir", "data/backups"))
    if client is _FROM_ENV:
        client = r2_client()

    if client is not None:
        r2_cfg = backup_cfg.get("r2") or {}
        bucket = r2_cfg["bucket"]
        prefix = r2_cfg.get("prefix", "")
        names = list_remote_snapshots(client, bucket, prefix)
        if not names:
            report = RestoreReport(source=f"r2://{bucket}/{prefix}")
            report.problems.append("no snapshots found in R2")
            return report
        latest = names[-1]
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / latest
            client.download_file(bucket, _remote_key(prefix, latest), str(local))
            return verify_snapshot(local, db_path,
                                   source_label=f"r2://{bucket}/{_remote_key(prefix, latest)}")

    local_names = sorted(p.name for p in backups_dir.glob("gpw-*.db")
                         if SNAPSHOT_RE.match(p.name))
    if not local_names:
        report = RestoreReport(source=str(backups_dir))
        report.problems.append("no local snapshots found — run `make backup` first")
        return report
    return verify_snapshot(backups_dir / local_names[-1], db_path)

"""Pack B: backups (VACUUM INTO snapshot, retention, R2 seam, restore test)."""
import sqlite3

import pytest

from app import backup as bkp
from app.db import connect, init_db
from app.ingestion import stooq

from tests.conftest import make_stooq_csv, synthetic_series


def _build_live_db(path, n=30):
    conn = connect(str(path))
    init_db(conn)
    iid = stooq.upsert_instrument(conn, {"ticker": "aaa", "name": "aaa", "sector": "x"})
    stooq.store_bars(conn, iid, stooq.parse_csv(make_stooq_csv(synthetic_series(n=n))))
    conn.commit()
    conn.close()
    return path


class FakeS3:
    """Minimal S3 double: upload_file / list_objects_v2 / delete_object / download_file."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def upload_file(self, filename, bucket, key):
        with open(filename, "rb") as fh:
            self.objects[key] = fh.read()

    def list_objects_v2(self, Bucket, Prefix, **kwargs):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def download_file(self, bucket, key, filename):
        with open(filename, "wb") as fh:
            fh.write(self.objects[key])


def _cfg(tmp_path, daily=14, monthly=12):
    return {
        "backups_dir": str(tmp_path / "backups"),
        "r2": {"bucket": "b", "prefix": "gpw"},
        "retention": {"daily": daily, "monthly": monthly},
    }


def test_snapshot_is_valid_sqlite_with_expected_tables(tmp_path):
    live = _build_live_db(tmp_path / "live.db")
    snap = bkp.make_snapshot(live, tmp_path / "backups")
    assert snap.exists() and bkp.SNAPSHOT_RE.match(snap.name)
    conn = sqlite3.connect(str(snap))
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert set(bkp.EXPECTED_TABLES) <= tables
    assert conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 30
    conn.close()


def test_classify_keep_newest_daily_plus_monthly_firsts():
    names = [
        "gpw-20260401-020000.db", "gpw-20260415-020000.db",  # April: first kept as monthly
        "gpw-20260501-020000.db", "gpw-20260520-020000.db",  # May
        "gpw-20260601-020000.db", "gpw-20260602-020000.db",
        "gpw-20260603-020000.db",                              # June
        "not-a-snapshot.db",
    ]
    keep = bkp.classify_keep(names, keep_daily=2, keep_monthly=2)
    # newest 2 daily
    assert "gpw-20260602-020000.db" in keep and "gpw-20260603-020000.db" in keep
    # earliest of the 2 newest months (May, June)
    assert "gpw-20260501-020000.db" in keep and "gpw-20260601-020000.db" in keep
    # April monthly falls outside keep_monthly=2; mid-May daily not kept
    assert "gpw-20260401-020000.db" not in keep
    assert "gpw-20260520-020000.db" not in keep
    # non-matching names are never considered (and never deleted by callers)
    assert "not-a-snapshot.db" not in keep


def test_local_retention_deletes_only_expired_snapshots(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    names = ["gpw-20260601-020000.db", "gpw-20260602-020000.db",
             "gpw-20260603-020000.db", "unrelated.db"]
    for n in names:
        (backups / n).write_bytes(b"x")
    deleted = bkp.apply_local_retention(backups, keep_daily=2, keep_monthly=1)
    # 0601 is both oldest daily AND the June monthly-first -> kept
    assert deleted == []
    deleted = bkp.apply_local_retention(backups, keep_daily=1, keep_monthly=0)
    assert deleted == ["gpw-20260601-020000.db", "gpw-20260602-020000.db"]
    assert (backups / "unrelated.db").exists()


def test_run_backup_uploads_and_prunes_remote_with_fake_client(tmp_path, monkeypatch):
    monkeypatch.delenv("HEALTHCHECK_URL_BACKUP", raising=False)
    live = _build_live_db(tmp_path / "live.db")
    fake = FakeS3()
    # pre-existing old remote snapshots beyond retention
    fake.objects["gpw/gpw-20200101-020000.db"] = b"old"
    fake.objects["gpw/gpw-20200102-020000.db"] = b"old"
    report = bkp.run_backup(live, _cfg(tmp_path, daily=1, monthly=1), client=fake)
    assert report.uploaded_key.startswith("gpw/gpw-")
    assert report.uploaded_key in {f"gpw/{n}" for n in
                                   [k.rsplit('/', 1)[-1] for k in fake.objects]}
    # retention kept: the new snapshot (daily=1) + earliest of its month +
    # earliest of Jan-2020 month... monthly=1 keeps only the NEWEST month
    remaining = sorted(fake.objects)
    assert all("2020" not in k for k in remaining), f"old snapshots not pruned: {remaining}"


def test_run_backup_without_creds_is_local_only(tmp_path, monkeypatch):
    monkeypatch.delenv("HEALTHCHECK_URL_BACKUP", raising=False)
    live = _build_live_db(tmp_path / "live.db")
    report = bkp.run_backup(live, _cfg(tmp_path), client=None)
    assert report.uploaded_key is None
    assert report.snapshot.exists()
    assert "LOCAL ONLY" in report.as_text()


def test_run_backup_pings_healthcheck_on_success(tmp_path, monkeypatch):
    live = _build_live_db(tmp_path / "live.db")
    pinged = []
    monkeypatch.setattr(bkp.healthcheck, "ping", lambda env: pinged.append(env) or True)
    report = bkp.run_backup(live, _cfg(tmp_path), client=None)
    assert report.pinged
    assert pinged == [bkp.HEALTHCHECK_ENV]


def test_restore_test_ok_on_good_snapshot(tmp_path):
    live = _build_live_db(tmp_path / "live.db")
    bkp.make_snapshot(live, tmp_path / "backups")
    report = bkp.run_restore_test(live, _cfg(tmp_path), client=None)
    assert report.integrity_ok
    assert report.ok, report.as_text()
    assert report.counts["prices"] == (30, 30)


def test_restore_test_fails_on_corrupt_snapshot(tmp_path):
    live = _build_live_db(tmp_path / "live.db")
    snap = bkp.make_snapshot(live, tmp_path / "backups")
    # corrupt everything past the SQLite header — a targeted 256-byte patch can
    # land in an unused page and legitimately pass integrity_check
    data = bytearray(snap.read_bytes())
    data[100:] = b"\xff" * (len(data) - 100)
    snap.write_bytes(bytes(data))
    report = bkp.run_restore_test(live, _cfg(tmp_path), client=None)
    assert not report.ok


def test_restore_test_flags_snapshot_with_more_rows_than_live(tmp_path):
    live = _build_live_db(tmp_path / "live.db", n=30)
    bkp.make_snapshot(live, tmp_path / "backups")
    # simulate live data loss after the snapshot
    conn = connect(str(live))
    conn.execute("DELETE FROM prices")
    conn.commit()
    conn.close()
    report = bkp.run_restore_test(live, _cfg(tmp_path), client=None)
    assert not report.ok
    assert any("MORE rows" in p for p in report.problems)


def test_restore_test_pulls_latest_from_r2_when_client_present(tmp_path):
    live = _build_live_db(tmp_path / "live.db")
    fake = FakeS3()
    bkp.run_backup(live, _cfg(tmp_path), client=fake)
    report = bkp.run_restore_test(live, _cfg(tmp_path), client=fake)
    assert report.source.startswith("r2://b/gpw/")
    assert report.ok, report.as_text()


def test_restore_test_without_any_snapshot_fails_loudly(tmp_path):
    live = _build_live_db(tmp_path / "live.db")
    report = bkp.run_restore_test(live, _cfg(tmp_path), client=None)
    assert not report.ok
    assert any("no local snapshots" in p for p in report.problems)


def test_verify_snapshot_missing_live_db_fails_without_creating_it(tmp_path):
    """A wrong live path must fail loudly, never create a stray empty DB."""
    live = _build_live_db(tmp_path / "live.db")
    snap = bkp.make_snapshot(live, tmp_path / "backups")
    wrong = tmp_path / "does-not-exist.db"
    report = bkp.verify_snapshot(snap, wrong)
    assert not report.ok
    assert any("cannot open live DB" in p for p in report.problems)
    assert not wrong.exists(), "restore-test created a stray empty live DB"


def test_snapshot_part_files_never_match_retention_or_status(tmp_path):
    """A crashed VACUUM INTO leaves only a .part file — invisible to the
    snapshot pattern, so retention/status/restore never trust it."""
    live = _build_live_db(tmp_path / "live.db")
    backups = tmp_path / "backups"
    backups.mkdir()
    leftover = backups / "gpw-20260101-020000.db.part"
    leftover.write_bytes(b"partial garbage")
    assert not bkp.SNAPSHOT_RE.match(leftover.name)
    snap = bkp.make_snapshot(live, backups)
    assert snap.exists()
    assert not leftover.exists(), "stale .part leftovers must be swept"
    assert not list(backups.glob("*.part"))


def test_retention_zero_daily_never_deletes_fresh_snapshot(tmp_path, monkeypatch):
    monkeypatch.delenv("HEALTHCHECK_URL_BACKUP", raising=False)
    live = _build_live_db(tmp_path / "live.db")
    report = bkp.run_backup(live, _cfg(tmp_path, daily=0, monthly=0), client=None)
    assert report.snapshot.exists(), "retention deleted the snapshot it just took"


def test_remote_listing_ignores_sibling_prefixes(tmp_path):
    fake = FakeS3()
    fake.objects["gpw/gpw-20260601-020000.db"] = b"prod"
    fake.objects["gpw-staging/gpw-20260701-020000.db"] = b"staging"
    fake.objects["gpw2/gpw-20260702-020000.db"] = b"other"
    names = bkp.list_remote_snapshots(fake, "b", "gpw")
    assert names == ["gpw-20260601-020000.db"], (
        "sibling prefixes leaked into the listing — retention could delete "
        "still-needed production monthlies"
    )


def test_partial_r2_credentials_raise(monkeypatch):
    monkeypatch.setenv("R2_ENDPOINT_URL", "https://r2.example")
    monkeypatch.delenv("R2_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("R2_SECRET_ACCESS_KEY", raising=False)
    with pytest.raises(RuntimeError, match="partial R2 credentials"):
        bkp.r2_client()


def test_r2_required_refuses_local_only_degradation(tmp_path):
    live = _build_live_db(tmp_path / "live.db")
    cfg = _cfg(tmp_path)
    cfg["r2"]["required"] = True
    with pytest.raises(RuntimeError, match="refusing to degrade"):
        bkp.run_backup(live, cfg, client=None)


def test_restore_test_cli_reports_creds_errors_gracefully(tmp_path, monkeypatch):
    from app import cli

    def boom():
        raise RuntimeError("partial R2 credentials: ...")
    monkeypatch.setattr(bkp, "r2_client", boom)
    rc = cli.main(["--db", str(tmp_path / "x.db"), "restore-test"])
    assert rc == 1  # handled error message, not a traceback


def test_healthcheck_ping_skips_silently_without_env(monkeypatch):
    from app.alerts import healthcheck
    monkeypatch.delenv("HC_TEST_URL", raising=False)
    calls = []
    monkeypatch.setattr(healthcheck.requests, "get",
                        lambda url, timeout: calls.append(url))
    assert healthcheck.ping("HC_TEST_URL") is False
    assert calls == []


def test_healthcheck_ping_fires_and_never_raises(monkeypatch):
    from app.alerts import healthcheck
    monkeypatch.setenv("HC_TEST_URL", "https://hc.example/ping")
    calls = []
    monkeypatch.setattr(healthcheck.requests, "get",
                        lambda url, timeout: calls.append(url))
    assert healthcheck.ping("HC_TEST_URL") is True
    assert calls == ["https://hc.example/ping"]

    def boom(url, timeout):
        raise OSError("network down")
    monkeypatch.setattr(healthcheck.requests, "get", boom)
    assert healthcheck.ping("HC_TEST_URL") is False  # swallowed, not raised


def test_collector_pings_healthcheck_only_on_healthy_cycle(tmp_path, monkeypatch):
    from app.ingestion import collect_news, news_collector

    pings = []
    monkeypatch.setattr(collect_news.healthcheck, "ping",
                        lambda env: pings.append(env) or True)
    config = {"db_path": str(tmp_path / "c.db")}

    healthy = news_collector.CycleStats(feeds_configured=1, feeds_polled=1)
    monkeypatch.setattr(news_collector, "run_cycle", lambda conn, cfg_: healthy)
    assert collect_news.run_once(config) == 0
    assert pings == ["HEALTHCHECK_URL_COLLECT"]

    pings.clear()
    unhealthy = news_collector.CycleStats(feeds_configured=1, feeds_failed=1)
    monkeypatch.setattr(news_collector, "run_cycle", lambda conn, cfg_: unhealthy)
    assert collect_news.run_once(config) == 2
    assert pings == []  # a degraded cycle must NOT feed the dead-man's switch

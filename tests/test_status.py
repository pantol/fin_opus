"""Pack B: `make status` — deployment liveness report + staleness alerting."""
from datetime import datetime, timedelta, timezone

from app import status as statusmod
from app.db import connect, init_db
from app.ingestion import filings_db, stooq

from tests.conftest import make_stooq_csv, synthetic_series


NOW = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)


def _cfg(tmp_path, collector_stale_minutes=120, backup_stale_hours=36):
    return {
        "backups_dir": str(tmp_path / "backups"),
        "retention": {"daily": 14, "monthly": 12},
        "status": {"collector_stale_minutes": collector_stale_minutes,
                   "backup_stale_hours": backup_stale_hours},
    }


def _seed_prices(conn):
    iid = stooq.upsert_instrument(conn, {"ticker": "aaa", "name": "aaa"})
    stooq.store_bars(conn, iid, stooq.parse_csv(make_stooq_csv(synthetic_series(n=10))))
    conn.commit()


def _fresh_backup(tmp_path, age_hours=1.0):
    backups = tmp_path / "backups"
    backups.mkdir(exist_ok=True)
    taken = NOW - timedelta(hours=age_hours)
    (backups / f"gpw-{taken:%Y%m%d-%H%M%S}.db").write_bytes(b"x")


def test_all_fresh_reports_ok(conn, tmp_path):
    _seed_prices(conn)
    filings_db.mark_run_success(conn, new_items=3)  # writes "now" (UTC); NOW is fixed
    _fresh_backup(tmp_path)
    # use a NOW far enough in the future config-wise? No: mark_run_success uses the
    # real clock, so pass a wide threshold to keep this test time-independent.
    report = statusmod.run_status(conn, _cfg(tmp_path, collector_stale_minutes=10**9,
                                             backup_stale_hours=10**9))
    assert report.ok, report.as_text()
    assert "STATUS: OK" in report.as_text()


def test_stale_collector_flagged_with_polish_alert(conn, tmp_path):
    _seed_prices(conn)
    _fresh_backup(tmp_path)
    conn.execute(
        "INSERT INTO collector_health (id, last_successful_run, last_cycle_new_items)"
        " VALUES (1, ?, 0)",
        ((NOW - timedelta(hours=8)).isoformat(),))
    conn.commit()
    report = statusmod.run_status(conn, _cfg(tmp_path), now=NOW)
    assert not report.ok
    assert any("kolektor" in s for s in report.stale)
    assert "Status GPW" in report.alert_pl()


def test_collector_never_ran_is_stale(conn, tmp_path):
    _seed_prices(conn)
    _fresh_backup(tmp_path)
    report = statusmod.run_status(conn, _cfg(tmp_path), now=NOW)
    assert any("kolektor" in s for s in report.stale)


def test_missing_backups_flagged(conn, tmp_path):
    _seed_prices(conn)
    filings_db.mark_run_success(conn, new_items=0)
    report = statusmod.run_status(conn, _cfg(tmp_path, collector_stale_minutes=10**9))
    assert any("kopii zapasowych" in s for s in report.stale)


def test_old_backup_flagged(conn, tmp_path):
    _seed_prices(conn)
    _fresh_backup(tmp_path, age_hours=72.0)
    report = statusmod.run_status(conn, _cfg(tmp_path, collector_stale_minutes=10**9),
                                  now=NOW)
    assert any("przestarzala" in s for s in report.stale)


def test_empty_prices_flagged(conn, tmp_path):
    _fresh_backup(tmp_path)
    filings_db.mark_run_success(conn, new_items=0)
    report = statusmod.run_status(conn, _cfg(tmp_path, collector_stale_minutes=10**9))
    assert any("cenowych" in s for s in report.stale)


def test_init_db_now_creates_collector_schema(conn):
    """Schema unification: every CLI command sees filings/collector_health."""
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "filings" in tables and "collector_health" in tables


def test_paper_not_started_is_informational(conn, tmp_path):
    _seed_prices(conn)
    filings_db.mark_run_success(conn, new_items=0)
    _fresh_backup(tmp_path)
    report = statusmod.run_status(conn, _cfg(tmp_path, collector_stale_minutes=10**9,
                                             backup_stale_hours=10**9))
    assert "paper: not started" in report.as_text()
    assert not any("paper" in s for s in report.stale)


def test_paper_loop_behind_is_flagged(conn, tmp_path):
    _seed_prices(conn)  # 10 sessions of bars
    filings_db.mark_run_success(conn, new_items=0)
    _fresh_backup(tmp_path)
    first = conn.execute("SELECT MIN(date) FROM prices WHERE adjusted = 0").fetchone()[0]
    conn.execute(
        "INSERT INTO paper_state (user_id, cash, peak_equity, initial_capital,"
        " inception_date, last_settled_date, config_hash, updated_at)"
        " VALUES ('paper:default', 100000, 100000, 100000, ?, ?, 'h', ?)",
        (first, first, NOW.isoformat()))
    conn.commit()
    report = statusmod.run_status(conn, _cfg(tmp_path, collector_stale_minutes=10**9,
                                             backup_stale_hours=10**9))
    assert "paper[paper:default]" in report.as_text()
    assert any("petla paper" in s for s in report.stale)

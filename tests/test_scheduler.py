"""Scheduler (Stage 1): slot math, journal idempotency, catch-up policy,
chain semantics, digest content, monitor user_id scoping. ZERO decision
changes — these tests prove the clock never invents behavior of its own."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import scheduler
from app.alerts import digest as digest_mod
from app.alerts import monitor

WARSAW = ZoneInfo("Europe/Warsaw")

CFG = {
    "window": {"start": "07:00", "end": "19:00", "tz": "Europe/Warsaw",
               "days": ["mon", "tue", "wed", "thu", "fri"]},
    "tick_seconds": 30,
    "jobs": [
        {"name": "collect", "every_min": 15},
        {"name": "intraday", "every_min": 5, "window": ["09:00", "17:10"]},
        {"name": "digest", "at": "07:30"},
        {"name": "evening", "at": "19:30", "chain": ["ingest", "llm", "signals"]},
        {"name": "health", "at": "19:45", "chain": ["status", "backup"]},
    ],
}

# 2026-07-24 is a Friday; 2026-07-25 a Saturday.
FRIDAY = datetime(2026, 7, 24, 12, 0, tzinfo=WARSAW)
SATURDAY = datetime(2026, 7, 25, 12, 0, tzinfo=WARSAW)


def _job(name):
    return next(j for j in CFG["jobs"] if j["name"] == name)


# --- slot math ---------------------------------------------------------------

def test_every_min_slots_respect_global_window():
    slots = scheduler.job_slots_for_day(_job("collect"), FRIDAY, CFG)
    assert slots[0].strftime("%H:%M") == "07:00"
    assert slots[-1].strftime("%H:%M") == "19:00"
    assert all((s.minute % 15) == 0 for s in slots)


def test_per_job_window_intersects_global():
    slots = scheduler.job_slots_for_day(_job("intraday"), FRIDAY, CFG)
    assert slots[0].strftime("%H:%M") == "09:00"
    assert slots[-1].strftime("%H:%M") == "17:10"


def test_at_job_may_sit_outside_window():
    (slot,) = scheduler.job_slots_for_day(_job("evening"), FRIDAY, CFG)
    assert slot.strftime("%H:%M") == "19:30"  # outside 07-19 deliberately


def test_weekend_has_no_slots():
    for job in CFG["jobs"]:
        assert scheduler.job_slots_for_day(job, SATURDAY, CFG) == []


# --- journal idempotency + catch-up ------------------------------------------

def test_slot_never_runs_twice(conn):
    now = datetime(2026, 7, 24, 7, 31, tzinfo=WARSAW)
    ran = []
    res1 = scheduler.fire_due(conn, CFG, now, None,
                              digest_fn=lambda db: ran.append("digest") or "ok",
                              run_step_fn=lambda s, db: ran.append(s) or (0, ""))
    res2 = scheduler.fire_due(conn, CFG, now, None,
                              digest_fn=lambda db: ran.append("digest") or "ok",
                              run_step_fn=lambda s, db: ran.append(s) or (0, ""))
    assert any(r.job == "digest" for r in res1)
    assert res2 == []  # every slot journaled — second pass is a no-op
    assert ran.count("digest") == 1


def test_catchup_runs_only_latest_missed_every_min_slot(conn):
    # Laptop slept 07:00-12:02: collect has ~21 missed slots; exactly one runs.
    now = datetime(2026, 7, 24, 12, 2, tzinfo=WARSAW)
    ran = []
    results = scheduler.fire_due(conn, CFG, now, None,
                                 digest_fn=lambda db: "ok",
                                 run_step_fn=lambda s, db: ran.append(s) or (0, ""))
    collect = [r for r in results if r.job == "collect"]
    assert ran.count("collect") == 1
    assert sum(1 for r in collect if r.status == "ok") == 1
    skipped = [r for r in collect if r.status == "skipped"]
    assert len(skipped) >= 15  # older missed slots journaled, not hidden
    # the one that RAN is the latest slot (12:00)
    ok = next(r for r in collect if r.status == "ok")
    assert ok.scheduled_for.endswith("12:00:00+02:00")


def test_missed_at_job_fires_same_day_but_not_across_days(conn):
    ran = []
    # 20:00 same day: evening (19:30) missed -> still fires.
    now = datetime(2026, 7, 24, 20, 0, tzinfo=WARSAW)
    scheduler.fire_due(conn, CFG, now, None, digest_fn=lambda db: "ok",
                       run_step_fn=lambda s, db: ran.append(s) or (0, ""))
    assert "signals" in ran
    # Next Monday morning: Friday's missed slots are gone (no resurrection).
    ran.clear()
    monday = datetime(2026, 7, 27, 7, 1, tzinfo=WARSAW)
    results = scheduler.fire_due(conn, CFG, monday, None, digest_fn=lambda db: "ok",
                                 run_step_fn=lambda s, db: ran.append(s) or (0, ""))
    assert all(r.scheduled_for.startswith("2026-07-27") for r in results)


def test_abort_stale_running_is_terminal(conn):
    now = datetime(2026, 7, 24, 19, 40, tzinfo=WARSAW)
    slot = datetime(2026, 7, 24, 19, 30, tzinfo=WARSAW)
    conn.execute(
        "INSERT INTO schedule_runs (job, scheduled_for, started_at, status) "
        "VALUES ('evening', ?, ?, 'running')", (slot.isoformat(), now.isoformat()))
    conn.commit()
    assert scheduler.abort_stale_running(conn, now) == 1
    row = conn.execute("SELECT status FROM schedule_runs").fetchone()
    assert row["status"] == "aborted"
    # the aborted slot is spent: nothing re-fires it
    ran = []
    scheduler.fire_due(conn, CFG, now, None, digest_fn=lambda db: "ok",
                       run_step_fn=lambda s, db: ran.append(s) or (0, ""))
    assert "signals" not in ran


# --- chain semantics ---------------------------------------------------------

def test_required_step_failure_stops_chain():
    calls = []

    def fake_run(step, db):
        calls.append(step)
        return (2, "boom") if step == "ingest" else (0, "")

    status, detail = scheduler.execute_job(_job("evening"), None, run_step_fn=fake_run)
    assert status == "failed"
    assert calls == ["ingest"]  # llm/signals never ran on a failed ingest


def test_optional_llm_failure_does_not_block_signals():
    calls = []

    def fake_run(step, db):
        calls.append(step)
        return (3, "budget exhausted") if step == "llm" else (0, "")

    status, detail = scheduler.execute_job(_job("evening"), None, run_step_fn=fake_run)
    assert status == "ok"
    assert calls == ["ingest", "llm", "signals"]
    assert "optional" in detail


def test_scheduler_never_passes_accept_config_change():
    argv = scheduler.JOB_COMMANDS["signals"]
    assert "--accept-config-change" not in argv


# --- digest ------------------------------------------------------------------

def _seed_book(conn):
    conn.execute("INSERT INTO instruments (id, ticker, name) VALUES (1, 'pko', 'PKO')")
    conn.execute(
        "INSERT INTO prices (instrument_id, date, as_of_date, open, high, low, close, volume, adjusted, source) "
        "VALUES (1, '2026-07-23', '2026-07-23', 44, 46, 43, 45.10, 1000, 0, 'gpw')")
    conn.execute(
        "INSERT INTO paper_state (user_id, cash, peak_equity, initial_capital, inception_date, "
        "last_settled_date, config_hash, updated_at) "
        "VALUES ('paper:default', 1000, 100000, 100000, '2026-07-01', '2026-07-23', 'h', 'now')")
    conn.execute(
        "INSERT INTO positions (user_id, instrument_id, qty, entry_date, entry_price, stop_price, status) "
        "VALUES ('paper:default', 1, 10, '2026-07-10', 40.0, 43.80, 'OPEN')")
    conn.commit()


def test_digest_shows_positions_stops_and_filings(conn):
    _seed_book(conn)
    now = datetime(2026, 7, 24, 7, 30, tzinfo=WARSAW)
    conn.execute(
        "INSERT INTO filings (source, issuer_isin, instrument_id, title, "
        "published_at, fetched_at, content_hash, dedup_key) "
        "VALUES ('pap', 'PLPKO0000016', 1, 'Raport biezacy 21/2026', ?, ?, 'ch1', 'dk1')",
        ((now.isoformat()), now.isoformat()))
    conn.execute(
        "INSERT INTO llm_features (instrument_id, as_of_date, llm_score, created_at) "
        "VALUES (1, '2026-07-23', -0.4, 'now')")
    conn.commit()
    card = digest_mod.build_digest(conn, now=now)
    assert card is not None
    assert "PKO: 45.10 PLN, stop 43.80" in card
    assert "Nowe komunikaty (24h): 1" in card
    assert "[LLM -0.40]" in card
    assert "decyzje zapadaja WYLACZNIE wieczorem" in card


def test_digest_none_when_nothing_to_say(conn):
    assert digest_mod.build_digest(
        conn, now=datetime(2026, 7, 24, 7, 30, tzinfo=WARSAW)) is None


# --- monitor scoping (the Stage-1 bug fix) -----------------------------------

def _seed_two_books(conn):
    conn.execute("INSERT INTO instruments (id, ticker, name) VALUES (1, 'pko', 'PKO')")
    for uid in ("paper:default", "paper:dron", "default"):
        conn.execute(
            "INSERT INTO positions (user_id, instrument_id, qty, entry_date, entry_price, stop_price, status) "
            "VALUES (?, 1, 10, '2026-07-10', 40.0, 44.0, 'OPEN')", (uid,))
    conn.execute(
        "INSERT INTO prices_intraday (instrument_id, bar_start, interval_min, open, high, low, close, volume, as_of_ts, source) "
        "VALUES (1, '2026-07-24T10:00:00+02:00', 5, 43, 43, 42, 42.50, 100, '2026-07-24T10:16:00+02:00', 'yahoo_delayed')")
    conn.commit()


def test_monitor_watches_only_paper_books(conn):
    _seed_two_books(conn)
    now = datetime(2026, 7, 24, 10, 20, tzinfo=WARSAW)
    warnings = monitor.check_positions(conn, send_fn=None, now=now)
    assert {w["user_id"] for w in warnings} == {"paper:default", "paper:dron"}
    assert all(w["state"] == monitor.STOP_BREACH for w in warnings)


def test_monitor_user_id_filter_narrows_to_one_book(conn):
    _seed_two_books(conn)
    now = datetime(2026, 7, 24, 10, 20, tzinfo=WARSAW)
    warnings = monitor.check_positions(conn, send_fn=None, now=now,
                                       user_id="paper:dron")
    assert [w["user_id"] for w in warnings] == ["paper:dron"]

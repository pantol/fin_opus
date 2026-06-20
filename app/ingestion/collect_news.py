"""Entrypoints for the ESPI/EBI + news collector.

One-shot (single cycle, then exit):
    python -m app.ingestion.collect_news
    make collect

Scheduler loop (poll every N minutes; N from config):
    python -m app.ingestion.collect_news --loop
    make collect-loop

Runs standalone on a VPS, separate from the backtest pipeline, writing into the
same SQLite DB. See README "ESPI/EBI + news collector" for cron/systemd setup.
ZERO LLM in this path.
"""
from __future__ import annotations

import argparse
import logging
import sys

from app import config as cfg
from app.db import connect
from app.ingestion import filings_db, news_collector


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _db_path(config: dict) -> str | None:
    # null in config -> use the main app DB (DEFAULT_DB_PATH via connect()).
    return config.get("db_path")


def run_once(config: dict | None = None) -> int:
    """Run a single collection cycle. Returns process exit code."""
    config = config or cfg.load_news_sources()
    conn = connect(_db_path(config))
    try:
        filings_db.ensure_schema(conn)
        log = logging.getLogger("news_collector")
        try:
            stats = news_collector.run_cycle(conn, config)
        except Exception as exc:  # noqa: BLE001 - record health, then surface
            filings_db.mark_run_error(conn, str(exc))
            log.exception("cycle crashed")
            return 1
        # Non-zero on an unhealthy cycle so VPS cron/monitoring detects a degraded
        # collector (a feed down, or a feed still left as a placeholder URL) even
        # though the process did not crash.
        if not stats.healthy:
            log.error(
                "unhealthy cycle: %d/%d feeds polled, %d failed, %d skipped",
                stats.feeds_polled, stats.feeds_configured,
                stats.feeds_failed, stats.feeds_skipped,
            )
            return 2
        return 0
    finally:
        conn.close()


def run_loop(config: dict | None = None) -> int:
    """Run cycles forever on a schedule (APScheduler BlockingScheduler)."""
    from apscheduler.schedulers.blocking import BlockingScheduler

    config = config or cfg.load_news_sources()
    interval = int(config.get("poll_interval_minutes", 10))
    log = logging.getLogger("news_collector")
    log.info("starting scheduler: every %d minute(s)", interval)

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        lambda: run_once(config),
        "interval", minutes=interval,
        next_run_time=__import__("datetime").datetime.now(),  # run immediately, then on interval
        max_instances=1, coalesce=True,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler stopped")
    return 0


def main(argv=None) -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(
        prog="app.ingestion.collect_news",
        description="ESPI/EBI + news collector (standalone, ZERO LLM).",
    )
    parser.add_argument("--loop", action="store_true",
                        help="Run continuously on a schedule (interval from config).")
    args = parser.parse_args(argv)
    return run_loop() if args.loop else run_once()


if __name__ == "__main__":
    sys.exit(main())

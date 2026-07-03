.PHONY: setup test ingest ingest-offline features backtest backtest-offline ab ab-offline llm collect collect-loop refdata check-data backup restore-test status clean

# Prefer the local virtualenv if present, else fall back to python3.
PYTHON ?= $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)

setup:
	python3 -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -e ".[dev]"

test:
	$(PYTHON) -m pytest

ingest:
	$(PYTHON) -m app.cli ingest

ingest-offline:
	$(PYTHON) -m app.cli ingest --offline

# Deep anti-survivorship backfill from the GPW archive: every PLN instrument
# in every session file (dead companies included). ~1 request/second per
# session day — a multi-year range takes HOURS; run it once, then plain
# `make ingest` stays incremental.
backfill:
	$(PYTHON) -m app.cli ingest --full --start 2015-01-02

features:
	$(PYTHON) -m app.cli features

# Load index membership + corporate action fixtures (config/*.yaml) and derive
# the adjusted price series for instruments that have actions.
refdata:
	$(PYTHON) -m app.cli refdata

# Data-quality report: missing sessions, zero/negative volume, unexplained
# price jumps, stale tickers. Telegram alert on issues (dry-run without token).
check-data:
	$(PYTHON) -m app.cli check-data

# Full chain: ingest (live Stooq) -> features -> walk-forward backtest -> metrics.
backtest: ingest
	$(PYTHON) -m app.cli backtest

# Same chain but with deterministic DEMO data (offline; NOT real prices).
backtest-offline: ingest-offline
	$(PYTHON) -m app.cli backtest

# A/B: baseline vs baseline+LLM (reads pre-materialized LLM features; no LLM call).
ab:
	$(PYTHON) -m app.cli ab

# A/B on deterministic DEMO data (offline; NOT real prices).
ab-offline: ingest-offline
	$(PYTHON) -m app.cli ab

# Materialize point-in-time LLM features from collected filings (calls
# OpenRouter; needs OPENROUTER_API_KEY). backtest/ab then read the
# materialized rows with NO LLM call — the LLM is only an INPUT.
llm:
	$(PYTHON) -m app.cli llm

# ESPI/EBI + news collector (standalone, ZERO LLM). One-shot cycle.
collect:
	$(PYTHON) -m app.ingestion.collect_news

# Same collector, but run forever on the configured schedule (VPS daemon).
collect-loop:
	$(PYTHON) -m app.ingestion.collect_news --loop

# Online DB snapshot (VACUUM INTO — never a live-file copy), push to R2 when
# R2_* env credentials exist, apply retention (config/backup.yaml).
backup:
	$(PYTHON) -m app.cli backup

# A backup that was never restored is not a backup: pull the latest snapshot,
# integrity-check it, compare row counts vs the live DB. Run monthly.
restore-test:
	$(PYTHON) -m app.cli restore-test

# One command to verify everything is alive: prices, collector heartbeat,
# filings backlog, newest backup. Non-zero exit + Telegram alert when stale.
status:
	$(PYTHON) -m app.cli status

clean:
	rm -f data/*.db
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name .pytest_cache -prune -exec rm -rf {} +

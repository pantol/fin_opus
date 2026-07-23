.PHONY: setup test ingest ingest-offline features backtest backtest-offline ab ab-offline llm collect collect-loop intraday intraday-loop refdata check-data backup restore-test status signals label eval-llm web clean

# Prefer the local virtualenv if present, else fall back to python3.
PYTHON ?= $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)

# Demo (synthetic) data lives in its OWN database: the provenance guard
# refuses to mix demo and real bars in one file, so the offline targets must
# never touch the real data/gpw.db.
DEMO_DB ?= data/demo.db

setup:
	python3 -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -e ".[dev]"

test:
	$(PYTHON) -m pytest

ingest:
	$(PYTHON) -m app.cli ingest

ingest-offline:
	$(PYTHON) -m app.cli --db $(DEMO_DB) ingest --offline

# Deep anti-survivorship backfill from the GPW archive: every PLN instrument
# in every session file (dead companies included). ~1 request/second per
# session day — a multi-year range takes HOURS; run it once. With
# universe.mode=full (backtest.yaml) plain `make ingest` then keeps the WHOLE
# market fresh incrementally (the session file already contains everything, so
# it costs zero extra requests) and resumes after the last full-market session.
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
# Runs entirely against $(DEMO_DB) so it works alongside a real data/gpw.db.
backtest-offline: ingest-offline
	$(PYTHON) -m app.cli --db $(DEMO_DB) backtest

# A/B: baseline vs baseline+LLM (reads pre-materialized LLM features; no LLM call).
ab:
	$(PYTHON) -m app.cli ab

# A/B on deterministic DEMO data (offline; NOT real prices). Uses $(DEMO_DB).
ab-offline: ingest-offline
	$(PYTHON) -m app.cli --db $(DEMO_DB) ab

# Materialize point-in-time LLM features from collected filings (calls
# OpenRouter; needs OPENROUTER_API_KEY). backtest/ab then read the
# materialized rows with NO LLM call — the LLM is only an INPUT.
llm:
	$(PYTHON) -m app.cli llm

# Label collected filings for the golden eval set (interactive; ZERO LLM).
label:
	$(PYTHON) -m app.cli label

# Prompt-regression harness: run the CURRENT research prompt against the
# golden set; accuracy + per-class F1 vs your labels, history in eval_runs.
# README rule: no prompt/model change ships if it regresses here.
eval-llm:
	$(PYTHON) -m app.cli eval-llm

# ESPI/EBI + news collector (standalone, ZERO LLM). One-shot cycle.
collect:
	$(PYTHON) -m app.ingestion.collect_news

# Same collector, but run forever on the configured schedule (VPS daemon).
collect-loop:
	$(PYTHON) -m app.ingestion.collect_news --loop

# Delayed intraday recorder + stop monitor (informational tier, ZERO
# decisions; ~15-min delayed free feed). One-shot cycle.
intraday:
	$(PYTHON) -m app.ingestion.intraday

# Same recorder, but run forever every N minutes inside the session window.
intraday-loop:
	$(PYTHON) -m app.ingestion.intraday --loop

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

# Daily paper-trading run: ingest today's session, settle yesterday's pending
# orders at today's open, decide today's signals, send Polish alert cards.
# Cron (evening, Warsaw): 30 19 * * 1-5  cd /opt/fin_opus && make signals
signals: ingest
	$(PYTHON) -m app.cli signals

# Read-only per-user web dashboard (display layer, ZERO decisions).
# Serves data/gpw.db on http://127.0.0.1:8765 by default.
web:
	$(PYTHON) -m app.cli web

clean:
	rm -f data/*.db
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name .pytest_cache -prune -exec rm -rf {} +

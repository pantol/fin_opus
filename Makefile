.PHONY: setup test ingest ingest-offline features backtest backtest-offline clean

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

features:
	$(PYTHON) -m app.cli features

# Full chain: ingest (live Stooq) -> features -> walk-forward backtest -> metrics.
backtest: ingest
	$(PYTHON) -m app.cli backtest

# Same chain but with deterministic DEMO data (offline; NOT real prices).
backtest-offline: ingest-offline
	$(PYTHON) -m app.cli backtest

clean:
	rm -f data/*.db
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name .pytest_cache -prune -exec rm -rf {} +

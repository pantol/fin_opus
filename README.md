# GPW Decision System — Deterministic Core (Phases 0+1)

Decision-support system for investing on the GPW (Warsaw Stock Exchange). It
filters, scores, and orders signals. **All money/risk logic is deterministic
code — zero LLM in the financial path. Paper trading only.**

This repository currently implements **Phases 0+1**: data + features + one
rule-based strategy + full risk layer + realistic walk-forward backtest +
decision logging + a Telegram alert stub. **No LLM, no news, no regime radar.**

## Quick start

```bash
make setup              # create .venv and install dependencies
make test               # run the full test suite (money + time correctness)

# Full chain with LIVE Stooq data: ingest -> features -> walk-forward -> metrics
make backtest

# If Stooq blocks automated access from your network, use deterministic
# DEMO data (clearly NOT real prices) to exercise the same pipeline:
make backtest-offline
```

Individual steps:

```bash
make ingest             # fetch EOD data from Stooq into SQLite (data/gpw.db)
make ingest-offline     # populate DB with deterministic demo data (no network)
make features           # compute + preview the point-in-time feature panel
python -m app.cli backtest --strategy trend_momentum
```

## Project structure

```
app/
  config.py             # YAML + path loading
  db.py                 # SQLite schema (portable to Postgres/TimescaleDB)
  ingestion/
    stooq.py            # live EOD ingestion (raw + adjusted, as_of_date)
    demo.py             # deterministic offline demo data (NOT real prices)
  features/compute.py   # point-in-time quant features (pure functions)
  strategy/engine.py    # YAML-driven rule engine — SIGNALS ONLY
  risk/manager.py       # deterministic sizing, stops, exposure, circuit-breaker
  backtest/
    fills.py            # spread + commission + slippage + volume cap
    metrics.py          # CAGR, Sharpe, Sortino, maxDD, Calmar, PF, turnover...
    engine.py           # event-driven sim + walk-forward OOS harness
  logging/decisions.py  # persist decisions + features snapshot + trades + equity
  alerts/telegram.py    # alert stub (dry-run prints a Polish card if no token)
  cli.py                # ingest / features / backtest entrypoints
config/
  universe.yaml         # WIG20 members + indices + delisted tickers
  backtest.yaml         # costs, walk-forward windows, capital, seed
  strategies/*.yaml     # one engine runs any strategy config
tests/                  # features, point-in-time, risk, fills, metrics,
                        # reproducibility, end-to-end integration
```

## Non-negotiable invariants (enforced by tests)

- **Point-in-time / no look-ahead** — a feature for date *T* reads only rows with
  `as_of_date <= T`; signals decide on *T*'s close and fill on the **next** bar.
  See `tests/test_point_in_time.py`.
- **Anti-survivorship** — the universe includes delisted tickers; they are only
  traded within `[listed_from, delisted_on]`. See `tests/test_integration.py`.
- **Deterministic money logic** — sizing/stops/limits are pure code; identical
  inputs yield identical outputs (seed pinned). `tests/test_risk.py`,
  `tests/test_integration.py::test_reproducibility_same_seed_same_result`.
- **Realistic fills** — buy at ask, sell at bid, commission (bps + min),
  slippage, and a volume-participation cap. `tests/test_fills.py`.
- **Walk-forward OOS, benchmark = WIG20TR** — only out-of-sample segments are
  measured, always against WIG20TR (never SPY).
- **Multi-tenant seam** — `user_id` column on decisions/positions/trades/equity.
- **Strategies are YAML** — one engine evaluates any config.

## Strategy config (example: `config/strategies/trend_momentum.yaml`)

Long when `close > SMA200` **and** 6-month momentum `> 0`; exit on a 2.5×ATR
trailing stop **or** a trend break (`close < SMA200`). All thresholds and risk
parameters live in the YAML, not in code.

## Data notes

- Source: Stooq daily CSV (`https://stooq.pl/q/d/l/?s=<ticker>&i=d`).
- Raw and adjusted prices are stored **separately and flagged** (`adjusted` column).
- `as_of_date` = the date a bar became available (EOD bar of day *D* ⇒ `as_of_date = D`).
- Stooq occasionally serves a JS bot-check instead of CSV; ingestion detects this
  and fails with a clear message. Use `--offline` demo data meanwhile.

## Telegram alerts

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (see `.env.example`). Without a
token the notifier runs in **dry-run** mode and prints the (Polish) alert card to
the console. The notifier is output-only and never part of the decision path.

## Seams for later phases (NOT built here)

The architecture leaves clean extension points; these are intentionally **not**
implemented in Phases 0+1:

- **Phase 2 — LLM features (OpenRouter):** LLM outputs become *inputs* to the
  deterministic risk layer only. The walk-forward in-sample window is currently a
  no-op (Phase-1 strategy has fixed thresholds); it is the hook for parameter
  selection / model-driven features. Provider/model/generation must be pinned and
  logged per decision (see `CLAUDE.md`).
- **Phase 3 — Regime radar / turning points.**
- **Phase 4 — Academic strategies (additional YAML configs; same engine).**
- **Phase 5 — Survey / user profile.**
- **Phase 6 — Multi-tenant:** `user_id` already threads through the schema and
  backtest; promote it to a first-class auth boundary.

**Real/live trading is forbidden — paper only.**

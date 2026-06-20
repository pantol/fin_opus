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

## ESPI/EBI + news collector (standalone plumbing, ZERO LLM)

A **separate, independently runnable** collector polls company-filing/news RSS
feeds on a schedule, captures every new item at **publication time**
(point-in-time anchor), maps it to an issuer by **ISIN**, fetches full text, and
stores it **append-only + idempotently** into the `filings` table of the same
SQLite DB. It never loses items and never alters timestamps of stored rows. The
LLM (Phase 2) only *reads* what this collects — no LLM here.

```bash
make collect        # run ONE collection cycle, then exit
make collect-loop   # run forever, polling every N minutes (N from config)
```

**Configure feeds in `config/news_sources.yaml`** — adding a feed is editing
config, not code. The shipped URLs are **placeholders**; copy the exact channel
feed URL from each provider's RSS page (GPW `gpw.pl/_rss`, NewConnect/
GlobalConnect `gpwglobalconnect.pl/_rss`, Bankier `bankier.pl/rss`) and paste it
in. A feed left as a `PLACEHOLDER_*` URL is skipped with a warning.

Key guarantees:
- **Point-in-time:** `published_at` comes from the feed `pubDate` (Europe/Warsaw,
  stored tz-aware), never from fetch time; `fetched_at` is UTC. A
  `published_at <= T` query never returns a later item.
- **Idempotent / append-only:** dedup by `guid`/`link`/content-hash with
  `ON CONFLICT(dedup_key) DO NOTHING`; re-running a cycle adds nothing and
  rewrites nothing.
- **Cross-source dedup:** the same report on GPW + Bankier is stored once, with
  the **earliest** `published_at`, keyed by `(issuer_isin, report_number, type)`.
- **ISIN mapping:** resolved to `instruments.id` by ISIN when available, else
  `instrument_id` is null and resolvable later (the collector runs even before
  the rest of the app/instruments exist — it owns and creates `filings`).
- **Resilience:** per-feed `try/except` (one bad feed never blocks the others);
  per-cycle structured logging; a `collector_health.last_successful_run` beacon
  for staleness alerting.

### VPS deployment

Secrets/config come from `config/news_sources.yaml` and env (`.env` is
gitignored) — never hardcoded. Be a polite poller: keep a sane interval and the
descriptive `user_agent`.

**Option A — cron** (one-shot every 10 min):

```cron
*/10 * * * * cd /opt/fin_opus && /opt/fin_opus/.venv/bin/python -m app.ingestion.collect_news >> /var/log/gpw_collect.log 2>&1
```

**Option B — systemd** (long-running scheduler):

```ini
# /etc/systemd/system/gpw-collector.service
[Unit]
Description=GPW ESPI/EBI + news collector
After=network-online.target

[Service]
WorkingDirectory=/opt/fin_opus
ExecStart=/opt/fin_opus/.venv/bin/python -m app.ingestion.collect_news --loop
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now gpw-collector
```

**Verify it is running** (health beacon + recent rows):

```bash
sqlite3 data/gpw.db "SELECT last_successful_run, last_cycle_new_items, last_error FROM collector_health;"
sqlite3 data/gpw.db "SELECT source, published_at, title FROM filings ORDER BY published_at DESC LIMIT 5;"
```

Alert if `last_successful_run` goes stale (older than a few poll intervals).

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

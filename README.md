# GPW Decision System

Decision-support system for investing on the GPW (Warsaw Stock Exchange). It
filters, scores, and orders signals. **All money/risk logic is deterministic
code — zero LLM in the financial path. Paper trading only.**

Implemented today:

- **Phases 0+1** — real EOD data, point-in-time features, one YAML rule
  strategy, full deterministic risk layer, realistic walk-forward backtest,
  decision logging, Telegram notifier (dry-run by default).
- **Phase 2 (LLM features)** — ESPI/EBI/news collector over live RSS feeds, an
  OpenRouter research→judge pipeline that materializes a point-in-time
  `llm_score` feature, and an A/B harness comparing baseline vs baseline+LLM.
  The LLM is **only an input**: it can gate entries via YAML, it never sizes
  money, and the backtest itself makes zero LLM calls.

## How to run

### 1. One-time setup

Python 3.11–3.12 recommended (3.10+ supported). The compiled deps
(`curl_cffi`, `xlrd`) need wheels for your interpreter, so a bleeding-edge or
pre-3.10 Python may fail to install.

```bash
make setup                      # create .venv and install dependencies
cp .env.example .env            # then fill in the secrets you need (all optional):
                                #   OPENROUTER_API_KEY  - only for `make llm`
                                #   TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID - optional alerts
                                #   R2_* / HEALTHCHECK_URL_* - deployment (backups, health)
make test                       # 246 tests: money math, point-in-time, fills, LLM contracts
```

The CLI auto-loads a local `.env` at startup; any variable already exported in
your shell takes precedence over the file. You can still export vars manually
or use a tool like `direnv`.

### 2. Get price data (GPW official archive)

```bash
make backfill                   # ONE-TIME deep backfill, 2015 -> today, FULL market
                                # (~1 request/second, takes ~1h; safe to re-run/resume)
make ingest                     # afterwards: incremental top-up (resumes after last bar)
```

`make ingest` pulls from **GPW's official quotes archive** (one file per
session covering the entire market, dead companies included — this is what
makes the universe survivorship-bias-free) plus **WIG20TR** history from GPW
Benchmark. Before 18:00 Warsaw it ingests only up to *yesterday* — today's
archive file exists intraday but holds partial bars, which would break the
point-in-time convention.

Useful variants:

```bash
python -m app.cli ingest --start 2020-01-01 --end 2020-12-31   # explicit range
python -m app.cli ingest --source stooq                        # legacy path (login-gated by Stooq)
make ingest-offline                                            # deterministic DEMO data (NOT real prices)
```

### 2b. Reference data + data quality

```bash
make refdata                    # load index membership + corporate actions fixtures,
                                # derive the adjusted price series (adjusted=1 rows)
make check-data                 # data-quality report; non-zero exit + Telegram alert on issues
python -m app.cli override --decision-id 42 --action "skipped ENTER" --reason "earnings tomorrow"
```

- `config/index_membership.yaml` — **point-in-time index membership** (WIG20
  revisions). When `universe.index` is set in `config/backtest.yaml`, the
  backtest only evaluates NEW entries for instruments that were members **as of
  each simulated day** (former members keep their historical ranges). Ships
  with placeholder dates — fill the real revision dates from GPW announcements.
- `config/corporate_actions.yaml` — dividends/splits/rights issues by ex-date.
  Loaded actions (a) derive a back-adjusted `adjusted=1` price series and
  (b) shield the ATR stop in the backtest: a gap explained by an action is
  re-based, never treated as a market crash.
- `make check-data` scans for missing sessions vs the exchange calendar,
  zero/negative volume, close-to-close jumps above `config/data_quality.yaml`'s
  threshold with **no matching corporate action**, and stale (alive but silent)
  tickers. Run it after every ingest; it exits non-zero when something needs
  attention, so it is cron-friendly.
- `override` appends to an **append-only journal** (`overrides` table) — log
  every manual deviation from a system signal so future-you can audit the
  damage honestly.

### 3. Inspect features and run the backtest

```bash
make features                   # preview the point-in-time feature panel
make backtest                   # ingest (incremental) -> walk-forward backtest vs WIG20TR
python -m app.cli backtest --strategy trend_momentum   # skip the ingest step
```

The backtest decides on day *T*'s close, fills at *T+1*'s open, and charges
commission, both-sides half-spread, slippage, and a 10%-of-volume fill cap.
Metrics are reported against buy-and-hold WIG20TR on the same dates, followed
by an **anti-luck validation block**: trials-to-date from the registry, the
Deflated Sharpe Ratio, and the strategy's percentile against
`validation.mc_sims` cost-matched random-entry strategies (config in
`config/backtest.yaml`; write your kill criteria in `docs/kill_criteria.md`
BEFORE looking at results).

### 4. Collect filings and news (standalone, ZERO LLM)

```bash
make collect                    # one cycle over the live RSS feeds, then exit
make collect-loop               # poll forever (interval from config) — run this on a VPS
```

Feeds are configured in `config/news_sources.yaml` (bankier.pl ESPI/EBI,
stockwatch.pl, PAP Biznes — all live-verified). **RSS has no backfill and the
feed windows are only hours long**: filing history starts accumulating the day
the collector starts running, so keep it running continuously.

### 5. Materialize LLM features (Phase 2, needs `OPENROUTER_API_KEY`)

```bash
make llm                                        # today's features for the whole universe
python -m app.cli llm --date 2026-07-01         # a specific decision date
python -m app.cli llm --ticker pko              # a single instrument
```

**Needs filings first (step 4).** On a fresh clone there are none — RSS has no
backfill — so `make llm` and `make ab` produce nothing until the collector has
run for a while. Run `make collect-loop` and let it accumulate history first;
`make llm` says so when it finds an empty `filings` table.

For each instrument with unprocessed filings published up to the decision
date (end-of-day Europe/Warsaw), the pipeline runs research → judge on the
filing TEXT, validates strict JSON, and stores one `llm_score` in [-1, 1]
plus a discrete `relevance` label (`relevant_interesting` /
`relevant_uninteresting` / `irrelevant`, exposed to strategies as the numeric
`llm_relevance` feature) in `llm_features`. Every call logs the served
provider/model/generation id and cache status (printed as a provider audit)
for reproducibility. Results are cached by input hash, so replays cost nothing.

**Spend cap:** every live call's cost (tokens × per-model price from
`config/llm.yaml`) lands in `llm_costs`; when the month's total reaches
`budget.monthly_usd_cap`, live calls STOP, the run is recorded as `degraded`
in `llm_runs` (baseline-only operation — unprocessed filings simply wait), a
Polish Telegram alert fires, and `make llm` exits 3. Cache hits are free and
keep working.

### 5b. Golden eval set: label filings, catch prompt regressions

```bash
make label                      # label filings one at a time (interactive, ZERO LLM)
make eval-llm                   # current research prompt vs your labels (accuracy + F1)
```

Label 50–100 filings (`eval_labels`), then run `make eval-llm` after ANY
prompt or model change: it scores the CURRENT research prompt against your
labels and stores each run in `eval_runs` (prompt fingerprint, model, served
provider, accuracy, per-class F1). **Rule: no prompt/model change ships if it
regresses on the golden set.** Note that changing the prompt invalidates the
input-hash cache — the next `make llm` re-spends on unprocessed filings.

### 6. A/B: does the LLM gate actually help?

```bash
make ab                         # baseline vs baseline+llm_score gate, same OOS window
```

Reads only pre-materialized `llm_features` (no LLM call), runs both strategies
through the identical engine/costs, and reports per-metric deltas with a gate
verdict (Sharpe strictly better AND Sortino/maxDD not worse).

### 7. Backups + deployment status

```bash
make backup                     # VACUUM INTO snapshot -> R2 (if creds) -> retention
make restore-test               # pull the latest snapshot and PROVE it restores
make status                     # prices/collector/filings/backup liveness; alerts when stale
```

Snapshots are taken with SQLite's online `VACUUM INTO` — never by copying the
live file. With `R2_ENDPOINT_URL` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY`
in the environment the snapshot is pushed to Cloudflare R2 (bucket/prefix and
retention in `config/backup.yaml`: N newest daily + the first snapshot of each
of M months); without credentials it stays local-only and says so. R2 uploads
need the optional extra: `pip install -e ".[backup]"`.

**A backup that was never restored is not a backup** — run `make restore-test`
monthly: it pulls the latest snapshot (R2 when configured, else local), runs
`PRAGMA integrity_check`, verifies the expected tables, and compares row counts
against the live DB. The `filings` history is irreplaceable (RSS has no
backfill); this is what protects it.

### A typical day, end to end

```bash
# on a schedule (VPS): collector runs continuously
make collect-loop &

# after the session close (>= 18:00 Europe/Warsaw):
make ingest        # top up EOD bars
make check-data    # sanity-check the fresh data (alerts on issues)
make llm           # turn today's filings into llm_score features (optional)
make backtest      # walk-forward metrics vs WIG20TR
make ab            # optional: baseline vs +LLM comparison
```

## Project structure

```
app/
  config.py             # YAML + path loading
  db.py                 # SQLite schema (portable to Postgres/TimescaleDB)
  ingestion/
    gpw_archive.py      # PRIMARY: GPW official session archive + GPW Benchmark indices
    stooq.py            # legacy Stooq CSV path (login-gated by Stooq as of 2026)
    demo.py             # deterministic offline demo data (NOT real prices)
    refdata.py          # index membership + corporate actions + adjusted-series deriver
    quality.py          # data-quality monitor (make check-data)
    news_collector.py   # ESPI/EBI + news RSS collector (point-in-time, ZERO LLM)
    filings_db.py       # filings table, dedup, health beacon, issuer resolution
  features/
    compute.py          # point-in-time quant features (pure functions)
    fundamentals.py     # point-in-time fundamentals seam (publication-dated)
  strategy/engine.py    # YAML-driven rule engine — SIGNALS ONLY
  risk/manager.py       # deterministic sizing, stops, exposure, circuit-breaker
  backtest/
    fills.py            # spread + commission + slippage + volume cap
    metrics.py          # CAGR, Sharpe, Sortino, maxDD, Calmar, PF, turnover...
    engine.py           # event-driven sim + walk-forward OOS harness
    ab_harness.py       # baseline vs baseline+LLM comparison (same engine/costs)
  llm/
    client.py           # OpenRouter wrapper: pinned provider, audit log, input-hash cache,
                        # per-call cost ledger + monthly hard cap
    schemas.py          # strict JSON validation — malformed is rejected, never guessed
    research.py         # extraction agent (filing text -> structured JSON + evidence check)
    synthesis.py        # judge (research + quant context -> verdict -> llm_score)
    pipeline.py         # materializes point-in-time llm_features rows
    evalset.py          # golden-set labeling CLI + prompt-regression harness
  logging/decisions.py  # persist decisions + feature snapshots + trades + equity
  alerts/
    telegram.py         # notifier: real send with a token, else dry-run Polish card
    healthcheck.py      # dead-man's-switch pings (healthchecks.io-style)
  backup.py             # VACUUM INTO snapshots, R2 upload, retention, restore test
  status.py             # one-command deployment liveness (make status)
  cli.py                # ingest / features / backtest / ab / llm / backup / status ...
config/
  universe.yaml         # WIG20 members + indices + delisted tickers (ISIN-keyed)
  backtest.yaml         # costs, walk-forward windows, capital, seed, universe gate
  index_membership.yaml # point-in-time index revisions (fill real GPW dates)
  corporate_actions.yaml # dividends/splits/rights by ex-date (fill from ESPI)
  data_quality.yaml     # check-data thresholds
  llm.yaml              # models, pinned providers, caching (Phase 2)
  news_sources.yaml     # live-verified RSS feeds + per-feed timezone quirks
  strategies/*.yaml     # one engine runs any strategy config
tests/                  # 246 tests: features, point-in-time, risk, fills, metrics,
                        # collector, LLM contracts, A/B, reproducibility, e2e
```

## Non-negotiable invariants (enforced by tests)

- **Point-in-time / no look-ahead** — a feature for date *T* reads only rows with
  `as_of_date <= T`; signals decide on *T*'s close and fill on the **next** bar;
  filings count only if published by end-of-day *T* (Europe/Warsaw).
  See `tests/test_point_in_time.py`.
- **Next-open fills, enforced** — a fill lag `< 1` is rejected outright; every
  fill derives from the fill bar's open (close fallback and lapsed orders are
  recorded as auditable anomalies, never silent). `tests/test_fill_timing.py`.
- **Point-in-time index membership** — with `universe.index` set, an instrument
  that joined the index in year Y is absent from the tradable universe before Y;
  exits on held positions always run. `tests/test_index_membership.py`.
- **Corporate actions shield stops** — a gap explained by a recorded
  split/dividend/rights issue re-bases the position and stop instead of firing
  the ATR stop; the same gap without an action still fires it.
  `tests/test_corporate_actions.py`.
- **Anti-survivorship** — the universe includes delisted tickers (Getin Noble,
  Petrolinvest, BPH…), traded only within `[listed_from, delisted_on]`, and the
  full-market backfill stores every instrument that appears in the historical
  session files.
- **Deterministic money logic** — sizing/stops/limits are pure code; identical
  inputs yield identical outputs. The LLM can only gate entries through a YAML
  condition; `tests/test_ab_harness.py` proves a permissive score changes
  nothing and sizing never sees it.
- **Realistic fills** — buy at ask, sell at bid, commission (bps + min),
  slippage, and a volume-participation cap. `tests/test_fills.py`.
- **Walk-forward OOS, benchmark = WIG20TR** — metrics are measured out-of-sample
  against WIG20TR total return (never SPY).
- **Purged walk-forward** — an embargo of `walk_forward.embargo_sessions`
  trading sessions (default 252 = the longest feature lookback) separates each
  train window from its test window, so no feature computed in OOS can read
  train data. `tests/test_validation.py`.
- **Anti-luck validation** — every backtest run lands in the `strategy_trials`
  registry; reports carry the **Deflated Sharpe Ratio** (Bailey & López de
  Prado; deflated by the number of distinct configs ever tried) and the
  strategy's **percentile vs N cost-matched random-entry strategies** (same
  trade count, holding periods, universe, sizing, and cost model). The A/B
  acceptance gate requires OOS improvement AND the configured DSR/percentile
  floors — deltas alone cannot tell luck from edge. `tests/test_validation.py`.
- **Multi-tenant seam** — `user_id` column on decisions/positions/trades/equity.
- **Strategies are YAML** — one engine evaluates any config.
- **Reproducible LLM** — pinned provider (`allow_fallbacks: false`), served
  provider/model/generation id logged on every call, local cache by input hash,
  malformed JSON rejected (never repaired).
- **LLM spend is capped, degradation is graceful** — per-call costs ledgered in
  `llm_costs`; at the monthly cap live calls stop, the run is marked `degraded`
  and filings wait untouched (no retry attempts burned).
  `tests/test_llm_guardrails.py`.
- **Prompt changes are evaluated, not vibes-shipped** — `make eval-llm` scores
  the current research prompt against the human-labeled golden set; a
  regression blocks the change. `tests/test_llm_guardrails.py`.

## Strategy config (example: `config/strategies/trend_momentum.yaml`)

Long when `close > SMA200` **and** 6-month momentum `> 0`; exit on an ATR
trailing stop **or** a trend break (`close < SMA200`). All thresholds and risk
parameters live in the YAML, not in code. `trend_momentum_llm.yaml` adds one
condition — `llm_score >= 0.0` (a non-negative score) — as an entry gate.

## Data notes

- **Primary source: GPW's official quotes archive** (`gpw.pl/archiwum-notowan`)
  — one legacy `.xls` per session with the whole market, ISIN-keyed, verified
  back to 1995. Historical files naturally include companies that later died,
  so the backfill is survivorship-bias-free **by construction**.
- **Indices** come from GPW Benchmark's chart API (WIG20TR history back to 2005
  in one request); index ISINs are discovered from GPW's own indices file,
  never hardcoded.
- Both hosts reject bare HTTP clients — fetches use `curl_cffi` browser TLS
  impersonation. Be polite: the ingester sleeps ~1s between session requests.
- `as_of_date` = the date a bar became available (EOD bar of day *D* ⇒
  `as_of_date = D`). Today's archive file exists **intraday with partial bars**;
  the default ingest window therefore ends yesterday until 18:00 Warsaw.
- Prices are stored **raw** (`adjusted=0`). `make refdata` derives a
  back-adjusted series (`adjusted=1`) from `config/corporate_actions.yaml` for
  instruments that have recorded actions; the backtest still runs on raw prices
  (with corporate-action stop shielding), so long momentum/SMA features remain
  distorted across ex-dates until the fixture is filled and features are
  switched — a deliberate, separate decision because it changes every
  historical number. `make check-data` lists the exact dates that need action
  entries (e.g. PZU 2015-11-30 and DNP 2025-07-31 show split-shaped −90% gaps).
- Stooq's CSV endpoint (`--source stooq`) is kept as a fallback but has been
  login-gated since mid-2026; expect `StooqUnavailableError` without a session.

## ESPI/EBI + news collector (standalone plumbing, ZERO LLM)

A separate, independently runnable collector polls the configured RSS feeds,
captures every new filing/news item at **publication time** (point-in-time
anchor), resolves the issuer (ISIN when present, else a deterministic
name/ticker match from the title prefix — exact match only, `None` on
ambiguity), fetches full text where the source allows it, and stores
everything **append-only + idempotently** into `filings`.

Live-verified feeds (2026-07) and their quirks, all handled in config/code:

- `bankier.pl/rss/espi.xml` — ESPI+EBI; labels Warsaw wall-clock with a fixed
  `+0100` offset year-round → `timezone_override: Europe/Warsaw` discards the
  bogus offset.
- `stockwatch.pl …/rss.aspx` — naive `YYYY-MM-DD HH:MM` timestamps (parsed as
  Warsaw); ESPI/EBI marker lives in the link slug.
- `biznes.pap.pl/rss` — correct UTC stamps and report numbers in titles; the
  article pages are JS-rendered, so PAP items store title+summary only.

Key guarantees: `published_at` never comes from fetch time; earliest
publication wins across sources; re-running a cycle adds nothing and rewrites
nothing; one failing feed never blocks the others; a
`collector_health.last_successful_run` beacon supports staleness alerting.

### VPS deployment

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

**Backups + status on a timer** (protects the irreplaceable filings history):

```ini
# /etc/systemd/system/gpw-backup.service
[Unit]
Description=GPW DB backup (snapshot -> R2 -> retention)
[Service]
Type=oneshot
WorkingDirectory=/opt/fin_opus
EnvironmentFile=/opt/fin_opus/.env
ExecStart=/opt/fin_opus/.venv/bin/python -m app.cli backup

# /etc/systemd/system/gpw-backup.timer
[Unit]
Description=Nightly GPW DB backup
[Timer]
OnCalendar=*-*-* 02:30
Persistent=true
[Install]
WantedBy=timers.target

# /etc/systemd/system/gpw-status.service
[Unit]
Description=GPW deployment status check (alerts when stale)
[Service]
Type=oneshot
WorkingDirectory=/opt/fin_opus
EnvironmentFile=/opt/fin_opus/.env
ExecStart=/opt/fin_opus/.venv/bin/python -m app.cli status

# /etc/systemd/system/gpw-status.timer
[Unit]
Description=Hourly GPW status check
[Timer]
OnCalendar=hourly
Persistent=true
[Install]
WantedBy=timers.target
```

Enable with `systemctl enable --now gpw-backup.timer gpw-status.timer`.
**Monthly routine:** run `make restore-test` (or add a third timer) — a backup
that was never restored is not a backup.

**Environment variables** (all optional; features degrade gracefully):

| Variable | Used by | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | `make llm` | LLM feature materialization |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | alerts | live alerts (dry-run prints without them) |
| `R2_ENDPOINT_URL` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | `make backup` | snapshot upload to Cloudflare R2 |
| `HEALTHCHECK_URL_COLLECT` | collector | pinged after each fully healthy cycle |
| `HEALTHCHECK_URL_BACKUP` | `make backup` | pinged after each successful backup |

**Verify it is running** (`make status` wraps all of this + alerting):

```bash
make status
sqlite3 data/gpw.db "SELECT last_successful_run, last_cycle_new_items, last_error FROM collector_health;"
sqlite3 data/gpw.db "SELECT source, published_at, title FROM filings ORDER BY published_at DESC LIMIT 5;"
```

## Telegram alerts

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (see `.env.example`). Without a
token the notifier runs in **dry-run** mode and prints the (Polish) alert card
to the console. The notifier is output-only and never part of the decision path.

## Current status (honest)

First full-history real-data run (2015→2026, walk-forward OOS from 2017):
`trend_momentum` as shipped is roughly flat (total return ≈ −2%, maxDD −26%)
vs **WIG20TR +147%** — much lower drawdown, far lower return. The plumbing is
sound; the strategy has no proven edge yet. The A/B LLM comparison needs weeks
of collected filings before it can say anything (RSS has no backfill).

## Seams for later phases (not built yet)

- **Walk-forward parameter fitting** — the IS/OOS machinery exists but Phase-1
  parameters are fixed constants, so fitting is currently a documented no-op.
- **Adjusted-price features** — the adjusted series is derived (`make refdata`)
  but features still read raw prices; switching them is a one-line change with
  system-wide metric consequences, deferred deliberately.
- **Phase 3 — regime radar / turning points. Phase 4 — academic strategies
  (more YAMLs; same engine). Phase 5 — survey/profile. Phase 6 — multi-tenant
  auth** (the `user_id` column already threads everywhere).

**Real/live trading is forbidden — paper only.**

# Implementation Progress

> Living progress log for the GPW Decision System. **Update this file on every
> implementation step** (new feature, fix, phase gate). Keep entries newest-first.

**Current phase:** Phase 2 (LLM FEATURES layer) — **plumbing COMPLETE & green;
empirical A/B verdict BLOCKED on real data.** Phase 0+1 deterministic core and the
standalone ESPI/EBI collector remain complete. Hardening packs (A: core, B: infra,
C: validation, D: LLM guardrails) in progress. The LLM is ALWAYS only an INPUT;
ZERO LLM in the money path. **Tests:** 246 passing.

---

## Phase status

| Phase | Scope | Status |
|-------|-------|--------|
| 0 | Scaffold: `app/` package, config, db schema, Makefile, README | ✅ Done |
| 1 | Data + features + 1 strategy + full risk + backtest + log + Telegram stub (no LLM) | ✅ Done |
| 2 | LLM via OpenRouter as *features* + A/B harness | 🟨 Plumbing done; A/B improvement unproven (needs live OpenRouter + real ESPI filings) |
| 3 | Regime radar / turning points | ⬜ Not started |
| 4 | Academic strategies (more YAML, same engine) | ⬜ Not started |
| 5 | Survey / user profile | ⬜ Not started |
| 6 | Multi-tenant (promote `user_id` to auth boundary) | ⬜ Not started |

---

## Component checklist (Phase 0+1)

- [x] **DB schema** (`app/db.py`) — SQLite, Postgres/Timescale-clean, `user_id` seam, `as_of_date` on bars.
- [x] **Ingestion — Stooq** (`app/ingestion/stooq.py`) — EOD CSV, raw/adjusted split, delisted tickers, bot-check detection.
- [x] **Ingestion — offline demo** (`app/ingestion/demo.py`) — deterministic data, clearly NOT real prices.
- [x] **Features** (`app/features/compute.py`) — point-in-time returns, SMA, ATR, vol, momentum, rel-strength.
- [x] **Strategy engine** (`app/strategy/engine.py`) — YAML-driven rules, SIGNALS ONLY (ENTER/EXIT/HOLD).
- [x] **Strategy config** (`config/strategies/trend_momentum.yaml`) — close>SMA200 & mom6m>0; exit on 2.5×ATR or trend break.
- [x] **Risk layer** (`app/risk/manager.py`) — fixed-fractional sizing, ATR stops, exposure caps, drawdown circuit-breaker.
- [x] **Fills** (`app/backtest/fills.py`) — spread, commission, slippage, volume-participation cap.
- [x] **Metrics** (`app/backtest/metrics.py`) — CAGR, Sharpe, Sortino, maxDD, Calmar, PF, turnover, win-rate.
- [x] **Backtest engine** (`app/backtest/engine.py`) — event-driven, next-bar fills, walk-forward continuous OOS.
- [x] **Decision logging** (`app/logging/decisions.py`) — decisions + features snapshot + trades + equity, full reproducibility.
- [x] **Telegram stub** (`app/alerts/telegram.py`) — dry-run Polish alert card, output-only (never in decision path).
- [x] **CLI** (`app/cli.py`) — `ingest` / `features` / `backtest` (+`--offline`).
- [x] **Skill** (`.claude/skills/point-in-time-backtest/SKILL.md`).

## Collector checklist (ESPI/EBI + news — standalone plumbing, ZERO LLM)

- [x] **Config** (`config/news_sources.yaml`) — feeds via config (placeholders + RSS-source comments), interval, db_path, UA.
- [x] **Filings storage** (`app/ingestion/filings_db.py`) — owns `filings` + `collector_health`; idempotent migration; append-only insert; dedup/asof/health helpers.
- [x] **Collector core** (`app/ingestion/news_collector.py`) — fetch RSS (feedparser), parse, ISIN/report/type extraction, full-text fetch, two-layer dedup, per-feed resilience.
- [x] **Entrypoints** (`app/ingestion/collect_news.py`) — one-shot + APScheduler `--loop`; `make collect` / `make collect-loop`.
- [x] **ISIN seam** — nullable `isin` on `instruments` (idempotent `ALTER TABLE` migration in `app/db.py`).
- [x] **Tests** (`tests/test_news_collector.py`) — dedup (incl. earliest-wins regardless of feed order), idempotency, point-in-time/tz, ISIN mapping, resilience, health/exit-code, timestamp formats (18 tests).
- [x] **VPS deploy docs** — README cron + systemd + health verification.

## Component checklist (Phase 2 — LLM features, LLM is only an INPUT)

- [x] **Skill** (`.claude/skills/llm-provider-routing/SKILL.md`) — pinned provider, logged served provider+model+gen-id, cache via cached_tokens, cost routing, validated JSON, LLM-as-input rule.
- [x] **Config** (`config/llm.yaml`) — base URL, cheap-extraction / pricier-synthesis models, pinned provider (`allow_fallbacks:false`), low temp, cache; loader `cfg.load_llm_config()`.
- [x] **DB tables** (`app/db.py`) — `fundamentals`, `llm_calls` (audit), `llm_cache`, `llm_features`; all idempotent.
- [x] **LLM client** (`app/llm/client.py`) — OpenRouter wrapper, injectable transport, cache-by-input-hash (hit = no network), logs served provider+model+gen-id+cached_tokens on EVERY call, key from `OPENROUTER_API_KEY`.
- [x] **Schemas** (`app/llm/schemas.py`) — strict jsonschema for research + synthesis; malformed REJECTED (no guessing).
- [x] **Research Agent** (`app/llm/research.py`) — filing TEXT → validated research JSON; `evidence_quote` must appear in source else confidence lowered.
- [x] **Fundamentals seam** (`app/features/fundamentals.py`) — point-in-time `load_fundamentals_asof` (as_of_date ≤ T); NUMBERS computed by code, passed to synthesis as context text only.
- [x] **Synthesis/Judge** (`app/llm/synthesis.py`) — research + quant_score + fundamentals (context) → verdict JSON → `llm_score` ∈ [-1,1]; the ONLY value handed to the strategy/risk layer.
- [x] **Pipeline** (`app/llm/pipeline.py`) — consume unprocessed filings (published_at ≤ T), research→synthesis, persist `llm_features`, mark filings processed; deterministic score loader for backtest.
- [x] **Engine injection** (`app/backtest/engine.py`) — point-in-time `llm_score` injected into the feature snapshot; sizing/risk byte-for-byte unchanged.
- [x] **LLM strategy** (`config/strategies/trend_momentum_llm.yaml`) — baseline + one `llm_score >= 0` entry gate (can veto/permit, never sizes).
- [x] **A/B harness** (`app/backtest/ab_harness.py`) — same engine/costs/OOS/WIG20TR; baseline vs +LLM; honest gate (Sharpe up, Sortino & maxDD not worse). CLI `ab`, `make ab` / `make ab-offline`.
- [x] **`mark_processed`** (`app/ingestion/filings_db.py`) — idempotent; `select_filings_asof` gains `instrument_id` + `only_unprocessed` filters.

## Invariants (enforced by tests)

- [x] Next-open fills enforced: lag >= 1 or raise; no fill from the signal bar; fill anomalies audited (`tests/test_fill_timing.py`)
- [x] Point-in-time index membership: no trading before `date_from` (`tests/test_index_membership.py`)
- [x] Corporate-action gaps shield the ATR stop instead of firing it; unexplained gaps still fire (`tests/test_corporate_actions.py`)
- [x] Data-quality monitor: missing sessions, bad volume, unexplained jumps, stale tickers (`tests/test_data_quality.py`)
- [x] Point-in-time / no look-ahead (`tests/test_point_in_time.py`)
- [x] Anti-survivorship: delisted tickers traded only in `[listed_from, delisted_on]` (`tests/test_integration.py`)
- [x] Deterministic money logic, reproducible with pinned seed (`tests/test_risk.py`, `tests/test_integration.py`)
- [x] Realistic fills: buy@ask / sell@bid + costs + volume cap (`tests/test_fills.py`)
- [x] Walk-forward OOS vs WIG20TR (never SPY)
- [x] Multi-tenant `user_id` seam on decisions/positions/trades/equity
- [x] ZERO LLM in the money path (A/B: with permissive scores, +LLM metrics == baseline, delta≈0 — proves the LLM is a pure gate, `tests/test_ab_harness.py`)
- [x] LLM output validated; malformed rejected, not guessed (`tests/test_llm_schemas.py`)
- [x] LLM reproducibility: served provider+model+gen-id logged every call; cache hit = no 2nd network call (`tests/test_llm_client.py`)
- [x] Point-in-time for filings AND fundamentals feeding synthesis (`tests/test_llm_pipeline.py`, `tests/test_llm_synthesis.py`, `tests/test_fundamentals.py`)
- [x] `evidence_quote` must appear in source else confidence lowered (`tests/test_llm_research.py`)

---

## Changelog (newest first)

### 2026-07-05 — Onboarding + doc-accuracy pass (readiness audit fixes)
No behavior change to the money/LLM path; a readiness audit found stale docs
and two onboarding rough edges. Suite is **246 passing** (Pack D logged 242;
the +4 came from the post-Pack-D review-fix commits `c351106`, which added
trials-registry/MC tests without their own changelog entry — reconciled here).
- **`.env` auto-load (zero-dep):** `app/cli.py` now loads a local `.env` at
  startup (shell exports still win); `.env.example` was missing 5 of 8 vars —
  now lists all (TELEGRAM_*, OPENROUTER_API_KEY, R2_*, HEALTHCHECK_URL_*).
- **Collector-first hint:** `make llm` now prints a clear "run `make collect`
  first (RSS has no backfill)" message when it finds an empty `filings` table,
  instead of silently materializing zero features.
- **Doc corrections:** README/PROGRESS test counts 173/242 → **246**; README
  LLM gate corrected to `llm_score >= 0.0` (matches the shipped YAML); Telegram
  "stub" language → "notifier" (the live-send path is real); README setup gains
  a tested-Python-version note. Removed the resolved "GitHub push blocked" item
  and the stale "Next up (Phase 2 — not started)" block below.

### 2026-07-04 — Pack D: LLM guardrails (golden eval set, relevance, spend cap)
Evaluation + cost guardrails on the LLM features layer; 242 tests green (+14).
The LLM remains ONLY an input; all new logic is deterministic code.
- **Golden eval set:** `eval_labels` (one human relevance label per filing,
  upsert on relabel) + `make label` interactive Polish CLI (ZERO LLM). Target
  50-100 labels.
- **Prompt-regression harness:** `make eval-llm` runs the CURRENT research
  prompt over the golden set; accuracy + per-class F1 vs labels, stored per
  run in `eval_runs` with a prompt content fingerprint + model + served
  provider. README rule: a regression blocks the prompt/model change.
- **Discrete relevance:** `relevance` enum added ATOMICALLY to
  RESEARCH_SCHEMA + prompt (additionalProperties:false makes one-sided
  rollouts reject everything); materialized as llm_features.relevance and
  exposed to strategies as numeric `llm_relevance`
  (RELEVANCE_TO_SCORE in code, never the LLM); engine detection generalized
  to any `llm_*` feature. Logged with every decision via the snapshot.
  NOTE: the prompt change invalidates the llm_cache — next `make llm`
  re-spends on unprocessed filings.
- **Spend cap:** per-call cost ledger `llm_costs` (tokens x per-model price
  from config; prices REQUIRED when a cap is set); monthly hard cap checked
  between cache lookup and network (cache hits stay free); on exhaustion the
  pipeline records a `degraded` run in `llm_runs`, sends a Polish Telegram
  alert, exits 3, and leaves interrupted filings untouched (no processed
  flags, no attempt bumps — the wallet is empty, the filings are fine).

### 2026-07-03 — Pack C: validation methodology (purged walk-forward, DSR, MC benchmark)
Anti-luck upgrade; 225 tests green (+15). Pure deterministic code, no LLM.
- **Purged walk-forward:** `walk_forward.embargo_sessions` (default 252 = the
  longest feature lookback, ret_12m) inserts a trading-session gap between
  each IS window and its OOS window; a 252-lookback feature at the first OOS
  date provably reads zero train data. NOTE: this shifts the OOS start by ~1
  year, so headline metrics change vs Pack A/B runs — by design.
- **Random-entry Monte Carlo benchmark:** `validation.mc_sims` (default 1000)
  random strategies with the same trade count, bootstrapped holding periods,
  universe/membership gating, fixed-fractional sizing, and full cost model
  (next-open fills, spread, commission, slippage, volume cap); reports the
  strategy's mid-rank percentile per metric (CAGR/Sharpe/maxDD). Deterministic
  per seed.
- **Trials registry + DSR:** every `backtest`/`ab` run logs into
  `strategy_trials` (config-hash keyed; re-running the same config is the
  same trial). Deflated Sharpe Ratio (Bailey & Lopez de Prado) computed from
  distinct-trial count and per-period Sharpe variance; pure-python normal
  CDF/PPF (erf + Acklam), no scipy. Trials=1 => SR*=0 (no deflation).
- **Reporting + gates:** backtest report gains a validation block (trials,
  raw Sharpe, DSR, random percentiles); the A/B acceptance verdict now
  requires OOS improvement AND `validation.gates` floors (min_dsr,
  min_random_percentile) — unavailable evidence fails the gate.
- `docs/kill_criteria.md` template added (fill in BEFORE looking at results).

### 2026-07-03 — Pack B: infra & backup (snapshots, restore-test, healthchecks, status)
VPS-reliability pack; 203 tests green (+21). The filings history is
irreplaceable — this pack protects it.
- **Backups:** `make backup` = online snapshot via `VACUUM INTO` (never a
  live-file copy) → Cloudflare R2 upload when `R2_*` env creds exist
  (injectable S3 seam; boto3 as optional `[backup]` extra) → retention
  N daily + first-of-month M monthlies, applied locally AND remotely
  (config/backup.yaml).
- **Restore verification:** `make restore-test` pulls the latest snapshot
  (R2 else local), runs PRAGMA integrity_check, verifies expected tables,
  row-count sanity vs live (snapshot > live = failure). Documented as a
  monthly routine.
- **Healthchecks:** dead-man's-switch pings (app/alerts/healthcheck.py) after
  each fully healthy collector cycle (`HEALTHCHECK_URL_COLLECT`) and each
  successful backup (`HEALTHCHECK_URL_BACKUP`); silent no-op when unset,
  never raises.
- **Status:** `make status` = prices freshness + collector heartbeat vs
  threshold + filings backlog + newest snapshot age; Polish Telegram alert
  and exit 2 when stale (cron-able).
- **Schema unification:** `init_db` now also ensures the collector-owned
  filings schema, killing the `no such table: filings` trap for `make llm`
  on a DB where the collector never ran.

### 2026-07-03 — Pack A: core hardening (next-open enforcement, membership, corporate actions, check-data, overrides)
Hardening pack over the deterministic core; 173 tests green (+27). No new
features, no LLM. Note: this entry also reconciles the stale counts above —
between 2026-06-22 and this entry the repo gained real GPW-archive ingestion,
verified ESPI/EBI feeds, LLM CLI wiring, and a README rewrite (commits
`2b4d345`..`62f0a51`, 121 → 146 tests) that were not logged here.
- **A.1 next-open fills:** already implemented (decide T close, fill T+1 open) —
  now ENFORCED: `signal_to_fill_lag_days < 1` raises; lapsed orders and
  close-fallback fills are recorded in `BacktestResult.fill_anomalies` (printed
  by the CLI) instead of passing silently. Fixed a latent bug: a NULL open/close
  on the fill bar produced a NaN reference price instead of falling back/lapsing.
- **A.2 index membership:** `index_membership` table + `config/index_membership.yaml`
  fixture (placeholder dates) + `make refdata` loader. Opt-in via
  `universe.index` in backtest.yaml: entries gated on membership AS OF T;
  exits on held positions always evaluate.
- **A.3 corporate actions:** `corporate_actions` table + fixture + loader; on
  ex-date the engine re-bases held positions (split: qty×r, entry/stop ÷r;
  dividend: cash credit + stop −D; rights: stop ×factor) and in-flight orders,
  so action gaps never fire the ATR stop as market moves — unexplained gaps
  still do. Deterministic back-adjusted series derived into `adjusted=1` rows;
  backtest stays on raw prices (separate decision).
- **A.4 check-data + overrides:** `make check-data` (missing sessions vs the
  benchmark-derived exchange calendar, volume<=0, jumps>threshold without a
  matching action, stale tickers) + Polish Telegram alert via new generic
  `send_text` (dry-run contract preserved). First real-data run immediately
  flagged split-shaped −90% gaps on PZU (2015-11-30) and DNP (2025-07-31) —
  fill `config/corporate_actions.yaml` accordingly. Append-only `overrides`
  journal + `python -m app.cli override`.

### 2026-06-22 — Phase 2 review fixes (PIT tz, persist-then-mark, CLI gate, provider audit)
Code-review hardening of the Phase-2 LLM features layer. 121 tests green
(+8); offline A/B still honestly reports NO improvement (no filings → gate
blocks all entries). Still ZERO LLM in the money path.
- **P1 (point-in-time tz):** filing cutoff now uses Europe/Warsaw local
  end-of-day (`datetime.combine(... time(23,59,59), tzinfo=Warsaw)`), so an
  01:30 CEST filing counts for its OWN local day instead of leaking into the
  prior UTC day. New boundary test.
- **P2 (integrity):** filings are marked `processed` ONLY after the
  `llm_features` row is persisted. A rejected research/synthesis bumps a new
  `attempts` column instead; a filing is retired (marked processed, no feature)
  only after `MAX_ATTEMPTS`, so a malformed item is neither hidden nor retried
  forever. `select_filings_asof` gains a `max_attempts` filter.
- **P2 (CLI gate):** `backtest --strategy trend_momentum_llm` now attaches
  materialized `llm_scores` when the strategy references `llm_score`
  (`engine.strategy_uses_llm_score` + shared `engine.attach_llm_scores`);
  previously only the A/B harness did, so the LLM strategy silently took no
  entries.
- **P2 (provider audit):** the served provider is fetched from the OpenRouter
  generation-metadata endpoint (the chat response omits it) via an injectable
  GET transport; resolved provider is cached so a cache hit keeps it; honest
  NULL on failure (never guessed).

### 2026-06-22 — Phase 2: LLM FEATURES layer + A/B harness (LLM = INPUT only)
Full Phase-2 plumbing, 113 tests green. The LLM never touches sizing/risk/
execution: it produces validated JSON that maps to a single `llm_score` feeding
the existing deterministic strategy/risk engine.
- `app/llm/`: client (pinned provider, cache-by-hash, audited), schemas
  (strict; malformed rejected), research agent (evidence check), synthesis judge
  (quant+fundamentals as context text), pipeline (point-in-time filing
  consumption → `llm_features`, marks processed).
- Point-in-time fundamentals seam (`app/features/fundamentals.py`).
- Engine injects point-in-time `llm_score`; `trend_momentum_llm.yaml` adds one
  `llm_score >= 0` gate. A/B harness + CLI `ab` + `make ab`.
- DB: `fundamentals`, `llm_calls`, `llm_cache`, `llm_features` (idempotent).
- **HONEST GATE STATUS:** offline/demo data has no filings, so the LLM gate
  blocks all entries (safe default) and the harness correctly reports NO
  improvement. A real OOS A/B verdict requires live OpenRouter + real ESPI
  filings; the harness/gate are proven, the empirical win is NOT yet shown.
  Reported honestly per CLAUDE.md — not faked.

### 2026-06-20 — Collector review fixes (dedup order, health, ISIN, timestamps)
Code-review hardening of the collector. 78 tests green; ISIN seam verified
end-to-end (universe → stooq → `instruments.isin`).
- High: cross-source dedup now collects candidates from ALL feeds, sorts
  GLOBALLY by true publication instant, then stores — earliest-published wins
  regardless of feed order in the config (not first-feed-in-config).
- High: health beacon marks success ONLY on a genuinely healthy cycle (every
  configured feed polled OK; none failed/skipped); failed/placeholder feeds
  record an error. `run_once` now returns non-zero on an unhealthy cycle so VPS
  cron/monitoring detects a degraded collector that did not crash.
- Med: real ISINs added to active members in `config/universe.yaml` so ISIN →
  `instrument_id` resolution actually works; stooq persists `isin`.
- Med: `parse_published_at`/`parse_datetime` rewritten — authoritative parsing of
  the raw pubDate (feedparser leaves CET/CEST unconverted, drops no-offset
  dates). Handles RFC numeric-offset / no-offset / GMT / CET / CEST and ISO
  naive/offset; naive → Europe/Warsaw. New tests for each format.

### 2026-06-20 — ESPI/EBI + news collector (standalone, ZERO LLM)
New, independently runnable RSS collector writing append-only into `filings` in
the shared SQLite DB. 68 tests green; one-shot + standalone runs verified.
- `filings` + `collector_health` tables, owned & created by the collector
  (runs before the rest of the app exists); append-only `ON CONFLICT DO NOTHING`.
- Point-in-time: `published_at` from feed pubDate (Europe/Warsaw, tz-aware),
  never from fetch time; `published_at <= T` reads are look-ahead-safe.
- Two-layer dedup: per-item (guid/link/hash) idempotency + cross-source
  (isin, report_number, type) earliest-published-wins.
- ISIN → `instrument_id` resolution (null + resolvable later when unknown);
  added nullable `isin` to `instruments` via idempotent migration.
- Per-feed try/except, structured per-cycle logging, health beacon; placeholder
  feed URLs skipped (config-driven feeds via `config/news_sources.yaml`).
- APScheduler loop + one-shot entrypoints; `make collect` / `collect-loop`;
  README VPS section (cron/systemd/verify). Deps: feedparser, apscheduler.

### 2026-06-20 — Backtest accounting fixes (`52ad1d0`)
Code-review hardening of the event-driven engine + persistence. 60 tests green;
offline end-to-end backtest verified (0 accounting-identity violations).
- High: cash deltas applied atomically in `_execute_order` **before** mark-to-market.
- High: volume-capped SELLs reduce position and re-queue the unfilled remainder.
- Med: `load_instruments` restricts trading to universe-config tickers.
- Med: pending BUY/SELL visible to signal+risk state (cash reserved, no duplicate orders).
- Med: thread `strategy_id` + params into logged decisions; real cash/exposure on every equity row.
- Walk-forward now ONE continuous OOS pass (contiguous union of OOS windows).
- Added `tests/test_engine_accounting.py` (5 regression tests).

### 2026-06-20 — Phase 0+1 deterministic core (`20fe02f` first commit + prior)
Full deterministic decision-support core built and tested (no LLM):
schema, Stooq + offline ingestion, point-in-time features, YAML rule strategy,
risk layer, realistic fills + metrics, event-driven walk-forward backtest,
decision/trade/equity logging, Telegram dry-run stub, CLI, Makefile, README.

---

## Outstanding / blocked

- _(none open)_ — `main` is pushed to `origin` and PRs #3/#4 are merged.

## Next up (Phase 3 — not started)

- [ ] Regime radar / turning points: LLM as *features* feeding a deterministic
  regime signal (still ZERO LLM in the money path). See README "Seams for later
  phases".
- [ ] Fill real fixtures for live use: `config/index_membership.yaml` (GPW WIG20
  revision dates) and `config/corporate_actions.yaml` (dividends/splits/rights),
  then decide on the adjusted-price feature switch.

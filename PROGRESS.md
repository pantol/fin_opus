# Implementation Progress

> Living progress log for the GPW Decision System. **Update this file on every
> implementation step** (new feature, fix, phase gate). Keep entries newest-first.

**Current phase:** Phase 2 (LLM FEATURES layer) — **plumbing COMPLETE & green;
empirical A/B verdict BLOCKED on real data.** Phase 0+1 deterministic core and the
standalone ESPI/EBI collector remain complete. The LLM is ALWAYS only an INPUT;
ZERO LLM in the money path. **Tests:** 113 passing.

---

## Phase status

| Phase | Scope | Status |
|-------|-------|--------|
| 0 | Scaffold: `app/` package, config, db schema, Makefile, README | ✅ Done |
| 1 | Data + features + 1 strategy + full risk + backtest + log + Telegram stub (no LLM) | ✅ Done |
| 2 | LLM via OpenRouter as *features* + A/B harness | 🟨 Plumbing done (113 green); A/B improvement unproven (needs live OpenRouter + real ESPI filings) |
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

- [ ] **GitHub push** — blocked by environment network wall. Run from your own
  terminal: `git push -u origin main` (remote: `https://github.com/pantol/fin_opus.git`).

## Next up (Phase 2 entry — not started)

- [ ] OpenRouter client (`app/llm/`), config in `config/llm.yaml`, pinned provider + logged generation id.
- [ ] LLM outputs as validated JSON → *inputs* to the deterministic risk layer only.
- [ ] Use the `llm-provider-routing` skill; cache by input hash; verify cached_tokens.

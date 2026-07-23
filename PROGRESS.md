# Implementation Progress

> Living progress log for the GPW Decision System. **Update this file on every
> implementation step** (new feature, fix, phase gate). Keep entries newest-first.

**Current phase:** Phase 2 (LLM FEATURES layer) — **plumbing COMPLETE & green;
empirical A/B verdict BLOCKED on real data.** Phase 0+1 deterministic core and the
standalone ESPI/EBI collector remain complete. Hardening packs (A: core, B: infra,
C: validation, D: LLM guardrails) in progress. The LLM is ALWAYS only an INPUT;
ZERO LLM in the money path. **Tests:** 348 passing.

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

### 2026-07-23 (evening) — Fresh-start v2 sim (full market + LLM) + explainable alert cards
- **Fresh-start v2 simulation** (`docs/simulations/fresh-start-full-llm-2026-07-23.md`
  + Telegram mockup): first run of the whole collect → LLM → decide chain on
  the 2026-07-23 close, full-market universe. Fresh 100k `trend_momentum_llm`
  book vs a baseline control in twin sandboxes (`data/sandbox-full-llm.db`,
  `data/sandbox-full-base.db`): the LLM gate vetoed KUP PKN (−0.65, 1.9 bn PLN
  impairment filing) and KUP ALR (−0.70, CJEU −96.5 mln), permitted
  PKO/PEO/OPL, failed closed on 46 scoreless wide-market rule-passers
  (Digitanet/AB PL/ACTION entered the control only). Real book + day-series
  sandbox untouched; ZERO LLM calls in the decide path.
- **Wide-market LLM backlog drained live** (user direction: every company
  should carry an updatable score driving portfolio moves): the whole
  non-curated filing backlog was scored in one evening run —
  **+34 wide-market verdicts** (42 llm_features total as_of 2026-07-23, e.g.
  WIKANA −0.85, SCANWAY −0.80, AIRWAY −0.70, XTPL +0.65, EUROTEL +0.60;
  evidence-quote guardrail lowered confidence on 8), filings backlog now 0,
  month-to-date LLM spend $0.14 of the $10 cap. The reusable DB-driven
  `make llm` implementation (pipeline-level discovery with PIT cutoff,
  "NAME (ISIN)" prompt identity shared with the evalset, per-run cap on new
  instruments) lands via branch `claude/nervous-kapitsa-17a20f` (846eca2,
  based on d23bfdc, 353 tests) — awaiting user merge. Still open for the
  "score-driven book" vision: candidate ranking incl. llm_score (chip),
  explicit neutral default + staleness decay for no-news names (a verdict
  must age out), and the Stage-0 schedule from `docs/plan-window-7-19.md`
  so scores refresh without manual runs.
- **Explainable alert cards** (user review feedback: the bot's cards carried
  none of the "why"): BUY signal cards now show `Werdykt LLM: +0.40
  (pozytywny)` from the order's decision-time snapshot; a new informational
  **🧠 Radar LLM** card lists vetoed/permitted/scoreless entry candidates per
  session; the portfolio summary gains a `Kandydaci do wejscia: 113 • nowe
  sygnaly: 3` funnel line. `strip_llm_conditions()` (strategy engine) tells
  which flat candidates only the llm_* condition kept out — display-layer
  telemetry computed AFTER the deterministic decide; sizing/stops/exits
  untouched. Tests: **348 passing** (8 new: card formats, strip helper,
  radar integration, baseline unchanged).

### 2026-07-23 — Full-market tradable universe (whole GPW main market)
- **`universe.mode: full`** (backtest.yaml, now the default): the tradable
  universe is EVERY non-index instrument in the DB — 627 instruments incl.
  dead tickers (anti-survivorship by construction) — instead of the 17-name
  `universe.yaml` whitelist. `mode: config` keeps the legacy behavior;
  `universe.yaml` remains the benchmark/index definition + curated metadata
  (sectors, listing windows) for the named companies.
- **Deterministic point-in-time liquidity entry gate** (`universe.liquidity`):
  NEW entries require a bar ON the decision session (suspended names are never
  entered on stale quotes) and a 63-session median PLN turnover
  (`turnover_med_63`, new feature) >= 250k as of T. Missing history fails
  closed; exits on held positions always evaluate. Zero look-ahead (tested by
  boosting future volumes and asserting unchanged past decisions).
- **Liquidity-tiered costs** (`costs.liquidity_tiers`): spread/slippage now
  resolve per order from the DECISION-day turnover snapshot (5 tiers,
  20/10 bps for >=20M PLN/day — identical to the old flat values — up to
  250/80 bps below 250k; unknown liquidity = worst tier; commission untiered).
  Applied identically in the engine, the paper loop (via shared
  `_execute_order`), forced final closes, and the random-entry MC benchmark.
- **Full-market ingest by default**: `make ingest` (GPW source) now stores the
  ENTIRE session file when `universe.mode: full` — same number of HTTP
  requests as before. Incremental runs resume after the last FULL-market
  session (data-derived watermark `last_full_market_session`, threshold 100
  instruments/day), so universe-only runs can never strand the wider market
  stale; healed a 2026-07-11..22 gap (3248 bars / 406 tickers) on first run.
- **Full-market performance**: bulk single-query price load, ndarray
  `FeatureView`s (searchsorted as-of lookups instead of pandas masks; ns-unit
  normalization for pandas 2/3 storage units), np.unique trading calendar, and
  a vectorized MC entry-eligibility matrix. `make backtest` end-to-end on 627
  instruments, 2015→2026 walk-forward OOS + 1000 MC sims: **~16s, <1GB RAM**.
  Paper loop evening run: ~8s.
- **Paper-loop guards at full scale**: session-coverage denominator now counts
  only instruments active in the trailing `paper.activity_window_sessions`
  (=15) so long-dead names can't dilute the gate; the config hash covers
  universe mode/gate/tiers, so the switch demands `--accept-config-change`
  (deliberate track-record break).
- **Honest metrics note**: trend_momentum on the full gated universe is
  CAGR -3.5% / Sharpe -0.87 vs WIG20TR +8.5% / 0.47 (DSR ~0, MC percentile
  0.05-0.11) — statistically indistinguishable from the old 17-name whitelist
  (-3.4% / -0.91): the baseline strategy has no edge; the widening changed
  breadth, not the verdict. The system now *sees* the whole market for future
  strategies (falling-knife etc.). Known limits (documented): archive-derived
  instruments carry no sector (per-sector caps don't bind) and no
  listed_from/delisted_on (the fresh-bar gate + coverage window compensate);
  corporate-actions fixtures still cover curated names only. **Tests: 340**
  (20 updated to pin the legacy no-gate config, 11 new in
  `tests/test_full_universe.py`).

### 2026-07-23 — Day-6/7 simulation (sessions 2026-07-21/22) + sandbox persistence
- Sandbox rebuilt: the original day-1…5 sandbox lived in /tmp and was lost;
  reconstructed by deterministic replay of sessions 2026-07-13→20 on a fresh
  DB copy — **byte-parity with the documented day-5 book** (equity 97,731.10,
  cash 23,975.68, all stops and pending orders), proving the loop's
  replayability. Sandbox now persists at `data/sandbox.db` (gitignored).
- **Day 6 (07-21):** Monday's re-entries filled at the open — PEO 67 @ 234.57,
  ALR 27 @ 138.23; whipsaw round-trip priced at +2.2%/+3.9% over Monday's
  exits. Zero new signals; equity 99,208.72 (+1.51% vs WIG20TR +1.78%);
  −0.79% since inception vs +0.85%.
- **Day 7 (07-22):** second zero-activity evening; equity 99,556.10 (+0.35%
  vs +0.60%); −0.44% vs +1.46% since inception. First locked-in profit:
  PKN trailing stop 144.51 crossed above its 144.29 entry.
- Simulation cards now captured via injected `send_fn` (NOT the CLI) because
  `.env` carries live Telegram credentials — a CLI sandbox run would fire
  real messages. Reports: `docs/simulations/day-06-2026-07-21.md`,
  `day-07-2026-07-22.md` (+ mockup HTMLs). Real track record still unstarted.

### 2026-07-20 (evening) — Day-5 simulation: settlement day (session 2026-07-20)
- First evening run executed in the production cron's real time slot.
  Friday's SELLs filled at the Monday open: PEO 82 @ 229.44, ALR 1 @ 133.03
  — first realized paper losses (≈ −985 PLN ≈ 1% of the book, matching the
  risk_per_trade budget). Textbook whipsaw: both banks closed HIGHER and
  re-qualified on the close → re-entry signals BUY PEO 67 / BUY ALR 27
  (freed sector headroom re-sized ALR up from 1 share). Equity 97,731.10
  (+0.16% vs WIG20TR +0.47%; −2.27% vs −0.91% since inception); PKO
  survived its stop by 0.40 PLN. Intraday dataset day 1 complete: 998
  five-minute bars (09:00–16:45, 12 tickers) after one post-close backfill
  cycle. Report: `docs/simulations/day-05-2026-07-20.md` (+ mockup HTML).

### 2026-07-20 — Intraday recorder + stop-monitor tier (day-trading groundwork)
- Direction accepted: day trading needs intraday data. Free real-time GPW
  APIs no longer exist (XTB xAPI shut down 03/2025; Stooq light endpoint
  gone — both verified live); bossaAPI (DM BOŚ account) is the future
  real-time path. v1 RECORDS the free ~15-min-delayed Yahoo chart feed to
  build the dataset any intraday backtest will need.
- New: `prices_intraday` (append-only, first write wins, `as_of_ts` = first
  observation — point-in-time extended to intraday) + `intraday_alerts`
  dedupe table; `app/ingestion/intraday.py` recorder (curl_cffi chrome
  impersonation — Yahoo 429s bare TLS clients like the GPW WAF; CCC + SPL
  are documented feed gaps, foreign-venue quotes rejected on principle);
  `app/alerts/monitor.py` — INFORMATIONAL stop monitor (NEAR_STOP /
  STOP_BREACH Polish cards, at most one per position+state+session, ZERO
  writes to money tables — decisions stay evening-only); `make intraday` /
  `make intraday-loop`; `config/intraday.yaml`.
- Verified live mid-session: 160 bars / 12 tickers stored; monitor flagged
  PKO +0.7%, PEO +0.1%, ALR +1.2% above their stops. Suite **320 passing**.

### 2026-07-20 — Fix: sessionless ingest window no longer aborts `make signals`
- A weekend/holiday-only incremental window produced phantom failures and
  exit 2: the chart-json endpoint answers sessionless ranges with a
  request-echo payload (loud parse failure for every index), and the
  ISIN-never-seen check reported every active instrument against 0 session
  files. On a weekday GPW holiday the 19:30 cron would have aborted the
  whole evening loop (no noop, no queued-card flush). Ingest now runs the
  equities pass first and skips both the index fetch and the absence check
  when the window held zero sessions; benign no-op prints a friendly message
  and exits 0. Real trading-range failures stay loud (exit 2). Suite **309
  passing**.

### 2026-07-18 — Fresh-start scenario (new book on session 2026-07-17)
- Separate sandbox: a brand-new 100k book bootstrapped on the Friday close →
  5 full-size BUY signals (PKO 161, PEO 69, PKN 122, ALR 52, OPL 957; ~72%
  of capital), filling at the Monday 07-20 open. KGH/PZU excluded by
  negative 6M momentum; SPL passes entry rules but sized to ZERO by the
  exhausted 40% banking cap. Documents the trailing-stop churn paradox: the
  day-1…4 book sells PEO/ALR at the same open this book buys them. Report:
  `docs/simulations/fresh-start-2026-07-18.md` (+ mockup HTML).

### 2026-07-18 — Day-4 simulation (session 2026-07-17)
- Sandbox book advanced one session: nothing to settle (empty queue), equity
  97,573.74 (−0.59% vs WIG20TR −0.77% — first day of relative
  outperformance; −2.43% vs −1.37% since inception), PKN/OPL stops trailed
  up, and the FIRST exit signals: PEO (close 229.00 < stop 231.37) and ALR
  (133.30 < 134.41) queued as SELLs for the Monday 07-20 open (weekend
  between decision and fill). Report: `docs/simulations/day-04-2026-07-18.md`
  (+ mockup HTML). Real track record still unstarted.

### 2026-07-17 — Day-3 simulation (session 2026-07-16)
- Sandbox book advanced one session: SPL filled at the Thursday open (1 @
  676.35, fee 3.00 = commission minimum, 0.44% effective — micro-orders are
  proportionally the priciest), equity 98,152.64 (−0.98% vs WIG20TR −0.20%;
  −1.85% since inception vs −0.61%), 8 positions, PKN stop trailed up, and
  the first zero-signal evening (queue empty — steady state). Report:
  `docs/simulations/day-03-2026-07-17.md` (+ mockup HTML). Real track record
  still unstarted.

### 2026-07-16 — Day-2 simulation (session 2026-07-15)
- Sandbox book advanced one session through the real loop: PZU filled at the
  Wednesday open (279 @ 69.24, fee 73.41), equity 99,124.09 (−0.51% vs
  WIG20TR −0.41%), 7 positions, OPL stop trailed up, new SPL signal sized to
  qty 1 by the banking-sector cap. Cards captured verbatim. Report:
  `docs/simulations/day-02-2026-07-16.md` (+ Telegram mockup HTML). Real
  paper track record still unstarted.

### 2026-07-15 — First live-data shakedown of `make signals` + day-1 simulation
- **Fix (`461e3a6`):** unquoted ISO dates in `config/universe.yaml`
  (`listed_from`) arrive from PyYAML as `datetime.date` and crashed
  `paper.loop.config_hash` on the first-ever real `signals` run. `default=str`
  at the three config-serialization sites (the convention
  `validation.config_hash` already used); regression test pins that date
  objects hash identically to their ISO strings. Suite **308 passing**.
- **Day-1 walkthrough on real data** (sandbox copy of `data/gpw.db`; real
  paper track record still unstarted): two evenings replayed through the real
  loop — 6 signals decided 2026-07-13, all filled at the 2026-07-14 open with
  the full cost model (equity 99,636.23 after day one), trailing stops moved,
  1 new signal queued; staleness-refusal ops demo; exact Telegram cards
  captured verbatim. Report: `docs/simulations/day-01-2026-07-15.md`.

### 2026-07-06 — Daily paper-trading loop (`make signals`)
Completes the Phase-1 blueprint gate ("paper trading 1 strategy vs WIG →
Telegram alert card"): the previously dead seams — the `positions` table and
`telegram.send_alert` — are now wired into an operational evening loop.
Suite is **272 passing** (was 246).
- **Contract = the backtest's, live:** decide on T's close → PENDING order →
  fill at T+1's open with the full cost model. Each evening run settles
  yesterday's orders (today's open is now known), then decides today's.
- **Zero duplicated money math:** settlement via `engine._execute_order`,
  sizing inputs via the newly extracted `engine.build_day_state` (also used by
  `run_backtest`), signals via `strategy.evaluate` + `risk.size_position`,
  corporate actions via the promoted `apply_corporate_action`/
  `apply_action_to_order`. `tests/test_paper_parity.py` locks paper == backtest
  (identical trades/equity/cash on a fixture with partial volume-capped sells,
  a suspension gap, dividends and a split).
- **New:** `app/paper/` (store + loop), `paper_state`/`paper_orders` tables,
  `python -m app.cli signals` (`--dry-run`, `--session`,
  `--accept-config-change`), `make signals`, `paper:` block in
  `config/backtest.yaml`, Polish signal/fill/lapse/summary cards, paper section
  in `make status` (stale when the loop falls ≥2 sessions behind).
- **Isolation:** all paper rows live under `user_id 'paper:default'`;
  `_persist_results` refuses the `paper:` namespace so backtests can never
  pollute the live track record.
- **Ops:** idempotent watermark (`last_settled_date`), one `BEGIN IMMEDIATE`
  transaction per session (crash-resumable catch-up), refusals on stale/partial
  data + config drift + oversized gaps, alerts flushed post-commit with
  at-least-once delivery. The LLM strategy variant reads pre-materialized
  `llm_features` only — ZERO LLM calls in this path.

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

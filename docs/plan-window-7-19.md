# PLAN: system operating in the 07:00–19:00 window

> Written 2026-07-23. Status: **accepted with amendments** (same-day review —
> see the amendment log at the bottom). Context: nearly all the building blocks
> already exist (intraday recorder, stop monitor, ESPI/news collector, LLM
> pipeline, A/B harness). What the system lacks most is a **clock**: nothing is
> scheduled, the collector is stalled (last filings 2026-07-01/02), the LLM has
> 0 runs, and the real paper book is unstarted. The Telegram token IS
> configured in `.env` (done) — cards now fire for real, which raises the
> stakes on alert dedup. The day 1–7 sandbox simulation persists at
> `data/sandbox.db`.

## Goal

A program running within defined hours (e.g. 07:00–19:00) that continuously
uses stock prices, market news, analyses from external sources, and LLM agent
analysis — and on that basis sends decision and informational alerts to
Telegram.

## Boundary rules (non-negotiable)

- **Only deterministic code makes decisions.** The LLM produces features
  (`llm_features`) and informational texts — it never decides money.
- **Intraday decisions only after an intraday backtest** (Stage 4). Until
  then, everything during the day is the **informational** tier — exactly like
  today's stop monitor.
- Paper only; a "real-time decision" = a Telegram card + a paper-ledger
  entry, never a real order.
- Point-in-time everywhere — `prices_intraday` already has `as_of_ts`, news
  has `published_at`.

## Target working day (configurable in `config/schedule.yaml`)

| Time | Task | Tier |
|---|---|---|
| 07:05 | `collect` — overnight ESPI/news | data |
| 07:30 | **Morning digest**: positions vs stops, new filings (+ LLM summaries), scheduled events | info |
| 09:00–17:10 every 5 min | intraday recorder + stop monitor (exists) | info |
| every 15 min | `collect` — news/ESPI during the session | data |
| every 30–60 min | LLM-events: evaluate fresh news against positions/watchlist | info |
| 17:45 | last `collect` → `make llm` (materialize today's features; filings-based, independent of EOD prices) | data |
| **19:30** | `ingest` → `signals` — **the only decision point** | **decisions** |
| 19:45 | healthcheck + status card + backup | ops |

The 19:30 decision slot sits **outside the 07:00–19:00 window deliberately**:
the day 1–5 simulation observed the GPW session file landing **after ~19:00**,
and the production cron already lives at 19:30 for that reason. Do NOT move
the slot earlier without empirically verifying earlier publication — deciding
at 18:15 on an unpublished session would silently trade on yesterday's data.

## Stages (each gated by `make test` / backtest)

### Stage 0 — start the tape rolling TODAY (zero code, hours)

The critical path is not the clock — it is **data accumulation**. Stage 2
needs processed filings; Stage 4 needs *weeks* of recorded 5-minute bars.
Every idle day is lost evidence, so before any scheduler exists:

1. Crude crontab: `collect` every 15 min (07:00–19:00), `intraday-loop`,
   evening `make signals` at 19:30. Stage 1 replaces this with the daemon;
   nothing is thrown away because the jobs themselves don't change.
2. **Bootstrap the real paper book deliberately**: one intentional manual
   `make signals` (19:30, after publication). Inception date and the
   config-hash pin are set there — it must not happen silently as a side
   effect of the daemon's first evening chain.

### Stage 1 — clock and channel (foundation, ~1–2 days)

New `app/scheduler.py` + `make daemon`: one process that reads
`config/schedule.yaml` (07:00–19:00 window, weekdays, GPW calendar) and fires
the existing jobs within the window; `schedule_runs` journal (idempotency,
visible in `make status`); morning digest; launchd on Mac (note: a sleeping
laptop = skipped jobs; VPS is the target — jobs are idempotent, so they catch
up). **Fix the known `monitor.py` bug**: `check_positions` selects all OPEN
positions with no `user_id` scoping — it must filter before any second user
exists. **Zero decision changes.**

**Explicitly deferred out of Stage 1:** the `alert_log` generalization
(priorities, quiet-hours in `config/alerts.yaml`, per-`user_id` routing). The
current dedup (`intraday_alerts` + NULL alert timestamps) suffices for one
user; per-user routing is Phase 6 work and will be better informed there.
Keeping Stage 1 to clock + journal + digest + the monitor fix is what makes
the "~1–2 days" estimate real.

### Stage 2 — broader sources + LLM as an informational radar (~2–3 days)

`config/news_sources.yaml` (curated RSS list — inspiration from
`mythos_finance`, but wired in here), macro calendar. **First decide whether
event scoring is a new `llm_event_features` table or an extension of
`llm_features`** — the latter already carries the model+provider+generation_id
audit and `relevance`; duplicating the audit pattern invites drift. During the
day, a card like: *"⚠️ ESPI: CCC — profit warning. LLM assessment: relevance
0.9, sentiment −0.7. Open position, stop 12.40. (informational — decision in
the evening)"*. Rules per the `llm-provider-routing` skill: cheap model for
extraction, hash cache, pinned provider, hard daily cost cap, validated JSON.
Gate: precision on the golden set (`eval_labels` already exists) + cost/day
from `llm_costs`.

### Stage 3 — LLM enters the evening decisions (~1 day + evidence time)

`make llm` scheduled before `signals`; OOS A/B with the existing harness
(`make ab`): `trend_momentum` vs `trend_momentum_llm`. Instead of breaking the
continuity of the freshly started ledger — a **second, parallel paper book**
(`user_id = paper:llm`) on the LLM strategy: an honest live comparison with no
`--accept-config-change` on the main book. Gate: OOS edge vs WIG20TR.

### Stage 4 — intraday decisions (hardest gate, ~1 week+)

Intraday backtest on the accumulated 5-min bars from the recorder (which is
why the recorder must run from Stage 0); the fill model includes the **15-min
feed delay** + spread + volume **and an explicit no-bar policy for feed gaps**
(CCC and SPL are documented dropouts — skip vs stale-reference must be decided
in the model, not improvised). Candidate rules: intraday stop execution
(instead of next-open), delayed entry after a gap. Only a positive
walk-forward OOS promotes a rule to the intraday "decision tick". Real-time
via bossaAPI = a separate adapter writing to the same `prices_intraday`
(future).

## Config and schema sketches

```yaml
# config/schedule.yaml
window: {start: "07:00", end: "19:00", tz: Europe/Warsaw, days: [mon,tue,wed,thu,fri]}
jobs:
  - {name: collect,    every_min: 15}
  - {name: intraday,   every_min: 5, window: ["09:00","17:10"]}
  - {name: llm_events, every_min: 30, budget_pln_day: 2.0}
  - {name: digest,     at: "07:30"}
  # Anchored OUTSIDE the window: GPW publishes the session file ~19:00.
  - {name: evening,    at: "19:30", chain: [ingest, llm, signals]}
  - {name: health,     at: "19:45"}
```

```sql
CREATE TABLE schedule_runs (job TEXT, scheduled_for TEXT, started_at TEXT,
  finished_at TEXT, status TEXT, detail TEXT);
-- DEFERRED (out of Stage 1; revisited with Phase 6 multi-user work):
-- CREATE TABLE alert_log (user_id TEXT, kind TEXT, dedup_key TEXT UNIQUE,
--   priority INTEGER, sent_at TEXT, payload TEXT);
```

## Risks / assumptions

- **Yahoo feed**: 15-min delayed, gaps (CCC/SPL) — it is never an execution
  reference; a conscious limitation until bossaAPI.
- **Host**: a Mac sleeps — launchd mitigates, but a reliable 07:00–19:00
  window ultimately needs a VPS (the monolith doesn't change, only where it
  runs).
- **LLM cost** at the current filing volume: single-digit PLN monthly (cheap
  extraction + cache); a hard daily cap in config.
- Intraday history is young — Stage 4 needs weeks of recording, which is why
  it is last and the recorder starts first (Stage 0).

## What we deliberately do NOT do

Real trading, LLM in the money path, microservices, paid feeds at the start.

## Amendment log

- **2026-07-23 (review)** — translated to English; incorporated review
  findings: Telegram token marked done (`.env` carries live credentials);
  evening decision slot moved 18:15 → 19:30 (observed ~19:00 publication in
  the day 1–5 simulation; moving earlier requires evidence); added Stage 0
  (crude cron + deliberate real-book bootstrap — data accumulation is the
  critical path); Stage 1 trimmed (`alert_log`/per-user routing deferred to
  Phase 6; `monitor.py` user_id-scoping bug added to scope); Stage 2 gained
  the `llm_event_features` vs `llm_features` dedup note; Stage 4 gained the
  explicit feed-gap fill policy.

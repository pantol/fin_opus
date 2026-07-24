# PLAN: expanding the LLM layer — recommendations & portfolio-move analysis

> Written 2026-07-24. Status: **PROPOSED (awaiting user review)**. The hard
> boundary does not move: ZERO LLM in sizing / risk / execution (CLAUDE.md
> rule 1). Every proposal below is either (a) a MATERIALIZED FEATURE that
> deterministic YAML rules may consume, or (b) a DISPLAY-layer narrative
> rendered strictly AFTER the deterministic decide, from logged data. All
> calls follow the llm-provider-routing skill: pinned provider, audited
> generation ids, input-hash cache, validated JSON (malformed = rejected),
> cost routing, monthly hard cap. Personal paper use only — sharing
> "recommendations" with other people enters MAR/MiFID territory
> (blueprint §14); nothing here changes that.

## Where the LLM works today (baseline to extend)

filings → research agent (validated JSON) → synthesis judge → one
`llm_score` per (instrument, day) → entry GATE + entry RANKING input;
age-decayed breadth feeds one regime component; alert cards show verdicts
(radar card, verdict line); onboarding chat extracts survey answers.
Everything else below builds on those rails.

---

## P1. Evening portfolio narrative — „Wieczorny przegląd portfela" (display)

**What:** one Polish card/page per book after each `signals` run: what was
decided today and WHY (which rules fired, ranking position, LLM verdict,
regime state), position-by-position health (P&L, stop distance, days held,
fresh filings on holdings), what would have to happen tomorrow for the
nearest entries/exits (thresholds read from config — stated, not invented).

**How:** a `narrator` role (cheap model) receives ONLY logged facts — the
run's `PaperRunReport`, decision snapshots (`features_json`), regime row,
fresh filings — pre-serialized by deterministic code into a compact context
block. The model turns facts into prose; a post-check verifies every number
quoted appears in the input (same spirit as `evidence_quote`), else the card
falls back to the plain deterministic summary. Delivered after commit
(best-effort, like the radar card) + stored for the dashboard.

**Decision-path impact:** NONE (display only).
**Schema:** `llm_narratives (user_id, session_date, kind, content, llm_call_id)`.
**Effort:** ~1 day. **Cost:** 1 call/evening/book, ~2–3k tokens → grosze/mies.

## P2. Position thesis tracking + thesis-break as an EXIT input (feature)

**What:** every position gets a living „teza wejścia". At entry the judge
writes it (from the entry snapshot + latest research). Each NEW filing on a
HELD name is evaluated against the stored thesis: confirms / neutral /
breaks → `llm_thesis_break` ∈ [-1, 1] materialized point-in-time. Strategy
YAML may then ADD an exit path:

```yaml
exit:
  any:
    - {type: atr_stop, atr_mult: 2.5}
    - {feature: close_vs_sma200, op: lt, value: 0.0}
    - {feature: llm_thesis_break, op: lte, value: -0.6}   # thesis broken
```

**Boundary:** an LLM feature may OPEN an extra exit door, never close one —
existing deterministic exits always evaluate; sizing/stops untouched. Missing
feature = condition false (fails closed, like llm_score).
**How:** new `thesis` role reusing the synthesis model; thesis stored per
position at entry (`llm_theses`); evaluations materialized into the existing
llm-features flow keyed (instrument, as_of_date) with `kind='thesis'`.
Backtest: A/B baseline-exit vs +thesis-exit through the standard harness
once filings history allows (same honesty gate as Phase 2).
**Decision-path impact:** NEW INPUT (exit gate) — the second decision-grade
LLM feature after llm_score; changes the config fingerprint of any book that
adopts it (deliberate `--accept-config-change`).
**Effort:** ~2 days (+tests: PIT, fails-closed, exits-always-evaluate parity).
**Cost:** 1 call per (new filing × held name) — a few/day at most.

## P3. Intraday LLM events radar (window-plan Stage 2, already accepted)

**What:** during 07:00–19:00 the scheduler's `llm_events` job (every 30–60
min) scores FRESH filings/news against positions + entry-watchlist and sends
informational cards: „⚠️ ESPI 14:20: CCC — profit warning. LLM: relevance
0.9, sentiment −0.7. Masz pozycję, stop 12.40. (informacyjnie — decyzja
wieczorem)". The evening decide stays the only decision point.

**How:** reuse the research agent verbatim on the event text; NEW table
`llm_event_features` (append-only, multiple per day, `published_at`
anchored, `llm_call_id` audit FK) — the daily `llm_features` row stays the
single decision-grade feature; a deterministic end-of-day fold (max-severity
per instrument) feeds the evening `make llm` run as context, nothing else.
Dedup per (filing, position-state); per-day budget in schedule.yaml
(`budget_pln_day`) on top of the monthly cap; quiet hours = the window.
**Decision-path impact:** NONE (informational tier, like the stop monitor).
**Effort:** ~2 days. **Cost:** bounded by the daily budget knob (~$0.02–0.05/day
at current filing volume).

## P4. LLM veto scoreboard — measuring the „recommendations" (deterministic)

**What:** the radar already logs which entries the LLM vetoed/permitted.
Track each veto's counterfactual DETERMINISTICALLY (what the vetoed entry
would have done under the standard cost model over the next N sessions) and
report monthly: „weta LLM zaoszczędziły/kosztowały X bps; trafność wet:
7/10". Zero LLM calls — this is price math ABOUT the LLM, the honest
evidence base the Phase-2 A/B gate is waiting for.

**Decision-path impact:** NONE (report).
**Schema:** none new (derives from decisions + prices); CLI `llm-scoreboard`.
**Effort:** ~0.5–1 day. **Cost:** zero.

## P5. Entry-candidate briefs (display, folds into P1)

Top-N of tomorrow's ranked entry candidates get 2–3-sentence Polish briefs:
quant setup (from the snapshot) + latest filing digest (from stored
research_json — usually NO new call thanks to the cache) + known risks.
Rendered inside the P1 narrative and the dashboard candidate funnel.
**Impact:** NONE. **Effort:** ~0.5 day inside P1. **Cost:** ≈0 (cache reads).

## P6. Weekly retrospective (display, extends P1)

Sunday-evening slot: equity vs WIG20TR, best/worst contributors, exits
audit (stop vs trend-break vs thesis), regime timeline, overrides journal
(discipline check), next week's calendar. Same narrator role + fact-check
guard; stored in `llm_narratives (kind='weekly')`.
**Impact:** NONE. **Effort:** ~1 day. **Cost:** 1 synthesis-class call/week.

## P7. Thematic radar — cocoa / El Niño class themes (later, user direction)

Extraction tags collected news with a CLOSED theme taxonomy (validated
enum, e.g. commodity_softs, energy_prices, rates, fx_pln, weather_supply);
deterministic code aggregates theme intensity and maps themes → sectors →
portfolio exposure; a card fires when a hot theme overlaps holdings or the
watchlist. Needs the news-source list widened first (rss curation — the
`mythos_finance` list as inspiration, wired HERE). Kept LAST deliberately:
value depends on source breadth, not model cleverness.
**Impact:** NONE initially (informational); a theme feature could later
enter YAML gates through the same materialized-feature door as llm_score.
**Effort:** ~2–3 days + source curation.

## P8. PDF fundamentals extraction (data enabler)

Periodic reports arrive as PDF attachments (known follow-up in PROGRESS).
Extraction role reads report text → REVENUE/PROFIT/DEBT numbers → the
`fundamentals` table (as_of = publication date). NUMBERS are then handled
only by deterministic code; synthesis keeps receiving them as context text.
Unlocks value-style strategies (Phase-4 library has no value leg today) and
richer synthesis context.
**Impact:** better INPUTS, same boundaries. **Effort:** ~2 days (incl. the
PDF fetch follow-up). **Cost:** per report, cheap model, cached.

---

## Common guardrails (all proposals)

- Roles in `config/llm.yaml` (`narrator`, `thesis` added alongside
  extraction/synthesis) — models/prices/temps there, never hardcoded; both
  covered by the existing monthly cap + per-day budget for P3.
- Validated JSON everywhere; narratives additionally pass the
  quoted-numbers-must-appear-in-input check or fall back to the plain
  deterministic card (no hallucinated P&L, ever).
- Point-in-time: events keyed by `published_at`, theses by entry date,
  narratives read only committed rows.
- Every new prompt lands in the eval harness before it ships changes
  (`make eval-llm` gate), and every decision-grade feature (P2) goes through
  the A/B + DSR/MC gate before any book adopts it.
- Polish for user-facing text; disclaimers on anything that smells like a
  recommendation; paper only.

## Suggested order (each step gated by make test / backtest)

| Step | Items | Why first | Effort |
|---|---|---|---|
| 1 | **P1 + P4** | Direct hit on „analiza ruchów w portfolio"; display-only (no fingerprint break); P4 starts accumulating the evidence Phase 2 needs | ~1.5 dnia |
| 2 | **P2** | The one genuinely NEW decision input (exit door); highest analytical value; needs the full test/gate treatment | ~2 dni |
| 3 | **P3** | Already-accepted Stage 2; makes the daytime window useful | ~2 dni |
| 4 | **P8** | Data enabler for synthesis + value strategies | ~2 dni |
| 5 | **P6 → P5 → P7** | Extensions once the core loop is richer | wg potrzeb |

Total new run-rate cost at current volumes: well under the existing 10 USD/mc
cap (dominant cost stays the wide-market filing backlog, already drained).

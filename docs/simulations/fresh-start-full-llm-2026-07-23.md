# Fresh start v2 — full market + LLM flow (2026-07-23)

New scenario alongside the day 1–7 series: **what does the system pick when
it can see the WHOLE market (PR #15) and the LLM layer reads today's
filings?** A fresh 100 000 PLN paper book decides on the 2026-07-23 close
under `universe.mode: full` (PIT liquidity gate, tiered costs) with
**`trend_momentum_llm` v1 — the first simulation to exercise the full
collect → LLM → decide chain.** A twin baseline book (`trend_momentum`)
ran the same session as the control. Both live in their own sandbox
copies; the real `data/gpw.db` book (bootstrapped this morning) and the
day-series `data/sandbox.db` are untouched. **Paper trading only** — and
the LLM stays an INPUT: `run_signals` made ZERO LLM calls, it only read
pre-materialized `llm_features` rows.

## What was done (the full flow, in order)

1. **Pull PR #15** (`universe.mode: full` + liquidity gate) — local main
   was 2 commits behind origin; `make test`: **340 passed**.
2. **Ingest** — session 2026-07-23 landed in the GPW archive ~18:45
   (second poll attempt); 406 instruments printed bars.
3. **Collect** — one cycle at 18:17: 3/3 feeds healthy, 52 items seen,
   3 new (189 filings total, newest 2026-07-23 17:50).
4. **LLM materialization** — `make llm` found 0 unprocessed curated
   filings: the 11:36 run had already written all 8 features for
   as_of 2026-07-23 (deepseek-v4-flash both roles, pinned Novita,
   generation ids logged; month-to-date spend ≈ $0.016 of the $10 cap).
5. **Two fresh sandboxes** (`VACUUM INTO` snapshots, `paper:*` wiped):
   `data/sandbox-full-llm.db` (scenario) and `data/sandbox-full-base.db`
   (control). Cards captured via injected `send_fn` — nothing was sent
   to Telegram.

## What the LLM read in today's filings (as_of 2026-07-23)

| Ticker | llm_score | Verdict | Filing behind it |
|---|---:|---|---|
| OPL | **+0.60** | bullish 0.60 | Strong quarterly results release |
| PKO | **+0.40** | bullish 0.40 | Synthetic securitisation, ~+25 bps CET1 |
| PEO | 0.00 | neutral 0.30 | Routine subordinated Tier-2 bond issuance |
| PZU | 0.00 | neutral 0.40 | BlackRock crosses 5% of shares |
| KGH | 0.00 | neutral 0.50 | June production/sales data, neutral |
| SPL | **−0.60** | bearish 0.60 | MREL regulatory update → compliance costs |
| PKN | **−0.65** | bearish 0.65 | **1.9 bn PLN impairment charge**, risk of more write-downs |
| ALR | **−0.70** | bearish 0.70 | **CJEU judgment: −96.5 mln PLN off Q2 net income** |

The other ~620 instruments have **no llm_score**, and in
`trend_momentum_llm` a missing score **fails the entry closed** (safe
default — no guessing). Rationale, research JSON and generation ids sit
in `llm_features` / `llm_calls` for every row above.

## The wide market through the deterministic gates (23.07 close)

**210 names pass the baseline entry rules** (close > SMA200 AND 6M
momentum > 0); **52 clear the liquidity gate** (fresh bar on T + 63-session
median turnover ≥ 250 k PLN). The momentum top of the LIQUID list:

| Name | Close | vs SMA200 | 6M mom | Median turnover |
|---|---:|---:|---:|---:|
| Trans Polonia | 13.05 | +48.8% | **+245.2%** | 343 k |
| ASBIS | 111.80 | +115.4% | +200.2% | 22.0 M |
| Medinice | 73.20 | +67.7% | +175.2% | 2.3 M |
| Digitanet (d. 4fun Media) | 297.00 | +69.8% | +90.6% | 2.5 M |
| XTB | 135.96 | +50.0% | +73.4% | 36.3 M |
| Odlewnie | 21.10 | +36.3% | +71.5% | 360 k |
| Comp | 90.50 | +34.6% | +61.6% | 756 k |
| Auto Partner | 27.80 | +38.0% | +52.7% | 2.9 M |
| Inter Cars | 915.00 | +37.4% | +52.5% | 2.7 M |
| PKN Orlen | 155.86 | +33.2% | +48.5% | 204.3 M |

(Names the old whitelist could never see; WIG20TR itself fell −1.13%
today.) PZU's 6M momentum flipped **negative today** (−0.7%) and KGH sits
at −3.7% — both out at the rules stage, regardless of their neutral LLM
scores.

## Decisions — two books, one session

| Name | Baseline (control) | LLM book | Why the difference |
|---|---|---|---|
| PKO | BUY 160, stop 100.99 | **BUY 160**, stop 100.99 | llm +0.40 permits |
| PEO | BUY 68, stop 218.40 | **BUY 68**, stop 218.40 | llm 0.00 permits (≥ 0) |
| OPL | BUY 1180, stop 13.68 | **BUY 1180**, stop 13.68 | llm +0.60 permits |
| PKN | BUY 128, stop 148.08 | — | **llm −0.65 veto** (impairment) |
| ALR | BUY 52, stop 123.30 | — | **llm −0.70 veto** (CJEU) |
| Digitanet | BUY 27, stop 260.75 | — | no llm_score → fail closed |
| AB PL | BUY 91, stop 128.07 | — | no llm_score → fail closed |
| ACTION | BUY 52, stop 40.17 | — | no llm_score → fail closed |

Both books queue everything as PENDING — fills at the **Friday 24.07
open** (+ spread, slippage, commission, volume cap). Identical quantities
where both books enter (same deterministic sizing). Baseline commits
≈ 99.97 k PLN (fully invested, banking cap 40 k exhausted by
PKO+PEO+ALR → SPL sized to zero); the LLM book commits ≈ 50.1 k and
keeps **~50% cash**, because text vetoed two momentum leaders and the
rest of the market has no scores yet.

**Cards captured (LLM book, 4):** 3× 📌 KUP (PKO 160, PEO 68, OPL 1180)
+ 📊 portfolio summary — see the
[Telegram mockup](fresh-start-full-llm-2026-07-23-telegram-mockup.html).

## Observations

1. **The LLM gate vetoed live BUY signals on real filings.** Baseline
   queues KUP PKN and KUP ALR — the same names the REAL book queued this
   morning — while the LLM book refuses both: PKN just disclosed a
   1.9 bn PLN impairment, ALR quantified a −96.5 mln PLN CJEU hit.
   This is Phase 2 doing exactly what it was built for: information the
   price/momentum features cannot see yet, converted into a
   deterministic entry veto. (SPL was excluded twice over: LLM −0.60 in
   the scenario book, banking-cap-to-zero in the control.)
2. **No cross-sectional ranking yet.** Candidates are evaluated in
   instrument-id order (curated names first, then archive discoveries),
   so the control filled its 8 slots before Trans Polonia (+245%),
   ASBIS (+200%), Medinice (+175%) or XTB (+73%) were even considered —
   AB PL (+16.6% momentum) got in on arrival order. With a 600-name
   universe, ranking the qualifying candidates (e.g. momentum-descending)
   before allocation is the single highest-leverage improvement this
   scenario exposes.
3. **LLM coverage is the binding constraint of the LLM book.** Only the
   8 curated names have scores, so at most the 5 with score ≥ 0 can ever
   enter — the wide market is visible but fail-closed. 46 unprocessed
   filings already map (via ISIN) to non-curated instruments; widening
   `make llm` beyond the curated list is the natural follow-up.
4. **A red-market fresh start.** WIG20TR −1.13% on decision day; the
   LLM book's half-cash posture is a direct, explainable consequence of
   two bearish filings — not a market-timing rule.
5. **Point-in-time identity churn, handled.** Yesterday's session file
   named `PL4FNMD00013` "4FUNMEDIA"; today's says "DIGITANET" (rebrand).
   The ISIN-keyed instrument row absorbed the rename with no duplicate.

## Reproduce

```bash
sqlite3 data/gpw.db "VACUUM INTO 'data/sandbox-full-llm.db'"
# wipe the inherited real-book rows (paper:* namespace) in the copy, then:
python -m app.cli --db data/sandbox-full-llm.db signals \
  --strategy trend_momentum_llm --session 2026-07-23
# control book: same with data/sandbox-full-base.db and --strategy trend_momentum
# (this run used an injected send_fn instead of the CLI so no Telegram fired)
```

## Relation to the other books

- **Real book** (`data/gpw.db`, `paper:default`): bootstrapped TODAY
  10:19 on the 2026-07-22 close under the pre-PR-#15 config — 6 pending
  BUYs (PKO 166, PEO 71, PKN 131, PZU 281, ALR 38, OPL 1212). **Tonight's
  real `make signals` will REFUSE** (config_hash changed with PR #15)
  until run with `--accept-config-change` — and note the morning queue
  holds KUP PKN + KUP ALR, the two names today's filings turned bearish.
- **Day-series sandbox** (`data/sandbox.db`): day-7 state, untouched —
  that book is fully invested under the old whitelist and cannot take
  new names anyway.

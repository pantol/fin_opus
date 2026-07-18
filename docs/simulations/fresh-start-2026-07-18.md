# Fresh start — a brand-new book from zero (2026-07-18)

Side scenario alongside the running day 1–4 simulation: **what would the
system suggest if the portfolio started today?** A fresh 100 000 PLN paper
book bootstraps and decides on the latest close (Friday 2026-07-17), with
no positions and no consumed risk limits. Run in its **own sandbox copy** —
both the real DB and the continuing day-1…4 sandbox are untouched. Same
rules: **paper trading** only, **zero LLM** (`trend_momentum` v1).

## New suggestions (decided on the 2026-07-17 close)

| Signal | Qty | Initial ATR stop | ~Value at Fri close |
|---|---:|---:|---:|
| BUY PKO | 161 | 100.60 | ~17.2k |
| BUY PEO | 69 | 214.62 | ~15.8k |
| BUY PKN | 122 | 138.07 | ~17.8k |
| BUY ALR | 52 | 123.87 | ~6.9k |
| BUY OPL | 957 | 13.48 | ~13.9k |

Total ≈ 71.6k PLN (~72% of capital) if filled at Friday's closes; actual
amounts are set by the **Monday 2026-07-20 open** (+ spread, slippage,
commission). All five queued as PENDING; the weekend sits between decision
and fill.

**Cards sent (6):** five 📌 KUP signal cards (as above) + the 📊 summary
(`Kapital: 100,000.00 PLN, Otwarte pozycje: 0`).

## Why this list differs from the day-1…4 book

Feature values on the 2026-07-17 close (from `compute.features_at`):

| Ticker | close vs SMA200 | 6M momentum | Outcome |
|---|---:|---:|---|
| KGH | +4.0% | **−5.5%** | fails momentum → out |
| PZU | +7.3% | **−0.8%** | fails momentum → out |
| SPL | +14.8% | +21.0% | **passes entry rules**, but the 40% banking cap is already exhausted by PKO+PEO+ALR (≈39.9k of 40k) → sized to zero → no signal |
| PEO | +5.7% | +9.9% | passes → BUY 69 |
| ALR | +14.1% | +19.7% | passes → BUY 52 |

Three observations worth keeping:

1. **Quantities are full-size now.** In the running book ALR was a 1-share
   position and SPL a 1-share order — leftovers of a nearly-full banking
   cap. A fresh book allocates the sector budget in universe order
   (PKO → PEO → ALR → SPL), so ALR gets a real 52-share position and SPL,
   fourth in line, gets nothing. Same rules, different arrival order of
   capital.
2. **The PEO/ALR "paradox".** The continuing book SELLS Pekao and Alior at
   Monday's open (their trailing stops — anchored at higher prices reached
   earlier — were pierced on Friday), while this fresh book BUYS them at
   the same open with new, lower stops. Both decisions are correct under
   the rules: the trailing stop remembers the position's peak; entry rules
   only look at today. This is the known churn cost of trailing-stop
   strategies, visible here in the open.
3. **Momentum is the active filter.** KGH and PZU are still above their
   200-session averages, but negative 6-month momentum keeps them out —
   the two-condition entry is stricter than a plain trend filter.

## Fresh sandbox state after the run

Inception 2026-07-17, last settled 2026-07-17, cash 100 000.00, 0 open
positions, 5 pending BUY orders (above). Next simulated evening for this
book (Monday's session) would fill all five at the Monday open.

## Reproduce

```bash
cp data/gpw.db /tmp/fresh.db
python -m app.cli --db /tmp/fresh.db signals --session 2026-07-17
```

## Relation to the other simulations

- Day 1–4 book (separate sandbox): 8 positions, equity 97 573.74, SELL
  PEO + SELL ALR pending for Monday — see
  [day-04-2026-07-18.md](day-04-2026-07-18.md).
- The real paper track record remains unstarted; a real `make signals`
  tonight would produce exactly this fresh-start decision set.

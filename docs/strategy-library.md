# Strategy library (Phase 4)

One engine runs any YAML (CLAUDE.md rule 7). The library adds academically
motivated strategies over the same deterministic feature/risk/cost machinery
— and, so far honestly, none of them has demonstrated an edge on GPW.

## Catalog

| YAML | Academic root | Core rule |
|---|---|---|
| `trend_momentum` | baseline (Phase 1) | close > SMA200 & 6M momentum > 0 |
| `trend_momentum_llm` | Phase 2 | baseline + `llm_score >= 0` gate |
| `trend_momentum_regime` | Phase 3 | baseline + `market_risk_on` gate |
| `xs_momentum` | Jegadeesh & Titman (1993) | top-quintile 12-1 momentum (`mom_12_1_pct >= 0.80`) + trend filter; exit on rank decay |
| `week52_high` | George & Hwang (2004) | within 5% of the 52w high + positive 6M momentum; exit at 80% of the high |
| `low_vol` | Blitz & van Vliet (2007) | calmest vol tercile (`vol_xs_pct <= 0.30`) + trend filter |
| `falling_knife` | De Bondt & Thaler (1985), user-directed | >= 40% below the 52w high AND a stabilization bounce (`ret_1m >= 5%`, `ret_3m > 0`); profit target at 85% of the high |

New per-instrument features: `mom_12_1` (12-month return skipping the last
month), `pct_52w_high` (close / trailing 252-session max). Cross-sectional
percentiles (`mom_12_1_pct`, `vol_xs_pct` — `app/features/cross_sectional.py`)
rank each name against every instrument with a defined value that day, dead
tickers included while alive (anti-survivorship). Attachment is
strategy-scoped: YAMLs that never reference an xs feature keep byte-identical
snapshots.

## Validation gate (unchanged, applies to every candidate)

`make backtest` / `python -m app.cli backtest --strategy <name>`:
walk-forward OOS (embargo 252 sessions) vs **WIG20TR**, realistic costs
(commission + tiered spread/slippage + volume cap), trials registry + Deflated
Sharpe + random-entry Monte Carlo. A strategy "passes" only with positive OOS
edge AND `min_dsr` / `min_random_percentile` (backtest.yaml
`validation.gates`). Every run — including failures — lands in
`strategy_trials`, so later successes are deflated by today's attempts.

## Honest results (first full run, 2026-07-24; OOS ~2017→2026)

| Strategy | CAGR | Sharpe | maxDD | Trades | DSR | Verdict |
|---|---|---|---|---|---|---|
| WIG20TR (benchmark) | +8.4% | 0.47 | −48% | — | — | — |
| trend_momentum (v2) | −2.6% | −0.52 | −28% | 74 | 0.02 | no edge |
| trend_momentum_regime | −3.0% | −0.43 | −27% | 121 | 0.02 | no edge (better Sharpe/maxDD than baseline, gates still failed) |
| xs_momentum | −5.2% | −0.58 | −39% | 51 | 0.00 | no edge |
| week52_high | −5.0% | −0.58 | −36% | 35 | 0.00 | no edge |
| low_vol | −7.8% | −0.57 | −52% | 23 | 0.00 | no edge |
| falling_knife | −3.7% | −0.74 | −27% | 60 | 0.00 | no edge |

**Phase-4 exit gate ("≥ 1 academic strategy passes validation on GPW") is NOT
met.** The library, the DSL, the cross-sectional features and the validation
harness all work — and the harness correctly refuses to bless any of these
first-parameter attempts. That refusal is the system working as designed, not
a failure of the phase's plumbing. Long-only daily rules with retail GPW costs
have a high bar; parameter studies must go through the same registry (every
try deflates the next Sharpe).

## Adding a strategy

1. Write `config/strategies/<name>.yaml` (entry/exit/entry_ranking/risk).
   New per-instrument features go in `app/features/compute.py`; new
   cross-sectional ones in `XS_FEATURES` (`app/features/cross_sectional.py`).
2. `make test` — the library smoke test picks up parseability; add a
   behavioral test for any new feature.
3. `python -m app.cli backtest --strategy <name>` — the gate decides, not
   enthusiasm. Paper-trade a passing candidate as a SECOND book before it
   touches the main one.

Seam (deliberately not built): an LLM PDF→spec extractor for turning papers
into YAML drafts. The DSL is the contract; a draft generator changes nothing
about the gate.

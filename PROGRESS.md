# Implementation Progress

> Living progress log for the GPW Decision System. **Update this file on every
> implementation step** (new feature, fix, phase gate). Keep entries newest-first.

**Current phase:** Phase 0+1 (deterministic core, no LLM) — **COMPLETE & green.**
**Tests:** 60 passing. **Last commit:** `52ad1d0`.

---

## Phase status

| Phase | Scope | Status |
|-------|-------|--------|
| 0 | Scaffold: `app/` package, config, db schema, Makefile, README | ✅ Done |
| 1 | Data + features + 1 strategy + full risk + backtest + log + Telegram stub (no LLM) | ✅ Done |
| 2 | LLM via OpenRouter as *features* (regime radar / turning points) | ⬜ Not started |
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

## Invariants (enforced by tests)

- [x] Point-in-time / no look-ahead (`tests/test_point_in_time.py`)
- [x] Anti-survivorship: delisted tickers traded only in `[listed_from, delisted_on]` (`tests/test_integration.py`)
- [x] Deterministic money logic, reproducible with pinned seed (`tests/test_risk.py`, `tests/test_integration.py`)
- [x] Realistic fills: buy@ask / sell@bid + costs + volume cap (`tests/test_fills.py`)
- [x] Walk-forward OOS vs WIG20TR (never SPY)
- [x] Multi-tenant `user_id` seam on decisions/positions/trades/equity
- [x] ZERO LLM in the money path

---

## Changelog (newest first)

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

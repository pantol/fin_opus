# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# GPW Decision System

> Claude Code reads this file automatically at the start of every session and keeps it in context. **Keep it concise (<100 lines) and stable.** These rules apply in EVERY session.

## What this project is
A decision-support system for investing on the GPW (Warsaw Stock Exchange; later also emerging markets). It filters, scores, and orders signals. **AI/LLM = language layer only**; all money and risk logic is deterministic code. **Paper trading only.** Architecture: a simple monolith designed for future multi-tenancy.

## Non-negotiable rules (NEVER break them)
1. **Money logic = deterministic code.** ZERO LLM calls in the financial decision / risk / sizing / execution path.
2. **Point-in-time:** every data row has `as_of_date` = publication/availability date. No look-ahead.
3. **Anti-survivorship:** the universe includes delisted / dead tickers.
4. **Realistic backtest:** commission + spread (buy at ask, sell at bid) + slippage + no fills beyond real volume. Always **walk-forward out-of-sample**.
5. **Benchmark = WIG / WIG20TR** (total return). NEVER SPY.
6. **Multi-tenant:** a `user_id` column on every decisions / positions / portfolio table, even with a single user.
7. **Strategies as YAML config files**, not hardcoded logic. One engine runs any config.
8. **Reproducibility:** every decision stored with full context (features, parameters, timestamp; for LLM — served provider + model + version).
9. **Simple monolith.** No microservices / Kubernetes / premature infrastructure.
10. **Real/live trading is FORBIDDEN.** Paper only. No integrations that place real orders.

## Working loop (build → test → gate) — follow it on every task
1. **Plan → implement** the smallest coherent piece.
2. **Run `make test`.** Red → fix and repeat until green. **Do NOT move on with failing tests.**
3. If the change touches data/features/strategy/backtest → **`make backtest`**, check metrics vs WIG20TR (OOS, realistic costs).
4. **Commit** with a clear message.
5. Only then the next task. **Do NOT pass through a failed phase gate.**
- If you're unsure whether something breaks point-in-time or money determinism — stop and verify with a test before proceeding.

## Conventions
- Python (pandas / numpy / pandas-ta; backtest: vectorbt or custom event-driven).
- **Code, comments, commit messages: English.** Strings for the end user (Telegram alerts): **Polish**.
- Structure: `app/` package (`ingestion`, `features`, `strategy`, `risk`, `backtest`, `logging`, `alerts`, `llm`, `cli`). Config in `config/`. Strategies in `config/strategies/*.yaml`. Skills in `.claude/skills/<name>/SKILL.md`.
- Database: SQLite locally; schema designed for a trivial migration to Postgres/TimescaleDB (clean types, no SQLite-only hacks).
- Secrets only from environment variables / `.env` (in `.gitignore`). Never in the repo.

## LLM access — OpenRouter (from Phase 2; Phases 0+1 are LLM-free)
- OpenAI-compatible API: base URL `https://openrouter.ai/api/v1`, key `OPENROUTER_API_KEY` from env. Model from `config/llm.yaml`.
- **Reproducibility (critical):** pin the provider (`provider: { order: [...], allow_fallbacks: false }`); log the served provider + model + generation id on EVERY decision.
- **Caching:** enable provider caching; **verify** hits via `usage.prompt_tokens_details.cached_tokens`; keep the prompt prefix stable.
- **Cost routing:** cheap model for extraction, escalate to a pricier one only for synthesis. Low temperature. Cache results by input hash.
- LLM outputs always as **validated JSON**; malformed → reject and log, do not guess. Use the `llm-provider-routing` skill. The LLM is always only an INPUT to the risk layer.

## Commands (keep them working)
- `make setup` — install dependencies.
- `make test` — tests (money/risk logic + point-in-time test).
- `make ingest` — fetch EOD data into SQLite.
- `make backtest` — data → features → walk-forward backtest → metrics vs WIG20TR.

## How you work
- First a short **PLAN** (file tree, DB schema, strategy config format). Wait for approval, or proceed with sensible defaults and list your assumptions.
- Ask only when something genuinely blocks you.
- Build incrementally; test everything that touches money or time.
- Implement only the current phase — leave clean seams for future ones.

## Phase scope (context — not to build all at once)
- **Phase 0+1:** data + features + 1 strategy + full risk + backtest + log + Telegram stub (**no LLM**).
- **Phases 2+:** LLM via OpenRouter as features → regime radar / turning points → academic strategies → survey/profile → multi-tenant.

**Current phase: Phase 0+1.**

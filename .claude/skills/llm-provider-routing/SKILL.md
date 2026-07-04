---
name: llm-provider-routing
description: Use when writing or modifying ANY code that calls an LLM (OpenRouter) in the GPW decision system — the LLM features layer, research/extraction agents, synthesis/judge, prompt building, or response handling. Enforces reproducibility (pinned provider, logged served provider+model+generation id), provider+local caching verified via cached_tokens, cost routing (cheap extraction / pricier synthesis), low temperature, validated JSON only, and the hard rule that the LLM is ALWAYS only an INPUT to deterministic code — never in the money/risk/sizing/execution path. Trigger phrases: LLM, OpenRouter, prompt, research agent, synthesis, judge, sentiment, model, provider, cache, cached_tokens, generation id, JSON schema.
---

# LLM provider routing & reproducibility

Apply whenever code calls an LLM. The LLM is a LANGUAGE layer only.

## 1. The LLM is ALWAYS only an INPUT
- ZERO LLM calls in sizing / risk / execution / any code that computes an amount (CLAUDE.md rule 1).
- TEXT (filings, news) -> the LLM reads it. NUMBERS (prices, fundamentals, ratios) -> deterministic code computes them; the LLM receives them as CONTEXT TEXT only and must never recompute them.
- LLM output is consumed only as validated JSON, mapped to a single numeric feature that feeds the deterministic strategy/risk layer.

## 2. OpenRouter access
- OpenAI-compatible API: base URL `https://openrouter.ai/api/v1`, key `OPENROUTER_API_KEY` from env (never in the repo).
- Model + provider come from `config/llm.yaml`, not hardcoded.

## 3. Reproducibility (critical)
- PIN the provider: `provider: { order: [<provider>], allow_fallbacks: false }`. No silent fallback to another provider.
- On EVERY call, log: served provider, served model, generation id, input hash. Store them (CLAUDE.md rule 8) so any decision is reproducible.
- Low temperature (0.0 for extraction). Deterministic prompt prefix.

## 4. Caching
- Cache results by INPUT HASH = sha256(model + params + prompt). A local cache hit returns stored JSON WITHOUT any network call — keep backtests deterministic on replay.
- Enable provider caching too; VERIFY hits via `usage.prompt_tokens_details.cached_tokens`. Keep the prompt prefix stable so it caches.

## 5. Cost routing
- Cheap model for extraction/research; escalate to a pricier model only for synthesis.
- Cache by input hash so repeated backtests cost nothing.

## 6. Validated JSON only
- Define a JSON schema per call. Validate the response. Malformed -> REJECT and log; never guess or repair into a fabricated value.
- Cross-check claims against the source where possible (e.g. an `evidence_quote` must appear in the source text; if not, lower confidence).
- Schema + prompt change TOGETHER (additionalProperties:false rejects any drift); a prompt change invalidates the input-hash cache and re-spends.

## 6b. Cost + evaluation guardrails
- Every live call's cost (tokens x per-model price from config/llm.yaml) is ledgered in `llm_costs`; the monthly hard cap (`budget.monthly_usd_cap`) blocks LIVE calls only — cache hits stay free. Budget exhaustion degrades gracefully: run marked `degraded` in `llm_runs`, alert sent, filings left untouched — NEVER bump attempts for a budget stop (the wallet is empty, the filings are fine).
- No prompt/model change ships if it regresses on the golden set (`make eval-llm` vs `eval_labels`); every eval run lands in `eval_runs` with the prompt fingerprint.

## 7. Self-check before finishing
- [ ] Any LLM call in the sizing/risk/execution path? If yes -> remove.
- [ ] Is the provider pinned (allow_fallbacks: false)?
- [ ] Are served provider + model + generation id logged on EVERY call?
- [ ] Is caching keyed by input hash, and a hit verified (no duplicate network call + cached_tokens)?
- [ ] Is the response validated against a schema, with malformed rejected (not guessed)?
- [ ] Are point-in-time rules honored for the TEXT (published_at <= T) and the NUMBERS (as_of_date <= T) the prompt is built from?
- [ ] Is the call's cost recorded, and does the monthly cap still trigger (prices present for every configured model)?
- [ ] If the prompt/schema changed: did `make eval-llm` run without a regression, and was the cache re-spend accounted for?

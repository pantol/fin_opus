"""OpenRouter client wrapper — reproducible, cached, audited. ZERO money logic.

Responsibilities (see the `llm-provider-routing` skill):
  * Call OpenRouter (OpenAI-compatible) with a PINNED provider
    (`provider.order` + `allow_fallbacks: false`) so the served provider is
    reproducible.
  * Log the served provider + model + generation id + cached_tokens on EVERY
    call into `llm_calls` (CLAUDE.md rule 8 reproducibility).
  * Cache by INPUT HASH = sha256(model + params + messages). A local cache hit
    returns stored JSON WITHOUT any network call, so backtests are deterministic
    on replay.

NOTE on the served provider: OpenRouter's chat-completion response does NOT
include the served provider name. It is exposed only by the generation-metadata
endpoint (GET /generation?id=<generation_id>). We therefore fetch it from there
(best-effort) after a live call; if it cannot be resolved the field is honestly
left NULL rather than guessed. See the `llm-provider-routing` skill.

The HTTP transports are injectable so tests run fully offline with a fake
provider (no network in CI). The wrapper never parses domain meaning — it only
returns the raw message content string; schema validation lives in `schemas.py`.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Callable

import requests

# A transport takes (url, headers, json_body, timeout) and returns a parsed dict
# (the OpenRouter JSON response). Injected in tests to avoid any network call.
Transport = Callable[[str, dict, dict, int], dict]

# A metadata transport takes (url, headers, timeout) and returns a parsed dict
# (the OpenRouter generation-metadata response). Injected in tests to stay
# offline; when absent, provider resolution is skipped (field left NULL).
MetaTransport = Callable[[str, dict, int], dict]


class LLMBudgetExceededError(RuntimeError):
    """Monthly LLM spend cap reached: live calls must stop.

    The pipeline catches this to degrade gracefully (baseline-only) — it must
    NOT be treated like a malformed response (no attempt bumping: the filings
    are fine, the wallet is empty).
    """


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def input_hash(model: str, params: dict, messages: list[dict]) -> str:
    """Deterministic content hash identifying a call's full input."""
    payload = json.dumps(
        {"model": model, "params": params, "messages": messages},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _default_transport(url: str, headers: dict, body: dict, timeout: int) -> dict:
    resp = requests.post(url, headers=headers, json=body, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _default_meta_transport(url: str, headers: dict, timeout: int) -> dict:
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


class LLMResult:
    """Outcome of one completion call (content + reproducibility metadata)."""

    def __init__(
        self,
        *,
        content: str,
        served_model: str | None,
        served_provider: str | None,
        generation_id: str | None,
        cached_tokens: int | None,
        cache_hit: bool,
        input_hash: str,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> None:
        self.content = content
        self.served_model = served_model
        self.served_provider = served_provider
        self.generation_id = generation_id
        self.cached_tokens = cached_tokens
        self.cache_hit = cache_hit
        self.input_hash = input_hash
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class LLMClient:
    """Thin OpenRouter wrapper. One instance per (conn, llm_config)."""

    def __init__(
        self,
        conn,
        llm_config: dict,
        *,
        transport: Transport | None = None,
        meta_transport: MetaTransport | None = None,
        api_key: str | None = None,
    ) -> None:
        self.conn = conn
        self.cfg = llm_config
        self.base_url = llm_config["base_url"].rstrip("/")
        self.cache_enabled = bool(llm_config.get("cache", {}).get("enabled", True))
        self.timeout = int(llm_config.get("request_timeout_seconds", 60))
        self._transport = transport or _default_transport
        # Optional GET transport for the generation-metadata endpoint (served
        # provider). When None, provider stays NULL rather than being guessed.
        self._meta_transport = meta_transport or _default_meta_transport
        # API key required only for a real network call; tests inject transport.
        self._api_key = api_key if api_key is not None else os.environ.get("OPENROUTER_API_KEY")
        # Monthly spend cap (USD). When set, every configured model MUST have a
        # price entry — an unpriced model would undercount cost and the cap
        # would silently never trigger.
        budget = llm_config.get("budget") or {}
        cap = budget.get("monthly_usd_cap")
        self.monthly_cap = float(cap) if cap is not None else None
        if self.monthly_cap is not None:
            prices = llm_config.get("prices") or {}
            for role in ("extraction", "synthesis"):
                model = (llm_config.get(role) or {}).get("model")
                if model and model not in prices:
                    raise ValueError(
                        f"budget.monthly_usd_cap is set but prices['{model}'] is "
                        f"missing in config/llm.yaml — the cap could never trigger"
                    )

    # -- public API ---------------------------------------------------------
    def complete_json(self, role: str, messages: list[dict]) -> LLMResult:
        """Run a chat completion for `role` ('extraction'|'synthesis').

        Returns an LLMResult whose `.content` is the raw assistant message
        (expected to be JSON; validation happens in schemas.py). A local cache
        hit short-circuits the network and is still logged (cache_hit=1).
        """
        role_cfg = self.cfg[role]
        model = role_cfg["model"]
        params = {
            "temperature": role_cfg.get("temperature", 0.0),
            "max_tokens": role_cfg.get("max_tokens"),
            "provider": role_cfg.get("provider", {}),
            "response_format": {"type": "json_object"},
        }
        ihash = input_hash(model, params, messages)

        cached = self._cache_get(ihash) if self.cache_enabled else None
        if cached is not None:
            result = self._result_from_response(cached, ihash, cache_hit=True)
            self._log_call(role, model, result)
            return result

        # Spend cap: only LIVE calls cost money, so the check sits between the
        # cache lookup and the network — replays from cache always work.
        self._check_budget()

        response = self._post(model, params, messages)
        # The chat response has no provider; resolve it from generation metadata
        # and stash it under a private key so a later cache hit keeps it too.
        gen_id = response.get("id")
        response["_served_provider"] = self._resolve_provider(gen_id)
        if self.cache_enabled:
            self._cache_put(ihash, response)
        result = self._result_from_response(response, ihash, cache_hit=False)
        call_id = self._log_call(role, model, result)
        self._record_cost(call_id, role, model, result)
        return result

    def month_spend_usd(self) -> float:
        """Total recorded spend in the current UTC calendar month."""
        month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) FROM llm_costs WHERE created_at LIKE ?",
            (f"{month_prefix}%",),
        ).fetchone()
        return float(row[0])

    def _check_budget(self) -> None:
        if self.monthly_cap is None:
            return
        spent = self.month_spend_usd()
        if spent >= self.monthly_cap:
            raise LLMBudgetExceededError(
                f"monthly LLM budget exhausted: {spent:.2f} USD spent, cap is "
                f"{self.monthly_cap:.2f} USD (config/llm.yaml budget.monthly_usd_cap)"
            )

    def _record_cost(self, call_id: int | None, role: str, model: str,
                     result: LLMResult) -> None:
        """Append the per-call cost (tokens x per-model config price)."""
        prices = (self.cfg.get("prices") or {}).get(model) or {}
        cost = 0.0
        if result.prompt_tokens:
            cost += result.prompt_tokens / 1e6 * float(prices.get("input_per_1m", 0.0))
        if result.completion_tokens:
            cost += result.completion_tokens / 1e6 * float(prices.get("output_per_1m", 0.0))
        self.conn.execute(
            "INSERT INTO llm_costs (llm_call_id, created_at, role, model,"
            " prompt_tokens, completion_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (call_id, _now_utc_iso(), role, model,
             result.prompt_tokens, result.completion_tokens, cost),
        )
        self.conn.commit()

    def _resolve_provider(self, generation_id: str | None) -> str | None:
        """Best-effort fetch of the served provider from generation metadata.

        OpenRouter's chat response omits the provider; the generation-metadata
        endpoint exposes it. Returns None (honest NULL) on any failure rather
        than guessing.
        """
        if not generation_id or not self._api_key:
            return None
        url = f"{self.base_url}/generation?id={generation_id}"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            meta = self._meta_transport(url, headers, self.timeout)
        except Exception:
            return None
        data = meta.get("data") if isinstance(meta, dict) else None
        if isinstance(data, dict):
            return data.get("provider_name")
        return None

    # -- internals ----------------------------------------------------------
    def _post(self, model: str, params: dict, messages: list[dict]) -> dict:
        if not self._api_key:
            raise RuntimeError(
                "OPENROUTER_API_KEY is not set; cannot make a live LLM call. "
                "Inject a transport for offline/test use."
            )
        body: dict[str, Any] = {"model": model, "messages": messages}
        for k, v in params.items():
            if v is not None and v != {}:
                body[k] = v
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/chat/completions"
        return self._transport(url, headers, body, self.timeout)

    @staticmethod
    def _result_from_response(response: dict, ihash: str, *, cache_hit: bool) -> LLMResult:
        choices = response.get("choices") or [{}]
        content = (choices[0].get("message") or {}).get("content", "")
        usage = response.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        # Provider comes from generation metadata (stashed under _served_provider
        # at call time); the raw chat response never carries it.
        served_provider = response.get("_served_provider")
        return LLMResult(
            content=content,
            served_model=response.get("model"),
            served_provider=served_provider,
            generation_id=response.get("id"),
            cached_tokens=details.get("cached_tokens"),
            cache_hit=cache_hit,
            input_hash=ihash,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )

    def _cache_get(self, ihash: str) -> dict | None:
        row = self.conn.execute(
            "SELECT response_json FROM llm_cache WHERE input_hash = ?", (ihash,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def _cache_put(self, ihash: str, response: dict) -> None:
        self.conn.execute(
            """
            INSERT INTO llm_cache (input_hash, response_json, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(input_hash) DO NOTHING
            """,
            (ihash, json.dumps(response, sort_keys=True), _now_utc_iso()),
        )
        self.conn.commit()

    def _log_call(self, role: str, requested_model: str, result: LLMResult) -> int | None:
        cur = self.conn.execute(
            """
            INSERT INTO llm_calls
                (created_at, role, requested_model, served_model, served_provider,
                 generation_id, input_hash, cached_tokens, cache_hit)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _now_utc_iso(),
                role,
                requested_model,
                result.served_model,
                result.served_provider,
                result.generation_id,
                result.input_hash,
                result.cached_tokens,
                1 if result.cache_hit else 0,
            ),
        )
        self.conn.commit()
        return cur.lastrowid

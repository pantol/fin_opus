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

The HTTP transport is injectable so tests run fully offline with a fake
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
    ) -> None:
        self.content = content
        self.served_model = served_model
        self.served_provider = served_provider
        self.generation_id = generation_id
        self.cached_tokens = cached_tokens
        self.cache_hit = cache_hit
        self.input_hash = input_hash


class LLMClient:
    """Thin OpenRouter wrapper. One instance per (conn, llm_config)."""

    def __init__(
        self,
        conn,
        llm_config: dict,
        *,
        transport: Transport | None = None,
        api_key: str | None = None,
    ) -> None:
        self.conn = conn
        self.cfg = llm_config
        self.base_url = llm_config["base_url"].rstrip("/")
        self.cache_enabled = bool(llm_config.get("cache", {}).get("enabled", True))
        self.timeout = int(llm_config.get("request_timeout_seconds", 60))
        self._transport = transport or _default_transport
        # API key required only for a real network call; tests inject transport.
        self._api_key = api_key if api_key is not None else os.environ.get("OPENROUTER_API_KEY")

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

        response = self._post(model, params, messages)
        if self.cache_enabled:
            self._cache_put(ihash, response)
        result = self._result_from_response(response, ihash, cache_hit=False)
        self._log_call(role, model, result)
        return result

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
        return LLMResult(
            content=content,
            served_model=response.get("model"),
            served_provider=response.get("provider"),
            generation_id=response.get("id"),
            cached_tokens=details.get("cached_tokens"),
            cache_hit=cache_hit,
            input_hash=ihash,
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

    def _log_call(self, role: str, requested_model: str, result: LLMResult) -> None:
        self.conn.execute(
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

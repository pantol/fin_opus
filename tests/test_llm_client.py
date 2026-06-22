"""LLM client wrapper tests — fully offline (injected transport, no network).

Covers: reproducibility logging (served provider+model+generation id on EVERY
call), cache-by-input-hash (a hit returns stored JSON with NO second network
call AND is logged with cache_hit), cached_tokens capture, and missing-API-key
guard.
"""
from __future__ import annotations

from app.config import load_llm_config
from app.db import connect, init_db
from app.llm.client import LLMClient, input_hash


def _fake_response(content='{"ok": true}'):
    # NOTE: the real OpenRouter chat response has NO `provider` field; the served
    # provider is exposed only by the generation-metadata endpoint (see
    # _fake_meta_transport). The fake deliberately omits it.
    return {
        "id": "gen-abc123",
        "model": "openai/gpt-4o-mini",
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens_details": {"cached_tokens": 7}},
    }


def _fake_meta_transport(provider="OpenAI"):
    """GET /generation?id=... stub returning the served provider name."""
    def meta(url, headers, timeout):
        return {"data": {"id": "gen-abc123", "provider_name": provider}}
    return meta


def _counting_transport(response):
    calls = {"n": 0}

    def transport(url, headers, body, timeout):
        calls["n"] += 1
        return response

    return transport, calls


def _client(transport, meta_transport=None):
    conn = connect(":memory:")
    init_db(conn)
    cfg = load_llm_config()
    return (
        LLMClient(
            conn, cfg, transport=transport,
            meta_transport=meta_transport or _fake_meta_transport(),
            api_key="test-key",
        ),
        conn,
    )


def _messages():
    return [{"role": "user", "content": "hello"}]


def test_served_provider_model_generation_logged_on_every_call():
    transport, _ = _counting_transport(_fake_response())
    client, conn = _client(transport)
    res = client.complete_json("extraction", _messages())
    assert res.served_provider == "OpenAI"
    assert res.served_model == "openai/gpt-4o-mini"
    assert res.generation_id == "gen-abc123"
    assert res.cached_tokens == 7
    row = conn.execute(
        "SELECT served_provider, served_model, generation_id, cached_tokens FROM llm_calls"
    ).fetchone()
    assert row["served_provider"] == "OpenAI"
    assert row["served_model"] == "openai/gpt-4o-mini"
    assert row["generation_id"] == "gen-abc123"
    assert row["cached_tokens"] == 7


def test_provider_resolved_from_generation_metadata_not_chat_response():
    # The chat response carries no provider; it must come from the metadata GET.
    transport, _ = _counting_transport(_fake_response())
    client, conn = _client(transport, meta_transport=_fake_meta_transport("Together"))
    res = client.complete_json("extraction", _messages())
    assert res.served_provider == "Together"
    row = conn.execute("SELECT served_provider FROM llm_calls").fetchone()
    assert row["served_provider"] == "Together"


def test_provider_is_null_when_metadata_unavailable():
    # A failing metadata endpoint must yield an honest NULL, never a guess.
    transport, _ = _counting_transport(_fake_response())

    def failing_meta(url, headers, timeout):
        raise RuntimeError("metadata endpoint down")

    client, conn = _client(transport, meta_transport=failing_meta)
    res = client.complete_json("extraction", _messages())
    assert res.served_provider is None
    row = conn.execute("SELECT served_provider FROM llm_calls").fetchone()
    assert row["served_provider"] is None


def test_cached_provider_survives_cache_hit():
    # A cache hit must still report the provider resolved on the original call,
    # WITHOUT a second metadata lookup.
    transport, tcalls = _counting_transport(_fake_response('{"v": 1}'))
    meta_calls = {"n": 0}

    def meta(url, headers, timeout):
        meta_calls["n"] += 1
        return {"data": {"provider_name": "OpenAI"}}

    client, _ = _client(transport, meta_transport=meta)
    r1 = client.complete_json("extraction", _messages())
    r2 = client.complete_json("extraction", _messages())
    assert tcalls["n"] == 1 and meta_calls["n"] == 1  # no second network/meta call
    assert r1.served_provider == r2.served_provider == "OpenAI"
    assert r2.cache_hit is True


def test_cache_hit_returns_stored_json_without_second_network_call():
    transport, calls = _counting_transport(_fake_response('{"v": 1}'))
    client, conn = _client(transport)
    r1 = client.complete_json("extraction", _messages())
    r2 = client.complete_json("extraction", _messages())  # identical input
    assert calls["n"] == 1  # NO second network call
    assert r1.cache_hit is False and r2.cache_hit is True
    assert r1.content == r2.content == '{"v": 1}'
    # both calls logged; exactly one is a cache hit
    rows = conn.execute("SELECT cache_hit FROM llm_calls ORDER BY id").fetchall()
    assert [r["cache_hit"] for r in rows] == [0, 1]


def test_different_input_is_a_cache_miss():
    transport, calls = _counting_transport(_fake_response())
    client, _ = _client(transport)
    client.complete_json("extraction", [{"role": "user", "content": "a"}])
    client.complete_json("extraction", [{"role": "user", "content": "b"}])
    assert calls["n"] == 2  # different prompts -> two network calls


def test_input_hash_is_deterministic_and_input_sensitive():
    h1 = input_hash("m", {"t": 0.0}, [{"role": "user", "content": "x"}])
    h2 = input_hash("m", {"t": 0.0}, [{"role": "user", "content": "x"}])
    h3 = input_hash("m", {"t": 0.0}, [{"role": "user", "content": "y"}])
    assert h1 == h2 and h1 != h3


def test_missing_api_key_raises_before_any_network():
    conn = connect(":memory:")
    init_db(conn)
    cfg = load_llm_config()

    def transport(url, headers, body, timeout):  # pragma: no cover - must not run
        raise AssertionError("network must not be touched without a key")

    client = LLMClient(conn, cfg, transport=transport, api_key="")
    try:
        client.complete_json("extraction", _messages())
        assert False, "expected RuntimeError for missing key"
    except RuntimeError as exc:
        assert "OPENROUTER_API_KEY" in str(exc)

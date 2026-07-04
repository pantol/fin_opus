"""Pack D: golden eval set, discrete relevance, LLM spend cap (all offline)."""
import json

import pytest

from app.config import load_llm_config
from app.llm import evalset, pipeline
from app.llm import research as research_mod
from app.llm.client import LLMBudgetExceededError, LLMClient
from app.llm.schemas import (
    RELEVANCE_LABELS,
    LLMValidationError,
    validate_research,
)
from app.ingestion import filings_db


# --- helpers ---------------------------------------------------------------------

def _research_payload(**over):
    base = {
        "sentiment": 0.5, "relevance": "relevant_interesting",
        "catalysts": [], "risks": [], "event_type": "report",
        "confidence": 0.9, "evidence_quote": "zysk wzrosl",
    }
    base.update(over)
    return base


def _fake_response(content: str, prompt_tokens=1000, completion_tokens=200):
    return {
        "id": "gen-1", "model": "openai/gpt-4o-mini",
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": prompt_tokens,
                  "completion_tokens": completion_tokens,
                  "prompt_tokens_details": {"cached_tokens": 0}},
    }


def _make_client(conn, content_fn, cfg_over=None):
    cfg = load_llm_config()
    if cfg_over:
        cfg = {**cfg, **cfg_over}

    def transport(url, headers, body, timeout):
        return _fake_response(content_fn(body))

    def meta(url, headers, timeout):
        return {"data": {"provider_name": "OpenAI"}}

    return LLMClient(conn, cfg, transport=transport, meta_transport=meta, api_key="k")


def _insert_filing(conn, filing_id_hint, title, text, published="2026-06-01T10:00:00+02:00"):
    filings_db.ensure_schema(conn)
    conn.execute(
        "INSERT INTO filings (source, issuer_name, title, published_at, fetched_at,"
        " url, full_text, content_hash, dedup_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("test", "Spolka SA", title, published, published, "http://x",
         text, f"hash{filing_id_hint}", f"dedup{filing_id_hint}"),
    )
    conn.commit()
    return conn.execute("SELECT MAX(id) FROM filings").fetchone()[0]


# --- relevance in the schema -------------------------------------------------------

def test_relevance_is_required_and_enum_bound():
    with pytest.raises(LLMValidationError, match="relevance"):
        payload = _research_payload()
        del payload["relevance"]
        validate_research(json.dumps(payload))
    with pytest.raises(LLMValidationError):
        validate_research(json.dumps(_research_payload(relevance="very_relevant")))
    out = validate_research(json.dumps(_research_payload(relevance="irrelevant")))
    assert out["relevance"] == "irrelevant"


def test_prompt_mentions_relevance_and_version_is_stable():
    messages = research_mod.build_messages("pko", "text")
    assert "relevance" in messages[1]["content"]
    assert all(label in messages[1]["content"] for label in RELEVANCE_LABELS)
    v1 = research_mod.prompt_version()
    assert v1 == research_mod.prompt_version()  # deterministic
    assert len(v1) == 16


def test_pipeline_materializes_relevance_column(conn):
    conn.execute("INSERT INTO instruments (id, ticker, name) VALUES (9, 'aaa', 'aaa')")
    _insert_filing(conn, 1, "Kontrakt", "Spolka zysk wzrosl znaczaco.")
    conn.execute("UPDATE filings SET instrument_id = 9")
    conn.commit()

    cfg = load_llm_config()

    def content_fn(body):
        if body["model"] == cfg["synthesis"]["model"]:
            return json.dumps({"verdict": "bullish", "conviction": 0.8, "rationale": "ok"})
        return json.dumps(_research_payload(relevance="relevant_uninteresting"))

    client = _make_client(conn, content_fn)
    verdict = pipeline.compute_feature_for_date(
        conn, client, instrument_id=9, ticker="aaa",
        as_of_date="2026-06-02", quant_score=0.1)
    assert verdict is not None
    row = conn.execute("SELECT relevance FROM llm_features").fetchone()
    assert row["relevance"] == "relevant_uninteresting"
    series = pipeline.load_llm_relevance(conn, 9)
    assert list(series) == [pipeline.RELEVANCE_TO_SCORE["relevant_uninteresting"]]


def test_strategy_llm_feature_detection_generalized():
    from app.backtest import engine
    llm_rel = {"entry": {"all": [{"feature": "llm_relevance", "op": "gte", "value": 1.0}]},
               "exit": {}}
    assert engine.strategy_uses_llm_features(llm_rel)
    assert not engine.strategy_uses_llm_features({"entry": {"all": []}, "exit": {}})


# --- labeling CLI -------------------------------------------------------------------

def test_labeling_loop_saves_skips_and_quits(conn, capsys):
    ids = [_insert_filing(conn, i, f"Komunikat {i}", "tresc") for i in range(3)]
    answers = iter(["1", "s", "3"])
    saved = evalset.run_labeling(conn, "u1", input_fn=lambda _: next(answers),
                                 print_fn=lambda *a: None)
    assert saved == 2
    rows = {r["filing_id"]: r["label"] for r in
            conn.execute("SELECT filing_id, label FROM eval_labels").fetchall()}
    # newest-first ordering: same published_at -> DB order preserved; two labels exist
    assert len(rows) == 2
    assert set(rows.values()) == {"relevant_interesting", "irrelevant"}
    # only unlabeled remain
    assert len(evalset.unlabeled_filings(conn)) == 1

    # quit immediately: nothing more saved
    saved2 = evalset.run_labeling(conn, "u1", input_fn=lambda _: "q",
                                  print_fn=lambda *a: None)
    assert saved2 == 0


def test_relabeling_upserts_single_row(conn):
    fid = _insert_filing(conn, 1, "Komunikat", "tresc")
    evalset.save_label(conn, fid, "irrelevant", "u1")
    evalset.save_label(conn, fid, "relevant_interesting", "u1", notes="poprawka")
    rows = conn.execute("SELECT * FROM eval_labels").fetchall()
    assert len(rows) == 1
    assert rows[0]["label"] == "relevant_interesting"
    assert rows[0]["notes"] == "poprawka"


# --- eval harness -------------------------------------------------------------------

def test_eval_harness_scores_against_golden_set(conn):
    # three labeled filings; the fake model predicts by title marker
    fid1 = _insert_filing(conn, 1, "DOBRY kontrakt", "tresc dobra")
    fid2 = _insert_filing(conn, 2, "NUDNY raport", "tresc nudna")
    fid3 = _insert_filing(conn, 3, "SMIECIOWY spam", "tresc smieciowa")
    evalset.save_label(conn, fid1, "relevant_interesting", "u1")
    evalset.save_label(conn, fid2, "relevant_uninteresting", "u1")
    evalset.save_label(conn, fid3, "irrelevant", "u1")

    def content_fn(body):
        text = body["messages"][1]["content"]
        if "DOBRY" in text:
            rel = "relevant_interesting"
        elif "NUDNY" in text:
            rel = "irrelevant"  # deliberate mistake -> accuracy 2/3
        else:
            rel = "irrelevant"
        return json.dumps(_research_payload(relevance=rel, evidence_quote="tresc"))

    client = _make_client(conn, content_fn)
    report = evalset.run_eval(conn, client)
    assert report.n_labels == 3
    assert report.accuracy == pytest.approx(2 / 3)
    assert report.f1["relevant_interesting"] == pytest.approx(1.0)
    assert report.f1["relevant_uninteresting"] == pytest.approx(0.0)
    # irrelevant: tp=1, fp=1, fn=0 -> P=0.5 R=1 -> F1=2/3
    assert report.f1["irrelevant"] == pytest.approx(2 / 3)
    run = conn.execute("SELECT * FROM eval_runs").fetchone()
    assert run["n_labels"] == 3
    assert run["prompt_version"] == research_mod.prompt_version()
    assert json.loads(run["f1_json"])["relevant_interesting"] == pytest.approx(1.0)


def test_eval_harness_counts_rejected_as_wrong(conn):
    fid = _insert_filing(conn, 1, "Komunikat", "tresc")
    evalset.save_label(conn, fid, "relevant_interesting", "u1")
    client = _make_client(conn, lambda body: "not json")
    report = evalset.run_eval(conn, client)
    assert report.n_rejected == 1
    assert report.accuracy == 0.0


def test_eval_without_labels_returns_none(conn):
    filings_db.ensure_schema(conn)
    client = _make_client(conn, lambda body: "{}")
    assert evalset.run_eval(conn, client) is None


def test_labeling_eof_ends_session_like_quit(conn):
    """Ctrl-D / exhausted piped stdin must end cleanly, not crash."""
    _insert_filing(conn, 1, "Komunikat", "tresc")

    def eof_input(_):
        raise EOFError
    saved = evalset.run_labeling(conn, "u1", input_fn=eof_input,
                                 print_fn=lambda *a: None)
    assert saved == 0  # clean return, no traceback


def test_previous_run_catches_model_only_change(conn):
    """A model swap with an unchanged prompt is still a comparable config."""
    filings_db.ensure_schema(conn)
    conn.execute(
        "INSERT INTO eval_runs (created_at, prompt_version, requested_model,"
        " n_labels, accuracy, f1_json) VALUES ('t1', 'promptA', 'model-big', 10, 0.85, '{}')")
    conn.commit()
    # same prompt, different model -> the model-big run IS the baseline
    prev = evalset.previous_run(conn, "promptA", "model-cheap")
    assert prev is not None
    assert prev["requested_model"] == "model-big"
    assert prev["accuracy"] == pytest.approx(0.85)
    # same prompt AND same model -> a replay, no baseline to compare against
    assert evalset.previous_run(conn, "promptA", "model-big") is None


# --- spend cap ----------------------------------------------------------------------

def test_costs_recorded_per_live_call(conn):
    client = _make_client(conn, lambda body: json.dumps(_research_payload()))
    client.complete_json("extraction", [{"role": "user", "content": "a"}])
    row = conn.execute("SELECT * FROM llm_costs").fetchone()
    assert row["prompt_tokens"] == 1000 and row["completion_tokens"] == 200
    # gpt-4o-mini: 1000/1e6*0.15 + 200/1e6*0.60 = 0.00015 + 0.00012
    assert row["cost_usd"] == pytest.approx(0.00027)
    assert row["llm_call_id"] is not None


def test_cap_blocks_live_calls_but_not_cache_hits(conn):
    tiny = {"budget": {"monthly_usd_cap": 0.0002}}
    client = _make_client(conn, lambda body: json.dumps(_research_payload()),
                          cfg_over=tiny)
    messages = [{"role": "user", "content": "a"}]
    client.complete_json("extraction", messages)  # first live call passes (spend 0)
    assert client.month_spend_usd() > tiny["budget"]["monthly_usd_cap"]

    # same input again: CACHE hit — free, must NOT raise
    result = client.complete_json("extraction", messages)
    assert result.cache_hit

    # a different input needs a live call -> blocked
    with pytest.raises(LLMBudgetExceededError):
        client.complete_json("extraction", [{"role": "user", "content": "b"}])


def test_cap_requires_prices_for_configured_models(conn):
    cfg = load_llm_config()
    broken = {**cfg, "prices": {}, "budget": {"monthly_usd_cap": 5.0}}
    with pytest.raises(ValueError, match="prices"):
        LLMClient(conn, broken, transport=lambda *a: {}, api_key="k")


def test_pipeline_degrades_gracefully_without_burning_attempts(conn):
    """Budget exhaustion must not mark/bump filings — they wait for next month."""
    conn.execute("INSERT INTO instruments (id, ticker, name) VALUES (9, 'aaa', 'aaa')")
    fid = _insert_filing(conn, 1, "Kontrakt", "tresc")
    conn.execute("UPDATE filings SET instrument_id = 9")
    conn.commit()
    # pre-existing spend this month exceeds the cap
    tiny = {"budget": {"monthly_usd_cap": 0.0001}}
    client = _make_client(conn, lambda body: json.dumps(_research_payload()),
                          cfg_over=tiny)
    client.complete_json("extraction", [{"role": "user", "content": "warmup"}])

    with pytest.raises(LLMBudgetExceededError):
        pipeline.compute_feature_for_date(
            conn, client, instrument_id=9, ticker="aaa",
            as_of_date="2026-06-02", quant_score=None)
    row = conn.execute("SELECT processed, attempts FROM filings WHERE id = ?",
                       (fid,)).fetchone()
    assert row["processed"] == 0, "budget stop must not consume the filing"
    assert row["attempts"] == 0, "budget stop must not burn retry attempts"


def test_cmd_llm_records_degraded_run_and_alerts(tmp_path, monkeypatch, capsys):
    """CLI: cap hit mid-run -> exit 3, llm_runs degraded, dry-run alert."""
    from app import cli, config as cfg_mod
    from app.db import connect, init_db
    from app.ingestion import stooq

    db = str(tmp_path / "t.db")
    conn = connect(db)
    init_db(conn)
    iid = stooq.upsert_instrument(conn, {"ticker": "aaa", "name": "aaa"})
    _insert_filing(conn, 1, "Kontrakt", "tresc")
    conn.execute("UPDATE filings SET instrument_id = ?", (iid,))
    # pre-existing spend beyond the tiny cap
    conn.execute("INSERT INTO llm_costs (created_at, role, model, cost_usd)"
                 " VALUES (datetime('now'), 'extraction', 'm', 99.0)")
    conn.commit()
    conn.close()

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(cfg_mod, "load_universe",
                        lambda: {"benchmark": {"ticker": "wig20tr"},
                                 "instruments": [{"ticker": "aaa"}]})
    base_cfg = cfg_mod.load_llm_config()
    monkeypatch.setattr(cfg_mod, "load_llm_config",
                        lambda: {**base_cfg, "budget": {"monthly_usd_cap": 1.0}})

    rc = cli.main(["--db", db, "llm", "--date", "2026-06-02"])
    assert rc == 3
    out = capsys.readouterr().out
    assert "RUN DEGRADED" in out
    assert "Budzet LLM wyczerpany" in out  # Polish dry-run alert card

    conn = connect(db)
    run = conn.execute("SELECT * FROM llm_runs").fetchone()
    assert run["status"] == "degraded"
    assert "budget" in run["detail"]
    row = conn.execute("SELECT processed, attempts FROM filings").fetchone()
    assert row["processed"] == 0 and row["attempts"] == 0
    conn.close()

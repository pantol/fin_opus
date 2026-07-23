"""CLI `llm` subcommand tests — offline (injected transport, no network).

Covers: the OPENROUTER_API_KEY gate, end-to-end materialization of an
llm_features row from a collected filing via `python -m app.cli llm`, and the
no-op path when there is nothing to process. The backtest itself never calls
the LLM; this command is the only production entry point that does.
"""
from __future__ import annotations

from datetime import datetime, timezone

import app.cli as cli
from app import config as cfg
from app.db import connect, init_db
from app.ingestion import filings_db
from app.llm import client as llm_client_mod

from tests.test_llm_pipeline import _client, _research_json, _synthesis_json


def _universe():
    return {
        "benchmark": {"ticker": "wig20tr", "name": "WIG20TR", "is_index": True},
        "instruments": [{"ticker": "pko", "name": "PKO"}],
    }


def _build_db(path):
    conn = connect(path)
    init_db(conn)
    filings_db.ensure_schema(conn)
    iid = int(conn.execute(
        "INSERT INTO instruments (ticker, name, isin) VALUES ('pko','PKO','PLPKO0000016')"
    ).lastrowid)
    conn.commit()
    return conn, iid


def _insert_filing(conn, iid, published_at, dedup="c1"):
    filings_db.insert_filing(conn, {
        "source": "test", "issuer_isin": "PLPKO0000016", "issuer_name": "PKO",
        "instrument_id": iid, "espi_ebi_type": "ESPI", "report_number": "1/2024",
        "title": "The company signed a contract.", "published_at": published_at,
        "fetched_at": datetime.now(timezone.utc).isoformat(), "url": "http://x",
        "full_text": "The company signed a contract.",
        "content_hash": dedup, "dedup_key": dedup,
    })
    conn.commit()


def _add_archive_instrument(conn, isin, name):
    """An instrument the GPW archive discovered (ticker = lowercase ISIN),
    absent from config/universe.yaml — see app.ingestion.gpw_archive."""
    iid = int(conn.execute(
        "INSERT INTO instruments (ticker, name, isin) VALUES (?,?,?)",
        (isin.lower(), name, isin),
    ).lastrowid)
    conn.commit()
    return iid


def test_cli_llm_requires_api_key(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # cli.main() consumes ./.env; run from an empty cwd so a developer's real
    # .env (with a live key) cannot defeat the delenv above.
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["--db", str(tmp_path / "x.db"), "llm"])
    assert rc == 2
    assert "OPENROUTER_API_KEY" in capsys.readouterr().out


def test_cli_llm_materializes_from_filing(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(cfg, "load_universe", _universe)

    db_path = str(tmp_path / "llm.db")
    conn, iid = _build_db(db_path)
    _insert_filing(conn, iid, "2024-05-01T09:00:00+02:00")
    conn.close()  # the CLI reopens its own connection on the same file

    # Swap in an offline client (fake transport); cmd_llm resolves the class
    # from app.llm.client at call time.
    def offline_client(conn_, llm_cfg):
        return _client(conn_, _research_json(), _synthesis_json(conviction=0.8))

    monkeypatch.setattr(llm_client_mod, "LLMClient", offline_client)

    rc = cli.main(["--db", db_path, "llm", "--date", "2024-05-02"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "llm_score=+0.800" in out
    assert "Provider audit" in out  # reproducibility trail printed

    conn = connect(db_path)
    row = conn.execute(
        "SELECT llm_score, as_of_date FROM llm_features WHERE instrument_id=?", (iid,)
    ).fetchone()
    assert row["llm_score"] == 0.8
    assert row["as_of_date"] == "2024-05-02"
    # The consumed filing is retired only after the feature was persisted.
    assert conn.execute("SELECT processed FROM filings").fetchone()[0] == 1
    conn.close()


def test_cli_llm_covers_non_curated_instruments_from_db(tmp_path, capsys, monkeypatch):
    """universe.yaml lists only pko, but a filing the collector resolved (by
    ISIN) to an archive-discovered instrument must be materialized too —
    targets are DB-driven, the curated list is not the gate."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(cfg, "load_universe", _universe)

    db_path = str(tmp_path / "wide.db")
    conn, pko = _build_db(db_path)
    gpw = _add_archive_instrument(conn, "PLGPW0000017", "GPW")
    _insert_filing(conn, pko, "2024-05-01T09:00:00+02:00")
    _insert_filing(conn, gpw, "2024-05-01T10:00:00+02:00", dedup="g1")
    conn.close()

    def offline_client(conn_, llm_cfg):
        return _client(conn_, _research_json(), _synthesis_json(conviction=0.8))

    monkeypatch.setattr(llm_client_mod, "LLMClient", offline_client)

    rc = cli.main(["--db", db_path, "llm", "--date", "2024-05-02"])
    assert rc == 0
    assert "plgpw0000017" in capsys.readouterr().out

    conn = connect(db_path)
    scored = {r["instrument_id"] for r in conn.execute(
        "SELECT instrument_id FROM llm_features").fetchall()}
    assert scored == {pko, gpw}
    assert conn.execute(
        "SELECT COUNT(*) FROM filings WHERE processed = 0").fetchone()[0] == 0
    conn.close()


def test_cli_llm_caps_new_instruments_per_run(tmp_path, capsys, monkeypatch):
    """--max-new-instruments bounds NON-curated targets per run (oldest backlog
    first, deterministic); deferred filings stay unprocessed and untouched for
    the next run, and the cap is announced — never silent."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(cfg, "load_universe", _universe)  # curated: pko only

    db_path = str(tmp_path / "cap.db")
    conn, pko = _build_db(db_path)
    older = _add_archive_instrument(conn, "PLOLD0000011", "OLDCO")
    newer = _add_archive_instrument(conn, "PLNEW0000012", "NEWCO")
    _insert_filing(conn, pko, "2024-05-01T12:00:00+02:00")               # curated
    _insert_filing(conn, older, "2024-05-01T08:00:00+02:00", dedup="o1")  # oldest
    _insert_filing(conn, newer, "2024-05-01T09:00:00+02:00", dedup="n1")
    conn.close()

    def offline_client(conn_, llm_cfg):
        return _client(conn_, _research_json(), _synthesis_json())

    monkeypatch.setattr(llm_client_mod, "LLMClient", offline_client)

    rc = cli.main(["--db", db_path, "llm", "--date", "2024-05-02",
                   "--max-new-instruments", "1"])
    assert rc == 0
    assert "deferred" in capsys.readouterr().out

    conn = connect(db_path)
    scored = {r["instrument_id"] for r in conn.execute(
        "SELECT instrument_id FROM llm_features").fetchall()}
    assert scored == {pko, older}  # curated uncapped + the OLDEST non-curated
    row = conn.execute(
        "SELECT processed, attempts FROM filings WHERE dedup_key = 'n1'").fetchone()
    assert row["processed"] == 0 and row["attempts"] == 0  # waits, untouched
    conn.close()


def test_cli_llm_ticker_can_target_non_curated_instrument(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(cfg, "load_universe", _universe)

    db_path = str(tmp_path / "one.db")
    conn, pko = _build_db(db_path)
    gpw = _add_archive_instrument(conn, "PLGPW0000017", "GPW")
    _insert_filing(conn, pko, "2024-05-01T09:00:00+02:00")
    _insert_filing(conn, gpw, "2024-05-01T10:00:00+02:00", dedup="g1")
    conn.close()

    def offline_client(conn_, llm_cfg):
        return _client(conn_, _research_json(), _synthesis_json())

    monkeypatch.setattr(llm_client_mod, "LLMClient", offline_client)

    rc = cli.main(["--db", db_path, "llm", "--date", "2024-05-02",
                   "--ticker", "plgpw0000017"])
    assert rc == 0

    conn = connect(db_path)
    scored = {r["instrument_id"] for r in conn.execute(
        "SELECT instrument_id FROM llm_features").fetchall()}
    assert scored == {gpw}  # the restriction leaves pko's filing untouched
    assert conn.execute(
        "SELECT processed FROM filings WHERE dedup_key = 'c1'").fetchone()[0] == 0
    conn.close()

    # A ticker with no instruments row is an explicit error, not a silent no-op.
    rc = cli.main(["--db", db_path, "llm", "--date", "2024-05-02",
                   "--ticker", "nope"])
    assert rc == 1


def test_cli_llm_nothing_to_process(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(cfg, "load_universe", _universe)

    db_path = str(tmp_path / "empty.db")
    conn, _ = _build_db(db_path)
    conn.close()

    def offline_client(conn_, llm_cfg):
        return _client(conn_, _research_json(), _synthesis_json())

    monkeypatch.setattr(llm_client_mod, "LLMClient", offline_client)

    rc = cli.main(["--db", db_path, "llm", "--date", "2024-05-02"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Materialized 0 feature rows" in out

    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM llm_features").fetchone()[0] == 0
    conn.close()

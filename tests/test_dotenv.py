"""Tests for the shared zero-dependency .env loader (app.config.load_dotenv).

The loader must behave identically for every entrypoint. Historically only
`app.cli` loaded `.env`, so the collector (`python -m app.ingestion.collect_news`,
i.e. `make collect` and the documented cron/systemd units) never saw
HEALTHCHECK_URL_COLLECT and its dead-man's-switch ping silently never fired.
These tests pin the loader semantics and the wiring in both entrypoints.
"""
from __future__ import annotations

import os

from app import cli
from app import config as cfg
from app.ingestion import collect_news


def _write_env(tmp_path, content: str) -> str:
    path = tmp_path / ".env"
    path.write_text(content, encoding="utf-8")
    return str(path)


def test_load_dotenv_sets_keys_from_file(tmp_path):
    path = _write_env(tmp_path, "GPW_TEST_DOTENV_ALPHA=from-file\n")
    try:
        cfg.load_dotenv(path)
        assert os.environ["GPW_TEST_DOTENV_ALPHA"] == "from-file"
    finally:
        os.environ.pop("GPW_TEST_DOTENV_ALPHA", None)


def test_shell_export_wins_over_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("GPW_TEST_DOTENV_BETA", "from-shell")
    path = _write_env(tmp_path, "GPW_TEST_DOTENV_BETA=from-file\n")
    cfg.load_dotenv(path)
    assert os.environ["GPW_TEST_DOTENV_BETA"] == "from-shell"


def test_load_dotenv_parsing_rules(tmp_path):
    path = _write_env(
        tmp_path,
        "\n"
        "# a comment\n"
        "export GPW_TEST_DOTENV_EXPORTED=yes\n"
        'GPW_TEST_DOTENV_DQ="double quoted"\n'
        "GPW_TEST_DOTENV_SQ='single quoted'\n"
        "not-a-key-value-line\n"
        "GPW_TEST_DOTENV_EQ=a=b=c\n",
    )
    keys = ["GPW_TEST_DOTENV_EXPORTED", "GPW_TEST_DOTENV_DQ",
            "GPW_TEST_DOTENV_SQ", "GPW_TEST_DOTENV_EQ"]
    try:
        cfg.load_dotenv(path)
        assert os.environ["GPW_TEST_DOTENV_EXPORTED"] == "yes"
        assert os.environ["GPW_TEST_DOTENV_DQ"] == "double quoted"
        assert os.environ["GPW_TEST_DOTENV_SQ"] == "single quoted"
        assert os.environ["GPW_TEST_DOTENV_EQ"] == "a=b=c"
        assert "not-a-key-value-line" not in os.environ
    finally:
        for k in keys:
            os.environ.pop(k, None)


def test_load_dotenv_missing_file_is_noop(tmp_path):
    before = dict(os.environ)
    cfg.load_dotenv(str(tmp_path / "does-not-exist.env"))
    assert dict(os.environ) == before


def test_load_dotenv_invalid_utf8_is_noop_not_crash(tmp_path):
    """A .env that is not valid UTF-8 (e.g. Polish text saved as CP1250) must
    be skipped, not crash the entrypoint — under systemd Restart=always a
    raising loader would crash-loop the collector."""
    path = tmp_path / ".env"
    path.write_bytes("GPW_TEST_DOTENV_CP1250=wartość\n".encode("cp1250"))
    before = dict(os.environ)
    cfg.load_dotenv(str(path))
    assert dict(os.environ) == before


def test_load_dotenv_utf8_bom_does_not_mangle_first_key(tmp_path):
    """A UTF-8 BOM (Windows editors) must not turn the first key into
    '\\ufeffKEY', which would silently drop it."""
    path = tmp_path / ".env"
    path.write_bytes(b"\xef\xbb\xbfGPW_TEST_DOTENV_BOM=first\n")
    try:
        cfg.load_dotenv(str(path))
        assert os.environ["GPW_TEST_DOTENV_BOM"] == "first"
        assert "﻿GPW_TEST_DOTENV_BOM" not in os.environ
    finally:
        os.environ.pop("GPW_TEST_DOTENV_BOM", None)


def test_collector_main_loads_dotenv_before_running(tmp_path, monkeypatch):
    """`python -m app.ingestion.collect_news` from a directory with a .env must
    see HEALTHCHECK_URL_COLLECT by the time the cycle runs, or the dead-man's-
    switch ping never fires under the documented cron/systemd setup."""
    monkeypatch.delenv("HEALTHCHECK_URL_COLLECT", raising=False)
    monkeypatch.chdir(tmp_path)
    _write_env(tmp_path, "HEALTHCHECK_URL_COLLECT=https://hc.example/ping\n")

    seen: dict[str, str | None] = {}

    def fake_run_once():
        seen["url"] = os.environ.get("HEALTHCHECK_URL_COLLECT")
        return 0

    monkeypatch.setattr(collect_news, "run_once", fake_run_once)
    try:
        rc = collect_news.main([])
        assert rc == 0
        assert seen["url"] == "https://hc.example/ping"
    finally:
        os.environ.pop("HEALTHCHECK_URL_COLLECT", None)


def test_cli_main_loads_dotenv(monkeypatch):
    """app.cli must keep loading .env now that the loader lives in app.config."""
    called = []
    monkeypatch.setattr(cfg, "load_dotenv", lambda *a, **kw: called.append(True))
    try:
        # Invalid subcommand: argparse exits AFTER the dotenv call, before any
        # command handler can touch the DB or network.
        cli.main(["definitely-not-a-command"])
    except SystemExit:
        pass
    assert called == [True]

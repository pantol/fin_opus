"""Configuration loading helpers (YAML + paths + .env)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
DEFAULT_DB_PATH = DATA_DIR / "gpw.db"


def load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE pairs from a local `.env` into os.environ (shell env wins).

    Zero-dependency by design: the project keeps its dependency surface small.
    Only sets keys NOT already present in the environment, so an explicit shell
    export always overrides the file. Silently no-ops if `.env` is absent or
    not decodable as UTF-8 (it is optional and .gitignored; a broken file must
    never take down an entrypoint). Handles blank lines, `#` comments, an
    optional `export ` prefix, single/double-quoted values, and a UTF-8 BOM.

    Every long-running/cron entrypoint must call this (app.cli main,
    app.ingestion.collect_news main) so env-driven features like healthcheck
    pings work identically under `make`, cron, and systemd.
    """
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8-sig") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                key, sep, value = line.partition("=")
                if not sep:
                    continue
                key, value = key.strip(), value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                os.environ.setdefault(key, value)
    except (OSError, UnicodeDecodeError):
        return


def load_yaml(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_universe() -> dict[str, Any]:
    return load_yaml(CONFIG_DIR / "universe.yaml")


def load_backtest_config() -> dict[str, Any]:
    return load_yaml(CONFIG_DIR / "backtest.yaml")


def load_strategy(name: str) -> dict[str, Any]:
    return load_yaml(CONFIG_DIR / "strategies" / f"{name}.yaml")


def load_news_sources() -> dict[str, Any]:
    return load_yaml(CONFIG_DIR / "news_sources.yaml")


def load_index_membership() -> dict[str, Any]:
    return load_yaml(CONFIG_DIR / "index_membership.yaml")


def load_corporate_actions() -> dict[str, Any]:
    return load_yaml(CONFIG_DIR / "corporate_actions.yaml")


def load_data_quality() -> dict[str, Any]:
    return load_yaml(CONFIG_DIR / "data_quality.yaml")


def load_llm_config() -> dict[str, Any]:
    return load_yaml(CONFIG_DIR / "llm.yaml")


def load_intraday_config() -> dict[str, Any]:
    return load_yaml(CONFIG_DIR / "intraday.yaml")


def load_backup_config() -> dict[str, Any]:
    return load_yaml(CONFIG_DIR / "backup.yaml")


def load_schedule_config() -> dict[str, Any]:
    return load_yaml(CONFIG_DIR / "schedule.yaml")


def load_profiles_config() -> dict[str, Any]:
    return load_yaml(CONFIG_DIR / "profiles.yaml")

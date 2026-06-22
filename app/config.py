"""Configuration loading helpers (YAML + paths)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
DEFAULT_DB_PATH = DATA_DIR / "gpw.db"


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


def load_llm_config() -> dict[str, Any]:
    return load_yaml(CONFIG_DIR / "llm.yaml")

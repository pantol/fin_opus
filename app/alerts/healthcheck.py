"""Healthcheck pings (healthchecks.io-style dead-man's switch).

Fire-and-forget: a ping failure is logged and swallowed — monitoring must never
crash the thing it monitors. Silently skipped when the env var is unset.
"""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)


def ping(env_var: str) -> bool:
    """GET the URL stored in `env_var`. True only when a ping was sent OK."""
    url = os.environ.get(env_var)
    if not url:
        return False
    try:
        requests.get(url, timeout=10)
        return True
    except Exception as exc:  # noqa: BLE001 — never let monitoring break the app
        log.warning("healthcheck ping failed (%s): %s", env_var, exc)
        return False

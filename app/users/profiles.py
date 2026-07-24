"""Phase 5 — survey -> user profile -> deterministic gating. ZERO LLM.

The Polish survey maps, in pure code, to a risk-tolerance bucket
(config/profiles.yaml), which resolves to: the user's strategy YAML, a
risk_per_trade multiplier, a personal drawdown circuit-breaker, a position
cap and excluded sectors. `apply_profile` overlays those onto the strategy's
risk block UNDER hard caps that no survey answer can exceed. The applied
overlay changes the strategy config — and therefore the user's paper config
fingerprint — which is exactly right: a profile change mid-track-record is a
deliberate, acknowledged break.

The blueprint's "personalized prompt" seam is deliberately NOT built: the LLM
layer stays shared and cache-friendly (one prompt prefix for all users);
personalization lives entirely in this deterministic layer.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

TOLERANCES = ("conservative", "balanced", "aggressive")

# Polish survey. Each answer carries POINTS toward the tolerance score;
# some answers feed direct fields (max_drawdown from Q3, exclusions from Q5).
SURVEY = [
    {
        "id": "horizon",
        "question": "Jaki jest Twoj horyzont inwestycyjny?",
        "options": {"a": ("ponizej 1 roku", 0), "b": ("1-5 lat", 1),
                    "c": ("powyzej 5 lat", 2)},
    },
    {
        "id": "reaction",
        "question": "Twoj portfel traci 20% w miesiac. Co robisz?",
        "options": {"a": ("sprzedaje wszystko", 0), "b": ("czekam", 1),
                    "c": ("dokupuje", 2)},
    },
    {
        "id": "max_loss",
        "question": "Jakie maksymalne obsuniecie kapitalu akceptujesz?",
        "options": {"a": ("okolo 10%", 0), "b": ("okolo 25%", 1),
                    "c": ("okolo 40%", 2)},
    },
    {
        "id": "experience",
        "question": "Jakie masz doswiadczenie z rynkiem akcji?",
        "options": {"a": ("zadne", 0), "b": ("podstawowe", 1),
                    "c": ("aktywnie inwestuje", 2)},
    },
    {
        "id": "exclusions",
        "question": ("Ktore sektory wykluczyc? (lista po przecinku, np. "
                     "banking,energy; puste = zadne)"),
        "options": None,  # free-form list, no points
    },
]

_MAX_DD_BY_ANSWER = {"a": 0.10, "b": 0.25, "c": 0.40}


def score_answers(answers: dict) -> str:
    """Deterministic tolerance bucket from the scored questions.

    0-2 points -> conservative, 3-5 -> balanced, 6-8 -> aggressive.
    Unknown/missing answers score 0 (the cautious direction).
    """
    total = 0
    for q in SURVEY:
        if not q["options"]:
            continue
        choice = str(answers.get(q["id"], "")).lower()
        if choice in q["options"]:
            total += q["options"][choice][1]
    if total <= 2:
        return "conservative"
    if total <= 5:
        return "balanced"
    return "aggressive"


def build_profile(user_id: str, answers: dict, profiles_cfg: dict,
                  *, display_name: str | None = None) -> dict:
    """Survey answers -> full profile dict (pure function, ZERO LLM)."""
    tolerance = score_answers(answers)
    base = profiles_cfg["tolerances"][tolerance]
    exclusions = answers.get("exclusions") or ""
    if isinstance(exclusions, str):
        excluded = [s.strip() for s in exclusions.split(",") if s.strip()]
    else:
        excluded = [str(s) for s in exclusions]
    max_dd = _MAX_DD_BY_ANSWER.get(str(answers.get("max_loss", "")).lower(),
                                   float(base["max_drawdown_pct"]))
    return {
        "user_id": user_id,
        "display_name": display_name or user_id,
        "risk_tolerance": tolerance,
        "max_drawdown_pct": min(max_dd, float(base["max_drawdown_pct"])),
        "risk_multiplier": float(base["risk_multiplier"]),
        "max_positions": int(base["max_positions"]),
        "excluded_sectors": excluded,
        "strategy": str(base["strategy"]),
        "initial_capital": None,
        "survey": dict(answers),
    }


def save_profile(conn, profile: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO user_profiles (user_id, display_name, risk_tolerance, "
        " max_drawdown_pct, risk_multiplier, max_positions, excluded_sectors, "
        " strategy, initial_capital, survey_json, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (user_id) DO UPDATE SET display_name = excluded.display_name, "
        " risk_tolerance = excluded.risk_tolerance, "
        " max_drawdown_pct = excluded.max_drawdown_pct, "
        " risk_multiplier = excluded.risk_multiplier, "
        " max_positions = excluded.max_positions, "
        " excluded_sectors = excluded.excluded_sectors, "
        " strategy = excluded.strategy, initial_capital = excluded.initial_capital, "
        " survey_json = excluded.survey_json, updated_at = excluded.updated_at",
        (profile["user_id"], profile.get("display_name"),
         profile["risk_tolerance"], float(profile["max_drawdown_pct"]),
         float(profile["risk_multiplier"]), profile.get("max_positions"),
         json.dumps(profile.get("excluded_sectors") or []),
         profile["strategy"], profile.get("initial_capital"),
         json.dumps(profile.get("survey") or {}), now, now),
    )
    conn.commit()


def load_profile(conn, user_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM user_profiles WHERE user_id = ?",
                       (user_id,)).fetchone()
    if row is None:
        return None
    return {
        "user_id": row["user_id"], "display_name": row["display_name"],
        "risk_tolerance": row["risk_tolerance"],
        "max_drawdown_pct": float(row["max_drawdown_pct"]),
        "risk_multiplier": float(row["risk_multiplier"]),
        "max_positions": row["max_positions"],
        "excluded_sectors": json.loads(row["excluded_sectors"] or "[]"),
        "strategy": row["strategy"],
        "initial_capital": row["initial_capital"],
        "survey": json.loads(row["survey_json"] or "{}"),
    }


def apply_profile(strategy_cfg: dict, profile: dict, profiles_cfg: dict) -> dict:
    """Overlay a profile onto a strategy config's risk block, under hard caps.

    Returns a DEEP COPY — the shared strategy YAML is never mutated. Every
    derived value is bounded by config/profiles.yaml `hard_caps`, so no
    profile can loosen risk beyond them regardless of survey answers.
    """
    caps = profiles_cfg.get("hard_caps") or {}
    out = copy.deepcopy(strategy_cfg)
    risk = out["risk"]
    risk["risk_per_trade"] = min(
        float(risk["risk_per_trade"]) * float(profile["risk_multiplier"]),
        float(caps.get("risk_per_trade_max", 0.02)))
    risk["drawdown_circuit_breaker"] = min(
        float(risk["drawdown_circuit_breaker"]),
        float(profile["max_drawdown_pct"]),
        float(caps.get("drawdown_breaker_max", 0.40)))
    if profile.get("max_positions"):
        risk["max_open_positions"] = min(int(risk["max_open_positions"]),
                                         int(profile["max_positions"]))
    risk["max_total_exposure"] = min(float(risk["max_total_exposure"]),
                                     float(caps.get("max_total_exposure", 1.0)))
    # The overlay is data, and it must be VISIBLE data: stamp the profile into
    # the config so the paper fingerprint + logged params carry it.
    out["profile"] = {
        "user_id": profile["user_id"],
        "risk_tolerance": profile["risk_tolerance"],
        "risk_multiplier": float(profile["risk_multiplier"]),
        "max_drawdown_pct": float(profile["max_drawdown_pct"]),
        "excluded_sectors": sorted(profile.get("excluded_sectors") or []),
    }
    return out


def excluded_sectors(profile: dict | None) -> frozenset[str]:
    if not profile:
        return frozenset()
    return frozenset(profile.get("excluded_sectors") or [])

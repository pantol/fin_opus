"""YAML-driven rule strategy engine.

One engine evaluates ANY strategy config. It produces SIGNALS only
(ENTER / EXIT / HOLD); it never sizes positions or computes money — that is the
risk layer's job. Deterministic: same features + config -> same signal.

Config grammar (see config/strategies/*.yaml):
    entry:
      all:  [<condition>, ...]   # every condition must hold
      any:  [<condition>, ...]   # at least one holds
    exit:
      any:  [<condition>, ...]
      all:  [<condition>, ...]

<condition> is either:
    {feature: <name>, op: gt|lt|gte|lte|eq, value: <number>}
or an exit-only special:
    {type: atr_stop, atr_mult: <number>}   # handled by risk/backtest, see notes
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

_OPS = {
    "gt": lambda a, b: a > b,
    "lt": lambda a, b: a < b,
    "gte": lambda a, b: a >= b,
    "lte": lambda a, b: a <= b,
    "eq": lambda a, b: a == b,
}


class Signal(str, Enum):
    ENTER = "ENTER"
    EXIT = "EXIT"
    HOLD = "HOLD"


@dataclass(frozen=True)
class EvalContext:
    """Inputs available to exit-rule evaluation beyond the feature snapshot."""
    in_position: bool = False
    entry_price: float | None = None
    stop_price: float | None = None   # current (possibly trailing) ATR stop level
    last_close: float | None = None


def _eval_feature_condition(cond: dict, features: dict) -> bool:
    name = cond["feature"]
    op = cond["op"]
    if op not in _OPS:
        raise ValueError(f"Unknown operator: {op}")
    val = features.get(name)
    if val is None:
        return False  # undefined feature -> condition fails (no guessing)
    return _OPS[op](float(val), float(cond["value"]))


def _eval_exit_special(cond: dict, ctx: EvalContext) -> bool:
    ctype = cond.get("type")
    if ctype == "atr_stop":
        # The trailing ATR stop level is maintained by the backtest/risk layer.
        # Here we simply check whether price has breached it.
        if ctx.stop_price is None or ctx.last_close is None:
            return False
        return ctx.last_close <= ctx.stop_price
    raise ValueError(f"Unknown exit condition type: {ctype}")


def _eval_clause(clause: dict, features: dict, ctx: EvalContext) -> bool:
    """Evaluate an all/any clause of conditions."""
    if "all" in clause:
        conds = clause["all"]
        return all(_eval_single(c, features, ctx) for c in conds)
    if "any" in clause:
        conds = clause["any"]
        return any(_eval_single(c, features, ctx) for c in conds)
    raise ValueError("Clause must contain 'all' or 'any'")


def _eval_single(cond: dict, features: dict, ctx: EvalContext) -> bool:
    if "type" in cond:
        return _eval_exit_special(cond, ctx)
    return _eval_feature_condition(cond, features)


def evaluate(config: dict, features: dict, ctx: EvalContext) -> Signal:
    """Return the signal for one instrument on one decision date.

    - When flat: evaluate `entry`; ENTER if it fires, else HOLD.
    - When in a position: evaluate `exit`; EXIT if it fires, else HOLD.
    """
    if features is None:
        return Signal.HOLD

    if ctx.in_position:
        exit_clause = config.get("exit")
        if exit_clause and _eval_clause(exit_clause, features, ctx):
            return Signal.EXIT
        return Signal.HOLD

    entry_clause = config.get("entry")
    if entry_clause and _eval_clause(entry_clause, features, ctx):
        return Signal.ENTER
    return Signal.HOLD


def strip_llm_conditions(config: dict) -> dict | None:
    """Copy of `config` whose entry clause drops every `llm_*` feature condition.

    Display-layer helper (LLM radar): re-evaluating a flat candidate against
    the stripped rules tells whether the LLM condition alone flipped its entry.
    Returns None when there is no entry clause or stripping would leave it
    empty — an empty all/any clause would pass vacuously, and the radar must
    never "guess" that a name was LLM-blocked.
    """
    entry = config.get("entry")
    if not isinstance(entry, dict):
        return None
    key = "all" if "all" in entry else "any" if "any" in entry else None
    if key is None:
        return None
    kept = [c for c in entry[key]
            if not str(c.get("feature", "")).startswith("llm_")]
    if not kept:
        return None
    return {**config, "entry": {key: kept}}

"""Anti-luck validation: trials registry + Deflated Sharpe Ratio (DSR).

Every backtested strategy/parameter set is a TRIAL. The more trials you run,
the higher the best Sharpe you will find by pure chance — so the registry
records every run, and the DSR (Bailey & Lopez de Prado, 2014) reports the
probability that the observed Sharpe beats the expected maximum Sharpe of
`n_trials` skill-less strategies. Raw Sharpe without this context is not
evidence.

Pure deterministic code, no external stats dependency: the normal CDF uses
math.erf and the inverse CDF uses Acklam's rational approximation.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

EULER_GAMMA = 0.5772156649015329


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# Acklam's inverse-normal-CDF rational approximation (|relative error| < 1.15e-9).
_A = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
      1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
_B = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
      6.680131188771972e+01, -1.328068155288572e+01)
_C = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
      -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
_D = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
      3.754408661907416e+00)


def norm_ppf(p: float) -> float:
    """Inverse standard normal CDF."""
    if not 0.0 < p < 1.0:
        raise ValueError(f"norm_ppf domain is (0, 1), got {p}")
    p_low, p_high = 0.02425, 1.0 - 0.02425
    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        return ((((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5])
                / ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0))
    if p > p_high:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -((((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5])
                 / ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0))
    q = p - 0.5
    r = q * q
    return ((((((_A[0] * r + _A[1]) * r + _A[2]) * r + _A[3]) * r + _A[4]) * r + _A[5]) * q
            / (((((_B[0] * r + _B[1]) * r + _B[2]) * r + _B[3]) * r + _B[4]) * r + 1.0))


def per_period_sharpe(equity: pd.Series) -> float:
    """Non-annualized Sharpe of period returns (the unit DSR math works in)."""
    returns = equity.pct_change().dropna()
    if len(returns) < 2:
        return 0.0
    std = float(returns.std(ddof=0))
    if std == 0:
        return 0.0
    return float(returns.mean()) / std


def expected_max_sharpe(n_trials: int, var_trial_sharpes: float) -> float:
    """E[max per-period Sharpe] of n skill-less trials (the DSR benchmark SR*).

    With a single trial (or no variance across trials) there is nothing to
    deflate: SR* = 0 and the DSR reduces to the plain PSR against zero.
    """
    if n_trials <= 1 or var_trial_sharpes <= 0:
        return 0.0
    sd = math.sqrt(var_trial_sharpes)
    return sd * ((1.0 - EULER_GAMMA) * norm_ppf(1.0 - 1.0 / n_trials)
                 + EULER_GAMMA * norm_ppf(1.0 - 1.0 / (n_trials * math.e)))


def probabilistic_sharpe(sharpe_pp: float, sr_star: float, n_obs: int,
                         skew: float, kurt_raw: float) -> float:
    """PSR: probability that the true per-period Sharpe exceeds sr_star."""
    if n_obs < 2:
        return 0.0
    denom = 1.0 - skew * sharpe_pp + (kurt_raw - 1.0) / 4.0 * sharpe_pp ** 2
    denom = math.sqrt(max(denom, 1e-12))
    z = (sharpe_pp - sr_star) * math.sqrt(n_obs - 1.0) / denom
    return norm_cdf(z)


@dataclass
class DSRResult:
    sharpe_pp: float
    sr_star: float
    dsr: float
    n_trials: int
    n_obs: int

    def as_dict(self) -> dict:
        return {"sharpe_pp": self.sharpe_pp, "sr_star": self.sr_star,
                "dsr": self.dsr, "n_trials": self.n_trials, "n_obs": self.n_obs}


def deflated_sharpe(equity: pd.Series, n_trials: int,
                    var_trial_sharpes: float) -> DSRResult:
    """DSR of a strategy equity curve given the trials-registry statistics."""
    returns = equity.pct_change().dropna()
    n_obs = len(returns)
    sharpe_pp = per_period_sharpe(equity)
    skew = float(returns.skew()) if n_obs > 2 else 0.0
    kurt_raw = float(returns.kurt()) + 3.0 if n_obs > 3 else 3.0  # pandas kurt is excess
    if math.isnan(skew):
        skew = 0.0
    if math.isnan(kurt_raw):
        kurt_raw = 3.0
    sr_star = expected_max_sharpe(n_trials, var_trial_sharpes)
    dsr = probabilistic_sharpe(sharpe_pp, sr_star, n_obs, skew, kurt_raw)
    return DSRResult(sharpe_pp=sharpe_pp, sr_star=sr_star, dsr=dsr,
                     n_trials=max(1, n_trials), n_obs=n_obs)


# --- trials registry -----------------------------------------------------------

def config_hash(strategy_cfg: dict, bt_cfg: dict) -> str:
    """Content hash identifying one trial: the strategy AND the backtest knobs
    that shape results (costs, windows, execution, universe gate)."""
    payload = {
        "strategy": strategy_cfg,
        "costs": bt_cfg.get("costs"),
        "walk_forward": bt_cfg.get("walk_forward"),
        "execution": bt_cfg.get("execution"),
        "universe": bt_cfg.get("universe"),
        "initial_capital": bt_cfg.get("initial_capital"),
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def log_trial(conn, *, cfg_hash: str, strategy_name: str, strategy_version: int,
              oos_start: str | None, oos_end: str | None, metrics: dict) -> None:
    conn.execute(
        "INSERT INTO strategy_trials (config_hash, strategy_name, strategy_version,"
        " run_at, oos_start, oos_end, metrics_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (cfg_hash, strategy_name, int(strategy_version),
         datetime.now(timezone.utc).isoformat(), oos_start, oos_end,
         json.dumps(metrics, sort_keys=True, default=str)),
    )
    conn.commit()


def trial_stats(conn) -> tuple[int, float]:
    """(n_trials, variance of per-period Sharpes) across DISTINCT configs.

    Re-running the same config is the same trial (its latest metrics win);
    only genuinely different strategy/parameter sets inflate the deflation.
    """
    rows = conn.execute(
        "SELECT config_hash, metrics_json FROM strategy_trials ORDER BY id"
    ).fetchall()
    latest: dict[str, dict] = {}
    for r in rows:
        try:
            latest[r["config_hash"]] = json.loads(r["metrics_json"])
        except (TypeError, ValueError):
            continue
    sharpes = [float(m["sharpe_pp"]) for m in latest.values()
               if isinstance(m.get("sharpe_pp"), (int, float))]
    n_trials = len(latest)
    if len(sharpes) < 2:
        return n_trials, 0.0
    mean = sum(sharpes) / len(sharpes)
    var = sum((s - mean) ** 2 for s in sharpes) / len(sharpes)
    return n_trials, var

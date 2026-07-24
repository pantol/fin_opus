"""Market regime radar (Phase 3) — deterministic composite risk score.

Every component is computed by THIS code from point-in-time data (bars with
date <= T; LLM verdicts materialized for as_of_date <= T). The LLM never
appears here as a caller — only its stored per-instrument verdicts enter one
component, decayed by age so a stale verdict fades to neutral instead of
steering the regime forever. ZERO LLM in the money path (CLAUDE.md rule 1):
the score becomes plain `market_*` features that strategy YAML rules gate on,
exactly like `llm_score`.

Components (each mapped into [-1, +1], weighted-averaged into
`market_risk_score`):
  trend     — benchmark close vs its SMA(trend_window), scaled by trend_scale
  breadth   — fraction of the tradable universe above its own SMA(trend_window)
  vol       — benchmark realized-vol percentile within a trailing window
              (high vol = risk-off)
  drawdown  — benchmark drawdown from its running peak, scaled by dd_scale
  llm       — age-decayed mean of materialized llm_score verdicts

`market_risk_on` (0/1) applies hysteresis (risk_on_above / risk_off_below) so
the state cannot flap on a score hovering at one threshold. Strategies gate
entries on these features; exits always evaluate (the engine only consults
entry rules for flat names).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

DEFAULTS = {
    "trend_window": 200,
    "trend_scale": 0.10,
    "vol_window": 20,
    "vol_rank_window": 252,
    "dd_scale": 0.20,
    "breadth": {"stale_limit_sessions": 5, "min_names": 10},
    "llm": {"half_life_sessions": 10, "max_age_sessions": 30, "min_count": 3},
    "weights": {"trend": 0.30, "breadth": 0.25, "vol": 0.15,
                "drawdown": 0.15, "llm": 0.15},
    "state": {"risk_on_above": 0.10, "risk_off_below": -0.10},
    "radar": {"enabled": True,
              "false_alarm": {"dd_threshold": 0.05, "horizon_sessions": 40}},
}


def regime_config(bt_cfg: dict) -> dict:
    """The `regime:` block of backtest.yaml over DEFAULTS (shallow per key).

    Living in backtest.yaml puts the regime model under the paper loop's
    config fingerprint: changing it mid-track-record demands an explicit
    --accept-config-change, like any other decision-relevant knob.
    """
    raw = (bt_cfg.get("regime") or {})
    out = {}
    for key, default in DEFAULTS.items():
        if isinstance(default, dict):
            merged = dict(default)
            merged.update(raw.get(key) or {})
            out[key] = merged
        else:
            out[key] = raw.get(key, default)
    return out


def needs_llm(bt_cfg: dict) -> bool:
    """True when the llm component carries weight — call sites must then
    attach materialized llm_scores BEFORE computing market features, or the
    component silently reads neutral (that silence is exactly what this
    helper exists to prevent)."""
    return float(regime_config(bt_cfg)["weights"].get("llm", 0.0)) > 0.0


def _clip01(x):
    return np.clip(x, -1.0, 1.0)


def _llm_component(calendar: pd.DatetimeIndex, instruments, llm_cfg: dict) -> np.ndarray:
    """Age-decayed mean of per-instrument verdicts; 0.0 (neutral) with none.

    A verdict materialized for as_of_date J is active from the first session
    >= J, decays with a half-life in SESSIONS, and expires entirely after
    max_age_sessions — "a verdict must age out": no news means the market
    view drifts back to neutral instead of freezing on the last headline.
    """
    half_life = float(llm_cfg["half_life_sessions"])
    max_age = int(llm_cfg["max_age_sessions"])
    min_count = max(1, int(llm_cfg["min_count"]))
    n = len(calendar)
    total = np.zeros(n)
    count = np.zeros(n)
    cal_vals = calendar.values
    for inst in instruments:
        scores = getattr(inst, "llm_scores", None)
        if scores is None or scores.empty:
            continue
        scores = scores.sort_index()
        # Position of each calendar day's LAST verdict (<= that day).
        vpos = np.searchsorted(scores.index.values, cal_vals, side="right") - 1
        has = vpos >= 0
        if not has.any():
            continue
        # Session-age of that verdict = day position - position of the first
        # session >= its as_of_date (verdicts between sessions age from the
        # next session).
        vdates = scores.index.values[np.clip(vpos, 0, None)]
        vday = np.searchsorted(cal_vals, vdates, side="left")
        age = np.arange(n) - vday
        active = has & (age >= 0) & (age <= max_age)
        decayed = scores.to_numpy()[np.clip(vpos, 0, None)] * np.power(0.5, age / half_life)
        total[active] += decayed[active]
        count[active] += 1
    denom = np.maximum(count, min_count)
    out = total / denom
    out[count == 0] = 0.0
    return _clip01(out)


def _breadth_component(calendar: pd.DatetimeIndex, instruments, cfg: dict) -> np.ndarray:
    window = int(cfg["trend_window"])
    stale_limit = int(cfg["breadth"]["stale_limit_sessions"])
    min_names = int(cfg["breadth"]["min_names"])
    closes = {}
    for inst in instruments:
        s = inst.prices["close"].dropna()
        if len(s):
            closes[inst.ticker] = s
    if not closes:
        return np.zeros(len(calendar))
    # Reindex on the SESSION calendar with a short forward-fill: a name that
    # stopped printing bars (suspension/delisting) drops out of the breadth
    # denominator after stale_limit sessions instead of freezing it forever.
    panel = pd.DataFrame(closes).reindex(calendar).ffill(limit=stale_limit)
    sma = panel.rolling(window, min_periods=window).mean()
    valid = panel.notna() & sma.notna()
    above = (panel > sma) & valid
    denom = valid.sum(axis=1).to_numpy().astype(float)
    breadth = np.divide(above.sum(axis=1).to_numpy(), denom,
                        out=np.full(len(calendar), 0.5), where=denom > 0)
    breadth = np.where(denom >= min_names, breadth, 0.5)  # too thin = neutral
    return _clip01(2.0 * breadth - 1.0)


def compute_market_features(instruments, benchmark_close: pd.Series,
                            bt_cfg: dict) -> pd.DataFrame:
    """Point-in-time market-regime feature frame on the benchmark calendar.

    Every row at date T uses ONLY data with date <= T (rolling windows,
    running peak, as-of verdict lookups) — verified by the no-look-ahead test.
    Rows before the benchmark SMA warms up are dropped: an unknown regime is
    an ABSENT feature, and an absent feature fails a YAML entry condition
    closed (same posture as a missing llm_score).
    """
    cfg = regime_config(bt_cfg)
    bench = benchmark_close.dropna().sort_index().astype(float)
    calendar = bench.index
    n = len(calendar)
    if n == 0:
        return pd.DataFrame()

    sma = bench.rolling(int(cfg["trend_window"]),
                        min_periods=int(cfg["trend_window"])).mean()
    c_trend = _clip01((bench / sma - 1.0).to_numpy() / float(cfg["trend_scale"]))

    ret = bench.pct_change()
    vol = ret.rolling(int(cfg["vol_window"]),
                      min_periods=int(cfg["vol_window"])).std()
    vol_pct = vol.rolling(int(cfg["vol_rank_window"]),
                          min_periods=int(cfg["vol_rank_window"])).rank(pct=True)
    c_vol = _clip01(1.0 - 2.0 * vol_pct.to_numpy())

    dd = (bench / bench.cummax() - 1.0).to_numpy()
    c_dd = _clip01(1.0 + 2.0 * dd / float(cfg["dd_scale"]))

    c_breadth = _breadth_component(calendar, instruments, cfg)
    c_llm = _llm_component(calendar, instruments, cfg["llm"])

    w = {k: float(v) for k, v in cfg["weights"].items()}
    total_w = sum(w.values())
    if total_w <= 0:
        raise ValueError("regime.weights must sum to a positive number")
    score = (w.get("trend", 0) * c_trend
             + w.get("breadth", 0) * c_breadth
             + w.get("vol", 0) * np.nan_to_num(c_vol, nan=0.0)
             + w.get("drawdown", 0) * c_dd
             + w.get("llm", 0) * c_llm) / total_w
    # Undefined while the trend anchor warms up — drop those rows entirely.
    score = np.where(np.isnan(c_trend), np.nan, score)

    state = _hysteresis_state(score, cfg["state"])
    df = pd.DataFrame({
        "market_risk_score": np.round(score, 6),
        "market_risk_on": state,
        "market_trend": np.round(c_trend, 6),
        "market_breadth": np.round(c_breadth, 6),
        "market_vol": np.round(np.nan_to_num(c_vol, nan=0.0), 6),
        "market_drawdown": np.round(c_dd, 6),
        "market_llm": np.round(c_llm, 6),
    }, index=calendar)
    return df[~np.isnan(score)]


def _hysteresis_state(score: np.ndarray, state_cfg: dict) -> np.ndarray:
    """1.0 risk-on / 0.0 risk-off with hysteresis; NaN scores keep prior state."""
    on_above = float(state_cfg["risk_on_above"])
    off_below = float(state_cfg["risk_off_below"])
    if off_below > on_above:
        raise ValueError("regime.state: risk_off_below must be <= risk_on_above")
    out = np.empty(len(score))
    cur = 1.0  # start risk-on: entering an unknown-but-not-negative market is
    #            the baseline behavior; the first defined score corrects it
    for i, s in enumerate(score):
        if not np.isnan(s):
            if s < off_below:
                cur = 0.0
            elif s > on_above:
                cur = 1.0
        out[i] = cur
    return out


def frame_asof(df: pd.DataFrame | None, day) -> dict | None:
    """Row as-of `day` (last row with index <= day) as a plain dict."""
    if df is None or df.empty:
        return None
    idx = df.index.searchsorted(day, side="right") - 1
    if idx < 0:
        return None
    return {k: float(v) for k, v in df.iloc[idx].items()}


# --- turning-point / false-alarm REPORT (retrospective display, not a feature) --

def switches(features: pd.DataFrame) -> list[dict]:
    """Every risk-state flip: {date, to_state, score}. Deterministic."""
    if features is None or features.empty:
        return []
    state = features["market_risk_on"].to_numpy()
    dates = features.index
    out = []
    for i in range(1, len(state)):
        if state[i] != state[i - 1]:
            out.append({"date": dates[i].date().isoformat(),
                        "to_state": "risk_off" if state[i] == 0.0 else "risk_on",
                        "score": float(features["market_risk_score"].iloc[i])})
    return out


def false_alarm_report(features: pd.DataFrame, benchmark_close: pd.Series,
                       bt_cfg: dict) -> dict:
    """Judge every risk-OFF switch against what the benchmark did NEXT.

    Uses FUTURE data relative to each switch on purpose — this is a
    retrospective scoreboard of the radar (the phase gate asks for a
    false-alarm report), never an input to any decision. A switch is
    justified when the benchmark fell at least dd_threshold within
    horizon_sessions after it; otherwise it is a false alarm. Switches whose
    horizon has not fully elapsed are reported as pending, not judged.
    """
    fa_cfg = regime_config(bt_cfg)["radar"]["false_alarm"]
    dd_thr = float(fa_cfg["dd_threshold"])
    horizon = int(fa_cfg["horizon_sessions"])
    bench = benchmark_close.dropna().sort_index().astype(float)
    judged, pending = [], []
    for sw in switches(features):
        if sw["to_state"] != "risk_off":
            continue
        pos = bench.index.searchsorted(pd.Timestamp(sw["date"]))
        fwd = bench.iloc[pos + 1: pos + 1 + horizon]
        entry = {**sw}
        if len(fwd) < horizon:
            pending.append(entry)
            continue
        realized = float(fwd.min() / bench.iloc[pos] - 1.0)
        entry["fwd_min_return"] = round(realized, 4)
        entry["false_alarm"] = realized > -dd_thr
        judged.append(entry)
    n_false = sum(1 for e in judged if e["false_alarm"])
    return {
        "risk_off_switches": judged, "pending": pending,
        "n_judged": len(judged), "n_false": n_false,
        "false_alarm_rate": round(n_false / len(judged), 4) if judged else None,
        "dd_threshold": dd_thr, "horizon_sessions": horizon,
    }

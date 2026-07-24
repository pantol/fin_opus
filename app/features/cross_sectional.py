"""Cross-sectional (percentile-rank) features for the strategy library.

A cross-sectional feature at date T ranks every instrument's own
point-in-time feature value against the Rest of the universe ON THAT DAY —
e.g. `mom_12_1_pct` = the percentile of an instrument's 12-1 momentum among
all instruments with a defined value at T. Ranks are computed per day over
per-instrument values that are themselves functions of rows <= T, so the
panel stays free of look-ahead by construction. Dead tickers participate
while alive (anti-survivorship: the 2016 momentum ranking includes the names
that later died).

Deterministic code only; attached (like llm_scores) ONLY for strategies that
reference these features, so every other strategy's snapshots stay
byte-identical.
"""
from __future__ import annotations

import pandas as pd

# xs feature name -> the per-instrument source column it ranks.
# Percentiles are ascending: pct 1.0 = the highest source value that day.
XS_FEATURES = {
    "mom_12_1_pct": "mom_12_1",
    "vol_xs_pct": "realized_vol",
}


def strategy_uses_cross_sectional(strategy_cfg: dict) -> bool:
    """True if any condition or ranking key references an XS_FEATURES name."""
    def _walk(node) -> bool:
        if isinstance(node, dict):
            if node.get("feature") in XS_FEATURES:
                return True
            return any(_walk(v) for v in node.values())
        if isinstance(node, list):
            return any(_walk(v) for v in node)
        return False

    return (_walk(strategy_cfg.get("entry")) or _walk(strategy_cfg.get("exit"))
            or _walk(strategy_cfg.get("entry_ranking")))


def cross_sectional_panels(instruments) -> dict[str, pd.DataFrame]:
    """{xs_name: percentile panel (rows = dates, cols = tickers)}.

    Per row, rank(pct=True) over the instruments with a DEFINED source value
    that day; NaN sources stay NaN (a young listing has no momentum rank —
    missing fails entry conditions closed, per the engine contract).
    """
    out: dict[str, pd.DataFrame] = {}
    for xs_name, source in XS_FEATURES.items():
        cols = {}
        for inst in instruments:
            feats = inst.features
            if feats is not None and source in feats.columns:
                cols[inst.ticker] = feats[source]
        if not cols:
            continue
        panel = pd.DataFrame(cols)
        out[xs_name] = panel.rank(axis=1, pct=True)
    return out


def attach_cross_sectional(instruments):
    """Copies of `instruments` whose feature frames carry the XS columns.

    The engine's feature views are built AFTER attachment, so xs features
    flow through snapshots/ranking exactly like native per-instrument ones.
    """
    panels = cross_sectional_panels(instruments)
    if not panels:
        return instruments
    out = []
    for inst in instruments:
        feats = inst.features.copy()
        for xs_name, panel in panels.items():
            if inst.ticker in panel.columns:
                feats[xs_name] = panel[inst.ticker].reindex(feats.index)
        out.append(_clone_with_features(inst, feats))
    return out


def _clone_with_features(inst, feats):
    cls = type(inst)
    return cls(
        instrument_id=inst.instrument_id, ticker=inst.ticker,
        sector=inst.sector, listed_from=inst.listed_from,
        delisted_on=inst.delisted_on, prices=inst.prices, features=feats,
        llm_scores=inst.llm_scores, llm_relevance=inst.llm_relevance,
        actions=inst.actions,
    )

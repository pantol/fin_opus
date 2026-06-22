"""A/B harness: baseline (no LLM) vs baseline + LLM features, same OOS window.

Both variants run the SAME deterministic engine, SAME costs, SAME walk-forward
OOS span, SAME WIG20TR benchmark. The ONLY difference is:
  * Variant A uses a baseline strategy config (no `llm_score` condition).
  * Variant B uses an LLM strategy config AND its instruments carry point-in-time
    `llm_scores` (materialized earlier from filings, read deterministically).

The report compares risk-adjusted OOS metrics. The GATE (CLAUDE.md / the
point-in-time skill): the LLM variant must show a measurable, consistent OOS
improvement; otherwise report it honestly -- do not fake it.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass

from app.backtest import engine


@dataclass
class ABReport:
    baseline: dict          # baseline OOS metrics
    llm: dict               # LLM-variant OOS metrics
    benchmark: dict         # WIG20TR metrics
    deltas: dict            # llm - baseline for key metrics
    improved: bool          # did the LLM variant improve on the gate metrics?

    def as_text(self) -> str:
        keys = ["cagr", "sharpe", "sortino", "max_drawdown", "calmar",
                "total_return", "n_trades"]
        w = 16
        lines = [
            "A/B backtest (out-of-sample, vs WIG20TR)",
            "-" * 60,
            f"{'metric':<14}{'baseline':>{w}}{'baseline+LLM':>{w}}{'delta':>{w}}",
        ]
        for k in keys:
            b = self.baseline.get(k, 0.0)
            l = self.llm.get(k, 0.0)
            d = self.deltas.get(k, l - b if isinstance(l, (int, float)) else 0.0)
            lines.append(f"{k:<14}{b:>{w}.4f}{l:>{w}.4f}{d:>{w}.4f}")
        lines.append("-" * 60)
        lines.append(f"benchmark cagr={self.benchmark.get('cagr', 0.0):.4f} "
                     f"sharpe={self.benchmark.get('sharpe', 0.0):.4f}")
        verdict = ("LLM variant IMPROVED risk-adjusted OOS results."
                   if self.improved else
                   "LLM variant did NOT consistently improve OOS results "
                   "(reported honestly; do not deploy as-is).")
        lines.append(verdict)
        return "\n".join(lines)


# Metrics where the LLM variant must not be worse, and the primary gate metric.
GATE_PRIMARY = "sharpe"
GATE_SECONDARY = "sortino"


def _evaluate_gate(baseline: dict, llm: dict) -> tuple[bool, dict]:
    deltas = {}
    for k in ("cagr", "sharpe", "sortino", "max_drawdown", "calmar",
              "total_return", "n_trades"):
        b, l = baseline.get(k, 0.0), llm.get(k, 0.0)
        deltas[k] = l - b
    # Honest gate: primary risk-adjusted metric strictly better AND the secondary
    # one not worse. max_drawdown is negative; "not worse" means >= baseline.
    improved = (
        deltas[GATE_PRIMARY] > 0
        and deltas[GATE_SECONDARY] >= 0
        and deltas["max_drawdown"] >= 0
    )
    return improved, deltas


def run_ab(
    conn,
    universe: dict,
    baseline_cfg: dict,
    llm_cfg: dict,
    bt_cfg: dict,
    *,
    benchmark_ticker: str | None = None,
) -> ABReport:
    """Run baseline vs baseline+LLM walk-forward OOS and build a comparison report."""
    bench_ticker = benchmark_ticker or universe["benchmark"]["ticker"]
    instruments, bench_close = engine.load_instruments(conn, universe, bench_ticker)

    base_res = engine.run_walk_forward(
        instruments, bench_close, copy.deepcopy(baseline_cfg), bt_cfg
    )
    llm_instruments = engine.attach_llm_scores(conn, instruments)
    llm_res = engine.run_walk_forward(
        llm_instruments, bench_close, copy.deepcopy(llm_cfg), bt_cfg
    )

    improved, deltas = _evaluate_gate(base_res.metrics, llm_res.metrics)
    return ABReport(
        baseline=base_res.metrics,
        llm=llm_res.metrics,
        benchmark=base_res.benchmark_metrics,
        deltas=deltas,
        improved=improved,
    )

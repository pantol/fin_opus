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
    improved: bool          # final acceptance verdict (deltas AND anti-luck gates)
    # Anti-luck context for the LLM candidate (filled by apply_validation_gates).
    dsr: float | None = None
    random_percentile: float | None = None
    gate_notes: list[str] = None  # type: ignore[assignment]
    # Full simulation results, for validation by the caller (not rendered).
    baseline_result: object | None = None
    llm_result: object | None = None

    def __post_init__(self):
        if self.gate_notes is None:
            self.gate_notes = []

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
        if self.dsr is not None:
            lines.append(f"LLM variant DSR = {self.dsr:.4f}")
        if self.random_percentile is not None:
            lines.append(f"LLM variant sharpe percentile vs cost-matched randomness = "
                         f"{self.random_percentile:.2f}")
        lines.extend(self.gate_notes)
        verdict = ("LLM variant IMPROVED risk-adjusted OOS results AND cleared "
                   "the anti-luck gates."
                   if self.improved else
                   "LLM variant did NOT clear the acceptance gates "
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
    membership: dict[int, list[tuple]] | None = None,
) -> ABReport:
    """Run baseline vs baseline+LLM walk-forward OOS and build a comparison report.

    `membership` must match what `make backtest` uses (same point-in-time
    universe gate), otherwise the A/B verdict describes a different universe
    than the production backtest.
    """
    bench_ticker = benchmark_ticker or universe["benchmark"]["ticker"]
    instruments, bench_close = engine.load_instruments(
        conn, universe, bench_ticker, mode=engine.universe_mode(bt_cfg))

    # Attach materialized LLM features per arm, by what each config actually
    # references (gate OR entry_ranking): since v2 the baseline ranks entries
    # by llm_score too, and starving it of scores would make the A/B compare
    # "gate + different ranking inputs" instead of the gate alone.
    base_instruments = (engine.attach_llm_scores(conn, instruments)
                        if engine.needs_llm_attach(baseline_cfg, bt_cfg)
                        else instruments)
    base_res = engine.run_walk_forward(
        base_instruments, bench_close, copy.deepcopy(baseline_cfg), bt_cfg,
        membership=membership,
    )
    llm_instruments = (engine.attach_llm_scores(conn, instruments)
                       if engine.needs_llm_attach(llm_cfg, bt_cfg)
                       else instruments)
    llm_res = engine.run_walk_forward(
        llm_instruments, bench_close, copy.deepcopy(llm_cfg), bt_cfg,
        membership=membership,
    )

    improved, deltas = _evaluate_gate(base_res.metrics, llm_res.metrics)
    return ABReport(
        baseline=base_res.metrics,
        llm=llm_res.metrics,
        benchmark=base_res.benchmark_metrics,
        deltas=deltas,
        improved=improved,
        baseline_result=base_res,
        llm_result=llm_res,
    )


def apply_validation_gates(report: ABReport, *, dsr: float | None,
                           sharpe_percentile: float | None, gates: dict) -> ABReport:
    """Final acceptance = OOS improvement AND the anti-luck thresholds.

    A candidate whose DSR or random-entry percentile is unavailable or below
    the configured floor is NOT accepted, however good its deltas look — the
    whole point of Pack C is that deltas alone cannot tell luck from edge.
    """
    report.dsr = dsr
    report.random_percentile = sharpe_percentile
    # Fail CLOSED on missing thresholds: DSR and the percentile are both >= 0,
    # so a defaulted floor of 0.0 would make the anti-luck gate trivially true
    # whenever the config section is trimmed or a key is misspelled.
    if "min_dsr" not in gates or "min_random_percentile" not in gates:
        report.gate_notes.append(
            "GATE: validation.gates.min_dsr / min_random_percentile not "
            "configured — refusing to accept without anti-luck thresholds")
        report.improved = False
        return report
    min_dsr = float(gates["min_dsr"])
    min_pct = float(gates["min_random_percentile"])
    dsr_ok = dsr is not None and dsr >= min_dsr
    pct_ok = sharpe_percentile is not None and sharpe_percentile >= min_pct
    if not dsr_ok:
        report.gate_notes.append(
            f"GATE: DSR {'unavailable' if dsr is None else f'{dsr:.4f}'} "
            f"< required {min_dsr:.2f}")
    if not pct_ok:
        report.gate_notes.append(
            f"GATE: random-entry percentile "
            f"{'unavailable' if sharpe_percentile is None else f'{sharpe_percentile:.2f}'} "
            f"< required {min_pct:.2f}")
    report.improved = report.improved and dsr_ok and pct_ok
    return report

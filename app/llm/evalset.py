"""Golden evaluation set: human labeling CLI + prompt-regression harness.

`make label` shows unlabeled filings one at a time and records the human's
relevance label (target: 50-100 labeled filings). `make eval-llm` runs the
CURRENT research prompt against the golden set and reports accuracy + per-class
F1 vs the human labels, storing every run in `eval_runs` so a prompt/model
change that regresses is visible before it ships.

The labels are the SAME closed set as the schema's `relevance` field
(schemas.RELEVANCE_LABELS), so predictions and ground truth always align.
Labeling itself involves ZERO LLM calls; the harness calls the LLM through the
normal client (cache-aware, budget-capped, fully audited).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.llm import pipeline
from app.llm import research as research_mod
from app.llm.schemas import RELEVANCE_LABELS

log = logging.getLogger("llm.evalset")

_LABEL_KEYS = {"1": RELEVANCE_LABELS[0], "2": RELEVANCE_LABELS[1], "3": RELEVANCE_LABELS[2]}

# End-user strings in Polish (repo convention); code/comments stay English.
_PROMPT_PL = (
    "[1] istotny i ciekawy   [2] istotny, nieciekawy   [3] nieistotny   "
    "[s] pomin   [q] koniec > "
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def unlabeled_filings(conn) -> list:
    return conn.execute(
        """
        SELECT f.id, f.source, f.published_at, f.issuer_name, f.title, f.full_text
        FROM filings f LEFT JOIN eval_labels e ON e.filing_id = f.id
        WHERE e.filing_id IS NULL
        ORDER BY f.published_at DESC
        """
    ).fetchall()


def save_label(conn, filing_id: int, label: str, labeled_by: str,
               notes: str | None = None) -> None:
    if label not in RELEVANCE_LABELS:
        raise ValueError(f"unknown label: {label}")
    conn.execute(
        """
        INSERT INTO eval_labels (filing_id, label, labeled_by, labeled_at, notes)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(filing_id) DO UPDATE SET
            label=excluded.label, labeled_by=excluded.labeled_by,
            labeled_at=excluded.labeled_at, notes=excluded.notes
        """,
        (int(filing_id), label, labeled_by, _now(), notes),
    )
    conn.commit()


def run_labeling(conn, labeled_by: str, *, input_fn=input, print_fn=print,
                 excerpt_chars: int = 600) -> int:
    """Interactive labeling loop (Polish UI). Returns the number saved."""
    rows = unlabeled_filings(conn)
    if not rows:
        print_fn("Brak nieoznaczonych komunikatow do etykietowania.")
        return 0
    total_labeled = conn.execute("SELECT COUNT(*) FROM eval_labels").fetchone()[0]
    print_fn(f"Nieoznaczone komunikaty: {len(rows)} (oznaczone dotad: {total_labeled}; "
             f"cel: 50-100).")
    saved = 0
    for row in rows:
        print_fn("\n" + "=" * 72)
        print_fn(f"[{row['id']}] {row['source']}  {row['published_at']}")
        print_fn(f"Emitent: {row['issuer_name'] or '?'}")
        print_fn(f"Tytul:   {row['title']}")
        body = (row["full_text"] or "").strip()
        if body:
            excerpt = body[:excerpt_chars]
            more = " [...]" if len(body) > excerpt_chars else ""
            print_fn(f"Tresc:   {excerpt}{more}")
        while True:
            try:
                choice = input_fn(_PROMPT_PL).strip().lower()
            except (EOFError, KeyboardInterrupt):
                # Ctrl-D / exhausted piped stdin / Ctrl-C = end of session,
                # same as 'q' — already-saved labels are committed per row.
                print_fn(f"\nZapisano {saved} etykiet.")
                return saved
            if choice == "q":
                print_fn(f"Zapisano {saved} etykiet.")
                return saved
            if choice == "s":
                break
            if choice in _LABEL_KEYS:
                save_label(conn, row["id"], _LABEL_KEYS[choice], labeled_by)
                saved += 1
                break
            print_fn("Niepoprawny wybor — 1, 2, 3, s lub q.")
    print_fn(f"Zapisano {saved} etykiet.")
    return saved


# --- prompt-regression harness --------------------------------------------------

@dataclass
class EvalReport:
    prompt_version: str
    requested_model: str
    served_provider: str | None
    n_labels: int
    n_rejected: int = 0
    accuracy: float = 0.0
    f1: dict = field(default_factory=dict)
    confusion: dict = field(default_factory=dict)  # (true -> predicted -> count)

    def as_text(self) -> str:
        lines = [
            f"Golden-set eval: prompt={self.prompt_version} model={self.requested_model} "
            f"provider={self.served_provider or '?'}",
            f"  labels evaluated : {self.n_labels} ({self.n_rejected} responses rejected "
            f"— rejected counts as wrong)",
            f"  accuracy         : {self.accuracy:.3f}",
        ]
        for label in RELEVANCE_LABELS:
            lines.append(f"  F1 {label:<24}: {self.f1.get(label, 0.0):.3f}")
        return "\n".join(lines)


def _resolve_ticker(conn, row) -> str:
    # Same identity the pipeline puts in prompts (pipeline.prompt_identity), so
    # an eval call on an already-processed filing hits the pipeline's cache
    # entry instead of re-spending on a differently-keyed prompt.
    if row["instrument_id"]:
        hit = conn.execute("SELECT ticker, name, isin FROM instruments WHERE id = ?",
                           (row["instrument_id"],)).fetchone()
        if hit:
            return pipeline.prompt_identity(hit["ticker"], hit["name"], hit["isin"])
    return (row["issuer_name"] or "unknown").lower()


def per_class_f1(pairs: list[tuple[str, str | None]]) -> dict:
    """Per-class F1 from (true_label, predicted_label|None) pairs. Pure python."""
    f1: dict[str, float] = {}
    for label in RELEVANCE_LABELS:
        tp = sum(1 for t, p in pairs if t == label and p == label)
        fp = sum(1 for t, p in pairs if t != label and p == label)
        fn = sum(1 for t, p in pairs if t == label and p != label)
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1[label] = (2 * precision * recall / (precision + recall)
                     if (precision + recall) else 0.0)
    return f1


def run_eval(conn, client) -> EvalReport | None:
    """Run the CURRENT research prompt over every labeled filing; store the run."""
    rows = conn.execute(
        """
        SELECT f.id, f.title, f.full_text, f.instrument_id, f.issuer_name, e.label
        FROM eval_labels e JOIN filings f ON f.id = e.filing_id
        ORDER BY f.id
        """
    ).fetchall()
    if not rows:
        return None

    requested_model = client.cfg["extraction"]["model"]
    served_provider = None
    pairs: list[tuple[str, str | None]] = []
    n_rejected = 0
    for row in rows:
        ticker = _resolve_ticker(conn, row)
        text = f"{row['title']}\n{row['full_text'] or ''}".strip()
        research = research_mod.analyze_filing(client, ticker, text)
        if research is None:
            n_rejected += 1
            pairs.append((row["label"], None))  # rejected counts as wrong
            continue
        pairs.append((row["label"], research.get("relevance")))
    # served provider: latest extraction call in the audit log
    hit = conn.execute(
        "SELECT served_provider FROM llm_calls WHERE role='extraction'"
        " ORDER BY id DESC LIMIT 1").fetchone()
    if hit:
        served_provider = hit["served_provider"]

    correct = sum(1 for t, p in pairs if t == p)
    report = EvalReport(
        prompt_version=research_mod.prompt_version(),
        requested_model=requested_model,
        served_provider=served_provider,
        n_labels=len(pairs),
        n_rejected=n_rejected,
        accuracy=correct / len(pairs),
        f1=per_class_f1(pairs),
    )
    conn.execute(
        "INSERT INTO eval_runs (created_at, prompt_version, requested_model,"
        " served_provider, n_labels, n_rejected, accuracy, f1_json)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (_now(), report.prompt_version, report.requested_model,
         report.served_provider, report.n_labels, report.n_rejected,
         report.accuracy, json.dumps(report.f1, sort_keys=True)),
    )
    conn.commit()
    return report


def previous_run(conn, prompt_version: str, requested_model: str) -> dict | None:
    """The most recent eval run of a DIFFERENT configuration.

    A configuration is (prompt fingerprint, requested model): a model-only
    change with an unchanged prompt is still a shippable change that must be
    compared against its baseline — otherwise a cheaper-model regression would
    ship silently. Re-runs of the SAME configuration are replays, not baselines.
    """
    row = conn.execute(
        "SELECT prompt_version, requested_model, accuracy, f1_json FROM eval_runs"
        " WHERE NOT (prompt_version = ? AND requested_model = ?)"
        " ORDER BY id DESC LIMIT 1",
        (prompt_version, requested_model),
    ).fetchone()
    if not row:
        return None
    return {"prompt_version": row["prompt_version"],
            "requested_model": row["requested_model"],
            "accuracy": row["accuracy"], "f1": json.loads(row["f1_json"])}

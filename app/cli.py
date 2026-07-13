"""Command-line entrypoints: ingest -> features -> walk-forward backtest.

Usage:
    python -m app.cli ingest
    python -m app.cli features
    python -m app.cli backtest [--strategy trend_momentum]
    python -m app.cli ab [--baseline trend_momentum] [--llm trend_momentum_llm]

The money path is deterministic. The `ab` command compares a baseline strategy
against one that adds an `llm_score` gate; it reads PRE-MATERIALIZED LLM features
from the DB and makes NO LLM call (sizing/risk stay deterministic).
"""
from __future__ import annotations

import argparse
import os
import sys

import yaml

from app import config as cfg
from app.backtest import ab_harness, engine
from app.db import connect, init_db
from app.ingestion import demo, provenance, stooq
from app.logging import decisions as declog
from app.paper import loop as paper_loop
from app.paper.store import PAPER_PREFIX


def cmd_ingest(args) -> int:
    from datetime import date, datetime, timedelta
    from zoneinfo import ZoneInfo

    conn = connect(args.db)
    init_db(conn)
    universe = cfg.load_universe()
    try:
        # Guard FIRST — before the incremental early-return below, so a mixed
        # database is refused (exit 2) even on a day with nothing to ingest.
        # Also takes the write lock, closing the concurrent-ingest race.
        provenance.assert_no_mixing(
            conn, provenance.DEMO_SOURCE if args.offline else args.source)
        if args.offline:
            print("Ingesting DETERMINISTIC DEMO DATA (offline, NOT real prices)...")
            report = demo.ingest_offline(conn, universe)
        elif args.source == "stooq":
            print("Ingesting from Stooq (this hits the network)...")
            report = stooq.ingest_universe(conn, universe, delay_seconds=1.0)
        else:  # gpw (default): official session archive + GPW Benchmark indices
            from app.ingestion import gpw_archive

            end = date.fromisoformat(args.end) if args.end else _default_ingest_end(
                datetime.now(ZoneInfo("Europe/Warsaw")))
            if args.start:
                start = date.fromisoformat(args.start)
            else:
                # Incremental: resume after the last stored REAL bar (demo rows
                # never anchor a live backfill); fresh DB -> 90 days.
                last = provenance.last_real_bar_date(conn)
                start = (date.fromisoformat(last) + timedelta(days=1)
                         if last else end - timedelta(days=90))
            if start > end:
                print(f"Already up to date (last bar {start - timedelta(days=1)}).")
                conn.close()
                return 0
            n_days = (end - start).days + 1
            print(f"Ingesting from GPW archive: {start} .. {end} "
                  f"({n_days} calendar days, ~1 request/session day"
                  f"{', FULL market' if args.full else ', universe only'})...")
            report = gpw_archive.ingest_range(conn, universe, start, end,
                                              full_market=args.full)
    except provenance.DataMixingError as exc:
        print(f"ERROR: {exc}")
        conn.close()
        return 2
    total = sum(report.counts.values())
    print(f"Ingested {total} bars across {len(report.counts)} tickers.")
    for tk, n in sorted(report.counts.items()):
        print(f"  {tk:10s} {n:6d} bars")
    if report.failures:
        print(f"\nFAILED {len(report.failures)} tickers (successes above were committed):")
        for tk, reason in sorted(report.failures.items()):
            print(f"  {tk:10s} {reason}")
        if not report.counts:
            print("\nStooq is refusing automated CSV access from this network "
                  "(bot-check / 'Access denied' / daily limit).")
            print("Retry later from a normal connection, or use: "
                  "make ingest-offline  (deterministic demo data in its own "
                  "database, data/demo.db).")
        conn.close()
        return 2
    conn.close()
    return 0


def _default_ingest_end(now_warsaw):
    """Latest session date safe to ingest as FINAL EOD data.

    GPW serves TODAY's archive file intraday with partial (still-changing)
    bars; ingesting one as a final EOD bar breaks the point-in-time
    convention (as_of_date = session date assumes the bar is closed). Today
    only counts after the session is over (closing auction ends 17:05;
    18:00 adds slack). An explicit --end overrides this guard.
    """
    from datetime import timedelta
    today = now_warsaw.date()
    return today if now_warsaw.hour >= 18 else today - timedelta(days=1)


# Everything derived from prices, in FK-safe deletion order (referencing
# tables before decisions). Wiped by purge-demo when the database never held
# real prices: every one of these rows then described synthetic data.
_DERIVED_TABLES = ("paper_orders", "overrides", "trades", "decisions",
                   "positions", "equity_curve", "paper_state", "strategy_trials")


def cmd_purge_demo(args) -> int:
    """Delete demo price rows AND, on a demo-only DB, everything derived.

    Demo rows are synthetic and carry no information, so deleting them is
    always safe. If the database never held real prices, every derived row
    (decisions, trades, equity, trials, paper state) described fake data too
    and is wiped with them — otherwise demo trials would keep polluting the
    Deflated Sharpe of future REAL backtests and a demo-anchored paper state
    would wedge the loop. If real rows DO remain (a pre-guard mixed database),
    derived rows cannot be attributed and are left with a warning.
    """
    conn = connect(args.db)
    init_db(conn)
    n = conn.execute("DELETE FROM prices WHERE source = ?",
                     (provenance.DEMO_SOURCE,)).rowcount
    if not n:
        conn.commit()
        print("No demo price rows found; nothing to purge.")
        conn.close()
        return 0
    wiped: dict[str, int] = {}
    if not provenance.real_rows_present(conn):
        for table in _DERIVED_TABLES:
            count = conn.execute(f"DELETE FROM {table}").rowcount  # noqa: S608 - fixed table list
            if count:
                wiped[table] = count
    conn.commit()
    print(f"Purged {n} demo price rows.")
    if wiped:
        print("Also cleared demo-derived state: "
              + ", ".join(f"{t} ({c})" for t, c in wiped.items()) + ".")
    elif provenance.real_rows_present(conn):
        print("Real price rows remain, so derived results were kept — but any "
              "decisions/trades/equity/trials/paper state created while demo "
              "data was present described fake prices; review them manually.")
    conn.close()
    return 0


def cmd_features(args) -> int:
    """Recompute and report a feature snapshot for the latest available date."""
    conn = connect(args.db)
    init_db(conn)
    universe = cfg.load_universe()
    bench = universe["benchmark"]["ticker"]
    instruments, _bench = engine.load_instruments(conn, universe, bench)
    if not instruments:
        print("No price data. Run `make ingest` first.")
        return 1
    print(f"Computed features for {len(instruments)} instruments.")
    for inst in instruments[:5]:
        last = inst.features.dropna(subset=["sma200"]).tail(1)
        if last.empty:
            continue
        row = last.iloc[0]
        print(f"  {inst.ticker:6s} close={row['close']:.2f} "
              f"mom6m={row['momentum_6m']:+.3f} vs_sma200={row['close_vs_sma200']:+.3f}")
    conn.close()
    return 0


def cmd_backtest(args) -> int:
    conn = connect(args.db)
    init_db(conn)
    universe = cfg.load_universe()
    bt_cfg = cfg.load_backtest_config()
    strat = cfg.load_strategy(args.strategy)

    strategy_id = declog.register_strategy(
        conn, strat["name"], int(strat["version"]), yaml.safe_dump(strat)
    )

    bench = universe["benchmark"]["ticker"]
    instruments, bench_close = engine.load_instruments(conn, universe, bench)
    if not instruments:
        print("No price data. Run `make ingest` first.")
        return 1

    # If the strategy gates on any llm_* feature, attach point-in-time LLM
    # features (materialized earlier; read deterministically, NO LLM call).
    # Otherwise the gate would never see a value and silently block every entry.
    if engine.strategy_uses_llm_features(strat):
        instruments = engine.attach_llm_scores(conn, instruments)
        with_scores = sum(
            1 for i in instruments if i.llm_scores is not None and not i.llm_scores.empty
        )
        print(f"Strategy uses llm_score; attached LLM features for "
              f"{with_scores}/{len(instruments)} instruments.")

    membership, membership_error = _load_membership(conn, bt_cfg)
    if membership_error:
        print(membership_error)
        conn.close()
        return 1

    print(f"Running walk-forward backtest on {len(instruments)} instruments "
          f"(strategy: {strat['name']} v{strat['version']})...")
    result = engine.run_walk_forward(instruments, bench_close, strat, bt_cfg,
                                     membership=membership)
    if result.fill_anomalies:
        n_lapsed = sum(1 for a in result.fill_anomalies
                       if a["type"] == "order_lapsed_no_bar")
        print(f"Fill audit: {len(result.fill_anomalies) - n_lapsed} orders used a "
              f"close reference (open missing), {n_lapsed} lapsed (fill bar missing).")

    _persist_results(conn, bt_cfg["user_id"], result, strategy_id=strategy_id,
                     params=strat)
    if result.metrics.get("walk_forward_windows") == 0:
        print("\nWARNING: not enough history for in-sample + embargo + OOS — "
              "this is a FULL-SPAN run, NOT out-of-sample. Treat these metrics "
              "as in-sample evidence only.")
    _print_metrics_table(result, bt_cfg["walk_forward"]["benchmark"])
    validation_text, _dsr, _mc = _validate_run(conn, strat, bt_cfg, result,
                                               instruments, membership)
    print(validation_text)
    conn.close()
    return 0


def cmd_llm(args) -> int:
    """Materialize point-in-time LLM features from unprocessed filings.

    TEXT (filings) goes to the LLM (research -> judge, validated JSON);
    NUMBERS (the deterministic quant context, here momentum_6m from the
    point-in-time feature panel) enter the prompt as context text only.
    The output is one llm_score per (instrument, as_of_date) in
    `llm_features`; the backtest later reads those rows with NO LLM call.
    """
    import os
    from datetime import datetime
    from zoneinfo import ZoneInfo

    if not os.environ.get("OPENROUTER_API_KEY"):
        print("ERROR: OPENROUTER_API_KEY is not set (see .env.example); "
              "cannot call OpenRouter.")
        return 2

    # Local imports: only this command touches the LLM layer, keeping the
    # ingest/backtest commands import-clean of app.llm (money-path audit).
    from app.features import compute
    from app.llm import pipeline as llm_pipeline
    from app.llm.client import LLMClient

    conn = connect(args.db)
    init_db(conn)
    universe = cfg.load_universe()
    client = LLMClient(conn, cfg.load_llm_config())

    as_of = args.date or datetime.now(ZoneInfo("Europe/Warsaw")).date().isoformat()
    entries = universe.get("instruments", [])
    if args.ticker:
        entries = [e for e in entries if e["ticker"].lower() == args.ticker.lower()]
        if not entries:
            print(f"Ticker '{args.ticker}' is not in the universe.")
            conn.close()
            return 1

    from datetime import timezone as _tz

    from app.alerts import telegram
    from app.llm.client import LLMBudgetExceededError

    print(f"Materializing LLM features as of {as_of} "
          f"(filings cutoff: end of day, Europe/Warsaw)...")
    n_features = n_nothing = 0
    degraded_reason = None
    for entry in entries:
        ticker = entry["ticker"].lower()
        row = conn.execute(
            "SELECT id FROM instruments WHERE ticker = ?", (ticker,)
        ).fetchone()
        if row is None:
            print(f"  {ticker:10s} not ingested (run `make ingest` first) — skipped")
            n_nothing += 1
            continue
        inst_id = int(row["id"])

        # Deterministic quant context (point-in-time; passed as text only).
        quant_score = None
        prices = compute.load_prices_asof(conn, inst_id, as_of)
        if not prices.empty:
            snap = compute.features_at(compute.compute_features(prices), as_of)
            if snap:
                quant_score = snap.get("momentum_6m")

        try:
            verdict = llm_pipeline.compute_feature_for_date(
                conn, client, instrument_id=inst_id, ticker=ticker,
                as_of_date=as_of, quant_score=quant_score,
            )
        except LLMBudgetExceededError as exc:
            # Budget, not a bad filing: nothing was marked processed or attempt-
            # bumped for the interrupted instrument — those filings simply wait.
            degraded_reason = str(exc)
            print(f"  {ticker:10s} STOPPED: {exc}")
            break
        if verdict is None:
            n_nothing += 1  # no unprocessed filings, or rejected (logged)
        else:
            n_features += 1
            print(f"  {ticker:10s} llm_score={verdict['llm_score']:+.3f} "
                  f"({verdict['verdict']}, conviction={verdict['conviction']:.2f})")

    status = "degraded" if degraded_reason else "ok"
    conn.execute(
        "INSERT INTO llm_runs (run_at, as_of_date, status, detail, features_written)"
        " VALUES (?, ?, ?, ?, ?)",
        (datetime.now(_tz.utc).isoformat(), as_of, status, degraded_reason,
         n_features),
    )
    conn.commit()

    print(f"\nMaterialized {n_features} feature rows; "
          f"{n_nothing} instruments had nothing to process.")
    if n_features == 0 and not degraded_reason:
        n_filings = conn.execute("SELECT COUNT(*) AS c FROM filings").fetchone()["c"]
        if n_filings == 0:
            print("No filings in the DB yet — the LLM pipeline has nothing to "
                  "read. Start the collector (`make collect` / `make collect-loop`) "
                  "and let it run: RSS has no backfill, so filing history only "
                  "accrues going forward.")
    if degraded_reason:
        print("RUN DEGRADED: monthly LLM budget exhausted — the pipeline "
              "continues WITHOUT new LLM features (baseline-only).")
        telegram.send_text(
            "⚠️ Budzet LLM wyczerpany\n"
            f"Data decyzji: {as_of}\n"
            "Pipeline dziala w trybie awaryjnym (bez nowych cech LLM).\n"
            f"Szczegoly: {degraded_reason}"
        )
    _print_llm_audit(conn)
    conn.close()
    return 3 if degraded_reason else 0


def cmd_label(args) -> int:
    """Interactive golden-set labeling of collected filings (ZERO LLM calls)."""
    from app.llm import evalset

    conn = connect(args.db)
    init_db(conn)
    user = args.user or cfg.load_backtest_config()["user_id"]
    evalset.run_labeling(conn, user)
    conn.close()
    return 0


def cmd_eval_llm(args) -> int:
    """Prompt-regression harness: current research prompt vs the golden set."""
    import os

    if not os.environ.get("OPENROUTER_API_KEY"):
        print("ERROR: OPENROUTER_API_KEY is not set; cannot call OpenRouter.")
        return 2

    from app.llm import evalset
    from app.llm.client import LLMBudgetExceededError, LLMClient

    conn = connect(args.db)
    init_db(conn)
    client = LLMClient(conn, cfg.load_llm_config())
    try:
        report = evalset.run_eval(conn, client)
    except LLMBudgetExceededError as exc:
        print(f"STOPPED: {exc}")
        conn.close()
        return 3
    if report is None:
        print("No labeled filings yet — run `make label` first (target: 50-100).")
        conn.close()
        return 1
    print(report.as_text())
    prev = evalset.previous_run(conn, report.prompt_version, report.requested_model)
    if prev:
        delta = report.accuracy - prev["accuracy"]
        print(f"\nvs previous config (prompt {prev['prompt_version']}, "
              f"model {prev['requested_model']}): accuracy "
              f"{prev['accuracy']:.3f} -> {report.accuracy:.3f} ({delta:+.3f})")
        if delta < 0:
            print("REGRESSION vs the previous configuration — do NOT ship this "
                  "prompt/model change (README rule).")
    conn.close()
    return 0


def _print_llm_audit(conn, limit: int = 10) -> None:
    """Reproducibility audit: served provider/model/generation id per call."""
    rows = conn.execute(
        "SELECT created_at, role, served_model, served_provider, generation_id,"
        " cached_tokens, cache_hit FROM llm_calls ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    if not rows:
        return
    print("\nProvider audit (latest llm_calls):")
    for r in rows:
        print(f"  {r['created_at'][:19]} {r['role']:<10} "
              f"model={r['served_model']} provider={r['served_provider']} "
              f"gen={r['generation_id']} cache_hit={r['cache_hit']} "
              f"cached_tokens={r['cached_tokens']}")


def _validate_run(conn, strategy_cfg, bt_cfg, result, instruments, membership,
                  run_mc: bool = True):
    """Anti-luck validation for one backtest result (Pack C).

    Logs the run into the trials registry, computes the Deflated Sharpe Ratio
    from the registry statistics, and (when run_mc) runs the random-entry
    Monte Carlo benchmark. Returns (report_text, dsr_result, mc_result).
    Deterministic.

    A FULL-SPAN fallback run (walk_forward_windows == 0) is in-sample, not OOS:
    it is NEVER logged into the trials registry and NEVER produces a DSR or
    percentile, so an in-sample Sharpe can never masquerade as anti-luck
    evidence (nor pollute the shared registry every future DSR reads).
    Returns (note, None, None) in that case.
    """
    from app.backtest import mc_benchmark, validation

    if result.metrics.get("walk_forward_windows") == 0:
        return (
            "\nValidation (anti-luck): SKIPPED — this is a full-span in-sample "
            "run (no walk-forward split), so it is not logged as a trial and no "
            "DSR / random-benchmark is computed.",
            None, None,
        )

    eq = result.equity_curve
    oos_start = eq.index[0].date().isoformat() if len(eq) else None
    oos_end = eq.index[-1].date().isoformat() if len(eq) else None
    metrics = dict(result.metrics)
    metrics["sharpe_pp"] = validation.per_period_sharpe(eq)
    validation.log_trial(
        conn, cfg_hash=validation.config_hash(strategy_cfg, bt_cfg),
        strategy_name=strategy_cfg["name"],
        strategy_version=int(strategy_cfg["version"]),
        oos_start=oos_start, oos_end=oos_end, metrics=metrics,
    )
    n_trials, var_pp = validation.trial_stats(conn)
    dsr = validation.deflated_sharpe(eq, n_trials, var_pp)

    v_cfg = bt_cfg.get("validation") or {}
    mc = mc_benchmark.run_random_benchmark(
        instruments, result, bt_cfg, strategy_cfg["risk"],
        n_sims=int(v_cfg.get("mc_sims", 0)) if run_mc else 0,
        seed=int(v_cfg.get("mc_seed", 4242)),
        membership=membership,
    )

    lines = [
        "",
        "Validation (anti-luck):",
        f"  trials to date        {dsr.n_trials} distinct config(s) ever backtested",
        f"  raw sharpe (annual)   {result.metrics['sharpe']:.4f}",
        f"  deflated sharpe DSR   {dsr.dsr:.4f}  "
        f"[P(true Sharpe beats the expected max of {dsr.n_trials} luck trials); "
        f"SR* = {dsr.sr_star:.5f}/period]",
        mc.as_text(),
    ]
    return "\n".join(lines), dsr, mc


def _load_membership(conn, bt_cfg):
    """Resolve the optional point-in-time universe gate (universe.index).

    Returns (membership_map | None, error_message | None). Used by BOTH
    backtest and ab so the two always simulate the same universe.
    """
    index_name = (bt_cfg.get("universe") or {}).get("index")
    if not index_name:
        return None, None
    membership = engine.load_membership_map(conn, index_name)
    if not membership:
        return None, (
            f"ERROR: universe.index='{index_name}' is set but the "
            f"index_membership table is empty. Run `python -m app.cli refdata` "
            f"(and fill config/index_membership.yaml) first."
        )
    print(f"Point-in-time universe: '{index_name}' membership loaded for "
          f"{len(membership)} instruments.")
    return membership, None


def cmd_refdata(args) -> int:
    """Load index membership + corporate action fixtures; derive adjusted prices."""
    from app.ingestion import refdata

    conn = connect(args.db)
    init_db(conn)
    report = refdata.load_refdata(
        conn, cfg.load_index_membership(), cfg.load_corporate_actions()
    )
    print(f"Loaded {report.membership_rows} index membership rows and "
          f"{report.action_rows} corporate actions.")
    if report.adjusted_instruments:
        print(f"Derived adjusted price series for {report.adjusted_instruments} "
              f"instruments ({report.adjusted_bars} bars, adjusted=1; raw rows untouched).")
    if report.failures:
        print(f"\n{len(report.failures)} problem(s):")
        for failure in report.failures:
            print(f"  {failure}")
        conn.close()
        return 2
    conn.close()
    return 0


def cmd_check_data(args) -> int:
    """Data-quality report; sends a Telegram alert (dry-run without token) on issues."""
    from app.alerts import telegram
    from app.ingestion import quality

    conn = connect(args.db)
    init_db(conn)
    report = quality.run_checks(conn, cfg.load_universe(), cfg.load_data_quality())
    print(quality.format_report(report))
    conn.close()
    if not report.ok:
        telegram.send_text(quality.format_alert_pl(report))
        return 2
    return 0


def cmd_override(args) -> int:
    """Journal a manual deviation from a system signal (append-only)."""
    from datetime import datetime, timezone

    conn = connect(args.db)
    init_db(conn)
    user_id = args.user or cfg.load_backtest_config()["user_id"]
    if args.decision_id is not None:
        row = conn.execute(
            "SELECT id FROM decisions WHERE id = ?", (args.decision_id,)
        ).fetchone()
        if row is None:
            print(f"ERROR: decision {args.decision_id} does not exist.")
            conn.close()
            return 1
    conn.execute(
        "INSERT INTO overrides (user_id, timestamp, decision_id, action_taken, reason)"
        " VALUES (?, ?, ?, ?, ?)",
        (user_id, datetime.now(timezone.utc).isoformat(), args.decision_id,
         args.action, args.reason),
    )
    conn.commit()
    ref = f" (decision {args.decision_id})" if args.decision_id is not None else ""
    print(f"Override recorded for user '{user_id}'{ref}.")
    conn.close()
    return 0


def cmd_backup(args) -> int:
    """Snapshot the DB (VACUUM INTO), push to R2 when creds exist, prune old ones."""
    from app import backup as bkp
    from app.config import DEFAULT_DB_PATH

    db_path = args.db or DEFAULT_DB_PATH
    try:
        report = bkp.run_backup(db_path, cfg.load_backup_config())
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        return 1
    except RuntimeError as exc:  # boto3 missing while creds are set
        print(f"ERROR: {exc}")
        return 1
    print(report.as_text())
    return 0


def cmd_restore_test(args) -> int:
    """Verify the latest snapshot actually restores (integrity + row counts)."""
    from app import backup as bkp
    from app.config import DEFAULT_DB_PATH

    try:
        report = bkp.run_restore_test(args.db or DEFAULT_DB_PATH,
                                      cfg.load_backup_config())
    except RuntimeError as exc:  # partial creds / boto3 missing
        print(f"ERROR: {exc}")
        return 1
    print(report.as_text())
    return 0 if report.ok else 2


def cmd_status(args) -> int:
    """One-command deployment liveness check; alerts + non-zero exit when stale."""
    from app import status as statusmod
    from app.alerts import telegram

    conn = connect(args.db)
    init_db(conn)
    report = statusmod.run_status(conn, cfg.load_backup_config())
    conn.close()
    print(report.as_text())
    if not report.ok:
        telegram.send_text(report.alert_pl())
        return 2
    return 0


def cmd_ab(args) -> int:
    """Run the baseline vs baseline+LLM A/B comparison on the OOS window."""
    conn = connect(args.db)
    init_db(conn)
    universe = cfg.load_universe()
    bt_cfg = cfg.load_backtest_config()
    baseline = cfg.load_strategy(args.baseline)
    llm = cfg.load_strategy(args.llm)

    instruments, _ = engine.load_instruments(conn, universe, universe["benchmark"]["ticker"])
    if not instruments:
        print("No price data. Run `make ingest` first.")
        return 1

    membership, membership_error = _load_membership(conn, bt_cfg)
    if membership_error:
        print(membership_error)
        conn.close()
        return 1

    print(f"A/B: baseline='{baseline['name']}' vs llm='{llm['name']}' "
          f"on {len(instruments)} instruments (OOS, realistic costs)...")
    report = ab_harness.run_ab(conn, universe, baseline, llm, bt_cfg,
                               membership=membership)

    # Anti-luck gates on the CANDIDATE (the LLM variant): the deltas gate alone
    # cannot tell luck from edge (Pack C). Both trials land in the registry.
    _validate_run(conn, baseline, bt_cfg, report.baseline_result, instruments,
                  membership, run_mc=False)  # baseline: registry entry only
    _, dsr, mc = _validate_run(conn, llm, bt_cfg, report.llm_result, instruments,
                               membership)
    gates = (bt_cfg.get("validation") or {}).get("gates") or {}
    # A full-span in-sample A/B yields no DSR / percentile (dsr, mc are None):
    # apply_validation_gates then fails CLOSED (unavailable evidence is not
    # accepted), so an LLM candidate can never pass on in-sample data.
    report = ab_harness.apply_validation_gates(
        report, dsr=dsr.dsr if dsr is not None else None,
        sharpe_percentile=(mc.percentiles.get("sharpe")
                           if (mc is not None and mc.n_sims) else None),
        gates=gates,
    )
    print()
    print(report.as_text())
    conn.close()
    return 0


def cmd_signals(args) -> int:
    """Daily paper-trading run: settle yesterday's orders, decide today's.

    Cron-friendly (evening, after `make ingest`). Exit codes: 0 = processed or
    idempotent no-op; 2 = refused (stale/partial data, config change, catch-up
    cap) — the refusal reason is printed and alerted, state is untouched.
    """
    conn = connect(args.db)
    init_db(conn)
    universe = cfg.load_universe()
    bt_cfg = cfg.load_backtest_config()
    strat = cfg.load_strategy(args.strategy)

    code, report = paper_loop.run_signals(
        conn,
        universe=universe,
        bt_cfg=bt_cfg,
        strategy_cfg=strat,
        session_end=args.session,
        accept_config_change=args.accept_config_change,
        dry_run=args.dry_run,
    )
    print(report.as_text())
    conn.close()
    return code


def _persist_results(conn, user_id: str, result, *, strategy_id=None, params=None) -> None:
    if user_id.startswith(PAPER_PREFIX):
        # The paper loop owns the 'paper:' namespace; a backtest writing into it
        # would corrupt the live track record (and vice versa is prevented by
        # paper_user_id always prefixing).
        raise ValueError(f"backtest user_id must not use the {PAPER_PREFIX!r} namespace")
    for d in result.decisions:
        dec_id = declog.log_decision(
            conn, user_id=user_id, strategy_id=strategy_id, instrument_id=d["instrument_id"],
            decision_date=d["decision_date"], action=d["action"],
            features=d.get("features", {}), params=params,
        )
        declog.log_trade(
            conn, user_id=user_id, instrument_id=d["instrument_id"],
            side="BUY" if d["action"] == "ENTER" else "SELL",
            qty=d["qty"], price=d["price"], fee=d["fee"], slippage=d["slippage"],
            trade_date=d["fill_date"], decision_id=dec_id,
        )
    cash_curve = result.cash_curve
    exposure_curve = result.exposure_curve
    for date, equity in result.equity_curve.items():
        cash = float(cash_curve.get(date, 0.0)) if not cash_curve.empty else 0.0
        exposure = float(exposure_curve.get(date, 0.0)) if not exposure_curve.empty else 0.0
        declog.record_equity(conn, user_id=user_id, date=date.date().isoformat(),
                             equity=float(equity), cash=cash, exposure=exposure)
    conn.commit()


def _fmt(v) -> str:
    if v == float("inf"):
        return "inf"
    return f"{v:,.4f}"


def _print_metrics_table(result, benchmark_name: str) -> None:
    m = result.metrics
    b = result.benchmark_metrics
    keys = ["cagr", "sharpe", "sortino", "max_drawdown", "calmar",
            "win_rate", "profit_factor", "turnover", "total_return", "n_trades"]
    print("\nOut-of-sample metrics (realistic costs) vs " + benchmark_name.upper())
    print(f"{'metric':<16}{'strategy':>16}{'benchmark':>16}")
    print("-" * 48)
    for k in keys:
        print(f"{k:<16}{_fmt(m[k]):>16}{_fmt(b[k]):>16}")


def main(argv=None) -> int:
    cfg.load_dotenv()  # consume a local .env (shell exports still take precedence)
    parser = argparse.ArgumentParser(prog="app.cli", description="GPW deterministic core")
    parser.add_argument("--db", default=None, help="SQLite path (default: data/gpw.db)")
    sub = parser.add_subparsers(dest="command", required=True)

    ing = sub.add_parser("ingest", help="Fetch EOD data into SQLite (GPW archive by default)")
    ing.add_argument("--offline", action="store_true",
                     help="Use deterministic DEMO data instead of a live source (NOT real prices)")
    ing.add_argument("--source", choices=list(provenance.REAL_SOURCES),
                     default=provenance.GPW_SOURCE,
                     help="Live source: GPW official archive (default) or Stooq CSV "
                          "(login-gated as of 2026)")
    ing.add_argument("--start", default=None,
                     help="Backfill start date (ISO). Default: resume after the "
                          "last stored real (non-demo) bar")
    ing.add_argument("--end", default=None, help="End date (ISO). Default: today (Warsaw)")
    ing.add_argument("--full", action="store_true",
                     help="GPW source: store EVERY PLN instrument found (anti-survivorship "
                          "backfill), not just the configured universe")
    sub.add_parser("purge-demo",
                   help="Delete demo (synthetic) price rows so real ingestion can proceed")
    sub.add_parser("features", help="Compute and preview features")
    bt = sub.add_parser("backtest", help="Run walk-forward backtest vs WIG20TR")
    bt.add_argument("--strategy", default="trend_momentum")
    ab = sub.add_parser("ab", help="A/B: baseline vs baseline+LLM (OOS, uses materialized LLM features)")
    ab.add_argument("--baseline", default="trend_momentum")
    ab.add_argument("--llm", default="trend_momentum_llm")
    llm = sub.add_parser(
        "llm", help="Materialize point-in-time LLM features from filings (calls OpenRouter)")
    llm.add_argument("--date", default=None,
                     help="Decision date T (ISO; default: today in Europe/Warsaw)")
    llm.add_argument("--ticker", default=None,
                     help="Restrict to one ticker (default: all universe instruments)")
    sub.add_parser("refdata",
                   help="Load index membership + corporate actions fixtures; derive adjusted prices")
    sub.add_parser("check-data",
                   help="Data-quality report (missing sessions, volume, jumps, stale tickers)")
    ov = sub.add_parser("override",
                        help="Journal a manual deviation from a system signal (append-only)")
    ov.add_argument("--decision-id", type=int, default=None,
                    help="The decisions.id being overridden (optional)")
    ov.add_argument("--action", required=True, help="What you actually did")
    ov.add_argument("--reason", required=True, help="Why you deviated from the signal")
    ov.add_argument("--user", default=None,
                    help="user_id (default: backtest.yaml user_id)")
    lbl = sub.add_parser("label",
                         help="Label filings for the golden eval set (interactive, no LLM)")
    lbl.add_argument("--user", default=None,
                     help="labeled_by (default: backtest.yaml user_id)")
    sub.add_parser("eval-llm",
                   help="Prompt-regression eval: current research prompt vs the golden set")
    sub.add_parser("backup",
                   help="Online DB snapshot (VACUUM INTO) + R2 upload + retention")
    sub.add_parser("restore-test",
                   help="Pull the latest snapshot and verify it restores")
    sub.add_parser("status",
                   help="Deployment liveness: prices, collector, filings, backups")
    sig = sub.add_parser("signals",
                         help="Daily paper-trading run: settle pending orders, "
                              "generate today's signals, send alert cards")
    sig.add_argument("--strategy", default="trend_momentum")
    sig.add_argument("--dry-run", action="store_true",
                     help="Process and print, then ROLL BACK all writes; no alerts")
    sig.add_argument("--session", default=None,
                     help="Clamp the calendar to this ISO date (ops/test hook; "
                          "fills still use only that session's bars; skips the "
                          "staleness gate — an explicit replay is deliberate)")
    sig.add_argument("--accept-config-change", action="store_true",
                     help="Acknowledge a strategy/cost config change and continue "
                          "the track record anyway")

    args = parser.parse_args(argv)
    if args.command == "ingest":
        return cmd_ingest(args)
    if args.command == "purge-demo":
        return cmd_purge_demo(args)
    if args.command == "features":
        return cmd_features(args)
    if args.command == "backtest":
        return cmd_backtest(args)
    if args.command == "ab":
        return cmd_ab(args)
    if args.command == "llm":
        return cmd_llm(args)
    if args.command == "refdata":
        return cmd_refdata(args)
    if args.command == "check-data":
        return cmd_check_data(args)
    if args.command == "override":
        return cmd_override(args)
    if args.command == "label":
        return cmd_label(args)
    if args.command == "eval-llm":
        return cmd_eval_llm(args)
    if args.command == "backup":
        return cmd_backup(args)
    if args.command == "restore-test":
        return cmd_restore_test(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "signals":
        return cmd_signals(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())

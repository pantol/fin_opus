"""Command-line entrypoints: ingest -> features -> walk-forward backtest.

Usage:
    python -m app.cli ingest
    python -m app.cli features
    python -m app.cli backtest [--strategy trend_momentum]

The deterministic core only. No LLM anywhere in this path.
"""
from __future__ import annotations

import argparse
import sys

import yaml

from app import config as cfg
from app.backtest import engine
from app.db import connect, init_db
from app.ingestion import demo, stooq
from app.logging import decisions as declog


def cmd_ingest(args) -> int:
    conn = connect(args.db)
    init_db(conn)
    universe = cfg.load_universe()
    if args.offline:
        print("Ingesting DETERMINISTIC DEMO DATA (offline, NOT real prices)...")
        counts = demo.ingest_offline(conn, universe)
    else:
        print("Ingesting from Stooq (this hits the network)...")
        try:
            counts = stooq.ingest_universe(conn, universe)
        except stooq.StooqUnavailableError as exc:
            print(f"ERROR: {exc}")
            print("Stooq is blocking automated CSV access from this network.")
            print("Retry later from a normal connection, or use: "
                  "python -m app.cli ingest --offline  (demo data only).")
            conn.close()
            return 2
    total = sum(counts.values())
    print(f"Ingested {total} bars across {len(counts)} tickers.")
    for tk, n in sorted(counts.items()):
        print(f"  {tk:10s} {n:6d} bars")
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

    print(f"Running walk-forward backtest on {len(instruments)} instruments "
          f"(strategy: {strat['name']} v{strat['version']})...")
    result = engine.run_walk_forward(instruments, bench_close, strat, bt_cfg)

    _persist_results(conn, bt_cfg["user_id"], result, strategy_id=strategy_id,
                     params=strat)
    _print_metrics_table(result, bt_cfg["walk_forward"]["benchmark"])
    conn.close()
    return 0


def _persist_results(conn, user_id: str, result, *, strategy_id=None, params=None) -> None:
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
    parser = argparse.ArgumentParser(prog="app.cli", description="GPW deterministic core")
    parser.add_argument("--db", default=None, help="SQLite path (default: data/gpw.db)")
    sub = parser.add_subparsers(dest="command", required=True)

    ing = sub.add_parser("ingest", help="Fetch EOD data from Stooq into SQLite")
    ing.add_argument("--offline", action="store_true",
                     help="Use deterministic DEMO data instead of live Stooq (NOT real prices)")
    sub.add_parser("features", help="Compute and preview features")
    bt = sub.add_parser("backtest", help="Run walk-forward backtest vs WIG20TR")
    bt.add_argument("--strategy", default="trend_momentum")

    args = parser.parse_args(argv)
    if args.command == "ingest":
        return cmd_ingest(args)
    if args.command == "features":
        return cmd_features(args)
    if args.command == "backtest":
        return cmd_backtest(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())

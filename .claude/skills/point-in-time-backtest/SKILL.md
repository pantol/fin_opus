---
name: point-in-time-backtest
description: Use when writing or modifying ANY data ingestion, feature engineering, backtesting, position-sizing, or risk code for the GPW decision system. Enforces point-in-time correctness (no look-ahead or survivorship bias), deterministic money logic (zero LLM in the financial path), and realistic fills (commission, spread, slippage, walk-forward OOS). Trigger phrases: backtest, as_of, point-in-time, look-ahead, feature, sizing, slippage, walk-forward, ingestion, risk, position.
---

# Point-in-time & realistic backtest discipline

Apply whenever code touches market data, features, signals, sizing, or simulated fills.

## 1. Point-in-time correctness (no look-ahead)
- Every data row carries `as_of_date` = the moment it became publicly available, NOT the period it describes.
- A feature computed for decision date T may use ONLY rows with `as_of_date <= T`.
- Never use restated/adjusted fundamentals as if they were known earlier. Keep raw + adjusted separately, flagged.
- Lag fundamentals to their real publication date, not the quarter-end they refer to.

## 2. No survivorship bias
- The universe MUST include delisted/dead tickers for any period in which they traded.
- Never build the backtest universe from "tickers that exist today."

## 3. Deterministic money logic
- Position sizing, stops, exposure limits, and any buy/sell quantity are computed by pure, deterministic code. NO LLM calls in this path.
- For identical inputs, the money logic must produce identical outputs (set seeds if any randomness exists; prefer none).

## 4. Realistic fills
- Buy at ask, sell at bid (model the spread).
- Apply commission per the broker's real schedule and slippage scaled to liquidity.
- Reject fills larger than realistically available volume — GPW small caps are illiquid.

## 5. Walk-forward out-of-sample only
- Optimize on an in-sample window, evaluate on the next out-of-sample window, then roll forward.
- In-sample metrics are NOT evidence. Report metrics on OOS data only.
- Benchmark = WIG / WIG20TR (total return), never SPY.
- Fewer parameters is better — every tuned knob inflates the backtest and degrades live results.

## 6. Self-check before finishing
- [ ] Could any feature read data with `as_of_date > T`? If yes -> fix.
- [ ] Does the universe include delisted tickers? If no -> fix.
- [ ] Any LLM call in the sizing/risk/execution path? If yes -> remove.
- [ ] Do fills account for spread + commission + slippage + volume cap?
- [ ] Are reported metrics out-of-sample, vs WIG20TR?
- [ ] Is there a unit test asserting no look-ahead?

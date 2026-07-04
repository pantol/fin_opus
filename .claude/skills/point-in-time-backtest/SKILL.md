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
- **Next-open timing**: signals decide on day T's close (EOD data), execution simulates the OPEN of T+lag with lag >= 1 — never the signal bar. A fill lag < 1 must raise. Fill-bar anomalies (missing open, missing bar) are recorded, never silent.

## 4b. Point-in-time universe (index membership)
- When the strategy universe is an index, membership for date T comes from the `index_membership` table AS OF T (revision dates from GPW announcements), never today's composition. Former members keep their historical ranges.
- Membership gates NEW entries only; exits on held positions always evaluate.

## 4c. Corporate actions
- Splits/dividends/rights issues live in `corporate_actions` keyed by ex-date. A price gap explained by an action is NOT a market move: re-base held positions, stops, and in-flight orders on the ex-date instead of letting the ATR stop fire.
- The adjusted price series is DERIVED from raw bars + actions and stored separately (`adjusted=1`); raw and adjusted are never mixed in one series.

## 5. Walk-forward out-of-sample only
- Optimize on an in-sample window, evaluate on the next out-of-sample window, then roll forward.
- **Purge with an embargo**: leave >= the longest feature lookback (252 sessions) between each train window and its test window (`walk_forward.embargo_sessions`), or OOS features silently read train data.
- In-sample metrics are NOT evidence. Report metrics on OOS data only.
- Benchmark = WIG / WIG20TR (total return), never SPY.
- Fewer parameters is better — every tuned knob inflates the backtest and degrades live results.

## 5b. Anti-luck discipline (multiple testing)
- EVERY backtested strategy/parameter set is a trial in `strategy_trials`; never bypass the registry.
- Report the Deflated Sharpe Ratio alongside raw Sharpe: raw Sharpe without the trial count is not evidence.
- A strategy must also beat cost-matched RANDOMNESS (random-entry Monte Carlo percentile), not just the index.
- Acceptance gates require BOTH OOS improvement AND the configured DSR/percentile floors.

## 6. Self-check before finishing
- [ ] Could any feature read data with `as_of_date > T`? If yes -> fix.
- [ ] Does the universe include delisted tickers? If no -> fix.
- [ ] Any LLM call in the sizing/risk/execution path? If yes -> remove.
- [ ] Do fills account for spread + commission + slippage + volume cap?
- [ ] Could any fill derive from the signal bar (lag < 1, same-bar reference)? If yes -> fix.
- [ ] Could a corporate-action gap fire a stop or distort accounting as if it were a market move?
- [ ] If the universe is an index: is membership resolved as of T, not today?
- [ ] Are reported metrics out-of-sample, vs WIG20TR?
- [ ] Is there a unit test asserting no look-ahead?

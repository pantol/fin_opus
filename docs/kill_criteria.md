# Kill criteria

> Written BEFORE seeing results, so future-you cannot quietly move the
> goalposts. Filling this in is a human commitment, not code — the system only
> keeps the file visible. Update the *decisions log* below when a criterion
> fires; never edit a criterion after its clock has started.

## Strategy: trend_momentum

- **Forward paper window:** from ____-__-__ to ____-__-__ (minimum N months: __)
- **Kill if** OOS total return underperforms WIG20TR by more than ____ pp over the window
- **Kill if** DSR stays below ____ after the window
- **Kill if** random-entry Sharpe percentile stays below ____
- **Action on kill:** ______________________ (cut / pivot to ____ / halve risk budget)

## LLM feature gate (trend_momentum_llm vs baseline)

- **Evaluation window:** ____ weeks of collected filings (RSS has no backfill)
- **Kill if** the A/B gate (deltas + DSR + percentile, `make ab`) fails on ____
  consecutive monthly evaluations
- **Action on kill:** drop the llm_score gate; keep the collector running

## Decisions log (append-only)

| date | criterion | verdict | action taken |
|------|-----------|---------|--------------|
|      |           |         |              |

"""LLM FEATURES layer (Phase 2).

The LLM is ALWAYS only an INPUT to the deterministic risk layer (CLAUDE.md
rule 1). Nothing in this package computes money, sizing, or risk. It reads TEXT
(filings) and emits validated JSON; deterministic code maps that JSON to a single
numeric feature consumed by the existing strategy/risk engine.
"""

"""MarketScalper V4 — the validated strategy layer.

Replaces the V1/V2/V3 strategy engines, all of which were refuted by the research
programme (see docs/V4/ARCHITECTURE.md §0). Reuses the proven infrastructure:
candle store, ChartService, DB, feed, paper-trading engine.

Design guarantees, inherited from the research engine that produced the evidence:
  * no lookahead        - a signal uses only bars closed at or before its decision time
  * fees on both sides  - always charged, in the reported R
  * funding charged     - on the holding period
  * real exits only     - a horizon exit is a market close paying fees, not a mark
  * honest losses       - a gap through the stop is charged in full, never floored
  * deterministic       - same candles in, same setups out
"""

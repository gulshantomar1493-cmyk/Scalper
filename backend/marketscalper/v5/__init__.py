"""V5 — the price-action engine.

Replaces the V4 strategy layer after it resolved 11 live trades as 11 stop-outs.
Keeps every piece of V4 plumbing that was never the problem: the candle store,
ChartService, outcome accounting, persistence, alerts and the paper engine.

See docs/V5/ARCHITECTURE.md for the failure analysis this design answers.
"""

"""MarketScalper V3 — the Virtual Trader engine (scratch rebuild).

Philosophy: MarketScalper never predicts. It observes. It maps. It waits.
It reacts. It recommends.  (docs/V3/ARCHITECTURE.md · docs/V3/DOMAIN-MODEL.md)

This package is fully independent of the legacy analytical core (engines/,
setup_engine, htf) — it reuses only infrastructure (ChartService candles,
config, API, frontend). Closed candles only; deterministic folds; every domain
object self-explains via an append-only history.
"""

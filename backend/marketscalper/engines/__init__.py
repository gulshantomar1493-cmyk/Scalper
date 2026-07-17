"""Analysis & decision engines (Phases P1-P4).

Structure, Trendline, Liquidity, SmartMoney, Volume (+ shared momentum
utilities), Strategy, Qualification, Planning, recommendation lifecycle.

Provider-blind by rule: consume normalized events/StateStore only.
No raw Binance/Delta JSON, no imports from concrete provider modules
(CI-enforced from P0.19).
"""

"""MarketScalper — deterministic market analysis & decision-support terminal.

Generates trade recommendations from closed-candle analysis. Never executes
trades: the owner executes manually on their exchange and logs outcomes.

Architecture: docs/Architecture.md (FROZEN v1.2)
Roadmap:      docs/IMPLEMENTATION_PLAN.md (v2.0)

Package name `marketscalper` is FINAL — stable from day one, do not rename.
"""

# Feeds engine_version stamping (roadmap A19: short git hash + this constant).
__version__ = "0.1.0"

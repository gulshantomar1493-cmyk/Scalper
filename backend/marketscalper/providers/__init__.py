"""Feed providers — the ONLY package that knows concrete data sources.

FeedProvider interface + normalized dataclasses land here in P0.9;
BinanceFeed (P0.10), ReplayFeed (P0.24), optional DeltaFeed (P6, public
market data only).

Boundary rule (enforced by CI import check, P0.19): engines, strategies,
planner and journal NEVER import anything from this package's concrete
modules — they consume normalized EventBus events and the StateStore only.
"""

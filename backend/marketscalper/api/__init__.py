"""FastAPI application — WebSocket push + REST (P0.21+).

Pushes {candle, state_diff, signal, recommendation} diffs to the browser
terminal. Single-user token auth. No order endpoints — the platform never
executes trades (frozen v1.2 scope).
"""

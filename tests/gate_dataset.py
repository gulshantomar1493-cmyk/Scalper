"""Deterministic gate datasets (roadmap P2.23 + P2.24-A).

Episode-based candle generator for the automated Phase-2 validation
gate. Each episode is a hand-designed ~50-bar journey engineered to make
the Phase-2 object families fire densely: an EQL pool (two equal lows),
a BULLISH establishment, a sweep of the pool, a CHOCH within the
3-candle shift window, a bearish relabeling into a displacement crash
(BEAR order block), and a recovery that breaks the block (breaker).
Episodes repeat with a deterministic base-price drift — pure arithmetic,
no RNG — so the gate verifies scores of independently-generated objects.

The generator does NOT promise specific events at specific bars (the
band rule and cross-episode pivot history make exact prediction
brittle); the gate's philosophy is the reverse: generate density, then
independently verify EVERY event the composition actually emitted
against the frozen definitions (see test_p2_gate.py). Minimum-count
floors in the gate prove the density materialized.

Validation-only module: never imported by production code.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from marketscalper.providers.base import Candle

UTC = timezone.utc
GATE_M0 = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)   # 00:00: day levels live

# One episode: relative (o, h, l, c) tuples. Phases annotated. Timing is
# tuned against the D10 band rule (empirically traced): the sweep+CHOCH
# strike lands within the short BULLISH window right after b21, the LH
# bounce peak sits >= 4 bars clear of the sweep/crash highs so its pivot
# window is not poisoned, and the displacement crash body (3.2) clears
# 1.2x the sweep-inflated ATR.
EPISODE = [
    # -- phase 1 (b0-12): EQL pool — two equal lows at 3.0
    (4.0, 5.0, 3.9, 4.9), (4.9, 4.5, 3.4, 3.5), (3.5, 4.0, 3.0, 3.9),
    (3.9, 4.5, 3.4, 4.4), (4.4, 5.0, 3.9, 4.9), (4.9, 5.5, 4.4, 5.4),
    (5.4, 6.0, 4.9, 5.9), (5.9, 5.0, 3.9, 4.0), (4.0, 4.5, 3.4, 3.6),
    (3.6, 4.0, 3.0, 3.9), (3.9, 4.5, 3.4, 4.4), (4.4, 5.0, 3.9, 4.9),
    (4.9, 5.5, 4.4, 5.4),
    # -- phase 2 (b13-21): rise to BULLISH (HH 8.0 conf b18, HL 5.5 conf
    #    b21 -> BULLISH from b21; H 9.5 at b21 confirms b24 as HH)
    (5.4, 6.0, 4.9, 5.9), (5.9, 7.0, 5.8, 6.9), (6.9, 8.0, 6.8, 7.9),
    (7.9, 7.5, 6.4, 6.5), (6.5, 7.0, 5.9, 6.1), (6.1, 6.5, 5.5, 6.4),
    (6.4, 7.5, 6.3, 7.4), (7.4, 8.5, 7.3, 8.4), (8.4, 9.5, 8.3, 9.4),
    # -- phase 3 (b22-23): the strike inside the BULLISH window
    (9.4, 9.45, 2.2, 6.8),         # sweep of EQL 3.0 (wick 4.6/7.25 = 63%)
    (6.8, 6.9, 4.0, 4.2),          # close 4.2 < HL 5.5 -> CHOCH (+shift)
    # -- phase 4 (b24-31): bearish relabel (LH peak b27, LL 2.2 conf b25)
    (4.2, 4.6, 4.1, 4.5), (4.5, 5.0, 4.4, 4.9), (4.9, 5.3, 4.6, 5.2),
    (5.2, 5.4, 4.8, 5.35), (5.35, 5.35, 4.6, 4.7), (4.7, 4.8, 4.0, 4.1),
    (4.1, 4.2, 3.4, 3.5),
    (3.5, 3.7, 3.1, 3.6),          # green: the BEAR-OB source [3.5, 3.7]
    # -- phase 5 (b32-35): displacement crash -> BOS DOWN + BEAR OB
    (3.6, 3.6, 0.2, 0.4),          # body 3.2, close < LL 2.2
    (0.4, 0.9, 0.1, 0.8), (0.8, 1.2, 0.4, 0.6), (0.6, 1.0, 0.3, 0.9),
    # -- phase 6 (b36-41): recovery breaks the OB (close 3.8 > 3.7)
    (0.9, 1.9, 0.8, 1.8), (1.8, 2.9, 1.7, 2.8), (2.8, 3.9, 2.7, 3.8),
    (3.8, 5.0, 3.7, 4.9), (4.9, 6.0, 4.8, 5.9), (5.9, 6.5, 5.4, 6.4),
    # -- phase 7 (b42-47): drift pad into the next episode
    (6.4, 6.8, 5.9, 6.2), (6.2, 6.6, 5.7, 6.0), (6.0, 6.4, 5.5, 5.8),
    (5.8, 6.2, 5.3, 5.6), (5.6, 6.0, 5.1, 5.4), (5.4, 5.8, 4.9, 5.2),
]

EPISODE_LEN = len(EPISODE)
EPISODE_DRIFT = 2.5                 # base-price shift per episode


def _candle(symbol: str, minute: int, o: float, h: float, l: float,
            c: float, m0: datetime) -> Candle:
    return Candle(symbol=symbol, tf="1m", ts=m0 + timedelta(minutes=minute),
                  o=o, h=max(o, h, c), l=min(o, l, c), c=c,
                  v=1.0, qv=o, n_trades=2, taker_buy_v=0.5)


def gate_candles(episodes: int, symbol: str = "BTCUSDT",
                 base: float = 100.0, m0: datetime = GATE_M0) -> list:
    """`episodes` chained episodes, contiguous 1m candles, drifting base."""
    out = []
    minute = 0
    for e in range(episodes):
        b = base + e * EPISODE_DRIFT
        for (o, h, l, c) in EPISODE:
            out.append(_candle(symbol, minute, b + o, b + h, b + l, b + c, m0))
            minute += 1
    return out

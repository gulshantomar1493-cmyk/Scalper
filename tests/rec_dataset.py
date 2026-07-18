"""Recommendation dataset — the V4 determinism stream (roadmap P3.20).

The V3 `s1_dataset` fires an S1 signal but lands at score 52.5
(BELOW_THRESHOLD), so it never produces a recommendation. This dataset
is that same engineered S1 reversal PLUS the two ingredients that carry
it to a TRADEABLE verdict and a real recommendation row, so the
recommendation projection + the persisted `recommendations` row appear
with genuine content inside the hashed determinism stream (the P2.23
"no structurally-guarded-only family" principle, extended to P3.18's
recommendations):

1. An **early EQL pool** at 97.00 (two equal lows late in the ASIA
   session, minutes 470/476) that is swept by the SAME final wick as the
   ASIA_L level. Because the pool's member pivots sit far from the
   signal-region band and the CHOCH fires only two bars after the sweep
   (before the 96.40 low confirms as a pivot), the trend stays BEARISH
   on the signal bar (Structure 80) while the qualification's
   pool-sweep +40 lifts Liquidity 50 -> 90.

2. A **20-day RVOL seed** (`rec_seed`), the same seeding the live
   composition and F2 replay apply, so the signal bar carries rvol >= 1.5
   -> Volume 70 (participation +40, VWAP side +20, no warning +10; the
   green reversal bar's delta does not align with the BEARISH trend, so
   the +30 delta item stays off — the recorded S1 consequence, D21.8).

Empirically verified through the real composition (probe): the signal
bar scores 79.0 {structure 80, liquidity 90, volume 70, momentum 70} ->
TRADEABLE -> one S1 LONG recommendation (entry ~97.48, SL ~96.30,
TP1 = the EQH pool ~104.53, net RR ~5.47). The whole construction is
pure arithmetic (no RNG); replay ≡ replay is the property under test.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from marketscalper.providers.base import Candle

UTC = timezone.utc
REC_M0 = datetime(2026, 7, 21, 0, 0, tzinfo=UTC)
_SEED_DAYS = 20                                    # RVOL window (D19.1)
_SEED_VOL = 0.6                                    # median -> stream rvol 1.667


def _tri(x: float, period: float = 14) -> float:
    """Deterministic triangle wave in [0, 1]."""
    x = x % period
    half = period / 2
    return x / half if x <= half else 2 - x / half


def _bars() -> list:
    out = []

    # ---- 0-479: ASIA warmup + the exact 97.00 dip (ASIA_L) + the early
    # EQL pool (two equal lows at 97.00, m=470/476) — see the module doc.
    def f(m):
        return 99.4 + 0.0028 * m + 0.35 * _tri(m)

    POOL_A, POOL_B = 470, 476
    for m in range(480):
        if 180 <= m <= 200:
            if m < 189:                        # descend f(179) -> 97.06
                a, b = f(179), 97.06
                v0 = a + (b - a) * (m - 179) / 10
                v1 = a + (b - a) * (m - 178) / 10
                o, c = v0, v1
                l = min(o, c) - 0.05
            elif m == 189:                     # the ASIA_L bar: low 97.00
                o, c, l = 97.29, 97.06, 97.00
            else:                              # recover 97.06 -> f(201)
                a, b = 97.06, f(201)
                v0 = a + (b - a) * (m - 190) / 11
                v1 = a + (b - a) * (m - 189) / 11
                o, c = v0, v1
                l = min(o, c) - 0.05
            out.append((o, max(o, c) + 0.05, l, c))
        elif m in (POOL_A, POOL_B):            # sharp equal-low dip -> pool
            out.append((f(m), f(m) + 0.05, 97.00, f(m) - 0.02))
        elif m in (POOL_A + 1, POOL_B + 1):    # strict bounce (pivot window)
            out.append((f(m), f(m) + 0.20, f(m) - 0.02, f(m) + 0.18))
        else:
            o, c = f(m), f(m + 1)
            out.append((o, max(o, c) + 0.05, min(o, c) - 0.05, c))

    # ---- rally with two pullbacks (14 bars)
    p = f(480)
    for d in (+0.4, +0.4, +0.4, -0.25, +0.4, +0.4, +0.4, -0.25,
              +0.35, +0.35, +0.3, +0.25, +0.2, +0.15):
        o = p
        c = p + d
        if d > 0:
            out.append((o, c + 0.05, o - 0.05, c))
        else:
            out.append((o, o + 0.04, c - 0.06, c))
        p = c

    # ---- double top relative to the rally end (10 bars, EQH pool = TP1)
    t = p
    out.extend([
        (t, t + 0.04, t - 0.12, t - 0.06),
        (t - 0.06, t + 0.02, t - 0.14, t - 0.04),
        (t - 0.04, t + 0.10, t - 0.10, t + 0.02),     # H1 = t+0.10
        (t + 0.02, t + 0.05, t - 0.20, t - 0.15),
        (t - 0.15, t - 0.05, t - 0.30, t - 0.22),
        (t - 0.22, t - 0.10, t - 0.32, t - 0.14),
        (t - 0.14, t + 0.08, t - 0.22, t - 0.02),     # H2 = t+0.08
        (t - 0.02, t + 0.03, t - 0.25, t - 0.18),
        (t - 0.18, t - 0.08, t - 0.35, t - 0.28),
        (t - 0.28, t - 0.18, t - 0.45, t - 0.38),
    ])
    p = t - 0.38

    # ---- staircase: 9 cycles (5 reds -0.22, 2 greens +0.23/+0.22)
    for _cyc in range(9):
        for _ in range(5):
            o = p
            c = o - 0.22
            out.append((o, o + 0.08, c - 0.10, c))
            p = c
        for d in (0.23, 0.22):
            o = p
            c = o + d
            out.append((o, c + 0.02, o - 0.06, c))
            p = c

    # ---- final leg: 4 reds, 2 greens (lastH), flush, sweep, recovery, C
    for _ in range(4):
        o = p
        c = o - 0.20
        out.append((o, o + 0.06, c - 0.09, c))
        p = c
    out.append((p, p + 0.24, p - 0.06, p + 0.22))      # g1
    p = p + 0.22
    out.append((p, p + 0.27, p - 0.06, p + 0.23))      # g2: high = lastH
    p = p + 0.23
    last_top = p + 0.04
    for d in (0.26, 0.26, 0.26):                       # flush, above 97.00
        o = p
        c = o - d
        out.append((o, o + 0.01, c - 0.04, c))
        p = c
    o = p                                              # the sweep bar
    out.append((o, o + 0.02, 96.40, o + 0.02))
    p = o + 0.02
    out.append((p, p + 0.50, p - 0.02, p + 0.48))      # recovery green
    p = p + 0.48
    c_close = last_top + 0.05                           # the CHOCH bar
    c_low = c_close - 0.10
    out.append((c_low + 0.02, c_close + 0.03, c_low, c_close))
    p = c_close

    # ---- pads to a 5m boundary plus one extra full window
    while len(out) % 5:
        out.append((p, p + 0.05, p - 0.05, p - 0.02))
        p = p - 0.02
    for _ in range(5):
        out.append((p, p + 0.05, p - 0.05, p + 0.01))
        p = p + 0.01
    return out


def rec_candles(symbol: str = "BTCUSDT") -> list[Candle]:
    result = []
    for m, (o, h, l, c) in enumerate(_bars()):
        h = max(o, h, l, c)
        l = min(o, h, l, c)
        result.append(Candle(symbol=symbol, tf="1m",
                             ts=REC_M0 + timedelta(minutes=m),
                             o=o, h=h, l=l, c=c, v=1.0, qv=o,
                             n_trades=2, taker_buy_v=0.5))
    return result


def rec_seed(symbol: str = "BTCUSDT") -> list[Candle]:
    """The 20-day RVOL bucket seed (D19.2) preceding the stream — flat
    low volume so every minute-of-day bucket fills and the stream's
    v=1.0 bars read rvol ~1.667. Pure/deterministic (replay ≡ live)."""
    out = []
    for day in range(_SEED_DAYS):
        base = REC_M0 - timedelta(days=_SEED_DAYS - day)
        for minute in range(1440):
            out.append(Candle(symbol=symbol, tf="1m",
                             ts=base + timedelta(minutes=minute),
                             o=100.0, h=100.1, l=99.9, c=100.0,
                             v=_SEED_VOL, qv=100.0, n_trades=1,
                             taker_buy_v=_SEED_VOL / 2))
    return out


REC_MINUTES = len(_bars())

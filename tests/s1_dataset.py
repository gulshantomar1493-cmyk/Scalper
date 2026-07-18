"""S1 reversal dataset — the V3 determinism stream (P3.12-P3.16 / P3.20).

One engineered, fully deterministic day-fragment (no RNG) whose closing
minutes legitimately fire strategy S1 through the complete frozen engine
chain — the only launch strategy that can fire on an unseeded run (D20.6:
S2/S3 confirms require rvol).

The story (empirically tuned through the real composition wiring):

  00:00-07:59  full ASIA session: drift-oscillation (extremes spaced
               beyond the 0.1*ATR EQ tolerance -> no accidental pools)
               with one exact dip to 97.00 (~03:00) -> at 08:00 the D12
               rollover promotes ASIA_L = 97.00 (the sweep target) and
               the dip's 5m pivot is the A8 external low (the discount
               range floor).
  08:00-08:13  14-bar rally to ~104.4 with two pullbacks (HL pivots keep
               the label chains sane for the later descent).
  08:14-08:23  double top +0.10/+0.08 above the rally end (two confirmed
               H pivots 0.02 apart -> EQH pool = the S1 take-profit).
  08:24-09:26  staircase descent: 9 cycles of 5 reds (-0.22) + 2 greens
               (+0.23/+0.22). The greens make every pre-bounce low a
               confirmed LL and every bounce top a confirmed LH (k=3
               windows engineered), the leading edge keeps bodies outside
               the D10 band -> trend BEARISH all the way down.
  then         final leg: 4 reds to ~97.41, 2 greens to the CHOCH target
               (lastH high ~97.90), a 3-red flush that stays above the
               97.00 level, the SWEEP bar (wick to 96.40 through ASIA_L,
               97% lower wick, close back above), a recovery green, and the
               CHOCH bar: a tight gap-up bar whose low is the FVG top
               and whose close breaks the 97.90 LH under BEARISH ->
               CHOCH UP -> SweepShift (+2) -> confluence zone (FVG
               anchor + ASIA_L/EQL members) in discount -> S1 LONG.

Bar count is padded to a 5m boundary so the ReplayFeed fold publishes
every 5m window (F1 completeness).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from marketscalper.providers.base import Candle

UTC = timezone.utc
S1_M0 = datetime(2026, 7, 21, 0, 0, tzinfo=UTC)


def _tri(x: float, period: float = 14) -> float:
    """Deterministic triangle wave in [0, 1]."""
    x = x % period
    half = period / 2
    return x / half if x <= half else 2 - x / half


def _bars() -> list:
    out = []

    # ---- 0-479: ASIA warmup + the exact 97.00 dip
    def f(m):
        return 99.4 + 0.0028 * m + 0.35 * _tri(m)

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

    # ---- double top relative to the rally end (10 bars, EQH pool)
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
    c_close = last_top + 0.05                          # the CHOCH bar
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


def s1_candles(symbol: str = "BTCUSDT") -> list[Candle]:
    result = []
    for m, (o, h, l, c) in enumerate(_bars()):
        h = max(o, h, l, c)
        l = min(o, h, l, c)
        result.append(Candle(symbol=symbol, tf="1m",
                             ts=S1_M0 + timedelta(minutes=m),
                             o=o, h=h, l=l, c=c, v=1.0, qv=o,
                             n_trades=2, taker_buy_v=0.5))
    return result


S1_MINUTES = len(_bars())

"""Strategy Engine (Architecture §5; Decision D20; roadmap P3.12–P3.16).

The three launch strategies as pure per-closed-candle folds over frozen
engine outputs (consumed, never recomputed): S1 Liquidity Sweep Reversal
(armed by the frozen SweepShift event), S2 Trend Pullback Continuation
(A15 impulse = last confirmed opposite swing → BOS close), S3 Trendline
Fake-Break Trap (armed by the frozen FAKE_BREAK event). Every §5 clause
and pinned boundary lives in D20; every unavailable input fails its
condition — signals are never emitted on incomplete evidence (D7).

Emits immutable Signal records; persistence is P3.18's, planning is the
D17 planner's (this engine's signals are its recorded first consumer).
Pure fold: no clock, no randomness; replay ≡ live (§0 rule 2).

COMPLETE and FROZEN (roadmap P3.12–P3.16; three-agent freeze audit
2026-07-19 — two conformance blockers fixed pre-freeze, accepted
readings recorded in the D20 addenda). Do not modify without a new
decision record.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime

from marketscalper.engines.confluence import (
    CONFLUENCE_BAND_ATR_RATIO,
)
from marketscalper.engines.liquidity import SweepShift
from marketscalper.engines.momentum import IncrementalATR
from marketscalper.engines.trendline import FAKE_BREAK_WINDOW
from marketscalper.providers.base import Candle

# D1 stamp component: bump on ANY logic/threshold change here.
ENGINE_VERSION = 1

# Frozen §5/D20 literals — module constants, not config.
SL_BUFFER_ATR_RATIO = 0.25         # §5: beyond wick/extreme + 0.25×ATR
S2_DEPTH_MIN = 0.30                # §5: pullback depth 30–70%, inclusive
S2_DEPTH_MAX = 0.70
S2_BODY_ATR_RATIO = 0.8            # §5: confirm body > 0.8×ATR (strict)
S2_RVOL_MIN = 1.2                  # §5: confirm rvol >= 1.2 (inclusive)
S2_TP2_EXTENSION = 1.618           # §5: fib extension
S3_RVOL_MIN = 1.5                  # §5: confirm rvol >= 1.5 (inclusive)
S3_EXTREME_WINDOW = FAKE_BREAK_WINDOW + 1   # watch span + event bar (D20.4)
INVALID_AFTER_BARS = 5             # §5 S1 INVALID; template default (D20)


@dataclass(frozen=True)
class Signal:
    """One strategy signal (immutable; §5 outputs + §8 fact trace)."""

    strategy: str                  # 'S1' | 'S2' | 'S3'
    direction: str                 # 'LONG' | 'SHORT'
    entry: float
    sl: float
    tp1: float
    tp2: float | None
    created_ts: datetime
    facts: tuple                   # deterministic rule-trace strings
    invalid_after_bars: int = INVALID_AFTER_BARS


class StrategyEngine:
    """§5 for one symbol's 1m stream (cadence per D20.5 — last consumer)."""

    __slots__ = ("_symbol", "_atr", "_bar", "_recent", "_choch_dirs",
                 "_last_h", "_last_l", "_ext_h", "_ext_l",
                 "_impulse")

    def __init__(self, symbol: str, atr: IncrementalATR) -> None:
        self._symbol = symbol
        self._atr = atr
        self._bar = -1
        self._recent: deque = deque(maxlen=8)      # ts-addressable candles
        self._choch_dirs: deque = deque(maxlen=8)  # (ts, direction) — S1
        self._last_h: float | None = None          # last confirmed 1m pivots
        self._last_l: float | None = None
        self._ext_h: float | None = None           # A8 external 5m swings
        self._ext_l: float | None = None
        self._impulse: dict | None = None          # S2 state (D20.3)

    # ------------------------------------------------------------ intakes

    def on_pivot(self, pivot) -> None:
        """Confirmed labeled 1m pivot (S2 impulse legs, S3 targets)."""
        if pivot.kind == "H":
            self._last_h = pivot.price
        else:
            self._last_l = pivot.price

    def on_external_pivot(self, pivot) -> None:
        """Confirmed labeled 5m pivot (A8 — the S1 TP2 target)."""
        if pivot.kind == "H":
            self._ext_h = pivot.price
        else:
            self._ext_l = pivot.price

    # --------------------------------------------------------------- fold

    def evaluate(self, candle: Candle, *, trend_5m, bos_event,
                 choch_event, tl_events, liq_events, zones, blocks, gaps,
                 pools, levels, premium_discount, session_vwap,
                 rvol) -> list:
        """One closed 1m candle after every other engine (D20.5)."""
        self._bar += 1
        self._recent.append(candle)
        if choch_event is not None:                # ts-addressable directions
            self._choch_dirs.append((choch_event.ts, choch_event.direction))
        signals: list[Signal] = []

        s1 = self._s1(candle, trend_5m, liq_events, zones, pools,
                      premium_discount)
        if s1 is not None:
            signals.append(s1)
        s2 = self._s2(candle, trend_5m, bos_event, blocks, gaps,
                      tl_events, session_vwap, rvol)
        if s2 is not None:
            signals.append(s2)
        s3 = self._s3(candle, tl_events, pools, levels, rvol)
        if s3 is not None:
            signals.append(s3)
        return signals

    # ------------------------------------------------- S1 (D20.2)

    def _s1(self, candle, trend_5m, liq_events, zones, pools,
            premium_discount):
        atr = self._atr.value
        if atr is None:
            return None
        for event in liq_events:
            if not isinstance(event, SweepShift):
                continue
            long = event.sweep.side == "LOW"
            direction = "LONG" if long else "SHORT"
            # Reversal check: the paired CHOCH must break in the trade
            # direction (LOW sweep -> CHOCH UP). D12.5 pairing is
            # direction-blind by frozen design; a sweep followed by a
            # CHOCH continuing the break is not the §5 reversal story.
            choch_dir = next((d for ts, d in self._choch_dirs
                              if ts == event.choch_ts), None)
            if choch_dir != ("UP" if long else "DOWN"):
                continue
            # CONTEXT: 5m trend alignment OR A8 extreme alignment
            trend_ok = trend_5m == ("BULLISH" if long else "BEARISH")
            extreme_ok = premium_discount == ("discount" if long
                                              else "premium")
            if not (trend_ok or extreme_ok):
                continue
            # CONFIRM(b): confluence zone >= 2 near the CHOCH close
            want = "BULL" if long else "BEAR"
            band = CONFLUENCE_BAND_ATR_RATIO * atr
            zone = next(
                (z for z in zones if z.count >= 2 and z.direction == want
                 and _gap(z.lo, z.hi, candle.c) <= band), None)
            if zone is None:
                continue
            sweep_candle = next(
                (c for c in self._recent if c.ts == event.sweep.ts), None)
            if sweep_candle is None:
                continue
            entry = (zone.lo + zone.hi) / 2.0
            sl = (sweep_candle.l - SL_BUFFER_ATR_RATIO * atr if long
                  else sweep_candle.h + SL_BUFFER_ATR_RATIO * atr)
            r = entry - sl if long else sl - entry
            if r <= 0:
                continue
            tp1 = self._nearest_opposing_pool(pools, entry, long)
            if tp1 is None or (tp1 < entry + r if long else tp1 > entry - r):
                continue                           # §5: 1R minimum / no pool
            ext = self._ext_h if long else self._ext_l
            tp2 = ext if ext is not None and (
                ext > tp1 if long else ext < tp1) else None
            ctx = "5m trend" if trend_ok else "A8 extreme"
            return Signal(
                "S1", direction, entry, sl, tp1, tp2, candle.ts,
                (f"swept {event.sweep.target} ({event.sweep.side})",
                 "CHOCH within 3 candles (sweep+shift)",
                 f"entry zone confluence {zone.count}",
                 f"context: {ctx}"))
        return None

    @staticmethod
    def _nearest_opposing_pool(pools, entry, long):
        want = "EQH" if long else "EQL"
        prices = [p.price for p in pools if p.kind == want and
                  (p.price > entry if long else p.price < entry)]
        if not prices:
            return None
        return min(prices) if long else max(prices)

    # ------------------------------------------------- S2 (D20.3)

    def _s2(self, candle, trend_5m, bos_event, blocks, gaps,
            tl_events, session_vwap, rvol):
        atr = self._atr.value
        # impulse arming: a with-trend 1m BOS (frozen BosDetector only
        # fires with-trend) whose leg has positive span (A15)
        if bos_event is not None:
            long = bos_event.direction == "UP"
            start = self._last_l if long else self._last_h
            if self._impulse is not None and \
                    self._impulse["long"] != long:
                self._impulse = None               # opposite BOS cancels
            if start is not None and (
                    bos_event.close > start if long
                    else bos_event.close < start):
                self._impulse = {
                    "long": long, "lo": min(start, bos_event.close),
                    "hi": max(start, bos_event.close),
                    "rvol": rvol,
                    "extreme": None, "setup_bar": None, "depth": None}
            return None                            # never confirm on BOS bar
        imp = self._impulse
        if imp is None:
            return None
        long = imp["long"]
        rng = imp["hi"] - imp["lo"]
        # cancellation: full retrace beyond the impulse start
        if (candle.c < imp["lo"] if long else candle.c > imp["hi"]):
            self._impulse = None
            return None
        # pullback extreme tracking (bars after the BOS bar)
        ext = candle.l if long else candle.h
        if imp["extreme"] is None or (
                ext < imp["extreme"] if long else ext > imp["extreme"]):
            imp["extreme"] = ext
        # CONFIRM (strictly after the setup bar)
        if imp["setup_bar"] is not None and self._bar > imp["setup_bar"]:
            body = abs(candle.c - candle.o)
            if ((candle.c > candle.o if long else candle.c < candle.o)
                    and atr is not None and body > S2_BODY_ATR_RATIO * atr
                    and rvol is not None and rvol >= S2_RVOL_MIN):
                entry = candle.c
                sl = (imp["extreme"] - SL_BUFFER_ATR_RATIO * atr if long
                      else imp["extreme"] + SL_BUFFER_ATR_RATIO * atr)
                r = entry - sl if long else sl - entry
                tp1 = imp["hi"] if long else imp["lo"]
                self._impulse = None               # one signal per impulse
                if r <= 0 or (tp1 < entry + r if long
                              else tp1 > entry - r):
                    return None                    # §5: 1R minimum
                tp2 = (imp["extreme"] + S2_TP2_EXTENSION * rng if long
                       else imp["extreme"] - S2_TP2_EXTENSION * rng)
                if (tp2 <= tp1 if long else tp2 >= tp1):
                    tp2 = None
                return Signal(
                    "S2", "LONG" if long else "SHORT", entry, sl, tp1,
                    tp2, candle.ts,
                    ("with-trend BOS impulse (A15)",
                     f"pullback depth {imp['depth']:.2f}",
                     "pullback into zone on declining rvol",
                     f"confirm body > 0.8xATR, rvol {rvol:.2f}"))
        # SETUP: 5m context (D20.3 — the 1m alignment is already
        # guaranteed by the with-trend arming BOS) + zone touch + depth
        # + declining rvol
        if imp["setup_bar"] is None:
            trend_ok = trend_5m == ("BULLISH" if long else "BEARISH")
            vwap_ok = (session_vwap is not None and
                       (candle.c > session_vwap if long
                        else candle.c < session_vwap))
            depth = ((imp["hi"] - imp["extreme"]) / rng if long
                     else (imp["extreme"] - imp["lo"]) / rng)
            depth_ok = S2_DEPTH_MIN <= depth <= S2_DEPTH_MAX
            rvol_ok = (rvol is not None and imp["rvol"] is not None
                       and rvol < imp["rvol"])
            if (trend_ok and vwap_ok and depth_ok and rvol_ok
                    and self._s2_zone_touch(candle, long, blocks, gaps,
                                            tl_events, session_vwap)):
                imp["setup_bar"] = self._bar
                imp["depth"] = depth               # the depth the rule saw
        return None

    @staticmethod
    def _s2_zone_touch(candle, long, blocks, gaps, tl_events,
                       session_vwap):
        want = "BULL" if long else "BEAR"
        for b in blocks:
            if b.status == "active" and b.direction == want and \
                    candle.l <= b.zone_hi and candle.h >= b.zone_lo:
                return True
        for g in gaps:
            if g.direction == want and \
                    candle.l <= g.hi and candle.h >= g.lo:
                return True
        if session_vwap is not None and \
                candle.l <= session_vwap <= candle.h:
            return True
        return any(e.kind == "TOUCH" for e in tl_events)

    # ------------------------------------------------- S3 (D20.4)

    def _s3(self, candle, tl_events, pools, levels, rvol):
        atr = self._atr.value
        if atr is None or rvol is None or rvol < S3_RVOL_MIN:
            return None
        for event in tl_events:
            if event.kind != "FAKE_BREAK":
                continue
            long = event.side == "support"
            window = list(self._recent)[-S3_EXTREME_WINDOW:]
            extreme = (min(c.l for c in window) if long
                       else max(c.h for c in window))
            entry = candle.c
            sl = (extreme - SL_BUFFER_ATR_RATIO * atr if long
                  else extreme + SL_BUFFER_ATR_RATIO * atr)
            r = entry - sl if long else sl - entry
            if r <= 0:
                continue
            tp1 = self._last_h if long else self._last_l
            if tp1 is None or (tp1 < entry + r if long
                               else tp1 > entry - r):
                continue                           # §5: 1R minimum / no swing
            barrier = [p.price for p in pools] + list(levels.values())
            if any(entry < p < entry + r if long else entry - r < p < entry
                   for p in barrier):
                continue                           # §5: opposing level in 1R
            return Signal(
                "S3", "LONG" if long else "SHORT", entry, sl, tp1, None,
                candle.ts,
                (f"fake break of validated {event.side} line",
                 f"re-entry close, rvol {rvol:.2f}",
                 "no opposing level within 1R"))
        return None


def _gap(lo: float, hi: float, price: float) -> float:
    return max(0.0, max(lo, price) - min(hi, price))

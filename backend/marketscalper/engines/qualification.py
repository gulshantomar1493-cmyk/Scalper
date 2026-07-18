"""Trade Qualification Engine — COMPLETE and FROZEN (engine-wise freeze
after the D16 conformance audit; Architecture §6; Decision D16 incl. its
freeze-audit record; roadmap P3.2–P3.11, decisions P3.1/P3.6 folded per
D16). Modify only on a genuine production defect.

Stage 1 hard gates G1–G6 (any fail → NO_SIGNAL, score never shown) +
Stage 2 weighted score 0.30×Structure + 0.30×Liquidity + 0.25×Volume +
0.15×Momentum with the D16.3 rubric. Direction/entry/SL/invalidation are
S1–S3 + planner outputs (P3.12–P3.17) — NOT produced here (D16.1).

Flagged placeholders (each recorded in D16.2/D16.3, self-healing when the
owning task lands): G1 clock arm without a provider (replay), G2 without a
BookTicker (replay), G3 until strategies (P3.12), G4 until events.yaml,
G5 until the journal (P4), G6 until the planner (P3.17), the Liquidity
R-distance item (P3.17), the ENTIRE Volume component = 0 until the Volume
Engine (P2.1–P2.7) — max achievable score is 72.0 until those land
(0.30×100 + 0.30×90 + 0.15×100), so TRADEABLE/A_PLUS are mathematically
unreachable this delivery: a recorded, conservative consequence of the
frozen weights, not a formula change.

Pure fold over frozen-engine outputs — recomputes nothing; replay and
live produce identical results for identical input streams (§0 rule 2).
No persistence — signals rows are P3.18's (D16.5).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import timedelta

from marketscalper.engines.confluence import (
    CONFLUENCE_BAND_ATR_RATIO,
    ConfluenceZone,
)
from marketscalper.engines.liquidity import SweepEvent, session_of
from marketscalper.engines.momentum import (
    IncrementalATR,
    MomentumState,
    RegimeClassifier,
)
from marketscalper.engines.structure import TrendState
from marketscalper.providers.base import Candle

# Frozen §6 literals — module constants, not config.
GAP_WINDOW_CANDLES = 30                # G1: no gap in last 30 candles
# G1 clock boundary is enforced by the frozen D6 sampler surface
# (in_sync = |offset| <= 2.0 s) — no constant duplicated here (D16.2).
SPREAD_LIMIT_PCT = 0.05                # G2: spread < 0.05 % (strict)
SCORE_TRADEABLE = 75.0                 # inclusive (§6 "≥ 75")
SCORE_A_PLUS = 85.0                    # inclusive (§6 "≥ 85")
WEIGHTS = {"structure": 0.30, "liquidity": 0.30,
           "volume": 0.25, "momentum": 0.15}

# D16.3 recency windows (bars, inclusive age ≤ W) — uncalibrated, P5-owned.
W_SWEEP = 20
W_STRUCT = 20
W_MOM = 5

_BOS_TREND = {"UP": "BULLISH", "DOWN": "BEARISH"}
# A CHOCH fires against the trend it broke: DOWN warns BULLISH, UP BEARISH.
_CHOCH_OPPOSES = {"DOWN": "BULLISH", "UP": "BEARISH"}


def spread_pct_of(bid_px: float, ask_px: float) -> float | None:
    """G2 input from a normalized BookTicker (P3.1 pin, D16.2):
    (ask − bid) / mid × 100. Degenerate books (mid ≤ 0) → None."""
    mid = (bid_px + ask_px) / 2.0
    if mid <= 0:
        return None
    return (ask_px - bid_px) / mid * 100.0


def verdict_of(score: float) -> str:
    """§6 thresholds, inclusive: ≥85 A_PLUS, ≥75 TRADEABLE."""
    if score >= SCORE_A_PLUS:
        return "A_PLUS"
    if score >= SCORE_TRADEABLE:
        return "TRADEABLE"
    return "BELOW_THRESHOLD"


@dataclass(frozen=True)
class GateResult:
    name: str                  # 'G1'..'G6'
    passed: bool
    flagged: bool              # placeholder pass (D16.2)
    detail: str


@dataclass(frozen=True)
class QualificationResult:
    gates: tuple               # six GateResults, G1..G6
    data_integrity: str        # 'PASS' | 'DEGRADED' (G1 AND G2)
    components: dict | None    # None on gate fail (§6: never shown)
    score: float | None        # None on gate fail
    verdict: str               # NO_SIGNAL|BELOW_THRESHOLD|TRADEABLE|A_PLUS
    aligned: int               # rubric items scoring > 0
    evaluable: int             # rubric items evaluable this delivery
    agreement: str             # A14: "{n} of {m} rules aligned"
    reasons: tuple             # deterministic rule-trace strings


class QualificationEngine:
    """§6 for one symbol's 1m stream (cadence per D16.5 — last engine)."""

    __slots__ = ("_symbol", "_atr", "_trend", "_momentum", "_regime",
                 "_bar", "_ts_window", "_bos_window", "_choch_window",
                 "_last_pool_sweep_bar", "_last_shift_bar",
                 "_last_tl_signal_bar", "_last_shift_flag_bar")

    def __init__(self, symbol: str, atr: IncrementalATR, trend: TrendState,
                 momentum: MomentumState, regime: RegimeClassifier) -> None:
        self._symbol = symbol
        self._atr = atr
        self._trend = trend
        self._momentum = momentum
        self._regime = regime
        self._bar = -1
        self._ts_window: deque = deque(maxlen=GAP_WINDOW_CANDLES)
        # Every (bar, direction) inside W_STRUCT — D16.3 quantifies over
        # the whole window (freeze-audit fix: last-event memory let a newer
        # non-agreeing event shadow an older in-window agreeing one)
        self._bos_window: deque = deque()
        self._choch_window: deque = deque()
        self._last_pool_sweep_bar: int | None = None
        self._last_shift_bar: int | None = None
        self._last_tl_signal_bar: int | None = None
        self._last_shift_flag_bar: int | None = None   # momentum shift

    def update(self, candle: Candle, *, bos_event, choch_event, tl_events,
               liq_events, zones: list[ConfluenceZone],
               spread_pct: float | None,
               clock: tuple | None) -> QualificationResult:
        """Fold one closed 1m candle after every other engine (D16.5).

        clock: None = no sampler wired (replay/tests, flagged pass) or
        (offset_s, in_sync) from the D6 sampler surface.
        """
        self._bar += 1
        cur = self._bar
        self._ts_window.append(candle.ts)

        # -------- recency bookkeeping (pure fold, D16.3 windows)
        if bos_event is not None:
            self._bos_window.append((cur, bos_event.direction))
        if choch_event is not None:
            self._choch_window.append((cur, choch_event.direction))
        for window in (self._bos_window, self._choch_window):
            while window and cur - window[0][0] > W_STRUCT:
                window.popleft()
        for event in liq_events:
            if isinstance(event, SweepEvent):
                if event.target in ("EQH", "EQL"):
                    self._last_pool_sweep_bar = cur
            else:                                  # SweepShift
                self._last_shift_bar = cur
        for event in tl_events:
            if event.kind in ("TOUCH", "FAKE_BREAK"):
                self._last_tl_signal_bar = cur
        if self._momentum.momentum_shift:
            self._last_shift_flag_bar = cur

        gates = self._gates(candle, spread_pct, clock)
        integrity = ("PASS" if gates[0].passed and gates[1].passed
                     else "DEGRADED")

        if not all(g.passed for g in gates):
            reasons = tuple(f"✗ {g.name}: {g.detail}"
                            for g in gates if not g.passed)
            return QualificationResult(
                gates=tuple(gates), data_integrity=integrity,
                components=None, score=None, verdict="NO_SIGNAL",
                aligned=0, evaluable=0,
                agreement="gates failed — no score", reasons=reasons)

        components, aligned, evaluable, reasons = self._score(candle, zones)
        score = sum(WEIGHTS[name] * value
                    for name, value in components.items())
        verdict = verdict_of(score)
        return QualificationResult(
            gates=tuple(gates), data_integrity=integrity,
            components=components, score=score, verdict=verdict,
            aligned=aligned, evaluable=evaluable,
            agreement=f"{aligned} of {evaluable} rules aligned",
            reasons=tuple(reasons))

    # ------------------------------------------------------------ stage 1

    def _gates(self, candle: Candle, spread_pct, clock) -> list[GateResult]:
        gates: list[GateResult] = []

        # G1 — feed live (per-close evaluation) + 30-candle continuity
        #      + clock sync (D6 surface; no provider → flagged pass)
        window = list(self._ts_window)
        if len(window) < GAP_WINDOW_CANDLES:
            g1, flagged = False, False
            detail = f"warming: {len(window)}/{GAP_WINDOW_CANDLES} candles"
        else:
            contiguous = all(
                (window[i + 1] - window[i]) == timedelta(minutes=1)
                for i in range(len(window) - 1))
            if not contiguous:
                g1, flagged, detail = False, False, "gap in last 30 candles"
            elif clock is None:
                g1, flagged = True, True
                detail = "contiguous; clock unmeasured (no sampler)"
            else:
                offset_s, in_sync = clock
                g1, flagged = bool(in_sync), False
                detail = (f"contiguous; clock offset {offset_s}s"
                          if offset_s is not None
                          else "contiguous; clock offset unknown")
        gates.append(GateResult("G1", g1, flagged, detail))

        # G2 — spread < 0.05% strict; no BookTicker → flagged pass
        if spread_pct is None:
            gates.append(GateResult(
                "G2", True, True, "no book ticker (replay)"))
        else:
            gates.append(GateResult(
                "G2", spread_pct < SPREAD_LIMIT_PCT, False,
                f"spread {spread_pct:.4f}%"))

        # G3–G6 — flagged placeholder passes (owners recorded in D16.2)
        gates.append(GateResult(
            "G3", True, True,
            f"session {session_of(candle.ts.hour)}; no strategy filter yet"))
        gates.append(GateResult("G4", True, True, "no events calendar yet"))
        gates.append(GateResult("G5", True, True, "no journal yet"))
        gates.append(GateResult("G6", True, True, "no trade plan yet"))
        return gates

    # ------------------------------------------------------------ stage 2

    def _score(self, candle: Candle, zones):
        cur = self._bar
        atr = self._atr.value
        trend = self._trend.state
        reasons: list[str] = []

        def recent(bar: int | None, window: int) -> bool:
            return bar is not None and cur - bar <= window

        # Structure (4 items, D16.3)
        structure = 0.0
        if trend in ("BULLISH", "BEARISH"):
            structure += 30.0
            reasons.append(f"✓ established trend {trend}"
                           " (+30 structure)")
        if any(_BOS_TREND[direction] == trend
               for _bar, direction in self._bos_window):
            structure += 30.0
            reasons.append("✓ recent with-trend BOS (+30 structure)")
        if trend in ("BULLISH", "BEARISH"):
            opposing = any(_CHOCH_OPPOSES[direction] == trend
                           for _bar, direction in self._choch_window)
            if not opposing:
                structure += 20.0
                reasons.append("✓ no recent opposing CHOCH"
                               " (+20 structure)")
        if recent(self._last_tl_signal_bar, W_STRUCT):
            structure += 20.0
            reasons.append("✓ validated-trendline interaction"
                           " (+20 structure)")

        # Liquidity (3 evaluable items; R-distance flagged 0 until P3.17)
        liquidity = 0.0
        if recent(self._last_pool_sweep_bar, W_SWEEP):
            liquidity += 40.0
            reasons.append("✓ sweep of multi-touch pool"
                           " (+40 liquidity)")
        if recent(self._last_shift_bar, W_SWEEP):
            liquidity += 30.0
            reasons.append("✓ sweep with CHOCH confirm"
                           " (+30 liquidity)")
        if atr is not None:
            band = CONFLUENCE_BAND_ATR_RATIO * atr
            near = any(
                zone.count >= 2
                and max(0.0, max(zone.lo, candle.c) - min(zone.hi, candle.c))
                <= band
                for zone in zones)
            if near:
                liquidity += 20.0
                reasons.append("✓ entry zone confluence ≥2 objects"
                               " (+20 liquidity)")

        # Volume — component absent (flagged, D16.3): 0.0
        volume = 0.0

        # Momentum (3 items)
        momentum = 0.0
        regime = self._regime.regime
        if regime == "expansion":
            momentum += 40.0
            reasons.append("✓ regime expansion (+40 momentum)")
        elif regime == "normal":
            momentum += 20.0
            reasons.append("✓ regime normal (+20 momentum)")
        if recent(self._last_shift_flag_bar, W_MOM):
            momentum += 30.0
            reasons.append("✓ momentum shift (+30 momentum)")
        body = self._momentum.body_dominance
        if body is not None and body > 0.5:
            momentum += 30.0
            reasons.append("✓ body dominance > 0.5 (+30 momentum)")

        aligned = len(reasons)
        evaluable = 10                     # D16.4: 4 + 3 + 0 + 3
        components = {"structure": structure, "liquidity": liquidity,
                      "volume": volume, "momentum": momentum}
        return components, aligned, evaluable, reasons

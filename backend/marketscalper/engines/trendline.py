"""Trendline Engine — candidate detection (roadmap P1.13; Architecture §4.3
Steps 1–2 + the frozen score formula; every rule per Decision D11).

Discovers candidate trendlines from confirmed 1m pivots (D11.2: the 1m
stream only — 5m pivots serve A8 external structure elsewhere):

    Step 1  last N=12 confirmed same-kind pivots per side; every ordered
            pair (a older, b newer) is a candidate line in LOG-price space
            on a POSITIONAL bar axis (D11.3/D11.4); direction filter —
            support slope > 0, resistance slope < 0 (strict; slope-zero is
            the Liquidity Engine's EQH/EQL territory); no close may cut
            the anchor segment (D11.6).
    Step 2  touches from the older anchor to the current bar, one per
            candle: |ln(extreme) - y(t)| <= 0.15*ATR(t)/close(t) with the
            close not strictly crossing the line; ANCHORS COUNT (D11.7);
            candidates need >= 3 touches.
    Score   touches*2 + span_bars/20 - bars_since_last_touch/100 (D11.8).

P1.15 adds the kept-line set on top: greedy dedup + the 3+3 cap
(TrendlineBook), the active/archived lifecycle (staleness + eviction,
both terminal) and the persistence capability (line_to_row; R1: no
wiring in Phase 1). Break episodes ('broken', Step 4) and channels
(Step 5) are P1.16/P1.17.

Cadence per closed 1m candle (established engine order): ATR update ->
pivot detection/labeling -> on_pivot fan-out -> update(candle).
candidates() is an on-demand pure function of the fold state — the
candidate SET changes only with pivots (D11.6) while touches/age evolve
per bar. Deterministic: no clock, no randomness; identical stream ->
identical candidate lists (replay-safe, no repaint — only confirmed
pivots and closed candles are ever consulted).

Memory is bounded: candle history is pruned to the oldest bar still
referenced by the two 12-pivot windows.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import log

from marketscalper.engines.momentum import IncrementalATR
from marketscalper.engines.structure import Pivot
from marketscalper.providers.base import Candle

# Frozen §4.3/D11 literals — module constants, not config.
N_PIVOTS = 12
TOUCH_TOLERANCE_ATR_RATIO = 0.15
MIN_TOUCHES = 3
SCORE_TOUCH_WEIGHT = 2.0
SCORE_SPAN_DIVISOR = 20.0
SCORE_AGE_DIVISOR = 100.0
DEDUP_SLOPE_REL = 0.10                 # near-parallel slope threshold
DEDUP_INTERCEPT_ATR_RATIO = 0.3        # current-bar log-value threshold
CAP_PER_SIDE = 3                       # kept lines: 3 support + 3 resistance
ARCHIVE_AGE_BARS = 300                 # bars since last touch -> archived
BREAK_BODY_ATR_RATIO = 0.8             # BREAK quality: body > 0.8*ATR strict
FAKE_BREAK_WINDOW = 3                  # re-entry candles after a close-through
# FLAGGED PLACEHOLDER (roadmap P1.16 / D11.9): the RVOL >= 1.5 condition of
# a qualifying BREAK evaluates True until P2.2 swaps in the real RVOL and
# re-runs determinism. Do not add volume logic here.
RVOL_PLACEHOLDER_PASSES = True


@dataclass(frozen=True)
class TrendlineCandidate:
    """One valid candidate line (>=3 touches), D11 geometry.

    y(t) = intercept + slope * (t - a_index), everything in log-price
    space over positional bar indices; intercept = ln(a_pivot.price).
    """

    side: str            # 'support' | 'resistance'
    a_index: int         # older anchor bar index
    b_index: int         # newer anchor bar index
    a_pivot: Pivot
    b_pivot: Pivot
    slope: float         # log-price per bar (D11.4)
    intercept: float     # ln(a_pivot.price)
    touches: int         # anchors included (D11.7)
    last_touch_index: int
    score: float         # D11.8


class TrendlineDetector:
    """§4.3 Step 1–2 candidate discovery for the 1m stream (one instance)."""

    __slots__ = ("_atr", "_bars", "_offset", "_next", "_ts_index",
                 "_h_pivots", "_l_pivots")

    def __init__(self, atr: IncrementalATR) -> None:
        self._atr = atr
        self._bars: list[tuple] = []     # (ts, low, high, close, atr_value)
        self._offset = 0                 # absolute index of _bars[0]
        self._next = 0                   # next bar index to assign
        self._ts_index: dict = {}        # candle ts -> absolute bar index
        self._h_pivots: list[tuple[int, Pivot]] = []   # (bar index, pivot)
        self._l_pivots: list[tuple[int, Pivot]] = []

    # ------------------------------------------------------------- intake

    def update(self, candle: Candle) -> None:
        """Fold one closed candle in (ATR already updated for it)."""
        self._ts_index[candle.ts] = self._next
        self._bars.append(
            (candle.ts, candle.l, candle.h, candle.c, self._atr.value))
        self._next += 1

    def on_pivot(self, pivot: Pivot) -> None:
        """Track a confirmed pivot; slides its kind's 12-window (D11.6)."""
        entry = (self._ts_index[pivot.ts], pivot)
        window = self._h_pivots if pivot.kind == "H" else self._l_pivots
        window.append(entry)
        if len(window) > N_PIVOTS:
            window.pop(0)
        self._prune()

    def _prune(self) -> None:
        """Drop history older than the oldest windowed pivot (bounded memory)."""
        needed = min(w[0][0] for w in (self._h_pivots, self._l_pivots) if w)
        while self._offset < needed:
            ts = self._bars.pop(0)[0]
            del self._ts_index[ts]
            self._offset += 1

    # --------------------------------------------------------- candidates

    def candidates(self) -> list[TrendlineCandidate]:
        """All valid candidates, deterministically ordered:
        score desc, then newer b, newer a, side. Empty while ATR unwarm
        (the engine is inactive until warm, D11.5)."""
        if self._atr.value is None or self._next == 0:
            return []
        cur = self._next - 1
        out: list[TrendlineCandidate] = []
        for side, window in (("support", self._l_pivots),
                             ("resistance", self._h_pivots)):
            for i in range(len(window)):
                for j in range(i + 1, len(window)):
                    cand = self._evaluate(side, window[i], window[j], cur)
                    if cand is not None:
                        out.append(cand)
        out.sort(key=lambda c: (-c.score, -c.b_index, -c.a_index, c.side))
        return out

    def _evaluate(self, side, a_entry, b_entry, cur):
        a_idx, a_pivot = a_entry
        b_idx, b_pivot = b_entry
        slope = (log(b_pivot.price) - log(a_pivot.price)) / (b_idx - a_idx)
        if side == "support":
            if not slope > 0:                      # D11.6: strictly ascending
                return None
        elif not slope < 0:                        # strictly descending
            return None
        intercept = log(a_pivot.price)

        support = side == "support"
        # D11.6 validity: no close strictly beyond the line BETWEEN anchors
        for t in range(a_idx + 1, b_idx):
            y = intercept + slope * (t - a_idx)
            close = self._bars[t - self._offset][3]
            log_close = log(close)
            if (log_close < y) if support else (log_close > y):
                return None

        # D11.7 touches: older anchor .. current bar, one per candle
        touches = 0
        last_touch = a_idx
        for t in range(a_idx, cur + 1):
            ts, low, high, close, atr = self._bars[t - self._offset]
            if atr is None:
                continue                           # bar unusable (unwarm)
            y = intercept + slope * (t - a_idx)
            log_close = log(close)
            if (log_close < y) if support else (log_close > y):
                continue                           # close crossed: no touch
            extreme = low if support else high
            tol = TOUCH_TOLERANCE_ATR_RATIO * atr / close
            if abs(log(extreme) - y) <= tol:
                touches += 1
                last_touch = t
        if touches < MIN_TOUCHES:
            return None

        span = b_idx - a_idx
        score = (SCORE_TOUCH_WEIGHT * touches + span / SCORE_SPAN_DIVISOR
                 - (cur - last_touch) / SCORE_AGE_DIVISOR)
        return TrendlineCandidate(side, a_idx, b_idx, a_pivot, b_pivot,
                                  slope, intercept, touches, last_touch, score)


@dataclass
class KeptTrendline:
    """One line in the kept set (roadmap P1.15; D11 Step 3 + lifecycle).

    Statuses: 'active' -> 'archived' (300-bar staleness or cap/dedup
    eviction; terminal) — 'broken' exists in the D11 vocabulary but its
    trigger is the P1.16 break episode, not implemented here. Once kept, a
    line lives by lifecycle: the book maintains its touches/age itself,
    independent of the detector's 12-pivot candidate window (D11 edge case).
    """

    side: str
    a_index: int
    b_index: int
    a_pivot: Pivot
    b_pivot: Pivot
    slope: float
    intercept: float
    touches: int
    last_touch_index: int
    accepted_index: int
    accepted_ts: object            # datetime — acceptance bar identity
    status: str = "active"
    watch_remaining: int | None = None   # open break watch (D11.9), else None
    watch_qualified: bool = False        # breaking candle met body+RVOL rule


@dataclass(frozen=True)
class TrendlineEvent:
    """One frozen trendline event (D11.9): TOUCH | BREAK | FAKE_BREAK."""

    kind: str
    side: str
    a_index: int
    b_index: int
    bar_index: int
    ts: object                     # emitting candle identity (datetime)
    close: float


@dataclass
class FlipCandidate:
    """Role-flip candidate (D11.10): the broken line's geometry re-registered
    on the opposite side. Direction-exempt; inherits NOTHING — touches
    restart at 0 from the break bar and it must earn >= 3 to be kept."""

    side: str                      # the flipped side
    a_index: int
    b_index: int
    a_pivot: Pivot
    b_pivot: Pivot
    slope: float
    intercept: float
    created_index: int
    touches: int = 0
    last_touch_index: int = -1


def line_to_row(line: KeptTrendline) -> dict:
    """KeptTrendline -> keyword arguments of db.insert_level (P0.7), per
    D11.1/A4. Channels are never persisted (D11.10) — no channel type
    exists in this engine, and the levels.kind vocabulary has no CHANNEL."""
    return {
        "symbol": line.a_pivot.symbol, "tf": line.a_pivot.tf,
        "kind": "TRENDLINE", "p1": line.a_pivot.price,
        "p2": line.b_pivot.price, "t1": line.a_pivot.ts,
        "t2": line.b_pivot.ts, "slope": line.slope,
        "created_ts": line.accepted_ts,
    }


class TrendlineBook:
    """The kept-line set: dedup, 3+3 cap, and lifecycle (roadmap P1.15;
    D11 Steps 3 + 5's archive rules).

    Cadence per closed 1m candle (after atr.update and detector.update):
    refresh(candle) — updates kept-line touches per D11.7, archives stale
    lines (>= 300 bars since last touch), then re-selects the kept set:
    kept lines (re-scored from their own maintained state) compete with
    fresh detector candidates; greedy best-first dedup (near-parallel iff
    slope delta < 10% relative AND current-bar log values differ by less
    than 0.3*ATR/close), cap 3 per side; ordering key: score desc, newer
    b, newer a, side. Any previously kept line that loses selection is
    archived. Archive is TERMINAL: an archived geometry key can never be
    re-accepted. Persistence stays capability-only per R1 (line_to_row +
    the existing P0.7 helpers; no pool anywhere in Phase 1).
    """

    __slots__ = ("_atr", "_detector", "_active", "_archived_keys",
                 "_broken_keys", "_flips", "_cur")

    def __init__(self, detector: TrendlineDetector, atr: IncrementalATR) -> None:
        self._detector = detector
        self._atr = atr
        self._active: list[KeptTrendline] = []
        self._archived_keys: set[tuple] = set()
        self._broken_keys: set[tuple] = set()
        self._flips: list[FlipCandidate] = []
        self._cur = -1

    @property
    def active(self) -> list[KeptTrendline]:
        """Kept lines in selection order (score desc at last refresh)."""
        return list(self._active)

    @property
    def archived_keys(self) -> frozenset:
        """(side, a_index, b_index) of every archived line — terminal."""
        return frozenset(self._archived_keys)

    @property
    def broken_keys(self) -> frozenset:
        """(side, a_index, b_index) of every broken line — terminal."""
        return frozenset(self._broken_keys)

    @property
    def flip_candidates(self) -> list[FlipCandidate]:
        """Role-flip candidates still earning their >= 3 touches."""
        return list(self._flips)

    def refresh(self, candle: Candle) -> list[TrendlineEvent]:
        """Fold one closed candle in; returns this bar's events, in the
        deterministic per-line iteration order (the stored selection order).
        At most one event per line per bar — no duplicates by construction."""
        self._cur += 1
        cur = self._cur
        atr = self._atr.value
        events: list[TrendlineEvent] = []

        if atr is not None:
            log_close = log(candle.c)
            tol = TOUCH_TOLERANCE_ATR_RATIO * atr / candle.c

            # 1. break episodes + touch maintenance (D11.9/D11.7), per line
            for line in self._active:
                support = line.side == "support"
                y = line.intercept + line.slope * (cur - line.a_index)
                beyond = (log_close < y) if support else (log_close > y)
                if line.watch_remaining is not None:      # open watch
                    if not beyond:                        # re-entry: trap
                        line.watch_remaining = None
                        line.watch_qualified = False
                        events.append(self._event("FAKE_BREAK", line, cur, candle))
                        continue                          # touches resume next bar
                    line.watch_remaining -= 1
                    if line.watch_remaining == 0:         # confirmed break
                        line.status = "broken"
                        if line.watch_qualified:
                            events.append(self._event("BREAK", line, cur, candle))
                    continue
                if beyond:                                # open a new watch
                    line.watch_remaining = FAKE_BREAK_WINDOW
                    body = abs(candle.c - candle.o)
                    line.watch_qualified = (
                        body > BREAK_BODY_ATR_RATIO * atr
                        and RVOL_PLACEHOLDER_PASSES)
                    continue
                extreme = candle.l if support else candle.h
                if abs(log(extreme) - y) <= tol:
                    line.touches += 1
                    line.last_touch_index = cur
                    events.append(self._event("TOUCH", line, cur, candle))

            # 2. remove broken lines (terminal) + create their role flips
            survivors = []
            for line in self._active:
                if line.status == "broken":
                    self._broken_keys.add(
                        (line.side, line.a_index, line.b_index))
                    flip_side = ("resistance" if line.side == "support"
                                 else "support")
                    self._flips.append(FlipCandidate(
                        flip_side, line.a_index, line.b_index, line.a_pivot,
                        line.b_pivot, line.slope, line.intercept, cur))
                else:
                    survivors.append(line)
            self._active = survivors

            # 3. flip candidates earn touches (incl. their creation bar)
            for flip in self._flips:
                support = flip.side == "support"
                y = flip.intercept + flip.slope * (cur - flip.a_index)
                if (log_close < y) if support else (log_close > y):
                    continue
                extreme = candle.l if support else candle.h
                if abs(log(extreme) - y) <= tol:
                    flip.touches += 1
                    flip.last_touch_index = cur

        # 4. staleness archive (terminal)
        survivors = []
        for line in self._active:
            if cur - line.last_touch_index >= ARCHIVE_AGE_BARS:
                self._archive(line)
            else:
                survivors.append(line)
        self._active = survivors

        # 5. re-selection (dedup + cap); engine inactive while ATR unwarm
        if atr is None:
            return events
        kept_keys = {(l.side, l.a_index, l.b_index) for l in self._active}
        entries = []
        for line in self._active:
            entries.append((self._line_score(line, cur), line.b_index,
                            line.a_index, line.side, line, None))
        for cand in self._detector.candidates():
            key = (cand.side, cand.a_index, cand.b_index)
            if (key in kept_keys or key in self._archived_keys
                    or key in self._broken_keys):
                continue
            entries.append((cand.score, cand.b_index, cand.a_index,
                            cand.side, None, cand))
        for flip in self._flips:                   # eligible flips compete
            if flip.touches < MIN_TOUCHES:
                continue
            key = (flip.side, flip.a_index, flip.b_index)
            if (key in kept_keys or key in self._archived_keys
                    or key in self._broken_keys):
                continue
            score = (SCORE_TOUCH_WEIGHT * flip.touches
                     + (flip.b_index - flip.a_index) / SCORE_SPAN_DIVISOR
                     - (cur - flip.last_touch_index) / SCORE_AGE_DIVISOR)
            entries.append((score, flip.b_index, flip.a_index, flip.side,
                            None, flip))
        entries.sort(key=lambda e: (-e[0], -e[1], -e[2], e[3]))

        chosen: dict[str, list] = {"support": [], "resistance": []}
        geoms: dict[str, list] = {"support": [], "resistance": []}
        for score, _b, _a, side, line, other in entries:
            bucket = chosen[side]
            if len(bucket) == CAP_PER_SIDE:
                continue
            slope = line.slope if line is not None else other.slope
            a_index = line.a_index if line is not None else other.a_index
            intercept = (line.intercept if line is not None
                         else other.intercept)
            y = intercept + slope * (cur - a_index)
            if any(abs(slope - s2) < DEDUP_SLOPE_REL * max(abs(slope), abs(s2))
                   and abs(y - y2) < DEDUP_INTERCEPT_ATR_RATIO * atr / candle.c
                   for s2, y2 in geoms[side]):
                continue                           # clustered: best already in
            bucket.append((line, other))
            geoms[side].append((slope, y))

        new_active = []
        selected_lines = {id(line) for side_bucket in chosen.values()
                          for line, _o in side_bucket if line is not None}
        for line in self._active:                  # evictions are terminal
            if id(line) not in selected_lines:
                self._archive(line)
        for side in ("support", "resistance"):
            for line, other in chosen[side]:
                if line is None:
                    if isinstance(other, FlipCandidate):
                        self._flips.remove(other)  # promoted: no inheritance
                        line = KeptTrendline(
                            other.side, other.a_index, other.b_index,
                            other.a_pivot, other.b_pivot, other.slope,
                            other.intercept, other.touches,
                            other.last_touch_index, cur, candle.ts)
                    else:
                        line = KeptTrendline(
                            other.side, other.a_index, other.b_index,
                            other.a_pivot, other.b_pivot, other.slope,
                            other.intercept, other.touches,
                            other.last_touch_index, cur, candle.ts)
                new_active.append(line)
        new_active.sort(key=lambda l: (-self._line_score(l, cur),
                                       -l.b_index, -l.a_index, l.side))
        self._active = new_active
        return events

    def _line_score(self, line: KeptTrendline, cur: int) -> float:
        return (SCORE_TOUCH_WEIGHT * line.touches
                + (line.b_index - line.a_index) / SCORE_SPAN_DIVISOR
                - (cur - line.last_touch_index) / SCORE_AGE_DIVISOR)

    @staticmethod
    def _event(kind: str, line: KeptTrendline, cur: int,
               candle: Candle) -> TrendlineEvent:
        return TrendlineEvent(kind, line.side, line.a_index, line.b_index,
                              cur, candle.ts, candle.c)

    def _archive(self, line: KeptTrendline) -> None:
        line.status = "archived"
        self._archived_keys.add((line.side, line.a_index, line.b_index))

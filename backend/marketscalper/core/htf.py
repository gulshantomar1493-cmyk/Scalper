"""Higher-Timeframe (HTF) analysis — ISOLATED, additive, DISPLAY-only.

HTF V1.1: a full Smart-Money-Concepts read of 15m / 1h / 4h / 1d by REUSING the
frozen analysis engines on aggregated candles. This layer is entirely isolated
from the canonical 1m/5m decision engine and the §10 determinism stream (D26.3):

  * it NEVER publishes on the engine EventBus (so it never enters the hashed
    Candle stream),
  * it NEVER writes into the `structure` payload / `store.set_structure` (so it
    never enters the hashed object stream),
  * it NEVER persists a candle or an analysis row.

It is surfaced only through a SEPARATE read path (GET /api/htf), exactly like the
ChartService read-model. The frozen engines are instantiated here and driven by
direct method calls that mirror the production `main.py` `step()` cadence; not one
frozen file is modified. `PivotDetector` borrows the k=2 "5m" confirmation depth
(its K_BY_TF gate only knows 1m/5m and `Pivot.tf` is cosmetic to everything this
module reads), which gives a uniform 5-bar swing definition across HTF tfs.

Execution stays 1m/5m: HTF only adds context, bias and confidence.
"""

from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timedelta, timezone

from marketscalper.core import indicators as ind
from marketscalper.engines.liquidity import LiquidityEngine, SweepEvent
from marketscalper.engines.momentum import IncrementalATR, MomentumState
from marketscalper.engines.orderblock import OrderBlockEngine
from marketscalper.engines.structure import (
    BosDetector,
    ChochDetector,
    PivotDetector,
    PivotLabeler,
    TrendState,
)
from marketscalper.engines.trendline import TrendlineBook, TrendlineDetector
from marketscalper.providers.base import Candle

# The four approved HTF timeframes, top-down (the market-story order).
HTF_TIMEFRAMES = ("1d", "4h", "1h", "15m")
TF_LABEL = {"15m": "15M", "1h": "1H", "4h": "4H", "1d": "Daily"}

# Recent window per tf: enough to warm the engines (ATR 14 / trend 20 / trendline
# 12 pivots) and establish the CURRENT structure without re-folding all history.
LOOKBACK_BARS = 500
_MIN_BARS = 30                    # below this a tf is "insufficient data"
_PIVOT_DEPTH_TF = "5m"            # borrow k=2 (K_BY_TF is 1m/5m-only; Pivot.tf cosmetic here)
_SR_WINDOW = 60                   # fallback support/resistance extreme window
_MAX_POOLS = 6
_MAX_ZONES = 4                    # supply / demand zones surfaced per side

# Higher timeframes carry more weight in the overall roll-up.
_TF_WEIGHT = {"15m": 1.0, "1h": 2.0, "4h": 3.0, "1d": 4.0}
# Max magnitude of the per-tf signed score: trend 3 + ema 2 + bos 1.5 + choch 1
# + momentum 0.5 + demand/supply 1 + ema200 0.5.
_SCORE_MAX = 9.5
_BIAS_THRESHOLD = 1.0            # |signed| below this reads NEUTRAL


def _to_candle(symbol: str, tf: str, d: dict) -> Candle:
    """An aggregated ChartService candle dict -> a normalized Candle. qv /
    n_trades / taker_buy_v are not read by any engine this module drives."""
    return Candle(
        symbol=symbol, tf=tf, ts=datetime.fromisoformat(d["ts"]),
        o=d["o"], h=d["h"], l=d["l"], c=d["c"], v=d["v"],
        qv=0.0, n_trades=0, taker_buy_v=0.0,
    )


class _Pipeline:
    """One symbol+tf analysis pass. Mirrors the frozen `main.py` step() cadence
    (analysis-only: no volume / qualification / strategy, no 5m external pivots,
    no bus, no persistence). Feed closed candles in order, then read a snapshot."""

    def __init__(self, symbol: str) -> None:
        self._atr = IncrementalATR()
        self._momentum = MomentumState(self._atr)
        self._detector = PivotDetector(symbol, _PIVOT_DEPTH_TF)      # borrowed k=2
        self._labeler = PivotLabeler()
        self._trend = TrendState()
        self._bos = BosDetector(self._trend, self._atr)
        self._choch = ChochDetector(self._trend)
        self._tl_detector = TrendlineDetector(self._atr)
        self._book = TrendlineBook(self._tl_detector, self._atr)     # rvol None = legacy arm
        self._liq = LiquidityEngine(symbol, self._atr)               # rvol None = legacy arm
        self._ob = OrderBlockEngine(symbol)
        self.pivots: list = []
        self.bos_events: list = []
        self.choch_events: list = []
        self.sweeps: list = []
        self.index = -1

    def step(self, candle: Candle) -> None:
        self.index += 1
        self._atr.update(candle)
        self._momentum.update(candle)
        self._tl_detector.update(candle)
        for pivot in self._detector.update(candle):
            labeled = self._labeler.label(pivot)
            self.pivots.append(labeled)
            self._trend.on_pivot(labeled)
            self._bos.on_pivot(labeled)
            self._choch.on_pivot(labeled)
            self._tl_detector.on_pivot(labeled)
            self._liq.on_pivot(labeled)
        self._trend.update(candle)
        bos_event = self._bos.update(candle)
        if bos_event is not None:
            self._choch.on_bos(bos_event)
            self._ob.on_bos(bos_event)
            self.bos_events.append(bos_event)
        choch_event = self._choch.update(candle)
        if choch_event is not None:
            self.choch_events.append(choch_event)
        self._book.refresh(candle)
        if choch_event is not None:
            self._liq.on_choch(choch_event)
        for event in self._liq.update(candle):
            if isinstance(event, SweepEvent):
                self.sweeps.append(event)
        self._ob.update(candle)

    # -- reads -----------------------------------------------------------------
    @property
    def momentum(self) -> MomentumState:
        return self._momentum

    @property
    def pools(self):
        return self._liq.pools

    @property
    def blocks(self):
        return self._ob.blocks

    def trendlines(self) -> list[dict]:
        cur = self.index
        out = []
        for line in self._book.active:
            price = math.exp(line.intercept + line.slope * (cur - line.a_index))
            out.append({
                "side": line.side,                                   # support | resistance
                "price": round(price, 2),
                "touches": line.touches,
                "slope": "up" if line.slope > 0 else "down",
            })
        return out


def _structure_label(highs, lows) -> str:
    lh = highs[-1].label if highs else None
    ll = lows[-1].label if lows else None
    if lh and ll:
        return f"{lh} / {ll}"
    return "forming"


def _derived_trend(highs, lows, ema_align: str) -> str:
    """The DISPLAYED HTF trend, from market structure (primary) + EMA stack
    (fallback when structure is mixed). The frozen TrendState is a memoryless
    band classifier that reads RANGE on most closed HTF bars — it drives
    BOS/CHOCH internally but is not a useful displayed trend."""
    lh = highs[-1].label if highs else None
    ll = lows[-1].label if lows else None
    if lh == "HH" and ll == "HL":
        return "Uptrend"
    if lh == "LH" and ll == "LL":
        return "Downtrend"
    if ema_align in ("bullish", "mixed-up"):
        return "Uptrend"
    if ema_align in ("bearish", "mixed-down"):
        return "Downtrend"
    return "Range"


def _ema_alignment(closes: list[float]) -> tuple[str, float | None]:
    """(alignment, ema200) — 'bullish' 20>50>200, 'bearish' 20<50<200, else
    'mixed'; 'n/a' until the EMAs are warm."""
    e20, e50, e200 = ind.ema(closes, 20), ind.ema(closes, 50), ind.ema(closes, 200)
    a, b, c = e20[-1], e50[-1], e200[-1]
    if a is None or b is None:
        return "n/a", c
    if c is not None and a > b > c:
        return "bullish", c
    if c is not None and a < b < c:
        return "bearish", c
    if a > b:
        return "mixed-up", c
    if a < b:
        return "mixed-down", c
    return "mixed", c


def _momentum_view(mom: MomentumState) -> dict:
    v = mom.velocity
    direction = "flat" if v is None or abs(v) < 1e-12 else ("up" if v > 0 else "down")
    return {
        "velocity": v,
        "acceleration": mom.acceleration,
        "shift": mom.momentum_shift,
        "body_dominance": mom.body_dominance,
        "direction": direction,
    }


def analyze_timeframe(symbol: str, tf: str, candle_dicts: list[dict]) -> dict:
    """Full SMC analysis for one HTF timeframe. `candle_dicts` are aggregated
    ChartService candles (closed only), oldest-first. Never raises on short/empty
    input — returns a `ready=False` stub instead."""
    recent = candle_dicts[-LOOKBACK_BARS:] if candle_dicts else []
    if len(recent) < _MIN_BARS:
        return {"tf": tf, "ready": False, "reason": "insufficient data",
                "trend": None, "bias": "NEUTRAL", "score": 50.0}

    candles = [_to_candle(symbol, tf, d) for d in recent]
    pipe = _Pipeline(symbol)
    for candle in candles:
        pipe.step(candle)

    close = candles[-1].c
    highs = [p for p in pipe.pivots if p.kind == "H"]
    lows = [p for p in pipe.pivots if p.kind == "L"]
    sr = candles[-_SR_WINDOW:]
    support = min(c.l for c in sr)              # recent range floor / ceiling
    resistance = max(c.h for c in sr)

    closes = [c.c for c in candles]
    ema_align, ema200 = _ema_alignment(closes)
    trend = _derived_trend(highs, lows, ema_align)

    last_bos = pipe.bos_events[-1] if pipe.bos_events else None
    last_choch = pipe.choch_events[-1] if pipe.choch_events else None
    last_sweep = pipe.sweeps[-1] if pipe.sweeps else None

    supply = [{"lo": round(ob.zone_lo, 2), "hi": round(ob.zone_hi, 2), "status": ob.status}
              for ob in pipe.blocks if ob.direction == "BEAR"][-_MAX_ZONES:]
    demand = [{"lo": round(ob.zone_lo, 2), "hi": round(ob.zone_hi, 2), "status": ob.status}
              for ob in pipe.blocks if ob.direction == "BULL"][-_MAX_ZONES:]

    pools = sorted(pipe.pools, key=lambda p: p.strength, reverse=True)[:_MAX_POOLS]
    liquidity = [{"kind": p.kind, "price": round(p.price, 2), "size": p.size,
                  "strength": round(p.strength, 3)} for p in pools]

    momentum = _momentum_view(pipe.momentum)
    analysis = {
        "tf": tf,
        "ready": True,
        "trend": trend,
        "structure": _structure_label(highs, lows),
        "bos": ({"direction": last_bos.direction, "ts": last_bos.ts.isoformat(),
                 "close": last_bos.close} if last_bos else None),
        "choch": ({"direction": last_choch.direction, "ts": last_choch.ts.isoformat(),
                   "close": last_choch.close} if last_choch else None),
        "swing_high": ({"price": round(highs[-1].price, 2), "label": highs[-1].label,
                        "ts": highs[-1].ts.isoformat()} if highs else None),
        "swing_low": ({"price": round(lows[-1].price, 2), "label": lows[-1].label,
                       "ts": lows[-1].ts.isoformat()} if lows else None),
        "liquidity": liquidity,
        "liquidity_sweep": ({"side": last_sweep.side, "target": last_sweep.target,
                             "price": round(last_sweep.target_price, 2),
                             "ts": last_sweep.ts.isoformat()} if last_sweep else None),
        "supply": supply,
        "demand": demand,
        "support": round(support, 2),
        "resistance": round(resistance, 2),
        "trendlines": pipe.trendlines(),
        "ema_alignment": ema_align,
        "momentum": momentum,
    }
    bias, score, signed = _score_timeframe(analysis, close, ema200)
    analysis["bias"] = bias
    analysis["score"] = score
    analysis["_signed"] = signed        # internal, consumed by aggregate_htf then dropped
    return analysis


def _in_zone(price: float, zones: list[dict]) -> bool:
    return any(z["lo"] <= price <= z["hi"] for z in zones)


def _score_timeframe(a: dict, close: float, ema200: float | None) -> tuple[str, float, float]:
    """Signed directional score -> (bias, 0..100 score with 50 = neutral, signed).
    Weighted components (bullish +, bearish -), max magnitude _SCORE_MAX."""
    signed = 0.0
    if a["trend"] == "Uptrend":
        signed += 3.0
    elif a["trend"] == "Downtrend":
        signed -= 3.0
    if a["ema_alignment"] == "bullish":
        signed += 2.0
    elif a["ema_alignment"] == "bearish":
        signed -= 2.0
    elif a["ema_alignment"] == "mixed-up":
        signed += 0.5
    elif a["ema_alignment"] == "mixed-down":
        signed -= 0.5
    if a["bos"]:
        signed += 1.5 if a["bos"]["direction"] == "UP" else -1.5
    if a["choch"]:
        signed += 1.0 if a["choch"]["direction"] == "UP" else -1.0
    if a["momentum"]["direction"] == "up":
        signed += 0.5
    elif a["momentum"]["direction"] == "down":
        signed -= 0.5
    if _in_zone(close, a["demand"]):
        signed += 1.0
    if _in_zone(close, a["supply"]):
        signed -= 1.0
    if ema200 is not None:
        signed += 0.5 if close > ema200 else -0.5

    signed = max(-_SCORE_MAX, min(_SCORE_MAX, signed))
    score = round(max(0.0, min(100.0, 50.0 + signed / _SCORE_MAX * 50.0)), 1)
    if signed >= _BIAS_THRESHOLD:
        bias = "BULLISH"
    elif signed <= -_BIAS_THRESHOLD:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"
    return bias, score, signed


def _market_story(per_tf: dict, overall: dict) -> str:
    """A deterministic top-down narrative (Daily -> 15M)."""
    parts = [
        f"Higher-timeframe bias is {overall['bias']} "
        f"({overall['score']}/100, {overall['confidence']}% timeframe agreement)."
    ]
    for tf in HTF_TIMEFRAMES:                       # already Daily -> 15M
        a = per_tf.get(tf)
        if not a or not a.get("ready"):
            parts.append(f"{TF_LABEL[tf]} has insufficient history.")
            continue
        note = ""
        if a["choch"]:
            note = f", recent CHOCH {a['choch']['direction']}"
        elif a["bos"]:
            note = f", recent BOS {a['bos']['direction']}"
        parts.append(
            f"{TF_LABEL[tf]} {a['trend'].lower()}, {a['bias'].lower()} bias "
            f"({a['structure']}{note})."
        )
    return " ".join(parts)


def _explanation(per_tf: dict, overall: dict) -> str:
    ready = [tf for tf in HTF_TIMEFRAMES if per_tf.get(tf, {}).get("ready")]
    agree = [tf for tf in ready if per_tf[tf]["bias"] == overall["bias"]]
    conflict = [tf for tf in ready
                if per_tf[tf]["bias"] not in (overall["bias"], "NEUTRAL")]
    if overall["bias"] == "NEUTRAL":
        return ("Timeframes are mixed / offsetting: no clear higher-timeframe "
                "bias. Treat 1m/5m signals as range trades until the HTFs align.")
    lead = (f"{overall['bias']} bias led by "
            f"{', '.join(TF_LABEL[t] for t in agree) or 'no'} timeframe(s)")
    if conflict:
        return (f"{lead}; {', '.join(TF_LABEL[t] for t in conflict)} "
                "conflict(s). Take counter-HTF 1m/5m signals with caution.")
    return f"{lead}; no conflicting timeframe. With-bias 1m/5m signals are best supported."


def aggregate_htf(per_tf: dict) -> dict:
    """Overall HTF score / bias / confidence / market story / explanation from the
    per-timeframe analyses. `per_tf` is {tf: analyze_timeframe(...)}."""
    ready = {tf: a for tf, a in per_tf.items() if a.get("ready")}
    if not ready:
        overall = {"score": 50.0, "bias": "NEUTRAL", "confidence": 0}
        overall["market_story"] = "Not enough higher-timeframe history yet."
        overall["explanation"] = "HTF analysis warms up as candle history loads."
        return overall

    total_w = sum(_TF_WEIGHT[tf] for tf in ready)
    weighted_signed = sum(_TF_WEIGHT[tf] * a["_signed"] for tf, a in ready.items()) / total_w
    score = round(max(0.0, min(100.0, 50.0 + weighted_signed / _SCORE_MAX * 50.0)), 1)
    if weighted_signed >= _BIAS_THRESHOLD:
        bias = "BULLISH"
    elif weighted_signed <= -_BIAS_THRESHOLD:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"
    agree_w = sum(_TF_WEIGHT[tf] for tf, a in ready.items() if a["bias"] == bias)
    confidence = round(agree_w / total_w * 100)

    overall = {"score": score, "bias": bias, "confidence": confidence}
    overall["market_story"] = _market_story(per_tf, overall)
    overall["explanation"] = _explanation(per_tf, overall)
    return overall


def analyze(symbol: str, candles_by_tf: dict[str, list[dict]]) -> dict:
    """Full HTF result for a symbol: per-timeframe analyses + overall roll-up.
    `candles_by_tf` maps each HTF timeframe to its aggregated closed candles."""
    per_tf = {tf: analyze_timeframe(symbol, tf, candles_by_tf.get(tf, []))
              for tf in HTF_TIMEFRAMES}
    overall = aggregate_htf(per_tf)
    for a in per_tf.values():
        a.pop("_signed", None)          # internal weight, never surfaced
    return {"symbol": symbol, "timeframes": per_tf, "overall": overall}


# Fetch a little more than the analysis window per tf so the last LOOKBACK_BARS
# are dense (closed-bucket exclusion / partial edges).
_FETCH_BARS = LOOKBACK_BARS + 60
_TF_MINUTES = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440}


class HtfService:
    """Compute-on-read HTF read-model: fetch the aggregated HTF candles through
    the engine-isolated ChartService, run analyze(), and cache per symbol for a
    short TTL (the analysis only changes when a new HTF candle closes, >= 15 min).

    Isolated exactly like ChartService — it touches no EventBus, no persistence,
    and never the `structure` payload; so it cannot move the determinism hash.
    Only RECENT ranges are fetched (ending now), so ChartService never triggers a
    deep historical gap-fill."""

    def __init__(self, chart_service, ttl_seconds: float = 30.0) -> None:
        self._cs = chart_service
        self._ttl = ttl_seconds
        self._cache: dict[str, tuple[float, dict]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, symbol: str) -> asyncio.Lock:
        lock = self._locks.get(symbol)
        if lock is None:
            lock = self._locks[symbol] = asyncio.Lock()
        return lock

    async def analyze(self, symbol: str, now: datetime | None = None) -> dict:
        hit = self._cache.get(symbol)
        if hit is not None and time.monotonic() - hit[0] < self._ttl:
            return hit[1]
        async with self._lock(symbol):
            hit = self._cache.get(symbol)               # re-check under the lock
            if hit is not None and time.monotonic() - hit[0] < self._ttl:
                return hit[1]
            result = await self._compute(symbol, now)
            self._cache[symbol] = (time.monotonic(), result)
            return result

    async def _compute(self, symbol: str, now: datetime | None) -> dict:
        end = now or datetime.now(timezone.utc)
        candles_by_tf: dict[str, list[dict]] = {}
        for tf in HTF_TIMEFRAMES:
            start = end - timedelta(minutes=_TF_MINUTES[tf] * _FETCH_BARS)
            chart = await self._cs.get_chart(symbol, tf, start, end)
            candles_by_tf[tf] = chart["candles"]
        return analyze(symbol, candles_by_tf)

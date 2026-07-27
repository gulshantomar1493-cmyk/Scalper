"""V4 orchestration — compute-on-read over the existing ChartService.

Isolation, same as the HTF/ChartService precedent: V4 never publishes on the
engine bus, never mutates the live payload, and never writes candles. It reads
canonical candles and returns setups.
"""
from __future__ import annotations
import logging
import time
from datetime import datetime, timedelta, timezone

import numpy as np

from . import config as C
from .signals import build_setups

log = logging.getLogger(__name__)

_TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800,
               "1h": 3600, "4h": 14400, "1d": 86400}

#: how much history each timeframe needs for its longest indicator to be warm
_BARS_NEEDED = {"5m": 400, "1h": 400, "4h": 400, "1d": 300}


def fold_5m(m1: dict) -> dict:
    """Fold canonical 1m bars into 5m, keeping only COMPLETE windows.

    ChartService serves 1m/5m from stored rows and never aggregates (D28.1),
    but stored 5m only exists for the live-builder era — over history there is
    none. Without this the 5m ATR series is empty, every strategy fails its
    warm-up check and the tool reports "no setup" forever while looking healthy.

    Requiring all five minutes matches the live builder's own rule (D7 / fix
    F1: a partial window is discarded, never published), so a folded bar and a
    stored one agree.
    """
    ts, o, h, l, c, v = (m1["ts"], m1["o"], m1["h"], m1["l"], m1["c"], m1["v"])
    n = len(ts)
    out_ts, out_o, out_h, out_l, out_c, out_v = [], [], [], [], [], []
    i = 0
    while i < n:
        head = int(ts[i])
        if head % 300 != 0:                  # not a window head — skip forward
            i += 1
            continue
        if i + 5 > n or int(ts[i + 4]) != head + 240:
            i += 1                           # gap inside the window: discard it
            continue
        out_ts.append(head)
        out_o.append(o[i]); out_c.append(c[i + 4])
        out_h.append(max(h[i:i + 5])); out_l.append(min(l[i:i + 5]))
        out_v.append(float(np.sum(v[i:i + 5])))
        i += 5
    return dict(ts=np.array(out_ts, np.int64), o=np.array(out_o, float),
                h=np.array(out_h, float), l=np.array(out_l, float),
                c=np.array(out_c, float), v=np.array(out_v, float), tf_s=300)


def to_arrays(candles: list[dict], tf: str) -> dict:
    """ChartService candle dicts -> the numpy bar dict the V4 engine consumes."""
    n = len(candles)
    ts = np.empty(n, np.int64)
    o = np.empty(n); h = np.empty(n); l = np.empty(n); c = np.empty(n); v = np.empty(n)
    for i, k in enumerate(candles):
        t = k["ts"]
        if isinstance(t, str):
            t = datetime.fromisoformat(t)
        ts[i] = int(t.timestamp())
        o[i] = k["o"]; h[i] = k["h"]; l[i] = k["l"]; c[i] = k["c"]; v[i] = k.get("v", 0.0)
    return dict(ts=ts, o=o, h=h, l=l, c=c, v=v, tf_s=_TF_SECONDS[tf])


class V4Service:
    """Builds setups on demand. Cached per (symbol, tf) until a new bar closes."""

    def __init__(self, chart_service, *, ttl_s: float = 30.0, settings=None):
        self._chart = chart_service
        self._ttl = ttl_s
        self._settings = settings
        self._bars: dict[tuple[str, str], tuple[float, dict]] = {}
        self._setups: dict[str, tuple[float, list[dict]]] = {}

    def is_enabled(self, strategy: C.Strategy) -> bool:
        """Catalogue default, overridden by the owner's runtime switch. With no
        settings store (replay/tests) the catalogue alone decides."""
        if not strategy.enabled:
            return False
        return self._settings is None or self._settings.v4_strategy_enabled(strategy.id)

    def set_enabled(self, strategy_id: str, enabled: bool) -> bool:
        if self._settings is None:
            raise RuntimeError("settings store not configured")
        self._settings.set_v4_strategy(strategy_id, enabled)
        self._setups.pop(strategy_id, None)      # never serve a stale cached list
        return self.is_enabled(C.BY_ID[strategy_id])

    async def bars(self, symbol: str, tf: str) -> dict:
        key = (symbol, tf)
        hit = self._bars.get(key)
        now = time.monotonic()
        if hit and hit[0] > now:
            return hit[1]
        need = _BARS_NEEDED.get(tf, 400)
        span = timedelta(seconds=_TF_SECONDS[tf] * (need + 20))
        end = datetime.now(timezone.utc)
        start = end - span
        payload = await self._chart.get_chart(symbol, tf, start, end)
        arr = to_arrays(payload.get("candles", []), tf)
        if tf == "5m" and len(arr["c"]) < need:
            # stored 5m does not cover this window (history, or a fresh DB) —
            # fold it from canonical 1m rather than run on an empty series.
            m1 = await self._chart.get_chart(symbol, "1m", start, end)
            folded = fold_5m(to_arrays(m1.get("candles", []), "1m"))
            if len(folded["c"]) > len(arr["c"]):
                log.info("v4: folded %d 5m bars from 1m for %s (stored had %d)",
                         len(folded["c"]), symbol, len(arr["c"]))
                arr = folded
        ttl = min(_TF_SECONDS[tf], 120)
        self._bars[key] = (now + ttl, arr)
        return arr

    async def setups_for(self, strategy: C.Strategy, *, only_last: bool = True) -> list[dict]:
        """Current actionable setups for one strategy."""
        cache = self._setups.get(strategy.id)
        now = time.monotonic()
        if cache and cache[0] > now:
            return cache[1]
        try:
            lvl = await self.bars(strategy.symbol, strategy.level_tf)
            ev = (lvl if strategy.eval_tf == strategy.level_tf
                  else await self.bars(strategy.symbol, strategy.eval_tf))
            dly = await self.bars(strategy.symbol, "1d")
            m5 = await self.bars(strategy.symbol, "5m")
        except Exception as exc:                      # never take the API down
            log.warning("v4: bar fetch failed for %s: %s", strategy.id, exc)
            return []
        thin = {name: len(b["c"]) for name, b in
                (("level", lvl), ("eval", ev), ("daily", dly), ("5m", m5))
                if len(b["c"]) < 60}
        if thin:
            # "No setup right now" is a normal state; NOT having enough data to
            # look is not, and the two are indistinguishable on screen. Say so.
            log.warning("v4: %s cannot be evaluated — insufficient candles %s",
                        strategy.id, thin)
            return []
        try:
            out = build_setups(strategy, lvl, dly, m5, ev, only_last=only_last)
        except Exception as exc:
            log.warning("v4: build_setups failed for %s: %s", strategy.id, exc)
            return []
        rows = [s.to_dict() for s in out]
        self._setups[strategy.id] = (now + self._ttl, rows)
        return rows

    async def all_setups(self, symbol: str | None = None,
                         strategy_id: str | None = None) -> list[dict]:
        out: list[dict] = []
        for st in C.STRATEGIES:
            if not self.is_enabled(st):
                continue
            if symbol and st.symbol != symbol:
                continue
            if strategy_id and st.id != strategy_id:
                continue
            out.extend(await self.setups_for(st))
        out.sort(key=lambda r: (-r["filters_passed"], -r["rr"]))
        return out

    def catalogue(self) -> list[dict]:
        """Strategy list with its EVIDENCE. Backtest and live stats stay separate."""
        return [dict(
            id=s.id, label=s.label, symbol=s.symbol, enabled=self.is_enabled(s),
            can_toggle=self._settings is not None,
            level=f"{s.level_source} {s.level_tf}", eval_tf=s.eval_tf,
            min_filters=s.min_filters, note=s.note,
            backtest=dict(trades_per_year=s.backtest_trades_per_year,
                          net_r=s.backtest_net_r, t_stat=s.backtest_t_stat,
                          profit_factor=s.backtest_profit_factor,
                          period="2017-2026 (9y)"),
        ) for s in C.STRATEGIES]

    async def quotes(self) -> dict:
        """Last close per symbol, from bars already cached for the strategies.
        Cheap by construction — no extra database work in the common case."""
        out = {}
        for sym in C.SYMBOLS:
            try:
                b = await self.bars(sym, "5m")
            except Exception:
                continue
            if len(b["c"]):
                out[sym] = dict(price=round(float(b["c"][-1]), 2),
                                ts=int(b["ts"][-1]) + b["tf_s"])
        return out

    async def levels_for_chart(self, symbol: str, tf: str) -> list[dict]:
        """Level lines to draw on the chart for the requested timeframe."""
        from . import levels as L
        try:
            b = await self.bars(symbol, tf)
        except Exception:
            return []
        if len(b["c"]) < 40:
            return []
        i = len(b["c"]) - 1
        hi, lo = L.donchian(b, 20)
        sh, sl_ = L.swings(b)
        pdh, pdl = L.prior_day(b)
        out = []
        for name, val in (("Donchian 20 High", hi[i]), ("Donchian 20 Low", lo[i]),
                          ("Swing High", sh[i]), ("Swing Low", sl_[i]),
                          ("Prior Day High", pdh[i]), ("Prior Day Low", pdl[i])):
            if val is not None and np.isfinite(val):
                out.append(dict(label=name, price=round(float(val), 2),
                                side="above" if val >= b["c"][i] else "below"))
        for r in L.round_levels(float(b["c"][i]), symbol):
            out.append(dict(label=f"Round {r:,.0f}", price=r,
                            side="above" if r >= b["c"][i] else "below"))
        return out

"""V3 analysis service — compute-on-read over ChartService candles.

One ChartReadEngine fold per (symbol, tf), cached until that TF prints a new
closed candle. Provider-blind (ChartService owns data access). Read-only.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from marketscalper.v3.chart_read import ChartReadEngine
from marketscalper.v3.config import V3Config, DEFAULT
from marketscalper.v3.market_map import build_map, build_memory

log = logging.getLogger(__name__)

# lookback per TF so ~cfg.history_bars closed candles are fetched
_TF_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


class V3AnalysisService:
    def __init__(self, chart_service, cfg: V3Config = DEFAULT):
        self._charts = chart_service
        self._cfg = cfg
        self._cache: dict = {}          # (symbol, tf) -> (expiry, last_ts, payload)
        self._map_cache: dict = {}      # symbol -> (expiry, payload)

    def timeframes(self) -> tuple:
        return self._cfg.read_tfs

    async def analysis(self, symbol: str, tf: str) -> dict:
        if tf not in self._cfg.read_tfs:
            raise ValueError(f"unknown v3 timeframe {tf!r} "
                             f"(valid: {', '.join(self._cfg.read_tfs)})")
        key = (symbol, tf)
        mono = time.monotonic()
        hit = self._cache.get(key)
        if hit and mono < hit[0]:              # fresh-enough — skip the DB round trip
            return hit[2]
        now = datetime.now(timezone.utc)
        span = timedelta(seconds=_TF_SECONDS[tf] * self._cfg.history_bars)
        chart = await self._charts.get_chart(symbol, tf, now - span, now)
        candles = [c for c in chart["candles"] if c.get("complete", True)]
        if not candles:
            return {"symbol": symbol, "tf": tf, "ready": False,
                    "reason": "no closed candles"}
        ttl = min(_TF_SECONDS[tf], 120)        # re-read at most every 2 min
        last_ts = candles[-1]["ts"]
        if hit and hit[1] == last_ts:          # no new closed candle — reuse fold
            self._cache[key] = (mono + ttl, last_ts, hit[2])
            return hit[2]
        t0 = time.perf_counter()
        payload = ChartReadEngine(symbol, tf, self._cfg).read(candles)
        payload["ready"] = True
        payload["generated_in_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        self._cache[key] = (mono + ttl, last_ts, payload)
        log.info("v3 read %s %s: %s bars in %sms", symbol, tf,
                 payload["bars"], payload["generated_in_ms"])
        return payload

    async def map(self, symbol: str) -> dict:
        """L2 Market Map + L3 Market Memory over all read TFs (each read cached)."""
        mono = time.monotonic()
        hit = self._map_cache.get(symbol)
        if hit and mono < hit[0]:
            return hit[1]
        t0 = time.perf_counter()
        reads = {}
        for tf in self._cfg.read_tfs:
            try:
                reads[tf] = await self.analysis(symbol, tf)
            except Exception as exc:               # a single TF must not kill the map
                log.warning("v3 map: %s %s read failed: %s", symbol, tf, exc)
                reads[tf] = None
        out = build_map(symbol, reads, self._cfg)
        out["memory"] = build_memory(symbol, reads, self._cfg)
        out["generated_in_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        self._map_cache[symbol] = (mono + 20.0, out)
        return out

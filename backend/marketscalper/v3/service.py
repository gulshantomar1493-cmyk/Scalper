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

log = logging.getLogger(__name__)

# lookback per TF so ~cfg.history_bars closed candles are fetched
_TF_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


class V3AnalysisService:
    def __init__(self, chart_service, cfg: V3Config = DEFAULT):
        self._charts = chart_service
        self._cfg = cfg
        self._cache: dict = {}          # (symbol, tf) -> (last_ts, payload)

    def timeframes(self) -> tuple:
        return self._cfg.read_tfs

    async def analysis(self, symbol: str, tf: str) -> dict:
        if tf not in self._cfg.read_tfs:
            raise ValueError(f"unknown v3 timeframe {tf!r} "
                             f"(valid: {', '.join(self._cfg.read_tfs)})")
        now = datetime.now(timezone.utc)
        span = timedelta(seconds=_TF_SECONDS[tf] * self._cfg.history_bars)
        chart = await self._charts.get_chart(symbol, tf, now - span, now)
        candles = [c for c in chart["candles"] if c.get("complete", True)]
        if not candles:
            return {"symbol": symbol, "tf": tf, "ready": False,
                    "reason": "no closed candles"}
        key = (symbol, tf)
        last_ts = candles[-1]["ts"]
        hit = self._cache.get(key)
        if hit and hit[0] == last_ts:
            return hit[1]
        t0 = time.perf_counter()
        payload = ChartReadEngine(symbol, tf, self._cfg).read(candles)
        payload["ready"] = True
        payload["generated_in_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        self._cache[key] = (last_ts, payload)
        log.info("v3 read %s %s: %s bars in %sms", symbol, tf,
                 payload["bars"], payload["generated_in_ms"])
        return payload

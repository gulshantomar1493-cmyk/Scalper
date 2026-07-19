"""Multi-timeframe ChartService (Decision D26/D27/D28) — Phase 1.

A read-only, display-layer read-model that serves the nine chart timeframes by
COMPUTE-ON-READ aggregation of the canonical 1m candles. It is deliberately and
provably ISOLATED from the decision engine (D26.3):

  * it NEVER publishes a candle onto the engine EventBus,
  * it NEVER writes into the `structure` / `_payload` dict, and
  * it NEVER persists a higher-timeframe row (only canonical 1m is stored).

Design decisions it enforces:
  * COMPUTE-ON-READ only — no materialization / Redis / workers / TimescaleDB /
    continuous aggregates (owner decision #4). The canonical store stays 1m.
  * 1m and 5m are served DIRECTLY from the stored rows, never aggregated (D28.1)
    — so the chart's 1m/5m match the engines' own view byte-for-byte.
  * 15m..1M are aggregated from stored 1m: fixed TFs via `date_bin` (epoch-modular,
    D27.1), calendar TFs via `date_trunc(field, ts, 'UTC')` (D27.2). Only CLOSED
    buckets are emitted (no-repaint); best-effort completeness with a per-candle
    `complete` flag (D28 + owner rule 5).
  * Provider-INDEPENDENT (owner decision #10 / P0.19): the concrete provider used
    for the DB-first → provider-second 1m gap-fill is INJECTED (duck-typed to
    providers.base.FeedProvider); this module imports no concrete provider. A future
    Delta provider works with zero changes here.

Deterministic: the output is a pure function of the stored 1m candles and the
requested [from, to) — no wall-clock reads in the aggregation.
"""

from __future__ import annotations

import calendar
import logging
from datetime import datetime, timedelta, timezone

from marketscalper import db
from marketscalper.bootstrap import candle_to_row   # Candle -> row tuple (reused)

log = logging.getLogger(__name__)

# Canonical timeframes served directly from stored rows (never aggregated).
STORED_TFS = ("1m", "5m")
# Fixed-width derived TFs → minutes (epoch-modular buckets, D27.1).
FIXED_MIN = {"15m": 15, "30m": 30, "1h": 60, "4h": 240}
# Calendar derived TFs → date_trunc field (D27.2). NOTE the case-sensitive
# collision: "1m" == one MINUTE (stored), "1M" == one MONTH (calendar).
CALENDAR_FIELD = {"1d": "day", "1w": "week", "1M": "month"}
# The full supported set, in display order.
TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M")

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)   # date_bin origin (UTC midnight)

# Aggregate stored 1m -> a fixed N-minute TF. Determinism guards (D26.6): ordered
# first-open / last-close, closed-buckets-only (HAVING), UTC-anchored date_bin.
_AGG_FIXED_SQL = (
    "SELECT date_bin(make_interval(mins => $4), ts, $5) AS bucket_ts,"
    " (array_agg(o ORDER BY ts))[1] AS o, max(h) AS h, min(l) AS l,"
    " (array_agg(c ORDER BY ts DESC))[1] AS c, sum(v) AS v, count(*) AS n"
    " FROM candles WHERE symbol = $1 AND tf = '1m' AND ts >= $2 AND ts < $3"
    " GROUP BY bucket_ts"
    " HAVING date_bin(make_interval(mins => $4), ts, $5)"
    "        + make_interval(mins => $4) <= $3"       # closed buckets only
    " ORDER BY bucket_ts"
)
# Aggregate stored 1m -> a calendar TF (day/week/month). $4 is the date_trunc field.
_AGG_CAL_SQL = (
    "SELECT date_trunc($4, ts, 'UTC') AS bucket_ts,"
    " (array_agg(o ORDER BY ts))[1] AS o, max(h) AS h, min(l) AS l,"
    " (array_agg(c ORDER BY ts DESC))[1] AS c, sum(v) AS v, count(*) AS n"
    " FROM candles WHERE symbol = $1 AND tf = '1m' AND ts >= $2 AND ts < $3"
    " GROUP BY bucket_ts"
    " HAVING date_trunc($4, ts, 'UTC') + ('1 ' || $4)::interval <= $3"
    " ORDER BY bucket_ts"
)


def _expected_minutes(tf: str, bucket_ts: datetime) -> int:
    """Full 1m count for a complete bucket of `tf` starting at bucket_ts."""
    if tf in FIXED_MIN:
        return FIXED_MIN[tf]
    if tf == "1d":
        return 1440
    if tf == "1w":
        return 7 * 1440
    if tf == "1M":
        return calendar.monthrange(bucket_ts.year, bucket_ts.month)[1] * 1440
    return 0   # unreachable for supported TFs


class ChartService:
    """Read-only multi-timeframe chart read-model. `provider` (optional) is a
    duck-typed FeedProvider used only for the 1m gap-fill; None disables it."""

    def __init__(self, pool, provider=None) -> None:
        self._pool = pool
        self._provider = provider

    async def get_chart(self, symbol: str, tf: str, start: datetime,
                        end: datetime) -> dict:
        """Return {candles, metadata, overlays} for [start, end). Raises
        ValueError on an unknown tf or a non-positive range (the endpoint maps
        that to HTTP 400)."""
        if tf not in TIMEFRAMES:
            raise ValueError(f"unknown timeframe {tf!r}")
        if not (start < end):
            raise ValueError("`from` must be before `to`")

        aggregated = tf not in STORED_TFS
        async with self._pool.acquire() as conn:
            if aggregated and self._provider is not None:
                await self._ensure_1m_coverage(conn, symbol, start, end)
            if not aggregated:
                rows = await db.select_candles(conn, symbol, tf, start, end)
                candles = [self._native_candle(tf, r) for r in rows]
            elif tf in FIXED_MIN:
                rows = await conn.fetch(_AGG_FIXED_SQL, symbol, start, end,
                                        FIXED_MIN[tf], _EPOCH)
                candles = [self._agg_candle(tf, r) for r in rows]
            else:  # calendar TF
                rows = await conn.fetch(_AGG_CAL_SQL, symbol, start, end,
                                        CALENDAR_FIELD[tf])
                candles = [self._agg_candle(tf, r) for r in rows]

        return {
            "candles": candles,
            "metadata": {
                "symbol": symbol, "timeframe": tf,
                "from": start.isoformat(), "to": end.isoformat(),
                "count": len(candles),
                "source_tf": tf if not aggregated else "1m",
                "aggregated": aggregated,
                "last_closed_ts": candles[-1]["ts"] if candles else None,
            },
            # Overlays are engine-live-computed and flow via the WS `structure`
            # payload (unchanged). ChartService is engine-isolated and does not
            # recompute historical overlays, so this REST field is null. Per the
            # contract only 1m/5m could ever carry overlays; 15m+ never do.
            "overlays": None,
        }

    # ------------------------------------------------------------- helpers

    def _native_candle(self, tf: str, r) -> dict:
        return {"ts": r["ts"].isoformat(), "o": float(r["o"]), "h": float(r["h"]),
                "l": float(r["l"]), "c": float(r["c"]), "v": float(r["v"]),
                "n": 1 if tf == "1m" else 5, "complete": True}

    def _agg_candle(self, tf: str, r) -> dict:
        ts = r["bucket_ts"]
        n = r["n"]
        return {"ts": ts.isoformat(), "o": float(r["o"]), "h": float(r["h"]),
                "l": float(r["l"]), "c": float(r["c"]), "v": float(r["v"]),
                "n": n, "complete": n == _expected_minutes(tf, ts)}

    async def _ensure_1m_coverage(self, conn, symbol: str, start: datetime,
                                  end: datetime) -> None:
        """DB-first → provider-second (owner decision #6): if the requested range
        begins before the earliest stored 1m candle, fetch the missing older 1m
        from the injected provider (NEVER a higher TF), store it append-only, and
        return — so the aggregation below sees a complete 1m base. Reuses the
        existing reconnect-backfill pattern; interior gaps stay the live
        reconnect-backfill's job."""
        provider = self._provider
        caps = getattr(provider, "capabilities", None)
        if caps is not None and not getattr(caps, "supports_historical_data", False):
            return
        earliest = await conn.fetchval(
            "SELECT min(ts) FROM candles WHERE symbol = $1 AND tf = '1m'", symbol)
        # fetch the head range [start, earliest) if we're scrolling before it
        fetch_end = earliest if earliest is not None else end
        if start >= fetch_end:
            return
        try:
            candles = await provider.fetch_historical_candles(
                symbol, "1m", start, fetch_end)
        except Exception:
            log.warning("chart gap-fill: provider fetch failed for %s [%s,%s) "
                        "(serving stored data only)", symbol, start, fetch_end)
            return
        if not candles:
            return
        # [start, earliest) is entirely BEFORE the earliest stored 1m, so these
        # rows cannot collide with existing ones (collision-free by construction).
        try:
            await db.insert_candles(conn, [candle_to_row(c) for c in candles])
        except Exception:
            # append-only + plain INSERT: any overlap duplicates raise; non-fatal
            # — we simply serve what is already stored.
            log.debug("chart gap-fill: insert skipped (overlap) for %s", symbol)

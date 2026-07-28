"""V4 API surface. Mounted additively by the composition root.

Kept in its own module (not bolted into the 1000-line legacy app.py) so the V4
layer can be reasoned about — and removed — as one unit.
"""
from __future__ import annotations
import logging
from fastapi import APIRouter, Depends, HTTPException, Query

from . import config as C

log = logging.getLogger(__name__)


def build_router(service, require_token, *, history_store=None) -> APIRouter:
    r = APIRouter(prefix="/api/v4", tags=["v4"],
                  dependencies=[Depends(require_token)])

    def _svc():
        if service is None:
            raise HTTPException(503, "v4 service not configured")
        return service

    @r.get("/strategies")
    async def strategies():
        """Catalogue + the evidence behind each strategy. Backtest stats only —
        live stats come from /performance and are never merged with these."""
        return {"strategies": _svc().catalogue(),
                "confidence_note": C.CONFIDENCE_NOTE,
                "rejected_ideas": C.REJECTED_IDEAS,
                "geometry": {"stop": f"{C.STOP_ATR5M_MULT}x ATR(5m)",
                             "target": f"{C.TARGET_R}R",
                             "entry": "resting STOP order at the level",
                             "max_hold_days": C.MAX_HOLD_DAYS,
                             "taker_fee": C.TAKER_FEE,
                             "funding_per_day": C.FUNDING_PER_DAY}}

    @r.post("/strategies/{strategy_id}/enabled")
    async def set_strategy_enabled(strategy_id: str, payload: dict | None = None):
        """Owner switch. Disabling stops the strategy issuing NEW setups; already
        recorded rows keep resolving so history stays honest."""
        if strategy_id not in C.BY_ID:
            raise HTTPException(400, f"unknown strategy {strategy_id!r}")
        want = (payload or {}).get("enabled")
        if not isinstance(want, bool):
            raise HTTPException(400, "body must be {\"enabled\": true|false}")
        try:
            now = _svc().set_enabled(strategy_id, want)
        except RuntimeError:
            raise HTTPException(503, "settings store not configured")
        return {"id": strategy_id, "enabled": now}

    @r.get("/setups")
    async def setups(symbol: str | None = Query(None),
                     strategy: str | None = Query(None)):
        if strategy and strategy not in C.BY_ID:
            raise HTTPException(400, f"unknown strategy {strategy!r}")
        rows = await _svc().all_setups(symbol=symbol, strategy_id=strategy)
        return {"setups": rows, "count": len(rows)}

    @r.get("/quotes")
    async def quotes():
        return {"quotes": await _svc().quotes()}

    @r.get("/levels")
    async def levels(symbol: str = Query(...), tf: str = Query("4h")):
        if symbol not in C.SYMBOLS:
            raise HTTPException(400, f"unknown symbol {symbol!r}")
        return {"symbol": symbol, "tf": tf,
                "levels": await _svc().levels_for_chart(symbol, tf)}

    @r.get("/history")
    async def history(symbol: str | None = None, strategy: str | None = None,
                      status: str | None = None, limit: int = Query(200, ge=1, le=1000)):
        if history_store is None:
            return {"rows": [], "count": 0, "note": "history store not configured"}
        rows = await history_store.query(symbol=symbol, strategy=strategy,
                                         status=status, limit=limit)
        return {"rows": rows, "count": len(rows)}

    @r.get("/trades")
    async def trades(limit: int = Query(50, ge=1, le=500)):
        """The two books a trader reads differently, in one round trip.

        `active` are FILLED — money is at risk, the decision is already made and
        what is left is management. `expired` are setups whose validity window
        elapsed without the level breaking: they belong beside the
        recommendations as a record of what was NOT taken, and must never be
        mistaken for positions. Keeping them in one response makes it
        impossible for the two lists to be fetched a poll apart and disagree.
        """
        if history_store is None:
            return {"active": [], "expired": [], "note": "history store not configured"}
        import time
        now = int(time.time())
        rows = await history_store.query(status="FILLED,CANCELLED,OPEN", limit=limit * 3)
        active = [x for x in rows if x["status"] == "FILLED"]
        # An OPEN row whose validity window has already elapsed is expired to
        # the trader the moment the clock passes it. The recorder only writes
        # CANCELLED once it has 1m bars covering the whole window, so between
        # those two moments the row would otherwise still read "waiting" —
        # inviting an order on a setup the engine has already abandoned.
        expired = [x for x in rows
                   if x["status"] == "CANCELLED"
                   or (x["status"] == "OPEN" and (x.get("valid_until_ts") or 0) <= now)]
        expired.sort(key=lambda x: x.get("valid_until_ts") or 0, reverse=True)
        return {"active": active, "expired": expired[:limit],
                "count": len(active) + len(expired[:limit])}

    @r.get("/performance")
    async def performance():
        """LIVE stats per strategy — separate from the backtest figures."""
        from .outcome import performance_report
        if history_store is None:
            return {"overall": {"n": 0}, "by_strategy": {}, "note": "history store not configured"}
        return performance_report(await history_store.query(limit=5000))

    return r

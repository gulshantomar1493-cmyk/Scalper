"""FastAPI application (roadmap P0.21; Architecture §1/§9; auth per D3).

Composes only components that already exist — EventBus, StateStore, the db
access layer — handed in by the caller. No DI container, no registries, no
lifecycle framework: create_app() is a plain factory.

Endpoints:
  GET  /health              liveness (unauthenticated — leaks nothing).
  GET  /candles             history fetch (db.select_candles), Bearer token.
  WS   /ws?token=...        pushes {"candle": ..., "state_diff": ...} on every
                            closed candle (§9: frontend renders diffs only).
  POST /replay/start        run the replay provider over a date range (P0.25).
  POST /replay/stop         halt replay (no-op when idle).
  GET  /replay/status       {running, symbol, start, end, speed}.
  GET  /replay/speeds       the four §10 speeds.

Replay control (roadmap P0.25): the app owns at most ONE replay instance.
The concrete provider class is handed in by composition as the
`replay_provider` argument — this module never imports a concrete provider,
keeping the P0.19 import boundary intact. Replay candles reach the browser
through the exact same WebSocket payload as live candles; no new protocol.

Auth (Decision D3): single static token. REST -> Authorization: Bearer
<token>; WebSocket -> same token as ?token= query parameter at handshake.
No accounts, no sessions, no OAuth.

Composition note: construct the StateStore BEFORE calling create_app() —
bus delivery is sequential in subscription order, so the store's update
runs first and the broadcast's state_diff already contains the candle
being announced.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from fastapi import Body, Depends, FastAPI, Header, HTTPException, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from marketscalper import db
from marketscalper.core.bus import EventBus
from marketscalper.core.state import StateStore
from marketscalper.providers.base import Candle

log = logging.getLogger(__name__)

_TFS = ("1m", "5m")
_REPLAY_SYMBOLS = ("BTCUSDT", "ETHUSDT")   # frozen v1 pair
_REPLAY_SPEEDS = (1, 10, 60, "max")        # §10


def _candle_json(c: Candle) -> dict:
    return {
        "symbol": c.symbol, "tf": c.tf, "ts": c.ts.isoformat(),
        "o": c.o, "h": c.h, "l": c.l, "c": c.c,
        "v": c.v, "qv": c.qv, "n_trades": c.n_trades,
        "taker_buy_v": c.taker_buy_v,
    }


def _diff_json(diff: dict) -> dict:
    # Candle fields serialize through _candle_json; engine-state fields
    # (P1.19: "structure") are already JSON-ready dicts and pass through.
    return {
        symbol: {
            field: _candle_json(value) if isinstance(value, Candle) else value
            for field, value in fields.items()
        }
        for symbol, fields in diff.items()
    }


def create_app(
    bus: EventBus,
    store: StateStore,
    pool,
    api_token: str,
    replay_provider=None,
) -> FastAPI:
    """Build the app around the already-constructed pipeline components.

    replay_provider: the concrete FeedProvider class/factory used for replay
    (composition passes ReplayFeed), invoked as
    replay_provider([symbol], bus, pool, start, end, speed=speed).
    None -> the replay endpoints answer 503 (not configured)."""
    app = FastAPI(title="MarketScalper", docs_url=None, redoc_url=None)
    # The standalone frontend (§9; deploy.sh: index.html opened from disk or
    # any static host) is always a foreign origin to this API, and file://
    # pages send the unpinnable literal origin "null" — so origins cannot be
    # allowlisted. Credentials stay off; the D3 bearer token is the only gate.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization"],
    )
    clients: set[WebSocket] = set()
    # at most one replay at a time; lazy latch turns completion into idle
    replay = {"feed": None, "info": None, "seen_connected": False}

    def require_token(authorization: str | None = Header(default=None)) -> None:
        if authorization != f"Bearer {api_token}":
            raise HTTPException(status_code=401, detail="invalid or missing token")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/candles", dependencies=[Depends(require_token)])
    async def candles(symbol: str, tf: str, start: datetime, end: datetime) -> list[dict]:
        if tf not in _TFS:
            raise HTTPException(status_code=400, detail=f"tf must be one of {_TFS}")
        async with pool.acquire() as conn:
            rows = await db.select_candles(conn, symbol, tf, start, end)
        # numeric columns arrive as Decimal — emit JSON numbers, not strings
        return [
            {"symbol": r["symbol"], "tf": r["tf"], "ts": r["ts"].isoformat(),
             "o": float(r["o"]), "h": float(r["h"]), "l": float(r["l"]),
             "c": float(r["c"]), "v": float(r["v"]), "qv": float(r["qv"]),
             "n_trades": r["n_trades"], "taker_buy_v": float(r["taker_buy_v"])}
            for r in rows
        ]

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        if websocket.query_params.get("token") != api_token:
            await websocket.close(code=1008)          # policy violation: bad token
            return
        await websocket.accept()
        clients.add(websocket)
        try:
            while True:                               # push-only socket; reads keep
                await websocket.receive_text()        # the connection state honest
        except Exception:
            pass
        finally:
            clients.discard(websocket)

    # ---------------------------------------------- replay control (P0.25)

    def _refresh_replay() -> None:
        """Request-driven idle detection: once a running feed disconnects
        (range exhausted), clear it — no background watcher tasks."""
        feed = replay["feed"]
        if feed is None:
            return
        if feed.connected:
            replay["seen_connected"] = True
        elif replay["seen_connected"]:
            replay["feed"] = None
            replay["info"] = None
            replay["seen_connected"] = False

    def _require_replay_configured() -> None:
        if replay_provider is None:
            raise HTTPException(status_code=503, detail="replay not configured")

    @app.post("/replay/start", dependencies=[Depends(require_token)])
    async def replay_start(payload: dict = Body(...)) -> dict:
        _require_replay_configured()
        _refresh_replay()
        if replay["feed"] is not None:
            raise HTTPException(status_code=409, detail="replay already running")

        symbol = payload.get("symbol")
        if symbol not in _REPLAY_SYMBOLS:
            raise HTTPException(status_code=400,
                                detail=f"symbol must be one of {_REPLAY_SYMBOLS}")
        speed = payload.get("speed")
        if speed not in _REPLAY_SPEEDS:
            raise HTTPException(status_code=400,
                                detail=f"speed must be one of {_REPLAY_SPEEDS}")
        try:
            start = datetime.fromisoformat(str(payload.get("start")))
            end = datetime.fromisoformat(str(payload.get("end")))
        except ValueError:
            raise HTTPException(status_code=400, detail="start/end must be ISO datetimes")
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise HTTPException(status_code=400,
                                detail="start/end must be timezone-aware and start < end")

        feed = replay_provider([symbol], bus, pool, start, end, speed=speed)
        await feed.start()
        replay["feed"] = feed
        replay["info"] = {"symbol": symbol, "start": start.isoformat(),
                          "end": end.isoformat(), "speed": speed}
        replay["seen_connected"] = False
        # Observe activation race-free: connected is set at the very start of
        # the replay task, BEFORE its first await (the DB load) — so a
        # zero-delay yield interleaves exactly there and must see True.
        # (A timer-based poll can miss the whole replay at ×max.) After this
        # latch, connected=False means finished.
        for _ in range(100):
            if feed.connected:
                replay["seen_connected"] = True
                break
            await asyncio.sleep(0)
        log.info("replay: started %s", replay["info"])
        return {"running": True, **replay["info"]}

    @app.post("/replay/stop", dependencies=[Depends(require_token)])
    async def replay_stop() -> dict:
        _require_replay_configured()
        feed = replay["feed"]
        if feed is not None:
            await feed.stop()
            replay["feed"] = None
            replay["info"] = None
            replay["seen_connected"] = False
            log.info("replay: stopped")
        return {"running": False}                      # idle stop = no-op success

    @app.get("/replay/status", dependencies=[Depends(require_token)])
    async def replay_status() -> dict:
        _require_replay_configured()
        _refresh_replay()
        info = replay["info"]
        if replay["feed"] is None:
            return {"running": False, "symbol": None, "start": None,
                    "end": None, "speed": None}
        return {"running": True, **info}

    @app.get("/replay/speeds", dependencies=[Depends(require_token)])
    async def replay_speeds() -> dict:
        return {"speeds": list(_REPLAY_SPEEDS)}

    async def broadcast(candle: Candle) -> None:
        """Push {candle, state_diff} for every closed candle (§9)."""
        if not clients:
            store.diff()                              # keep diffs consumed
            return
        payload = {
            "candle": _candle_json(candle),
            "state_diff": _diff_json(store.diff()),
        }
        for websocket in list(clients):
            try:
                await websocket.send_json(payload)
            except Exception:
                clients.discard(websocket)            # dead client: drop, move on

    bus.subscribe(Candle, broadcast)
    return app

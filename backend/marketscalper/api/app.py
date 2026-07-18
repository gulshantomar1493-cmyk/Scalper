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

Replay control (roadmap P0.25; isolation per verified defect F2): the app
owns at most ONE replay instance. Each replay session runs on its OWN
EventBus with its OWN StateStore and (when composition passes
`replay_wiring`) its OWN fresh engine pipelines — exactly the determinism
harness's wiring. Replay candles therefore drive a complete engine chain
without ever touching the live bus: no out-of-order drops, no duplicate
persistence, no reconciler leakage, and live processing continues
untouched underneath. While a replay is active the live WS push is
suppressed (the chart shows the replay stream); it resumes automatically
on completion or stop. The concrete provider class is handed in by
composition as the `replay_provider` argument — this module never imports
a concrete provider, keeping the P0.19 import boundary intact. Replay
candles reach the browser through the exact same WebSocket payload as
live candles; no new protocol.

Backpressure (verified defect F4): the bus-side broadcast never awaits a
network send. Each client gets a bounded queue drained by its own sender
task; a slow or blocked client fills its queue and is disconnected — the
feed, persistence, and engine chain can never stall on a browser socket.

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
from datetime import datetime, timedelta, timezone

from fastapi import Body, Depends, FastAPI, Header, HTTPException, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from marketscalper import db
from marketscalper.analytics import (compute_analytics,
                                     compute_mae_distribution, journal_list)
from marketscalper.core.bus import EventBus
from marketscalper.core.state import StateStore
from marketscalper.providers.base import Candle

log = logging.getLogger(__name__)

_TFS = ("1m", "5m")
_REPLAY_SYMBOLS = ("BTCUSDT", "ETHUSDT")   # frozen v1 pair
_REPLAY_SPEEDS = (1, 10, 60, "max")        # §10
_WS_QUEUE_MAX = 256    # F4: per-client send budget before disconnect


def _num_or_none(v):
    """numeric column (Decimal) -> JSON number, preserving NULL."""
    return None if v is None else float(v)


def _candle_json(c: Candle) -> dict:
    return {
        "symbol": c.symbol, "tf": c.tf, "ts": c.ts.isoformat(),
        "o": c.o, "h": c.h, "l": c.l, "c": c.c,
        "v": c.v, "qv": c.qv, "n_trades": c.n_trades,
        "taker_buy_v": c.taker_buy_v,
    }


def _row_to_candle(r) -> Candle:
    """Stored candle row -> normalized Candle (D19.2 replay-seed reads)."""
    return Candle(
        symbol=r["symbol"], tf=r["tf"], ts=r["ts"],
        o=float(r["o"]), h=float(r["h"]), l=float(r["l"]), c=float(r["c"]),
        v=float(r["v"]), qv=float(r["qv"]),
        n_trades=r["n_trades"], taker_buy_v=float(r["taker_buy_v"]),
    )


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
    replay_wiring=None,
    psych_guard=None,
) -> FastAPI:
    """Build the app around the already-constructed pipeline components.

    replay_provider: the concrete FeedProvider class/factory used for replay
    (composition passes ReplayFeed), invoked as
    replay_provider([symbol], session_bus, pool, start, end, speed=speed).
    None -> the replay endpoints answer 503 (not configured).
    replay_wiring (F2): callable(bus, store, symbols) wiring fresh engine
    pipelines onto a replay session's private bus — composition passes
    _wire_structure_engines so replay drives the full engine chain.
    None -> replay streams candles without engine output (chart-only)."""
    app = FastAPI(title="MarketScalper", docs_url=None, redoc_url=None)
    # The standalone frontend (§9; deploy.sh: index.html opened from disk or
    # any static host) is always a foreign origin to this API, and file://
    # pages send the unpinnable literal origin "null" — so origins cannot be
    # allowlisted. Credentials stay off; the D3 bearer token is the only gate.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "PATCH"],   # PATCH: the P4.8 journal
        allow_headers=["Authorization", "Content-Type"],
    )
    clients: dict[WebSocket, asyncio.Queue] = {}   # F4: per-client queues

    async def _close_quietly(websocket: WebSocket) -> None:
        try:
            await websocket.close(code=1013)       # "try again later"
        except Exception:
            pass

    async def _sender(websocket: WebSocket, queue: asyncio.Queue) -> None:
        """Per-client drain task (F4): network sends happen here, never in
        the bus subscriber — a blocked socket blocks only its own task."""
        try:
            while True:
                await websocket.send_json(await queue.get())
        except Exception:
            clients.pop(websocket, None)           # dead client: drop, move on

    def _push(candle: Candle, source_store: StateStore) -> None:
        """Fan a {candle, state_diff} payload out to every client without
        awaiting any network send (F4)."""
        if not clients:
            source_store.diff()                    # keep diffs consumed
            return
        payload = {
            "candle": _candle_json(candle),
            "state_diff": _diff_json(source_store.diff()),
        }
        for websocket, queue in list(clients.items()):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # F4: a client this far behind must never stall the
                # pipeline — drop it; the thin client reconnects and
                # re-bootstraps via REST (§9).
                log.warning("ws: dropping slow client (send queue full)")
                clients.pop(websocket, None)
                asyncio.ensure_future(_close_quietly(websocket))
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

    # ------------------------------------------------------ journal (P4.8)
    # The recommendation core + the AUTO journal context (reason_text,
    # chart_snapshot_path) are immutable — no endpoint writes them. PATCH
    # touches ONLY the owner's MANUAL outcome fields (db.update_journal_
    # manual writes exactly those columns).

    def _journal_json(row) -> dict:
        return {
            "recommendation_id": row["recommendation_id"],
            "reason_text": row["reason_text"],
            "chart_snapshot_path": row["chart_snapshot_path"],
            "taken": row["taken"], "result": row["result"],
            "actual_entry": _num_or_none(row["actual_entry"]),
            "actual_exit": _num_or_none(row["actual_exit"]),
            "actual_pnl": _num_or_none(row["actual_pnl"]),
            "actual_r": _num_or_none(row["actual_r"]),
            "rule_violations": row["rule_violations"],
            "notes": row["notes"],
            "tags": list(row["tags"]) if row["tags"] is not None else None,
        }

    @app.get("/journal/{recommendation_id}",
             dependencies=[Depends(require_token)])
    async def get_journal(recommendation_id: int) -> dict:
        async with pool.acquire() as conn:
            row = await db.select_journal(conn, recommendation_id)
        if row is None:
            raise HTTPException(status_code=404,
                                detail="no journal for that recommendation")
        return _journal_json(row)

    @app.patch("/journal/{recommendation_id}",
               dependencies=[Depends(require_token)])
    async def patch_journal(recommendation_id: int,
                            payload: dict = Body(...)) -> dict:
        # PATCH merge: validate the PROVIDED manual fields; keys absent from
        # the body keep their existing value (a partial update never wipes
        # unspecified fields). The AUTO context is never writable here.
        if "taken" in payload and payload["taken"] is not None \
                and not isinstance(payload["taken"], bool):
            raise HTTPException(status_code=400,
                                detail="taken must be a boolean or null")
        if "result" in payload and payload["result"] not in (
                None, "win", "loss", "be"):
            raise HTTPException(status_code=400,
                                detail="result must be win|loss|be|null")
        if "tags" in payload and payload["tags"] is not None and not (
                isinstance(payload["tags"], list)
                and all(isinstance(t, str) for t in payload["tags"])):
            raise HTTPException(status_code=400,
                                detail="tags must be a list of strings")
        if "notes" in payload and payload["notes"] is not None \
                and not isinstance(payload["notes"], str):
            raise HTTPException(status_code=400,
                                detail="notes must be a string or null")
        for k in ("actual_entry", "actual_exit", "actual_pnl", "actual_r"):
            if k in payload and payload[k] is not None:
                try:
                    payload[k] = float(payload[k])
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400,
                                        detail=f"{k} must be a number or null")
        async with pool.acquire() as conn:
            existing = await db.select_journal(conn, recommendation_id)
            if existing is None:
                raise HTTPException(
                    status_code=404,
                    detail="no journal for that recommendation")

            def merged(key):
                return payload[key] if key in payload else existing[key]

            await db.update_journal_manual(
                conn, recommendation_id,
                taken=merged("taken"), result=merged("result"),
                actual_entry=merged("actual_entry"),
                actual_exit=merged("actual_exit"),
                actual_pnl=merged("actual_pnl"),
                actual_r=merged("actual_r"),
                notes=merged("notes"), tags=merged("tags"))
            row = await db.select_journal(conn, recommendation_id)
            # P4.9: feed the psychology guard (D23.5). A taken trade WITH a
            # result counts; anything else drops the record (un-taken /
            # result cleared). The guard needs the rec's symbol.
            if psych_guard is not None:
                if row["taken"] and row["result"]:
                    rec = await db.select_recommendation(
                        conn, recommendation_id)
                    sig = (await db.select_signal(conn, rec["signal_id"])
                           if rec is not None else None)
                    if sig is not None:
                        psych_guard.record_taken(
                            recommendation_id, datetime.now(timezone.utc),
                            sig["symbol"], row["result"])
                else:
                    psych_guard.forget(recommendation_id)
        return _journal_json(row)

    # ---------------------------------------------------- analytics (P4.11)

    @app.get("/analytics", dependencies=[Depends(require_token)])
    async def analytics() -> dict:
        """Manual + hypothetical stats + system-vs-actual, overall and per
        strategy / per session. Read-only over the persisted rows."""
        async with pool.acquire() as conn:
            return await compute_analytics(conn)

    @app.get("/analytics/mae", dependencies=[Depends(require_token)])
    async def analytics_mae() -> dict:
        """P5.2: per-strategy MAE distribution + SL-tuning summary over the
        evaluator data. Read-only."""
        async with pool.acquire() as conn:
            return await compute_mae_distribution(conn)

    @app.get("/journal", dependencies=[Depends(require_token)])
    async def journal_list_endpoint(limit: int = 100) -> list:
        """Recent recommendations + journal context (the P4.12 journal
        tab), newest first. Read-only."""
        limit = max(1, min(limit, 500))               # bounded
        async with pool.acquire() as conn:
            return await journal_list(conn, limit)

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        if websocket.query_params.get("token") != api_token:
            await websocket.close(code=1008)          # policy violation: bad token
            return
        await websocket.accept()
        queue: asyncio.Queue = asyncio.Queue(maxsize=_WS_QUEUE_MAX)
        clients[websocket] = queue
        sender = asyncio.create_task(_sender(websocket, queue))
        try:
            while True:                               # push-only socket; reads keep
                await websocket.receive_text()        # the connection state honest
        except Exception:
            pass
        finally:
            clients.pop(websocket, None)
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

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
        if replay["feed"] is not None or replay.get("starting"):
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

        # Reserve the slot BEFORE any await (freeze-audit fix): the guard
        # above and this flag are one atomic section — a concurrent start
        # arriving during the seed read / feed launch gets the 409 instead
        # of orphaning this session.
        replay["starting"] = True
        try:
            # F2: every session runs on its own bus with its own store and
            # (when composition provides the wiring) its own fresh engine
            # pipelines — the determinism harness's exact shape. Replay never
            # touches the live bus: no out-of-order drops, no duplicate
            # persistence, no reconciler leakage.
            session_bus = EventBus()
            session_store = StateStore(session_bus)      # subscribes first
            if replay_wiring is not None:
                # D19.2: the same seeding rule as live startup — the 20 days
                # preceding the replay range warm the RVOL buckets, keeping
                # replay identical to a live run over the same period.
                async with pool.acquire() as conn:
                    rows = await db.select_candles(
                        conn, symbol, "1m", start - timedelta(days=20), start)
                seeds = {symbol: [_row_to_candle(r) for r in rows]}
                replay_wiring(session_bus, session_store, [symbol],
                              seed_candles=seeds)

            async def replay_broadcast(candle: Candle) -> None:
                _push(candle, session_store)         # last: diff is complete

            session_bus.subscribe(Candle, replay_broadcast)
            feed = replay_provider([symbol], session_bus, pool, start, end,
                                   speed=speed)
            await feed.start()
            replay["feed"] = feed
            replay["info"] = {"symbol": symbol, "start": start.isoformat(),
                              "end": end.isoformat(), "speed": speed}
            replay["seen_connected"] = False
            # Observe activation race-free: connected is set at the very
            # start of the replay task, BEFORE its first await (the DB
            # load) — so a zero-delay yield interleaves exactly there and
            # must see True. (A timer-based poll can miss the whole replay
            # at ×max.) After this latch, connected=False means finished.
            for _ in range(100):
                if feed.connected:
                    replay["seen_connected"] = True
                    break
                await asyncio.sleep(0)
        finally:
            replay["starting"] = False
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
        """Push {candle, state_diff} for every closed candle (§9).

        F2: while a replay session is active it owns the WS stream — the
        live push is suppressed (diffs stay consumed) and resumes
        automatically once the replay completes or is stopped. F4: no
        network send happens here (see _push)."""
        _refresh_replay()
        if replay["feed"] is not None:
            store.diff()
            return
        _push(candle, store)

    bus.subscribe(Candle, broadcast)
    return app

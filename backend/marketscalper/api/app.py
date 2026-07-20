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
import hmac
import logging
import math
from datetime import datetime, timedelta, timezone

from fastapi import (Body, Depends, FastAPI, Header, HTTPException, Query,
                     WebSocket)
from fastapi.middleware.cors import CORSMiddleware

from marketscalper import db, telegram
from marketscalper.analytics import (compute_analytics,
                                     compute_mae_distribution, journal_list)
from marketscalper.campaign import (data_quality_audit, expectancy_report)
from marketscalper.core import paper_service
from marketscalper.core.bus import EventBus
from marketscalper.core.live_bar import FormingBar
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
    auth_user: str = "",
    auth_password: str = "",
    replay_provider=None,
    replay_wiring=None,
    psych_guard=None,
    chart_service=None,
    htf_service=None,
    feed_status=None,
    started_at=None,
    ops_symbols=None,
    settings=None,
    live_indicators=None,
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
    # docs + the OpenAPI schema off: the schema would otherwise be served
    # unauthenticated (contract only, but consistent with docs disabled).
    app = FastAPI(title="MarketScalper", docs_url=None, redoc_url=None,
                  openapi_url=None)
    # The standalone frontend (§9; deploy.sh: index.html opened from disk or
    # any static host) is always a foreign origin to this API, and file://
    # pages send the unpinnable literal origin "null" — so origins cannot be
    # allowlisted. Credentials stay off; the D3 bearer token is the only gate.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "PATCH", "DELETE"],   # PATCH/DELETE: journals
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

    expected_auth = f"Bearer {api_token}"

    def require_token(authorization: str | None = Header(default=None)) -> None:
        # Constant-time compare (hmac.compare_digest): a plain `!=` leaks the
        # matched-prefix length via response time. Single static token (D3).
        if authorization is None or not hmac.compare_digest(
                authorization, expected_auth):
            raise HTTPException(status_code=401, detail="invalid or missing token")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def health_ready() -> dict:
        # Readiness probe (Phase E monitoring): liveness + a cheap DB round-
        # trip. Unauthenticated like /health and leaks nothing beyond up/down,
        # so a cron uptime check or the reverse proxy can probe it. 503 =>
        # the database is unreachable and the app cannot serve data.
        try:
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
        except Exception:
            raise HTTPException(status_code=503, detail="database unavailable")
        return {"status": "ready", "db": "ok"}

    @app.post("/login")
    async def login(payload: dict = Body(...)) -> dict:
        """Username/password login for the single-user tool. Validates against
        the env-configured credentials and returns the API token, which the
        frontend stores (localStorage) and sends as the Bearer on every request.
        The token stays the only data-route gate (D3); this endpoint just avoids
        asking the user to paste a raw token. Live-only: replay/tests pass no
        credentials, so login answers 503 (not configured)."""
        if not (auth_user and auth_password):
            raise HTTPException(status_code=503, detail="login not configured")
        u = str((payload or {}).get("username", ""))
        p = str((payload or {}).get("password", ""))
        # both compares constant-time AND both always evaluated (no early-out
        # timing leak on whether the username alone was correct)
        u_ok = hmac.compare_digest(u, auth_user)
        p_ok = hmac.compare_digest(p, auth_password)
        if not (u_ok and p_ok):
            raise HTTPException(status_code=401, detail="invalid credentials")
        return {"token": api_token}

    @app.get("/ops", dependencies=[Depends(require_token)])
    async def ops() -> dict:
        """Operational status for the Live status pill + Operations dashboard
        (pre-prod items 3/5/9/10). Read-only: feed/scanner/DB health, per-symbol
        last candle + data coverage, and uptime. Never touches the engine bus
        or the analysis payload — this is pure introspection.

        `feed_status`/`started_at`/`ops_symbols` are injected by main() (live
        only); replay/tests leave them None and the fields degrade gracefully."""
        now = datetime.now(timezone.utc)
        symbols = list(ops_symbols or _REPLAY_SYMBOLS)
        connected = bool(feed_status()) if feed_status is not None else None

        db_ok = True
        try:
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
        except Exception:
            db_ok = False

        last_candle: dict = {}
        coverage: dict = {}
        latest_seen = None
        for sym in symbols:
            st = store.snapshot(sym)
            c = getattr(st, "last_candle_1m", None) if st is not None else None
            ts = c.ts if c is not None else None
            last_candle[sym] = ts.isoformat() if ts is not None else None
            if ts is not None and (latest_seen is None or ts > latest_seen):
                latest_seen = ts
            coverage[sym] = None
            if db_ok:
                try:
                    async with pool.acquire() as conn:
                        row = await db.select_candle_coverage(conn, sym, "1m")
                    coverage[sym] = {
                        "earliest": row["earliest"].isoformat() if row["earliest"] else None,
                        "latest": row["latest"].isoformat() if row["latest"] else None,
                        "count": row["n"],
                    }
                except Exception:
                    coverage[sym] = None

        # Scanner is "running" whenever the feed is live (the analysis loop is
        # active and building candles) OR a candle closed recently (<180s) —
        # so it never reads idle during the first minute after startup while
        # the first live candle is still forming.
        last_scan_age = (now - latest_seen).total_seconds() if latest_seen else None
        scanner_running = bool(connected) or (
            last_scan_age is not None and last_scan_age < 180)
        # Backfill "active": a symbol's stored latest is well behind now — the
        # reconnect gap-fill (or the one-time bootstrap) is still catching up.
        backfill_active = False
        for sym in symbols:
            cov = coverage.get(sym)
            if cov and cov.get("latest"):
                if (now - datetime.fromisoformat(cov["latest"])).total_seconds() > 180:
                    backfill_active = True
        uptime_s = int((now - started_at).total_seconds()) if started_at is not None else None

        return {
            "now": now.isoformat(),
            "feed": {"connected": connected, "symbols": symbols},
            "scanner": {
                "running": bool(scanner_running),
                "last_scan": latest_seen.isoformat() if latest_seen else None,
                "last_scan_age_s": last_scan_age,
                "symbols_scanned": symbols,
            },
            "database": {"ok": db_ok},
            "last_candle": last_candle,
            "data_coverage": coverage,
            "backfill": {"active": backfill_active},
            "uptime_s": uptime_s,
        }

    # -------------------------------------- settings + notifications (items 7/8)
    # Owner-configurable at runtime (Telegram + notification toggles), persisted
    # by the injected SettingsStore. Live only — replay/tests pass settings=None
    # and these answer 503. The bot token is write-only via verify; GET never
    # returns it.
    def _require_settings() -> None:
        if settings is None:
            raise HTTPException(status_code=503, detail="settings not configured")

    @app.get("/settings", dependencies=[Depends(require_token)])
    async def get_settings() -> dict:
        _require_settings()
        return {"notifications": settings.notifications(),
                "telegram": settings.telegram_public(),        # legacy first-bot view
                "telegram_bots": settings.telegram_bots_public()}

    @app.put("/settings/notifications", dependencies=[Depends(require_token)])
    async def put_notifications(payload: dict = Body(...)) -> dict:
        _require_settings()
        return {"notifications": settings.set_notifications(payload or {})}

    # Verify a bot token, auto-detect its chat id, and ADD it to the list —
    # multiple bots are supported and every verified bot receives every alert.
    @app.post("/settings/telegram/verify", dependencies=[Depends(require_token)])
    async def telegram_verify(payload: dict = Body(...)) -> dict:
        _require_settings()
        token = (payload.get("token") or "").strip()
        label = (payload.get("label") or "").strip()
        if not token:
            raise HTTPException(status_code=400, detail="token required")
        result = await telegram.verify_and_detect(token)
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error", "verification failed"),
                    "bot_username": result.get("bot_username", "")}
        settings.add_telegram_bot(token=token, chat_id=result["chat_id"],
                                  bot_username=result["bot_username"],
                                  verified=True, label=label)
        await telegram.send_message(
            token, result["chat_id"],
            "✅ <b>MarketScalper connected</b> — you'll receive trade & system "
            "alerts here.")
        return {"ok": True, "chat_id": result["chat_id"],
                "bot_username": result["bot_username"],
                "telegram_bots": settings.telegram_bots_public()}

    @app.post("/settings/telegram/test", dependencies=[Depends(require_token)])
    async def telegram_test() -> dict:
        _require_settings()
        targets = settings.telegram_targets()          # all verified bots
        if not targets:
            raise HTTPException(status_code=400, detail="telegram not configured")
        results = await asyncio.gather(*[               # fire to every bot at once
            telegram.send_message(
                tok, chat,
                "🔔 <b>Test alert</b> from MarketScalper — notifications are working.")
            for tok, chat in targets])
        sent = sum(1 for ok in results if ok)
        return {"ok": sent > 0, "sent": sent, "total": len(targets)}

    # Remove ONE bot by id (multi-bot); the id-less route clears them all.
    @app.delete("/settings/telegram/{bot_id}", dependencies=[Depends(require_token)])
    async def telegram_remove(bot_id: int) -> dict:
        _require_settings()
        removed = settings.remove_telegram_bot(bot_id)
        if not removed:
            raise HTTPException(status_code=404, detail="bot not found")
        return {"ok": True, "telegram_bots": settings.telegram_bots_public()}

    @app.delete("/settings/telegram", dependencies=[Depends(require_token)])
    async def telegram_clear() -> dict:
        _require_settings()
        settings.clear_telegram()
        return {"ok": True, **settings.telegram_public(),
                "telegram_bots": settings.telegram_bots_public()}

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

    # ------------------------------------------- multi-timeframe chart (D26)
    # Additive read-only endpoint serving the nine chart timeframes via the
    # compute-on-read ChartService. /candles above is UNTOUCHED. Isolated from
    # the decision engine: chart data never touches the bus or the `structure`
    # payload. `from`/`to` are aliased (Python reserved words).
    @app.get("/api/chart", dependencies=[Depends(require_token)])
    async def api_chart(
        symbol: str,
        timeframe: str,
        start: datetime = Query(alias="from"),
        end: datetime = Query(alias="to"),
        ema: str | None = None,       # comma-separated EMA periods, e.g. "20,50,200"
        sma: int | None = None,       # SMA period
        rsi: int | None = None,       # RSI period
    ) -> dict:
        if chart_service is None:
            raise HTTPException(status_code=503,
                                detail="chart service not configured")

        def _period(v):               # sane display-indicator bounds
            return v if v is not None and 1 <= v <= 1000 else None

        ema_lens = None
        if ema:
            ema_lens = [int(x) for x in ema.split(",")
                        if x.strip().isdigit() and 1 <= int(x) <= 1000][:6]
        try:
            return await chart_service.get_chart(
                symbol, timeframe, start, end,
                ema=ema_lens or None, sma=_period(sma), rsi=_period(rsi))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # ------------------------------------------------------ HTF (V1.1)
    # Higher-timeframe intelligence: 15m/1h/4h/1d SMC analysis + overall
    # bias/confidence/market-story. ADDITIVE and ISOLATED (like /api/chart) —
    # off the engine bus / structure payload / determinism stream. Display-only:
    # execution stays 1m/5m; HTF only adds context and confidence.
    @app.get("/api/htf", dependencies=[Depends(require_token)])
    async def api_htf(symbol: str) -> dict:
        if htf_service is None:
            raise HTTPException(status_code=503, detail="htf service not configured")
        return await htf_service.analyze(symbol)

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
                if not math.isfinite(payload[k]):     # no NaN/inf into numeric
                    raise HTTPException(status_code=400,
                                        detail=f"{k} must be finite")
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

    @app.get("/campaign/audit", dependencies=[Depends(require_token)])
    async def campaign_audit() -> dict:
        """P5.5: data-quality audit over the persisted tables. Read-only."""
        async with pool.acquire() as conn:
            return await data_quality_audit(conn)

    @app.get("/campaign/expectancy", dependencies=[Depends(require_token)])
    async def campaign_expectancy() -> dict:
        """P5.7: fees-included expectancy report per strategy (the P5.8
        TRUSTED gate's number). Read-only."""
        async with pool.acquire() as conn:
            analytics = await compute_analytics(conn)
        return expectancy_report(analytics)

    @app.get("/journal", dependencies=[Depends(require_token)])
    async def journal_list_endpoint(limit: int = 100) -> list:
        """Recent recommendations + journal context (the P4.12 journal
        tab), newest first. Read-only."""
        limit = max(1, min(limit, 500))               # bounded
        async with pool.acquire() as conn:
            return await journal_list(conn, limit)

    # ------------------------------------------ user journal (P5, full CRUD)
    # The STANDALONE user journal (migration 003) — create / edit / delete /
    # search / filter. Namespaced under /api/* (covered by the reverse-proxy
    # matcher). Separate from the append-only recommendation `journal` above.
    _JE_TEXT = ("title", "symbol", "emotion", "mistakes", "lessons", "strategy",
                "notes", "screenshot")
    _JE_NUM = ("entry", "exit_px", "sl", "tp", "risk_pct")

    def _je_json(row) -> dict:
        d = dict(row)
        for k in ("created_at", "updated_at"):
            if d.get(k) is not None:
                d[k] = d[k].isoformat()
        for k in _JE_NUM:
            if d.get(k) is not None:
                d[k] = float(d[k])
        d["tags"] = list(d.get("tags") or [])
        return d

    def _bad(msg):
        raise HTTPException(status_code=400, detail=msg)

    def _validate_journal_entry(payload: dict) -> dict:
        if not isinstance(payload, dict):
            _bad("body must be an object")
        fields = {}
        for k in _JE_TEXT:
            if k in payload:
                if payload[k] is not None and not isinstance(payload[k], str):
                    _bad(f"{k} must be text")
                fields[k] = payload[k]
        for k in _JE_NUM:
            if k in payload:
                v = payload[k]
                if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
                    _bad(f"{k} must be a number")
                fields[k] = v
        if "direction" in payload:
            if payload["direction"] not in ("LONG", "SHORT", None):
                _bad("direction must be LONG or SHORT")
            fields["direction"] = payload["direction"]
        if "confidence" in payload:
            v = payload["confidence"]
            if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 10):
                _bad("confidence must be an int 1..10")
            fields["confidence"] = v
        if "tags" in payload:
            v = payload["tags"]
            if v is not None and not (isinstance(v, list) and all(isinstance(t, str) for t in v)):
                _bad("tags must be a list of strings")
            fields["tags"] = v
        if "recommendation_id" in payload:
            v = payload["recommendation_id"]
            if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
                _bad("recommendation_id must be an integer")
            fields["recommendation_id"] = v
        return fields

    @app.get("/api/journal", dependencies=[Depends(require_token)])
    async def api_journal_list(
        search: str | None = None, symbol: str | None = None,
        direction: str | None = None, strategy: str | None = None,
        limit: int = 200,
    ) -> list:
        async with pool.acquire() as conn:
            rows = await db.list_journal_entries(
                conn, search=search, symbol=symbol, direction=direction,
                strategy=strategy, limit=limit)
        return [_je_json(r) for r in rows]

    @app.post("/api/journal", dependencies=[Depends(require_token)])
    async def api_journal_create(payload: dict = Body(...)) -> dict:
        async with pool.acquire() as conn:
            row = await db.insert_journal_entry(conn, _validate_journal_entry(payload))
        return _je_json(row)

    @app.get("/api/journal/{entry_id}", dependencies=[Depends(require_token)])
    async def api_journal_get(entry_id: int) -> dict:
        async with pool.acquire() as conn:
            row = await db.get_journal_entry(conn, entry_id)
        if row is None:
            raise HTTPException(status_code=404, detail="journal entry not found")
        return _je_json(row)

    @app.patch("/api/journal/{entry_id}", dependencies=[Depends(require_token)])
    async def api_journal_update(entry_id: int, payload: dict = Body(...)) -> dict:
        async with pool.acquire() as conn:
            row = await db.update_journal_entry(conn, entry_id, _validate_journal_entry(payload))
        if row is None:
            raise HTTPException(status_code=404, detail="journal entry not found")
        return _je_json(row)

    @app.delete("/api/journal/{entry_id}", dependencies=[Depends(require_token)])
    async def api_journal_delete(entry_id: int) -> dict:
        async with pool.acquire() as conn:
            ok = await db.delete_journal_entry(conn, entry_id)
        if not ok:
            raise HTTPException(status_code=404, detail="journal entry not found")
        return {"deleted": entry_id}

    # ------------------------------------------ paper trading (P6, decision D31)
    # Simulation-only: isolated papertrade tables, the live mark price read from
    # the StateStore. NEVER places a real order; never touches the frozen engines
    # or the determinism stream.
    _PAPER_SYMBOLS = list(ops_symbols) if ops_symbols else ["BTCUSDT", "ETHUSDT"]

    def _paper_marks() -> dict:
        marks = {}
        for sym in _PAPER_SYMBOLS:
            st = store.snapshot(sym)
            c = getattr(st, "last_candle_1m", None) if st is not None else None
            if c is not None:
                marks[sym] = float(c.c)
        return marks

    def _validate_paper_order(p: dict) -> dict:
        if not isinstance(p, dict):
            _bad("body must be an object")
        if p.get("symbol") not in _PAPER_SYMBOLS:
            _bad("unknown symbol")
        if p.get("side") not in ("BUY", "SELL"):
            _bad("side must be BUY or SELL")
        otype = p.get("type", "market")
        if otype not in ("market", "limit", "stop"):
            _bad("type must be market / limit / stop")
        qty = p.get("qty")
        if isinstance(qty, bool) or not isinstance(qty, (int, float)) or qty <= 0:
            _bad("qty must be a positive number")
        spec = {"symbol": p["symbol"], "side": p["side"], "type": otype,
                "qty": float(qty), "reduce_only": bool(p.get("reduce_only", False))}
        lev = p.get("leverage")
        if lev is not None:
            if isinstance(lev, bool) or not isinstance(lev, (int, float)) or not 1 <= lev <= 125:
                _bad("leverage must be 1..125")
            spec["leverage"] = float(lev)
        if otype == "limit":
            price = p.get("price")
            if isinstance(price, bool) or not isinstance(price, (int, float)) or price <= 0:
                _bad("a limit order needs a positive price")
            spec["price"] = float(price)
        if otype == "stop":
            sp = p.get("stop_price")
            if isinstance(sp, bool) or not isinstance(sp, (int, float)) or sp <= 0:
                _bad("a stop order needs a positive stop_price")
            spec["stop_price"] = float(sp)
        return spec

    @app.get("/api/paper", dependencies=[Depends(require_token)])
    async def api_paper_state() -> dict:
        async with pool.acquire() as conn:
            return await paper_service.get_state(conn, _paper_marks())

    @app.post("/api/paper/order", dependencies=[Depends(require_token)])
    async def api_paper_order(payload: dict = Body(...)) -> dict:
        spec = _validate_paper_order(payload)
        try:
            async with pool.acquire() as conn:
                return await paper_service.place_order(conn, spec, _paper_marks())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/paper/close", dependencies=[Depends(require_token)])
    async def api_paper_close(payload: dict = Body(...)) -> dict:
        pid = payload.get("position_id")
        if isinstance(pid, bool) or not isinstance(pid, int):
            _bad("position_id (integer) required")
        try:
            async with pool.acquire() as conn:
                return await paper_service.close_position(conn, pid, _paper_marks())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/paper/order/cancel", dependencies=[Depends(require_token)])
    async def api_paper_cancel(payload: dict = Body(...)) -> dict:
        oid = payload.get("order_id")
        if isinstance(oid, bool) or not isinstance(oid, int):
            _bad("order_id (integer) required")
        async with pool.acquire() as conn:
            ok = await paper_service.cancel_order(conn, oid)
        if not ok:
            raise HTTPException(status_code=404, detail="order not found")
        return {"cancelled": oid}

    @app.post("/api/paper/wallet", dependencies=[Depends(require_token)])
    async def api_paper_wallet(payload: dict = Body(...)) -> dict:
        bal = payload.get("balance")
        if isinstance(bal, bool) or not isinstance(bal, (int, float)) or bal <= 0:
            _bad("balance must be a positive number")
        async with pool.acquire() as conn:
            return await paper_service.reset_wallet(conn, float(bal))

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        supplied = websocket.query_params.get("token")
        if supplied is None or not hmac.compare_digest(supplied, api_token):
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

    def _push_forming(fb: FormingBar) -> None:
        """Fan a display-only forming-bar payload to every client (no
        state_diff, no network send in the subscriber — F4). The interim
        indicator values (backend-computed) ride along so the frontend never
        extends an indicator itself (owner rule)."""
        if not clients:
            return
        indicators = (live_indicators.interim(fb.symbol, fb.c)
                      if live_indicators is not None else None)
        payload = {"forming": {
            "symbol": fb.symbol, "ts": fb.ts.isoformat(),
            "o": fb.o, "h": fb.h, "l": fb.l, "c": fb.c, "v": fb.v,
            "indicators": indicators}}
        for websocket, queue in list(clients.items()):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                clients.pop(websocket, None)
                asyncio.ensure_future(_close_quietly(websocket))

    async def forming_broadcast(fb: FormingBar) -> None:
        """Live forming candle (chart UX item 5). Display-only; suppressed
        while a replay owns the stream (like the closed-candle broadcast)."""
        if replay["feed"] is not None:
            return
        _push_forming(fb)

    bus.subscribe(FormingBar, forming_broadcast)
    return app

"""FastAPI application (roadmap P0.21; Architecture §1/§9; auth per D3).

Composes only components that already exist — EventBus, StateStore, the db
access layer — handed in by the caller. No DI container, no registries, no
lifecycle framework: create_app() is a plain factory.

Endpoints:
  GET  /health, /health/ready   liveness / readiness (unauthenticated).
  POST /login                   env-file credentials -> the API token.
  GET  /candles                 raw 1m/5m history (db.select_candles).
  GET  /api/chart               multi-timeframe read-model (ChartService).
  GET  /api/v4/*                the strategy layer (see v4/api.py).
  GET/POST/PATCH/DELETE /api/journal   the owner's journal (migration 003).
  /api/paper/*                  simulation-only paper trading (D31).
  GET  /ops, /settings/*        operations + runtime settings.

Auth (Decision D3): single static token, Authorization: Bearer <token>.
No accounts, no sessions, no OAuth.

The WebSocket push, the replay-control endpoints and /api/htf were removed
with the V1/V2/V3 cutover — the V4 terminal polls REST and nothing else
consumed them (docs/V4/ARCHITECTURE.md §6). The StateStore is still built
and still tracks the latest closed candle per symbol: paper-trading marks
and GET /ops read it.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from datetime import datetime, timezone

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from marketscalper import db, telegram
from marketscalper.core import paper_service
from marketscalper.core.bus import EventBus
from marketscalper.core.state import StateStore
from marketscalper.providers.base import Candle

log = logging.getLogger(__name__)

_TFS = ("1m", "5m")
_DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT")   # frozen v1 pair


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


def create_app(
    bus: EventBus,
    store: StateStore,
    pool,
    api_token: str,
    auth_user: str = "",
    auth_password: str = "",
    chart_service=None,
    feed_status=None,
    started_at=None,
    ops_symbols=None,
    settings=None,
    live_price=None,
    v4_service=None,
    v4_store=None,
) -> FastAPI:
    """Build the app around the already-constructed pipeline components.

    Every optional argument is supplied only by live main(); tests pass what
    they need and the corresponding routes answer 503 when it is missing."""
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
        # PUT: settings; PATCH/DELETE: journal entries. A verb missing here is
        # invisible to the server-side tests (aiohttp ignores CORS) and blocks
        # the real browser 100% of the time — it has now bitten twice.
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )
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

    # ---------------------------------------------------------------- V4 ---
    # The validated strategy layer (docs/V4/ARCHITECTURE.md). Mounted as its own
    # router so it can be reasoned about — and removed — as one unit.
    if v4_service is not None:
        from marketscalper.v4.api import build_router as _v4_router
        app.include_router(_v4_router(v4_service, require_token,
                                      history_store=v4_store,
                                      live_price=live_price))

    @app.get("/ops", dependencies=[Depends(require_token)])
    async def ops() -> dict:
        """Operational status for the Live status pill + Operations dashboard
        (pre-prod items 3/5/9/10). Read-only: feed/scanner/DB health, per-symbol
        last candle + data coverage, and uptime. Never touches the engine bus
        or the analysis payload — this is pure introspection.

        `feed_status`/`started_at`/`ops_symbols` are injected by main() (live
        only); replay/tests leave them None and the fields degrade gracefully."""
        now = datetime.now(timezone.utc)
        symbols = list(ops_symbols or _DEFAULT_SYMBOLS)
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
                "alerts": settings.alerts(),
                "telegram": settings.telegram_public(),        # legacy first-bot view
                "telegram_bots": settings.telegram_bots_public()}

    @app.put("/settings/alerts", dependencies=[Depends(require_token)])
    async def put_alerts(payload: dict = Body(...)) -> dict:
        _require_settings()
        try:
            return {"alerts": settings.set_alerts(payload or {})}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

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
        # Optional escape hatch: auto-detection cannot work through a webhook,
        # past Telegram's 24-hour update retention, or for a chat the owner
        # wants to name explicitly. A chat id is an address, not a secret.
        chat_id = (payload.get("chat_id") or "").strip()
        if not token:
            raise HTTPException(status_code=400, detail="token required")
        result = await telegram.verify_and_detect(token, chat_id)
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
        # Paper V2 (B4): prefer the LIVE forming price so a market order fills at
        # what the user sees, not the last CLOSED 1m candle (up to ~60s stale).
        # live_price is injected only by live main(); replay/tests fall back to
        # the closed candle -> determinism/tests unchanged.
        marks = {}
        for sym in _PAPER_SYMBOLS:
            px = live_price(sym) if live_price is not None else None
            if px is None:
                st = store.snapshot(sym)
                c = getattr(st, "last_candle_1m", None) if st is not None else None
                px = c.c if c is not None else None
            if px is not None:
                marks[sym] = float(px)
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
        for k in ("sl", "tp"):                        # optional bracket (006): applied on fill
            v = p.get(k)
            if v is not None:
                if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
                    _bad(k + " must be a positive number")
                spec[k] = float(v)
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

    @app.post("/api/paper/sltp", dependencies=[Depends(require_token)])
    async def api_paper_sltp(payload: dict = Body(...)) -> dict:
        pid = payload.get("position_id")
        if isinstance(pid, bool) or not isinstance(pid, int):
            _bad("position_id (integer) required")
        vals = {}
        for k in ("sl", "tp"):
            v = payload.get(k)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0):
                _bad(f"{k} must be a positive number or null")
            vals[k] = float(v) if v is not None else None
        async with pool.acquire() as conn:
            pos = await paper_service.set_sltp(conn, pid, vals["sl"], vals["tp"])
        if pos is None:
            raise HTTPException(status_code=404, detail="position not found or closed")
        return pos

    return app

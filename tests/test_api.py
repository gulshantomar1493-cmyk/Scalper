"""Tests for the FastAPI app (roadmap P0.21) — real server, real clients.

The app runs in-process under uvicorn on an ephemeral port and is driven with
aiohttp — an existing dependency, no test framework added.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import aiohttp
import pytest
import uvicorn
from conftest import TxPool

from marketscalper import db
from marketscalper.api.app import create_app
from marketscalper.core.bus import EventBus
from marketscalper.core.candle_builder import CandleBuilder
from marketscalper.core.chart_service import ChartService
from marketscalper.core.state import StateStore
from marketscalper.providers.base import Candle, Trade

UTC = timezone.utc
M0 = datetime(2026, 7, 14, 19, 0, tzinfo=UTC)
TOKEN = "test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


async def _serve(app):
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(300):
        if server.started:
            break
        await asyncio.sleep(0.01)
    assert server.started
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, task, f"127.0.0.1:{port}"


async def _stop(server, task):
    server.should_exit = True
    await asyncio.wait_for(task, timeout=5)


def _pipeline(pool=None, **kw):
    """bus + store (subscribed FIRST, per the composition note) + app."""
    bus = EventBus()
    store = StateStore(bus)
    return bus, store, create_app(bus, store, pool, TOKEN, **kw)


async def _seed_candles(db_conn, n=5):
    rows = [("BTCUSDT", "1m", M0 + timedelta(minutes=i),
             100.0 + i, 102.0 + i, 99.0 + i, 101.0 + i, 1.5, 150.0, 3, 0.5)
            for i in range(n)]
    await db.insert_candles(db_conn, rows)


# ------------------------------------------------------------------- REST


async def test_health_is_open():
    _, _, app = _pipeline()
    server, task, addr = await _serve(app)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://{addr}/health") as resp:
                assert resp.status == 200
                assert await resp.json() == {"status": "ok"}
    finally:
        await _stop(server, task)


async def test_health_ready_ok(db_conn):
    # Readiness (Phase E): liveness + a real DB round-trip, unauthenticated.
    _, _, app = _pipeline(pool=TxPool(db_conn))
    server, task, addr = await _serve(app)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://{addr}/health/ready") as resp:
                assert resp.status == 200
                assert await resp.json() == {"status": "ready", "db": "ok"}
    finally:
        await _stop(server, task)


class _BrokenPool:
    def acquire(self):
        raise RuntimeError("database unavailable")


async def test_health_ready_503_when_db_unreachable():
    # DB down -> readiness reports 503 so a monitor/proxy can act on it.
    _, _, app = _pipeline(pool=_BrokenPool())
    server, task, addr = await _serve(app)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://{addr}/health/ready") as resp:
                assert resp.status == 503
    finally:
        await _stop(server, task)


async def test_ops_endpoint(db_conn):
    # Operations status (items 3/5/9/10): feed/scanner/db + coverage + uptime.
    started = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
    bus = EventBus()
    store = StateStore(bus)
    app = create_app(bus, store, TxPool(db_conn), TOKEN,
                     feed_status=lambda: True, started_at=started,
                     ops_symbols=["BTCUSDT"])
    server, task, addr = await _serve(app)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://{addr}/ops") as r:
                assert r.status == 401                     # auth required
            async with s.get(f"http://{addr}/ops", headers=AUTH) as r:
                assert r.status == 200
                d = await r.json()
                assert d["feed"]["connected"] is True
                assert d["scanner"]["running"] is True     # feed connected => running
                assert d["database"]["ok"] is True
                assert "BTCUSDT" in d["data_coverage"]
                assert d["uptime_s"] >= 0
                assert d["backfill"]["active"] in (True, False)
    finally:
        await _stop(server, task)


async def test_settings_and_telegram_endpoints(monkeypatch, tmp_path):
    # Notification prefs + Telegram verify/clear (items 7/8). No DB needed — the
    # settings routes never touch the pool; Telegram is monkeypatched (no net).
    from marketscalper.settings_store import SettingsStore

    seen = []

    async def fake_verify(token, chat_id=""):
        seen.append((token, chat_id))
        return {"ok": True, "bot_username": "bot", "chat_id": chat_id or "42"}

    async def fake_send(token, chat_id, text):
        return True

    monkeypatch.setattr("marketscalper.telegram.verify_and_detect", fake_verify)
    monkeypatch.setattr("marketscalper.telegram.send_message", fake_send)
    settings = SettingsStore(path=tmp_path / "s.json")
    bus = EventBus()
    store = StateStore(bus)
    app = create_app(bus, store, None, TOKEN, settings=settings)
    server, task, addr = await _serve(app)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://{addr}/settings") as r:
                assert r.status == 401                     # auth required
            async with s.get(f"http://{addr}/settings", headers=AUTH) as r:
                d = await r.json()
                assert d["telegram"]["has_token"] is False
                assert "token" not in d["telegram"]        # never exposed
            async with s.put(f"http://{addr}/settings/notifications",
                             headers=AUTH, json={"desktop": False}) as r:
                assert (await r.json())["notifications"]["desktop"] is False
            async with s.post(f"http://{addr}/settings/telegram/verify",
                              headers=AUTH, json={"token": "T:OK"}) as r:
                d = await r.json()
                assert d["ok"] is True and d["chat_id"] == "42"
                assert "token" not in d
            async with s.get(f"http://{addr}/settings", headers=AUTH) as r:
                d = await r.json()
                assert d["telegram"]["verified"] is True
                assert d["telegram"]["has_token"] is True and "token" not in d["telegram"]
            async with s.delete(f"http://{addr}/settings/telegram", headers=AUTH) as r:
                assert (await r.json())["has_token"] is False
    finally:
        await _stop(server, task)


async def test_multiple_telegram_bots_endpoints(monkeypatch, tmp_path):
    # Verify two bots -> both listed; test fires to ALL; remove one by id.
    from marketscalper.settings_store import SettingsStore

    async def fake_verify(token, chat_id=""):
        return {"ok": True, "bot_username": "bot_" + token[0],
                "chat_id": chat_id or ("chat_" + token[0])}

    sent = []

    async def fake_send(token, chat_id, text):
        sent.append((token, chat_id))
        return True

    monkeypatch.setattr("marketscalper.telegram.verify_and_detect", fake_verify)
    monkeypatch.setattr("marketscalper.telegram.send_message", fake_send)
    settings = SettingsStore(path=tmp_path / "s.json")
    bus = EventBus()
    app = create_app(bus, StateStore(bus), None, TOKEN, settings=settings)
    server, task, addr = await _serve(app)
    try:
        async with aiohttp.ClientSession() as s:
            for tok in ("A:tok", "B:tok"):
                async with s.post(f"http://{addr}/settings/telegram/verify",
                                  headers=AUTH, json={"token": tok, "label": tok[0]}) as r:
                    d = await r.json()
                    assert d["ok"] is True
            async with s.get(f"http://{addr}/settings", headers=AUTH) as r:
                bots = (await r.json())["telegram_bots"]
                assert len(bots) == 2
                assert all("token" not in b for b in bots)      # never exposed
            # a test alert fans out to BOTH bots at once
            sent.clear()
            async with s.post(f"http://{addr}/settings/telegram/test", headers=AUTH, json={}) as r:
                d = await r.json()
                assert d["ok"] is True and d["sent"] == 2 and d["total"] == 2
            assert len(sent) == 2
            # remove one bot by id -> one remains
            rm_id = bots[0]["id"]
            async with s.delete(f"http://{addr}/settings/telegram/{rm_id}", headers=AUTH) as r:
                d = await r.json()
                assert d["ok"] is True and len(d["telegram_bots"]) == 1
                assert d["telegram_bots"][0]["id"] != rm_id
            async with s.delete(f"http://{addr}/settings/telegram/99999", headers=AUTH) as r:
                assert r.status == 404                          # unknown id
    finally:
        await _stop(server, task)


async def test_login_returns_token_and_rejects_bad_creds():
    # /login validates env credentials and returns the API token (the frontend
    # then sends it as the Bearer). Wrong user/password -> 401.
    bus = EventBus()
    app = create_app(bus, StateStore(bus), None, TOKEN,
                     auth_user="Scalper", auth_password="Scalper@01@")
    server, task, addr = await _serve(app)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"http://{addr}/login",
                              json={"username": "Scalper", "password": "Scalper@01@"}) as r:
                assert r.status == 200
                body = await r.json()
                assert body["token"] == TOKEN                   # the Bearer to use
            for creds in ({"username": "Scalper", "password": "wrong"},
                          {"username": "nope", "password": "Scalper@01@"},
                          {"username": "", "password": ""}):
                async with s.post(f"http://{addr}/login", json=creds) as r:
                    assert r.status == 401
            # the returned token actually authorizes a data route
            async with s.get(f"http://{addr}/ops",
                             headers={"Authorization": f"Bearer {TOKEN}"}) as r:
                assert r.status == 200
    finally:
        await _stop(server, task)


async def test_login_not_configured_returns_503():
    # no credentials set -> /login is disabled (503), so a ?token= URL is the
    # only path (dev); production sets the credentials.
    bus = EventBus()
    app = create_app(bus, StateStore(bus), None, TOKEN)     # no auth_user/password
    server, task, addr = await _serve(app)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"http://{addr}/login",
                              json={"username": "a", "password": "b"}) as r:
                assert r.status == 503
    finally:
        await _stop(server, task)


async def test_candles_requires_bearer_token():
    _, _, app = _pipeline()
    server, task, addr = await _serve(app)
    params = {"symbol": "BTCUSDT", "tf": "1m",
              "start": M0.isoformat(), "end": (M0 + timedelta(minutes=5)).isoformat()}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://{addr}/candles", params=params) as resp:
                assert resp.status == 401                       # missing
            bad = {"Authorization": "Bearer wrong"}
            async with s.get(f"http://{addr}/candles", params=params, headers=bad) as resp:
                assert resp.status == 401                       # wrong
    finally:
        await _stop(server, task)


async def test_candles_history_roundtrip(db_conn):
    rows = [("BTCUSDT", "1m", M0 + timedelta(minutes=i),
             67000 + i, 67010 + i, 66990 + i, 67005 + i, 1.0, 67000.0, 10 + i, 0.5)
            for i in range(3)]
    await db.insert_candles(db_conn, rows)

    _, _, app = _pipeline(pool=TxPool(db_conn))
    server, task, addr = await _serve(app)
    params = {"symbol": "BTCUSDT", "tf": "1m",
              "start": M0.isoformat(), "end": (M0 + timedelta(minutes=5)).isoformat()}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://{addr}/candles", params=params, headers=AUTH) as resp:
                assert resp.status == 200
                body = await resp.json()
        assert [c["ts"] for c in body] == [(M0 + timedelta(minutes=i)).isoformat()
                                           for i in range(3)]
        assert body[0]["o"] == 67000 and body[2]["n_trades"] == 12
    finally:
        await _stop(server, task)


async def test_candles_rejects_unknown_tf():
    _, _, app = _pipeline()
    server, task, addr = await _serve(app)
    params = {"symbol": "BTCUSDT", "tf": "15m",
              "start": M0.isoformat(), "end": (M0 + timedelta(minutes=5)).isoformat()}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://{addr}/candles", params=params, headers=AUTH) as resp:
                assert resp.status == 400
    finally:
        await _stop(server, task)


# ------------------------------------------------------------- journal (P4.8)


async def _seed_journal(db_conn) -> int:
    """Insert signal + recommendation + journal seed; return the rec id."""
    sig_id = await db.insert_signal(
        db_conn, ts=M0, symbol="BTCUSDT", tf="1m", strategy="S1",
        direction="LONG", score=80.0, gates=None, components=None,
        state_snapshot=None, engine_version="test")
    rec_id = await db.insert_recommendation(
        db_conn, signal_id=sig_id, ts=M0, direction="LONG", entry_px=100.0,
        sl=99.0, tp1=102.0, tp2=103.5, suggested_qty=1.0, risk_amt=50.0,
        est_fees=0.1, net_rr_tp1=1.7)
    await db.insert_journal_seed(
        db_conn, recommendation_id=rec_id,
        reason_text="LONG BTCUSDT @ 100 | S1 | Score 80\n✓ swept",
        chart_snapshot_path=None, rule_violations=None)
    return rec_id






# ------------------------------------------------------------- analytics (P4.11)


async def _seed_evaluated_rec(db_conn, strategy, outcome, eval_r, result,
                              actual_r, hour=9):
    ts = M0.replace(hour=hour)
    sig_id = await db.insert_signal(
        db_conn, ts=ts, symbol="BTCUSDT", tf="1m", strategy=strategy,
        direction="LONG", score=80.0, gates=None, components=None,
        state_snapshot=None, engine_version="test")
    rec_id = await db.insert_recommendation(
        db_conn, signal_id=sig_id, ts=ts, direction="LONG", entry_px=100.0,
        sl=99.0, tp1=102.0, tp2=None, suggested_qty=1.0, risk_amt=50.0,
        est_fees=0.1, net_rr_tp1=1.7)
    await db.update_recommendation_status(
        db_conn, rec_id, status="evaluated", status_ts=ts,
        status_reason="hypothetical " + outcome)   # consistent w/ the lifecycle
    await db.update_recommendation_eval(
        db_conn, rec_id, eval_outcome=outcome, eval_r=eval_r,
        eval_mae=-0.4, eval_mfe=2.2)
    await db.insert_journal_seed(
        db_conn, recommendation_id=rec_id, reason_text="x",
        chart_snapshot_path=None, rule_violations=None)
    await db.update_journal_manual(
        db_conn, rec_id, taken=True, result=result, actual_entry=None,
        actual_exit=None, actual_pnl=None, actual_r=actual_r, notes=None,
        tags=None)
    return rec_id






# -------------------------------------------------------------- WebSocket











# ------------------------------------- F2/F4 verified-defect regressions






# ------------------------------------------------ /api/chart (D26 multi-timeframe)


def _chart_app(db_conn, with_service=True):
    bus = EventBus()
    store = StateStore(bus)
    pool = TxPool(db_conn)
    cs = ChartService(pool) if with_service else None
    return create_app(bus, store, pool, TOKEN, chart_service=cs)


def _chart_params(tf="15m", frm=None, to=None):
    return {"symbol": "BTCUSDT", "timeframe": tf,
            "from": (frm or M0).isoformat(),
            "to": (to or (M0 + timedelta(hours=1))).isoformat()}


async def test_api_chart_requires_token(db_conn):
    server, task, addr = await _serve(_chart_app(db_conn))
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://{addr}/api/chart", params=_chart_params()) as r:
                assert r.status == 401
    finally:
        await _stop(server, task)


async def test_api_chart_503_when_not_configured(db_conn):
    server, task, addr = await _serve(_chart_app(db_conn, with_service=False))
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://{addr}/api/chart", params=_chart_params(),
                             headers=AUTH) as r:
                assert r.status == 503
    finally:
        await _stop(server, task)


async def test_api_chart_rejects_unknown_tf_and_bad_range(db_conn):
    server, task, addr = await _serve(_chart_app(db_conn))
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://{addr}/api/chart",
                             params=_chart_params(tf="3m"), headers=AUTH) as r:
                assert r.status == 400                     # unknown tf
            async with s.get(f"http://{addr}/api/chart",
                             params=_chart_params(to=M0), headers=AUTH) as r:
                assert r.status == 400                     # from == to
    finally:
        await _stop(server, task)


async def test_api_chart_aggregation_roundtrip(db_conn):
    await _seed_candles(db_conn, n=30)                     # M0 .. M0+30m of 1m
    server, task, addr = await _serve(_chart_app(db_conn))
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://{addr}/api/chart",
                             params=_chart_params(to=M0 + timedelta(minutes=30)),
                             headers=AUTH) as r:
                assert r.status == 200
                body = await r.json()
    finally:
        await _stop(server, task)
    assert set(body) == {"candles", "metadata", "overlays", "indicators", "context"}
    assert body["overlays"] is None                        # engine-isolated
    assert body["indicators"] is None                      # none requested
    assert body["context"] is None                         # only 2 candles (<30)
    assert body["metadata"]["timeframe"] == "15m"
    assert body["metadata"]["aggregated"] is True
    assert len(body["candles"]) == 2                       # 2 x 15m in 30m
    b0 = body["candles"][0]
    # _seed_candles: o=100+i, h=102+i, l=99+i, c=101+i
    assert (b0["o"], b0["h"], b0["l"], b0["c"]) == (100.0, 116.0, 99.0, 115.0)
    assert b0["n"] == 15 and b0["complete"] is True


async def test_api_chart_returns_display_indicators(db_conn):
    # Item 2: backend computes EMA/SMA/RSI (single source of truth); the
    # frontend only renders. Overlays stay null (engine isolation intact).
    await _seed_candles(db_conn, n=30)
    server, task, addr = await _serve(_chart_app(db_conn))
    params = {"symbol": "BTCUSDT", "timeframe": "1m",
              "from": M0.isoformat(),
              "to": (M0 + timedelta(minutes=30)).isoformat(),
              "ema": "5,10", "sma": "8", "rsi": "5"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://{addr}/api/chart", params=params,
                             headers=AUTH) as r:
                assert r.status == 200
                d = await r.json()
                assert d["overlays"] is None
                ind = d["indicators"]
                assert set(ind.keys()) == {"ema", "sma", "rsi"}
                assert set(ind["ema"].keys()) == {"5", "10"}
                pt = ind["ema"]["5"][0]
                assert "time" in pt and "value" in pt      # {time,value} points
                assert ind["sma"]["8"] and ind["rsi"]["5"]
    finally:
        await _stop(server, task)



# ------------------------------------------------ /api/htf (HTF V1.1)


class _FakeHtf:
    """A canned HtfService (the candle-fetching + analysis are unit-tested in
    test_htf.py); the endpoint test only proves the route + auth + shape."""

    async def analyze(self, symbol, now=None):
        return {"symbol": symbol, "timeframes": {},
                "overall": {"bias": "BULLISH", "score": 70.0, "confidence": 100,
                            "market_story": "story", "explanation": "why"}}


def _htf_app(with_service=True):
    bus = EventBus()
    store = StateStore(bus)
    return create_app(bus, store, None, TOKEN,
                      htf_service=_FakeHtf() if with_service else None)





# ------------------------------------------------ /api/journal (P5 user journal)


def _je_app(db_conn):
    bus = EventBus()
    return create_app(bus, StateStore(bus), TxPool(db_conn), TOKEN)


async def test_api_user_journal_crud(db_conn):
    server, task, addr = await _serve(_je_app(db_conn))
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://{addr}/api/journal") as r:
                assert r.status == 401                          # auth required
            body = {"title": "T", "symbol": "BTCUSDT", "direction": "LONG",
                    "entry": 64000, "confidence": 8, "tags": ["a", "b"], "notes": "n"}
            async with s.post(f"http://{addr}/api/journal", json=body, headers=AUTH) as r:
                assert r.status == 200
                created = await r.json(); eid = created["id"]
                assert created["direction"] == "LONG" and created["tags"] == ["a", "b"]
                assert created["confidence"] == 8 and created["entry"] == 64000.0
            async with s.get(f"http://{addr}/api/journal/{eid}", headers=AUTH) as r:
                assert r.status == 200 and (await r.json())["notes"] == "n"
            async with s.get(f"http://{addr}/api/journal?symbol=BTCUSDT", headers=AUTH) as r:
                assert r.status == 200 and any(e["id"] == eid for e in await r.json())
            async with s.patch(f"http://{addr}/api/journal/{eid}",
                               json={"notes": "edited"}, headers=AUTH) as r:
                assert r.status == 200 and (await r.json())["notes"] == "edited"
            for bad in ({"direction": "UP"}, {"confidence": 99}, {"tags": "x"}):
                async with s.post(f"http://{addr}/api/journal", json=bad, headers=AUTH) as r:
                    assert r.status == 400, bad
            async with s.delete(f"http://{addr}/api/journal/{eid}", headers=AUTH) as r:
                assert r.status == 200
            async with s.get(f"http://{addr}/api/journal/{eid}", headers=AUTH) as r:
                assert r.status == 404
            async with s.delete(f"http://{addr}/api/journal/999999999", headers=AUTH) as r:
                assert r.status == 404
    finally:
        await _stop(server, task)


# ------------------------------------------------ /api/paper (P6 paper trading)


async def test_api_paper_trading_flow(db_conn):
    bus = EventBus()
    store = StateStore(bus)
    await bus.publish(Candle(symbol="BTCUSDT", tf="1m", ts=M0, o=64000.0, h=64000.0,
                             l=64000.0, c=64000.0, v=1.0, qv=64000.0, n_trades=1, taker_buy_v=0.5))
    app = create_app(bus, store, TxPool(db_conn), TOKEN, ops_symbols=["BTCUSDT", "ETHUSDT"])
    server, task, addr = await _serve(app)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://{addr}/api/paper") as r:
                assert r.status == 401                              # auth required
            async with s.post(f"http://{addr}/api/paper/wallet", json={"balance": 10000}, headers=AUTH) as r:
                assert r.status == 200 and (await r.json())["balance"] == 10000.0
            async with s.post(f"http://{addr}/api/paper/order",
                              json={"symbol": "BTCUSDT", "side": "BUY", "type": "market", "qty": 1, "leverage": 10},
                              headers=AUTH) as r:
                assert r.status == 200 and (await r.json())["filled"] == 1.0
            async with s.get(f"http://{addr}/api/paper", headers=AUTH) as r:
                st = await r.json()
                assert len(st["positions"]) == 1
                pos = st["positions"][0]
                assert pos["side"] == "LONG" and pos["qty"] == 1.0 and pos["avg_entry"] == 64000.0
                assert pos["mark"] == 64000.0 and pos["margin"] == 6400.0     # 1*64000/10
                assert pos["liq_price"] == 57600.0                            # 64000*(1-0.1)
                assert st["portfolio"]["open_positions"] == 1
                pid = pos["id"]
            for bad in ({"symbol": "BTCUSDT", "side": "UP", "qty": 1},
                        {"symbol": "XRPUSDT", "side": "BUY", "qty": 1},
                        {"symbol": "BTCUSDT", "side": "BUY", "qty": -1}):
                async with s.post(f"http://{addr}/api/paper/order", json=bad, headers=AUTH) as r:
                    assert r.status == 400, bad
            async with s.post(f"http://{addr}/api/paper/close", json={"position_id": pid}, headers=AUTH) as r:
                assert r.status == 200
            async with s.get(f"http://{addr}/api/paper", headers=AUTH) as r:
                st = await r.json()
                assert len(st["positions"]) == 0 and len(st["history"]) == 2   # open + close
    finally:
        await _stop(server, task)


async def test_api_paper_sltp_and_trigger(db_conn):
    bus = EventBus()
    store = StateStore(bus)
    await bus.publish(Candle(symbol="BTCUSDT", tf="1m", ts=M0, o=64000.0, h=64000.0,
                             l=64000.0, c=64000.0, v=1.0, qv=64000.0, n_trades=1, taker_buy_v=0.5))
    app = create_app(bus, store, TxPool(db_conn), TOKEN, ops_symbols=["BTCUSDT", "ETHUSDT"])
    server, task, addr = await _serve(app)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"http://{addr}/api/paper/wallet", json={"balance": 10000}, headers=AUTH) as r:
                assert r.status == 200
            async with s.post(f"http://{addr}/api/paper/order",
                              json={"symbol": "BTCUSDT", "side": "BUY", "type": "market", "qty": 0.1, "leverage": 10},
                              headers=AUTH) as r:
                assert r.status == 200
            async with s.get(f"http://{addr}/api/paper", headers=AUTH) as r:
                pid = (await r.json())["positions"][0]["id"]
            # set SL 63000 / TP 65000 (the draggable chart brackets)
            async with s.post(f"http://{addr}/api/paper/sltp",
                              json={"position_id": pid, "sl": 63000, "tp": 65000}, headers=AUTH) as r:
                assert r.status == 200
                pos = await r.json()
                assert pos["sl"] == 63000.0 and pos["tp"] == 65000.0
            async with s.post(f"http://{addr}/api/paper/sltp",
                              json={"position_id": pid, "sl": -5}, headers=AUTH) as r:
                assert r.status == 400                          # bad SL
            # the mark drops below the SL -> the sync (on GET) closes the position at the SL
            await bus.publish(Candle(symbol="BTCUSDT", tf="1m", ts=M0 + timedelta(minutes=1),
                                     o=62000.0, h=62000.0, l=62000.0, c=62000.0, v=1.0, qv=62000.0,
                                     n_trades=1, taker_buy_v=0.5))
            async with s.get(f"http://{addr}/api/paper", headers=AUTH) as r:
                st = await r.json()
                assert len(st["positions"]) == 0                # closed by the stop
                assert st["history"][0]["price"] == 63000.0     # filled at the SL, not the mark
            async with s.post(f"http://{addr}/api/paper/sltp",
                              json={"position_id": 999999, "sl": 100}, headers=AUTH) as r:
                assert r.status == 404
    finally:
        await _stop(server, task)


async def test_candles_endpoint_unchanged_by_chart_feature(db_conn):
    # regression: /candles stays a BARE array with the pinned tf in {1m,5m}
    await _seed_candles(db_conn, n=5)
    server, task, addr = await _serve(_chart_app(db_conn))
    try:
        async with aiohttp.ClientSession() as s:
            p15 = {"symbol": "BTCUSDT", "tf": "15m", "start": M0.isoformat(),
                   "end": (M0 + timedelta(hours=1)).isoformat()}
            async with s.get(f"http://{addr}/candles", params=p15, headers=AUTH) as r:
                assert r.status == 400                     # 15m still rejected
            p1 = {"symbol": "BTCUSDT", "tf": "1m", "start": M0.isoformat(),
                  "end": (M0 + timedelta(minutes=5)).isoformat()}
            async with s.get(f"http://{addr}/candles", params=p1, headers=AUTH) as r:
                assert r.status == 200
                assert isinstance(await r.json(), list)    # bare array, no envelope
    finally:
        await _stop(server, task)



# ------------------------------------------------------------------- CORS


async def test_cors_allows_every_verb_the_frontend_uses():
    """A verb missing from allow_methods is invisible to these tests (aiohttp
    ignores CORS) but blocks the real browser on every call. It has bitten
    twice: PATCH for the journal, then PUT for the settings toggles."""
    _, _, app = _pipeline()
    server, task, addr = await _serve(app)
    try:
        async with aiohttp.ClientSession() as s:
            for verb in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                async with s.options(
                        f"http://{addr}/settings/alerts",
                        headers={"Origin": "http://example.test",
                                 "Access-Control-Request-Method": verb}) as r:
                    assert r.status == 200, verb
                    allowed = r.headers.get("Access-Control-Allow-Methods", "")
                    assert verb in allowed, f"{verb} missing from {allowed!r}"
    finally:
        await _stop(server, task)









async def test_a_manually_supplied_chat_id_reaches_telegram_verification(monkeypatch, tmp_path):
    """Auto-detection cannot see through a webhook, past Telegram's 24-hour
    update retention, or into a group nobody has messaged. The manual chat id
    is the way out — and it has to actually reach the verifier."""
    from marketscalper.settings_store import SettingsStore

    seen = []

    async def fake_verify(token, chat_id=""):
        seen.append((token, chat_id))
        return {"ok": True, "bot_username": "grp_bot", "chat_id": chat_id or "auto"}

    async def fake_send(token, chat_id, text):
        return True

    monkeypatch.setattr("marketscalper.telegram.verify_and_detect", fake_verify)
    monkeypatch.setattr("marketscalper.telegram.send_message", fake_send)
    settings = SettingsStore(path=tmp_path / "s.json")
    bus = EventBus()
    app = create_app(bus, StateStore(bus), None, TOKEN, settings=settings)
    server, task, addr = await _serve(app)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"http://{addr}/settings/telegram/verify", headers=AUTH,
                              json={"token": "T:tok",
                                    "chat_id": " -1001234567890 "}) as r:
                body = await r.json()
        assert body["ok"] is True
        assert body["chat_id"] == "-1001234567890"      # trimmed, not re-detected
        assert seen == [("T:tok", "-1001234567890")]
        # and it is stored as a real target, so alerts actually go there
        assert settings.telegram_targets() == [("T:tok", "-1001234567890")]
    finally:
        await _stop(server, task)

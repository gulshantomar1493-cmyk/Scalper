"""Tests for the FastAPI app (roadmap P0.21) — real server, real clients.

The app runs in-process under uvicorn on an ephemeral port; REST is tested
with aiohttp and the WebSocket with the websockets client — all existing
dependencies, no test frameworks added.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import aiohttp
import pytest
import uvicorn
import websockets
from conftest import TxPool

from marketscalper import db
from marketscalper.api.app import create_app
from marketscalper.core.bus import EventBus
from marketscalper.core.candle_builder import CandleBuilder
from marketscalper.core.state import StateStore
from marketscalper.providers.base import Candle, Trade
from marketscalper.providers.replay import ReplayFeed

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


def _pipeline(pool=None, replay_provider=None, replay_wiring=None):
    """bus + store (subscribed FIRST, per the composition note) + app."""
    bus = EventBus()
    store = StateStore(bus)
    app = create_app(bus, store, pool, TOKEN, replay_provider=replay_provider,
                     replay_wiring=replay_wiring)
    return bus, store, app


def _replay_body(speed="max", minutes=5, symbol="BTCUSDT"):
    return {
        "symbol": symbol,
        "start": M0.isoformat(),
        "end": (M0 + timedelta(minutes=minutes)).isoformat(),
        "speed": speed,
    }


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


async def test_journal_get_and_manual_patch_roundtrip(db_conn):
    rec_id = await _seed_journal(db_conn)
    _, _, app = _pipeline(pool=TxPool(db_conn))
    server, task, addr = await _serve(app)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://{addr}/journal/{rec_id}") as r:
                assert r.status == 401                     # Bearer required
            async with s.get(f"http://{addr}/journal/{rec_id}",
                             headers=AUTH) as r:
                assert r.status == 200
                body = await r.json()
            assert body["reason_text"].startswith("LONG BTCUSDT")
            assert body["taken"] is None and body["chart_snapshot_path"] is None
            # PATCH the owner's MANUAL fields
            patch = {"taken": True, "result": "win", "actual_entry": 100.5,
                     "actual_exit": 102.0, "actual_r": 1.8,
                     "notes": "clean setup", "tags": ["A+", "sweep"]}
            async with s.patch(f"http://{addr}/journal/{rec_id}",
                               json=patch, headers=AUTH) as r:
                assert r.status == 200
                body = await r.json()
            assert body["taken"] is True and body["result"] == "win"
            assert body["actual_entry"] == 100.5 and body["actual_r"] == 1.8
            assert body["tags"] == ["A+", "sweep"] and body["notes"] == "clean setup"
            # AUTO context immutable — reason_text unchanged by the PATCH
            assert body["reason_text"].startswith("LONG BTCUSDT")
            # PATCH merge: a partial body preserves unspecified fields
            async with s.patch(f"http://{addr}/journal/{rec_id}",
                               json={"notes": "revised"}, headers=AUTH) as r:
                assert r.status == 200
                body = await r.json()
            assert body["notes"] == "revised"
            assert body["taken"] is True and body["result"] == "win"  # kept
            assert body["actual_entry"] == 100.5                       # kept
    finally:
        await _stop(server, task)


async def test_journal_patch_feeds_psychology_guard(db_conn):
    """P4.9/D23.5: logging a taken LOSS via PATCH records it in the
    psychology guard (so a same-symbol signal within 5 min hits revenge)."""
    from datetime import timezone
    from marketscalper.engines.psychology import PsychologyGuard

    rec_id = await _seed_journal(db_conn)
    guard = PsychologyGuard()
    bus = EventBus()
    store = StateStore(bus)
    app = create_app(bus, store, TxPool(db_conn), TOKEN, psych_guard=guard)
    server, task, addr = await _serve(app)
    now = datetime.now(timezone.utc)
    try:
        # before: the guard is clean
        assert guard.evaluate(now, "BTCUSDT").passed
        async with aiohttp.ClientSession() as s:
            async with s.patch(f"http://{addr}/journal/{rec_id}",
                               json={"taken": True, "result": "loss"},
                               headers=AUTH) as r:
                assert r.status == 200
        # after: a taken loss on BTCUSDT is recorded -> revenge blocks now
        st = guard.evaluate(datetime.now(timezone.utc), "BTCUSDT")
        assert not st.passed and "revenge" in st.detail
        # un-take -> the record is dropped
        async with aiohttp.ClientSession() as s:
            async with s.patch(f"http://{addr}/journal/{rec_id}",
                               json={"taken": False}, headers=AUTH) as r:
                assert r.status == 200
        assert guard.evaluate(datetime.now(timezone.utc), "BTCUSDT").passed
    finally:
        await _stop(server, task)


async def test_journal_patch_cors_preflight_allowed():
    """The journal UI (P4.7) is always a foreign origin; a browser sends a
    CORS preflight for the JSON PATCH. Guard that PATCH + Content-Type are
    allowed (aiohttp ignores CORS, so this must be asserted explicitly)."""
    _, _, app = _pipeline()
    server, task, addr = await _serve(app)
    preflight = {
        "Origin": "null",                              # file:// pages send null
        "Access-Control-Request-Method": "PATCH",
        "Access-Control-Request-Headers": "authorization, content-type",
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.options(f"http://{addr}/journal/1",
                                 headers=preflight) as r:
                assert r.status == 200                 # preflight accepted
                allow = r.headers.get("Access-Control-Allow-Methods", "")
                assert "PATCH" in allow
    finally:
        await _stop(server, task)


async def test_journal_404_and_validation(db_conn):
    rec_id = await _seed_journal(db_conn)
    _, _, app = _pipeline(pool=TxPool(db_conn))
    server, task, addr = await _serve(app)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://{addr}/journal/999999", headers=AUTH) as r:
                assert r.status == 404
            async with s.patch(f"http://{addr}/journal/999999",
                               json={"taken": True}, headers=AUTH) as r:
                assert r.status == 404
            for bad in ({"result": "great"}, {"taken": "yes"},
                        {"tags": "notalist"}, {"actual_entry": "abc"}):
                async with s.patch(f"http://{addr}/journal/{rec_id}",
                                   json=bad, headers=AUTH) as r:
                    assert r.status == 400
    finally:
        await _stop(server, task)


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


async def test_analytics_endpoint_roundtrip(db_conn):
    await _seed_evaluated_rec(db_conn, "S1", "tp1", 2.0, "win", 1.8, hour=9)
    await _seed_evaluated_rec(db_conn, "S1", "sl", -1.0, "loss", -1.0, hour=15)
    await _seed_evaluated_rec(db_conn, "S2", "tp1", 3.0, "win", 2.5, hour=3)
    _, _, app = _pipeline(pool=TxPool(db_conn))
    server, task, addr = await _serve(app)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://{addr}/analytics") as r:
                assert r.status == 401                     # Bearer required
            async with s.get(f"http://{addr}/analytics", headers=AUTH) as r:
                assert r.status == 200
                body = await r.json()
        assert body["n_recommendations"] == 3
        assert set(body["by_strategy"]) == {"S1", "S2"}
        s1 = body["by_strategy"]["S1"]
        assert s1["hypothetical"]["wins"] == 1 and s1["hypothetical"]["losses"] == 1
        assert abs(s1["hypothetical"]["win_rate"] - 0.5) < 1e-9
        assert s1["manual"]["n_taken"] == 2
        # system-vs-actual delta present (user vs hypothetical)
        assert s1["system_vs_actual"]["n"] == 2
        assert set(body["by_session"]) == {"ASIA", "LONDON", "NY"}
    finally:
        await _stop(server, task)


async def test_analytics_mae_endpoint(db_conn):
    await _seed_evaluated_rec(db_conn, "S1", "tp1", 2.0, "win", 1.8, hour=9)
    await _seed_evaluated_rec(db_conn, "S1", "sl", -1.0, "loss", -1.0, hour=10)
    _, _, app = _pipeline(pool=TxPool(db_conn))
    server, task, addr = await _serve(app)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://{addr}/analytics/mae") as r:
                assert r.status == 401                     # Bearer required
            async with s.get(f"http://{addr}/analytics/mae",
                             headers=AUTH) as r:
                assert r.status == 200
                body = await r.json()
        assert "S1" in body
        assert body["S1"]["n_evaluated"] == 2 and body["S1"]["n_winners"] == 1
        assert len(body["S1"]["mae_histogram"]) == 4
        assert body["S1"]["sl_preserve_90"] is not None
    finally:
        await _stop(server, task)


async def test_journal_list_endpoint(db_conn):
    await _seed_evaluated_rec(db_conn, "S1", "tp1", 2.0, "win", 1.8, hour=9)
    await _seed_evaluated_rec(db_conn, "S2", "sl", -1.0, "loss", -1.0, hour=15)
    _, _, app = _pipeline(pool=TxPool(db_conn))
    server, task, addr = await _serve(app)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://{addr}/journal") as r:
                assert r.status == 401                     # Bearer required
            async with s.get(f"http://{addr}/journal?limit=100",
                             headers=AUTH) as r:
                assert r.status == 200
                body = await r.json()
        assert len(body) == 2
        # newest first (hour 15 before hour 9)
        assert body[0]["strategy"] == "S2" and body[1]["strategy"] == "S1"
        assert body[0]["eval_outcome"] == "sl" and body[0]["result"] == "loss"
        assert body[1]["taken"] is True and body[1]["reason_text"] == "x"
        assert "entry" in body[0] and "status" in body[0]
    finally:
        await _stop(server, task)


# -------------------------------------------------------------- WebSocket


async def test_ws_rejects_bad_token():
    _, _, app = _pipeline()
    server, task, addr = await _serve(app)
    try:
        with pytest.raises(Exception):                          # 403 handshake or 1008 close
            async with websockets.connect(f"ws://{addr}/ws?token=wrong") as ws:
                await asyncio.wait_for(ws.recv(), timeout=2)
    finally:
        await _stop(server, task)


async def test_ws_pushes_candle_and_state_diff():
    bus, _, app = _pipeline()
    CandleBuilder(bus)                                          # trades -> closed candles
    # prime: the builder discards each symbol's first bucket (startup rule)
    await bus.publish(Trade(symbol="BTCUSDT", price=1.0, qty=1.0,
                            ts=M0 - timedelta(minutes=1), is_buyer_maker=False))
    server, task, addr = await _serve(app)
    try:
        async with websockets.connect(f"ws://{addr}/ws?token={TOKEN}") as ws:
            await bus.publish(Trade(symbol="BTCUSDT", price=67200.0, qty=2.0,
                                    ts=M0 + timedelta(seconds=5), is_buyer_maker=False))
            await bus.publish(Trade(symbol="BTCUSDT", price=67210.0, qty=1.0,
                                    ts=M0 + timedelta(seconds=65), is_buyer_maker=True))
            import json
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        assert msg["candle"]["symbol"] == "BTCUSDT"
        assert msg["candle"]["tf"] == "1m"
        assert msg["candle"]["ts"] == M0.isoformat()
        assert msg["candle"]["o"] == 67200.0 and msg["candle"]["n_trades"] == 1
        diff = msg["state_diff"]["BTCUSDT"]["last_candle_1m"]
        assert diff["ts"] == M0.isoformat()                     # store updated before push
    finally:
        await _stop(server, task)


async def test_replay_endpoints_require_token_and_config():
    _, _, app = _pipeline()                               # replay NOT configured
    server, task, addr = await _serve(app)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"http://{addr}/replay/start", json=_replay_body()) as r:
                assert r.status == 401                    # auth first
            async with s.get(f"http://{addr}/replay/status", headers=AUTH) as r:
                assert r.status == 503                    # not configured
            async with s.post(f"http://{addr}/replay/start", json=_replay_body(),
                              headers=AUTH) as r:
                assert r.status == 503
    finally:
        await _stop(server, task)


async def test_replay_start_runs_to_completion_over_existing_ws(db_conn):
    await _seed_candles(db_conn)
    _, _, app = _pipeline(pool=TxPool(db_conn), replay_provider=ReplayFeed)
    server, task, addr = await _serve(app)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://{addr}/replay/status", headers=AUTH) as r:
                assert (await r.json())["running"] is False   # idle initially

            async with websockets.connect(f"ws://{addr}/ws?token={TOKEN}") as ws:
                async with s.post(f"http://{addr}/replay/start",
                                  json=_replay_body(speed="max"), headers=AUTH) as r:
                    assert r.status == 200
                    body = await r.json()
                    assert body["running"] is True and body["symbol"] == "BTCUSDT"
                    assert body["speed"] == "max"
                import json as _json
                msg = _json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                assert set(msg) == {"candle", "state_diff"}   # existing protocol only
                assert msg["candle"]["symbol"] == "BTCUSDT"

            for _ in range(100):                              # completion -> idle
                async with s.get(f"http://{addr}/replay/status", headers=AUTH) as r:
                    status = await r.json()
                if status["running"] is False:
                    break
                await asyncio.sleep(0.05)
            assert status == {"running": False, "symbol": None, "start": None,
                              "end": None, "speed": None}
    finally:
        await _stop(server, task)


async def test_replay_start_validation(db_conn):
    _, _, app = _pipeline(pool=TxPool(db_conn), replay_provider=ReplayFeed)
    server, task, addr = await _serve(app)
    bad = [
        _replay_body(speed=2),                                   # invalid speed
        {**_replay_body(), "start": _replay_body()["end"],
         "end": _replay_body()["start"]},                        # start >= end
        _replay_body(symbol="DOGEUSDT"),                         # invalid symbol
        {**_replay_body(), "start": "not-a-date"},               # unparseable
    ]
    try:
        async with aiohttp.ClientSession() as s:
            for payload in bad:
                async with s.post(f"http://{addr}/replay/start", json=payload,
                                  headers=AUTH) as r:
                    assert r.status == 400, payload
    finally:
        await _stop(server, task)


async def test_replay_second_start_409_then_stop(db_conn):
    await _seed_candles(db_conn)
    _, _, app = _pipeline(pool=TxPool(db_conn), replay_provider=ReplayFeed)
    server, task, addr = await _serve(app)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"http://{addr}/replay/start",
                              json=_replay_body(speed=60), headers=AUTH) as r:
                assert r.status == 200                           # slow replay: stays running
            async with s.post(f"http://{addr}/replay/start",
                              json=_replay_body(speed=60), headers=AUTH) as r:
                assert r.status == 409                           # already running
            async with s.post(f"http://{addr}/replay/stop", headers=AUTH) as r:
                assert r.status == 200 and (await r.json())["running"] is False
            async with s.get(f"http://{addr}/replay/status", headers=AUTH) as r:
                assert (await r.json())["running"] is False      # stopped -> idle
            async with s.post(f"http://{addr}/replay/stop", headers=AUTH) as r:
                assert r.status == 200                           # idle stop = no-op
    finally:
        await _stop(server, task)


async def test_replay_speeds_endpoint(db_conn):
    _, _, app = _pipeline(pool=TxPool(db_conn), replay_provider=ReplayFeed)
    server, task, addr = await _serve(app)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://{addr}/replay/speeds", headers=AUTH) as r:
                assert await r.json() == {"speeds": [1, 10, 60, "max"]}
    finally:
        await _stop(server, task)


async def test_ws_carries_structure_payload_verbatim():
    """P1.19: engine-state dicts written via set_structure ride the same
    WS diff, JSON-verbatim, next to the candle fields."""
    bus, store, app = _pipeline()
    server, task, addr = await _serve(app)
    try:
        async with websockets.connect(f"ws://{addr}/ws?token={TOKEN}") as ws:
            payload = {"trend": "BULLISH", "pivots": [
                {"ts": M0.isoformat(), "kind": "H", "price": 67230.0,
                 "label": "HH"}], "trendlines": [], "channels": []}
            store.set_structure("BTCUSDT", payload)        # composition order:
            candle = Candle(symbol="BTCUSDT", tf="1m", ts=M0,  # before close
                            o=67200.0, h=67230.0, l=67190.0, c=67215.0,
                            v=2.0, qv=134430.0, n_trades=10, taker_buy_v=1.5)
            await bus.publish(candle)
            import json
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        assert msg["candle"]["symbol"] == "BTCUSDT"
        diff = msg["state_diff"]["BTCUSDT"]
        assert diff["structure"] == payload                # verbatim JSON
        assert diff["last_candle_1m"]["c"] == 67215.0      # candles unaffected
    finally:
        await _stop(server, task)


async def test_ws_broadcasts_to_all_clients():
    bus, _, app = _pipeline()
    CandleBuilder(bus)
    # prime: the builder discards each symbol's first bucket (startup rule)
    await bus.publish(Trade(symbol="ETHUSDT", price=1.0, qty=1.0,
                            ts=M0 - timedelta(minutes=1), is_buyer_maker=False))
    server, task, addr = await _serve(app)
    try:
        async with websockets.connect(f"ws://{addr}/ws?token={TOKEN}") as ws1, \
                   websockets.connect(f"ws://{addr}/ws?token={TOKEN}") as ws2:
            await bus.publish(Trade(symbol="ETHUSDT", price=3500.0, qty=1.0,
                                    ts=M0, is_buyer_maker=False))
            await bus.publish(Trade(symbol="ETHUSDT", price=3501.0, qty=1.0,
                                    ts=M0 + timedelta(seconds=61), is_buyer_maker=False))
            import json
            m1 = json.loads(await asyncio.wait_for(ws1.recv(), timeout=5))
            m2 = json.loads(await asyncio.wait_for(ws2.recv(), timeout=5))
        assert m1 == m2
        assert m1["candle"]["symbol"] == "ETHUSDT"
    finally:
        await _stop(server, task)


# ------------------------------------- F2/F4 verified-defect regressions


async def test_replay_drives_engine_chain_on_isolated_bus(db_conn, caplog):
    """F2 fix: replay runs fresh pipelines on its own bus — the engine
    chain produces structure for replayed candles even after live has
    advanced, and the live bus sees no out-of-order drops."""
    from marketscalper.main import _wire_structure_engines

    await _seed_candles(db_conn)
    bus, store, app = _pipeline(pool=TxPool(db_conn),
                                replay_provider=ReplayFeed,
                                replay_wiring=_wire_structure_engines)
    _wire_structure_engines(bus, store, ["BTCUSDT"])       # live pipelines
    live_ts = M0 + timedelta(days=30)                      # live is far ahead
    await bus.publish(Candle("BTCUSDT", "1m", live_ts, 100.0, 101.0, 99.0,
                             100.0, 1.0, 100.0, 1, 0.5))
    live_structure = store.snapshot("BTCUSDT").structure
    server, task, addr = await _serve(app)
    try:
        with caplog.at_level("WARNING"):
            async with websockets.connect(f"ws://{addr}/ws?token={TOKEN}") as ws:
                async with aiohttp.ClientSession() as s:
                    async with s.post(f"http://{addr}/replay/start",
                                      json=_replay_body(speed="max"),
                                      headers=AUTH) as r:
                        assert r.status == 200
                import json as _json
                msg = _json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        assert msg["candle"]["ts"] == M0.isoformat()       # historical candle
        structure = msg["state_diff"]["BTCUSDT"]["structure"]
        assert structure["qualification"]["verdict"] == "NO_SIGNAL"  # engines ran
        assert not any("out-of-order" in r.message for r in caplog.records)
        assert store.snapshot("BTCUSDT").structure == live_structure  # live untouched
    finally:
        await _stop(server, task)


async def test_live_push_suppressed_while_replay_runs(db_conn):
    """F2 fix: while a replay session is active it owns the WS stream;
    the live push resumes after stop."""
    import json as _json

    await _seed_candles(db_conn)
    bus, _, app = _pipeline(pool=TxPool(db_conn), replay_provider=ReplayFeed)
    server, task, addr = await _serve(app)

    def live(minute):
        return Candle("BTCUSDT", "1m",
                      M0 + timedelta(days=30, minutes=minute),
                      100.0, 101.0, 99.0, 100.0, 1.0, 100.0, 1, 0.5)

    try:
        async with websockets.connect(f"ws://{addr}/ws?token={TOKEN}") as ws:
            async with aiohttp.ClientSession() as s:
                async with s.post(f"http://{addr}/replay/start",
                                  json=_replay_body(speed=60),
                                  headers=AUTH) as r:
                    assert r.status == 200                 # slow: stays running
                await bus.publish(live(0))                 # live during replay
                msg = _json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                assert msg["candle"]["ts"].startswith("2026-07-14")  # replay only
                async with s.post(f"http://{addr}/replay/stop",
                                  headers=AUTH) as r:
                    assert r.status == 200
                await bus.publish(live(1))                 # live resumes
                for _ in range(50):                        # drain queued replay
                    msg = _json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                    if not msg["candle"]["ts"].startswith("2026-07-14"):
                        break
                expected = (M0 + timedelta(days=30, minutes=1)).isoformat()
                assert msg["candle"]["ts"] == expected
    finally:
        await _stop(server, task)


async def test_slow_ws_client_cannot_stall_the_pipeline():
    """F4 fix: a client that never reads is dropped once its send queue
    fills; bus publishing never blocks and fresh clients keep working."""
    import json as _json

    bus, _, app = _pipeline()
    server, task, addr = await _serve(app)

    def candle(i):
        return Candle("BTCUSDT", "1m", M0 + timedelta(minutes=i),
                      100.0, 101.0, 99.0, 100.0, 1.0, 100.0, 1, 0.5)

    try:
        slow = await websockets.connect(f"ws://{addr}/ws?token={TOKEN}")
        loop = asyncio.get_event_loop()
        start_t = loop.time()
        for i in range(2000):                              # never read `slow`
            await bus.publish(candle(i))
        assert loop.time() - start_t < 10.0                # bus never stalled
        with pytest.raises(Exception):                     # server dropped it
            while True:
                await asyncio.wait_for(slow.recv(), timeout=5)
        async with websockets.connect(f"ws://{addr}/ws?token={TOKEN}") as fresh:
            await bus.publish(candle(3000))
            msg = _json.loads(await asyncio.wait_for(fresh.recv(), timeout=5))
            assert msg["candle"]["ts"] == (M0 + timedelta(minutes=3000)).isoformat()
    finally:
        await _stop(server, task)


async def test_concurrent_replay_starts_one_wins(db_conn, monkeypatch):
    """Freeze-audit fix (Volume milestone): the start slot is reserved
    BEFORE the seed read / feed launch awaits — two concurrent starts
    yield exactly one 200 and one 409, never two live sessions."""
    from marketscalper.main import _wire_structure_engines
    from marketscalper import db as _db

    await _seed_candles(db_conn)
    real_select = _db.select_candles

    async def slow_select(conn, symbol, tf, start, end):
        await asyncio.sleep(0.2)                   # widen the race window
        return await real_select(conn, symbol, tf, start, end)

    monkeypatch.setattr("marketscalper.api.app.db.select_candles",
                        slow_select)
    _, _, app = _pipeline(pool=TxPool(db_conn), replay_provider=ReplayFeed,
                          replay_wiring=_wire_structure_engines)
    server, task, addr = await _serve(app)
    try:
        async with aiohttp.ClientSession() as s:
            async def start():
                async with s.post(f"http://{addr}/replay/start",
                                  json=_replay_body(speed=60),
                                  headers=AUTH) as r:
                    return r.status
            statuses = sorted(await asyncio.gather(start(), start()))
            assert statuses == [200, 409]
            async with s.post(f"http://{addr}/replay/stop", headers=AUTH) as r:
                assert r.status == 200
    finally:
        await _stop(server, task)

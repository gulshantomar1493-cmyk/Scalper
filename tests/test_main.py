"""Entrypoint tests (contract updated at P0.27: main() is the composition
root — it refuses to start without required settings, and otherwise serves
until signalled). Run as real subprocesses: real composition, real signals."""

from __future__ import annotations

import asyncio
import os
import pathlib
import signal
import socket
import subprocess
import sys

import aiohttp
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CMD = [sys.executable, "-m", "marketscalper.main"]


def _env(tmp_path, extra: dict) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MARKETSCALPER_")}
    env["PYTHONPATH"] = str(ROOT / "backend")
    env["MARKETSCALPER_LOG_DIR"] = str(tmp_path / "logs")
    env.update(extra)
    return env


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------- refuse-to-start contracts


def test_refuses_without_api_token(tmp_path):
    r = subprocess.run(CMD, env=_env(tmp_path, {}), cwd=ROOT,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 2


def test_refuses_without_dsn(tmp_path):
    r = subprocess.run(CMD, env=_env(tmp_path, {"MARKETSCALPER_API_TOKEN": "t"}),
                       cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert r.returncode == 2


def test_refuses_unknown_feed_provider(tmp_path):
    env = _env(tmp_path, {
        "MARKETSCALPER_API_TOKEN": "t",
        "MARKETSCALPER_DB_DSN": "postgresql://unused",
        "MARKETSCALPER_FEED": "not-a-provider",
    })
    r = subprocess.run(CMD, env=env, cwd=ROOT,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 2


# --------------------------------------------- P1.19 structure composition


async def test_structure_pipeline_wiring_publishes_payload():
    """_wire_structure_engines: frozen engines in the pinned cadence feed
    the StateStore per closed 1m candle; 5m and foreign symbols ignored;
    the payload is JSON-shaped and deterministic across identical runs."""
    from datetime import datetime, timedelta, timezone
    from marketscalper.core.bus import EventBus
    from marketscalper.core.state import StateStore
    from marketscalper.main import _wire_structure_engines
    from marketscalper.providers.base import Candle

    M0 = datetime(2026, 7, 14, 19, 0, tzinfo=timezone.utc)

    def candle(i, h, symbol="BTCUSDT", tf="1m"):
        return Candle(symbol=symbol, tf=tf, ts=M0 + timedelta(minutes=i),
                      o=float(h - 1), h=float(h), l=float(h - 1), c=float(h),
                      v=1.0, qv=100.0, n_trades=1, taker_buy_v=0.5)

    async def run():
        bus = EventBus()
        store = StateStore(bus)
        _wire_structure_engines(bus, store, ["BTCUSDT"])
        # k=3 pivot scenario: H(15) at bar 3 confirms on bar 6's close
        for i, h in enumerate([10, 11, 12, 15, 12, 11, 10]):
            await bus.publish(candle(i, h))
        await bus.publish(candle(7, 11, tf="5m"))          # ignored: 5m
        await bus.publish(candle(7, 11, symbol="XRPUSDT"))  # ignored: unknown
        return store.snapshot("BTCUSDT").structure

    structure = await run()
    assert structure["pivots"] == [
        {"ts": (M0 + timedelta(minutes=3)).isoformat(), "kind": "H",
         "price": 15.0, "label": None}]
    assert structure["trend"] is None                      # chains unlabeled yet
    assert structure["bos"] == [] and structure["choch"] == []
    assert structure["trendlines"] == [] and structure["channels"] == []
    liquidity = structure["liquidity"]                     # Liquidity Engine
    assert liquidity["pools"] == [] and liquidity["sweeps"] == []
    assert liquidity["premium_discount"] is None           # no 5m pivots yet
    assert set(liquidity["levels"]) <= {"PDH", "PDL", "PWH", "PWL"} | {
        f"{s}_{x}" for s in ("ASIA", "LONDON", "NY", "LATE") for x in "HL"}
    assert structure["orderblocks"] == {"blocks": [], "breakers": []}
    assert structure["fvgs"] == []                         # FVG Engine (D14)
    assert await run() == structure                        # deterministic
    assert "XRPUSDT" not in str(structure)


async def test_pipeline_projects_order_blocks_non_empty():
    """Freeze-audit fix: the OB payload field mapping must be executed with
    real content, not just the empty shape."""
    from test_determinism import V1_DATASET
    from marketscalper.core.bus import EventBus
    from marketscalper.core.state import StateStore
    from marketscalper.main import _wire_structure_engines

    bus = EventBus()
    store = StateStore(bus)
    _wire_structure_engines(bus, store, ["BTCUSDT"])
    for candle in V1_DATASET:
        if candle.symbol == "BTCUSDT":
            await bus.publish(candle)
    ob = store.snapshot("BTCUSDT").structure["orderblocks"]
    [block] = ob["blocks"]
    assert block["direction"] == "BEAR"            # the displacement crash
    assert block["status"] == "active"             # pad bars never touch it
    assert block["lo"] < block["hi"]
    assert block["created_ts"].endswith("+00:00")
    assert ob["breakers"] == []


async def test_pipeline_projects_fvgs_non_empty():
    """The FVG payload field mapping must be executed with real content,
    not just the empty shape (the OB-projection precedent)."""
    from test_determinism import V1_DATASET
    from marketscalper.core.bus import EventBus
    from marketscalper.core.state import StateStore
    from marketscalper.main import _wire_structure_engines

    bus = EventBus()
    store = StateStore(bus)
    _wire_structure_engines(bus, store, ["BTCUSDT"])
    for candle in V1_DATASET:
        if candle.symbol == "BTCUSDT":
            await bus.publish(candle)
    fvgs = store.snapshot("BTCUSDT").structure["fvgs"]
    assert len(fvgs) == 3                          # empirically pinned (V1)
    for gap in fvgs:
        assert gap["direction"] == "BEAR"          # the crash-side imbalances
        assert gap["lo"] < gap["ce"] < gap["hi"]
        assert gap["ce"] == (gap["lo"] + gap["hi"]) / 2.0
        assert gap["status"] in ("active", "ce_tested")
        assert gap["created_ts"].endswith("+00:00")


async def test_pipeline_drops_out_of_order_candles():
    """Freeze-audit fix: the reconnect path can emit a stale bucket after
    its backfilled successors — the composition guard drops it before any
    engine ingests it."""
    from datetime import datetime, timedelta, timezone
    from marketscalper.core.bus import EventBus
    from marketscalper.core.state import StateStore
    from marketscalper.main import _wire_structure_engines
    from marketscalper.providers.base import Candle

    M0 = datetime(2026, 7, 14, 19, 0, tzinfo=timezone.utc)

    def candle(i, h):
        return Candle(symbol="BTCUSDT", tf="1m", ts=M0 + timedelta(minutes=i),
                      o=float(h - 1), h=float(h), l=float(h - 1), c=float(h),
                      v=1.0, qv=100.0, n_trades=1, taker_buy_v=0.5)

    bus = EventBus()
    store = StateStore(bus)
    _wire_structure_engines(bus, store, ["BTCUSDT"])
    for i, h in enumerate([10, 11, 12]):
        await bus.publish(candle(i, h))
    clean = store.snapshot("BTCUSDT").structure
    await bus.publish(candle(1, 99))               # stale duplicate: dropped
    await bus.publish(candle(1, 99))               # equal ts too
    assert store.snapshot("BTCUSDT").structure == clean   # engines untouched


# ------------------------------------------------------- full composition


# Windows cannot emulate production's SIGTERM path: Popen.send_signal(SIGTERM)
# hard-terminates the process there instead of exercising the asyncio handler.
@pytest.mark.skipif(os.name != "posix", reason="signal-based graceful shutdown")
async def test_full_composition_serves_and_shuts_down_cleanly(db_dsn, tmp_path):
    """The real service: composition root wires everything, the API answers,
    ReplayFeed is injected, and SIGTERM shuts down with exit code 0."""
    port = _free_port()
    env = _env(tmp_path, {
        "MARKETSCALPER_API_TOKEN": "compose-token",
        "MARKETSCALPER_DB_DSN": db_dsn,
        "MARKETSCALPER_API_HOST": "127.0.0.1",
        "MARKETSCALPER_API_PORT": str(port),
    })
    proc = subprocess.Popen(CMD, env=env, cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    base = f"http://127.0.0.1:{port}"
    auth = {"Authorization": "Bearer compose-token"}
    try:
        async with aiohttp.ClientSession() as s:
            up = False
            for _ in range(150):                       # binance WS may be blocked;
                try:                                   # the server must come up anyway
                    async with s.get(f"{base}/health") as r:
                        if r.status == 200:
                            up = True
                            break
                except aiohttp.ClientError:
                    pass
                await asyncio.sleep(0.1)
            assert up, "service did not come up"

            async with s.get(f"{base}/candles", params={
                "symbol": "BTCUSDT", "tf": "1m",
                "start": "2026-07-14T00:00:00+00:00",
                "end": "2026-07-14T00:05:00+00:00",
            }) as r:
                assert r.status == 401                 # auth enforced end-to-end

            async with s.get(f"{base}/replay/status", headers=auth) as r:
                assert r.status == 200                 # ReplayFeed injected (not 503)
                assert (await r.json())["running"] is False
    finally:
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=20)
    assert rc == 0, (proc.stdout.read() if proc.stdout else "")[-2000:]

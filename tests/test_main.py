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


@pytest.mark.parametrize("equity", ["0", "-5", "abc"])
def test_refuses_bad_equity(tmp_path, equity):
    # D21.5: non-positive or non-numeric equity would silently kill all
    # recommendations (plan_trade geometry-rejects) — refuse to start.
    env = _env(tmp_path, {
        "MARKETSCALPER_API_TOKEN": "t",
        "MARKETSCALPER_DB_DSN": "postgresql://unused",
        "MARKETSCALPER_EQUITY_USD": equity,
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
    volume = structure["volume"]                           # D19: unseeded
    assert volume["rvol"] is None and volume["spike"] is False
    assert volume["session_vwap"] is None                  # mid-day start
    assert isinstance(volume["delta"], float)
    assert volume["absorption"] is None and volume["exhaustion"] is None
    assert structure["confluence"] == []                   # ATR unwarm (D15)
    assert structure["signals"] == []                      # Strategy (D20)
    assert structure["recommendations"] == []              # D21.7 (P3.18)
    qual = structure["qualification"]                      # D16: G1 warming
    assert qual["verdict"] == "NO_SIGNAL" and qual["score"] is None
    assert [g["name"] for g in qual["gates"]] == ["G1", "G2", "G3",
                                                  "G4", "G5", "G6"]
    assert await run() == structure                        # deterministic
    assert "XRPUSDT" not in str(structure)


def test_d9_config_plumbing_propagates_to_engines():
    """D9: non-default regime_cfg / shift_accel reach the per-symbol engines
    (the plumbing is real, not cosmetic); the default path stays the frozen
    §4.2 literals (the byte-identical contract replay/tests rely on)."""
    from marketscalper.core.bus import EventBus
    from marketscalper.core.state import StateStore
    from marketscalper.engines.momentum import RegimeConfig
    from marketscalper.main import _StructurePipeline

    store = StateStore(EventBus())
    cfg = RegimeConfig(compression_ratio=0.4, expansion_ratio=2.0,
                       median_window_bars=120)
    tuned = _StructurePipeline("BTCUSDT", store, regime_cfg=cfg,
                               shift_accel_atr_ratio=0.33)
    assert tuned._regime._cfg is cfg
    assert tuned._momentum._ratio == 0.33

    default = _StructurePipeline("ETHUSDT", store)          # no D9 args
    assert default._regime._cfg == RegimeConfig(0.6, 1.5, 240)
    assert default._momentum._ratio == 0.1


async def test_pipeline_g5_reflects_psychology_guard():
    """P4.9/D23.5: a locked psychology guard threads through to G5 in the
    payload — the whole bar goes NO_SIGNAL (behavioral circuit-breaker)."""
    from datetime import datetime, timedelta, timezone
    from marketscalper.core.bus import EventBus
    from marketscalper.core.state import StateStore
    from marketscalper.engines.psychology import PsychologyGuard
    from marketscalper.main import _wire_structure_engines
    from marketscalper.providers.base import Candle

    guard = PsychologyGuard()
    base = datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc)
    for i in range(9):                             # 9 taken today -> locked
        guard.record_taken(i, base + timedelta(minutes=i), "BTCUSDT", "win")

    bus = EventBus()
    store = StateStore(bus)
    _wire_structure_engines(bus, store, ["BTCUSDT"], psych_guard=guard)
    for i in range(31):                            # warm past G1 (30 candles)
        await bus.publish(Candle(
            symbol="BTCUSDT", tf="1m", ts=base + timedelta(minutes=i),
            o=100.0, h=100.5, l=99.5, c=100.2, v=1.0, qv=100.0,
            n_trades=1, taker_buy_v=0.5))
    qual = store.snapshot("BTCUSDT").structure["qualification"]
    g5 = qual["gates"][4]
    assert g5["name"] == "G5" and not g5["passed"] and not g5["flagged"]
    assert "hard lock" in g5["detail"]
    assert qual["verdict"] == "NO_SIGNAL"          # any gate fail -> no signal


async def test_pipeline_recommendation_carries_lifecycle_status():
    """P4.2: an admitted recommendation flows through the lifecycle wiring
    with a 'status' in the payload (starts 'active'); the pipeline exposes
    drain_lifecycle for the recorder."""
    from rec_dataset import rec_candles, rec_seed
    from marketscalper.core.bus import EventBus
    from marketscalper.core.state import StateStore
    from marketscalper.main import _wire_structure_engines
    from marketscalper.providers.base import Candle

    def fold(w):
        return Candle(symbol=w[0].symbol, tf="5m", ts=w[0].ts, o=w[0].o,
                      h=max(c.h for c in w), l=min(c.l for c in w),
                      c=w[-1].c, v=sum(c.v for c in w), qv=sum(c.qv for c in w),
                      n_trades=sum(c.n_trades for c in w),
                      taker_buy_v=sum(c.taker_buy_v for c in w))

    bus = EventBus()
    store = StateStore(bus)
    _wire_structure_engines(bus, store, ["BTCUSDT"],
                            seed_candles={"BTCUSDT": rec_seed("BTCUSDT")})
    win = []
    seen_active = False
    for candle in rec_candles("BTCUSDT"):
        await bus.publish(candle)
        win.append(candle)
        if len(win) == 5:
            await bus.publish(fold(win))
            win = []
        recs = store.snapshot("BTCUSDT").structure["recommendations"]
        for r in recs:
            assert "status" in r                       # P4.2 wiring ran
            if r["status"] == "active":
                seen_active = True
    assert seen_active                                 # a real rec was tracked


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


async def test_pipeline_projects_confluence_and_qualification_non_empty():
    """The D15/D16 payload mappings must be executed with real content
    (the OB/FVG-projection precedent); values empirically pinned on V1."""
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
    structure = store.snapshot("BTCUSDT").structure
    zones = structure["confluence"]
    assert zones and zones[0]["count"] == 5                # V1 magnet stack
    assert zones[0]["htf_magnet"] is True
    assert zones[0]["members"][0] == zones[0]["kind"] == "FVG"
    assert all(z["lo"] <= z["hi"] for z in zones)
    qual = structure["qualification"]
    assert qual["data_integrity"] == "PASS"
    assert qual["score"] == 38.5                           # empirically pinned
    assert qual["verdict"] == "BELOW_THRESHOLD"
    assert qual["components"]["structure"] == 100.0        # flip-tail aligned
    # D21.3: the composition attaches the Volume Engine — on the unseeded
    # V1 tail only the no-absorption/exhaustion item fires (rvol None,
    # delta 0 on synthetic 50/50 taker volumes, VWAP None mid-day start)
    assert qual["components"]["volume"] == 10.0
    assert qual["agreement"].endswith("rules aligned")
    assert all(g["passed"] for g in qual["gates"])
    assert qual["gates"][0]["flagged"]                     # no sampler wired


async def test_pipeline_g2_reads_bus_book_tickers():
    """BookTicker subscription (D16.2): a wide live spread must reach the
    G2 gate; a later unknown-symbol ticker must not overwrite it."""
    from datetime import datetime, timedelta, timezone
    from marketscalper.core.bus import EventBus
    from marketscalper.core.state import StateStore
    from marketscalper.main import _wire_structure_engines
    from marketscalper.providers.base import BookTicker, Candle

    m0 = datetime(2026, 7, 14, 19, 0, tzinfo=timezone.utc)

    def candle(minute, price):
        return Candle(symbol="BTCUSDT", tf="1m",
                      ts=m0 + timedelta(minutes=minute),
                      o=price, h=price + 1, l=price - 1, c=price,
                      v=1.0, qv=100.0, n_trades=1, taker_buy_v=0.5)

    bus = EventBus()
    store = StateStore(bus)
    _wire_structure_engines(bus, store, ["BTCUSDT"])
    await bus.publish(candle(0, 100.0))
    await bus.publish(BookTicker("BTCUSDT", 100.0, 1.0, 100.2, 1.0, m0))
    await bus.publish(BookTicker("XRPUSDT", 1.0, 1.0, 2.0, 1.0, m0))
    await bus.publish(candle(1, 100.0))
    g2 = store.snapshot("BTCUSDT").structure["qualification"]["gates"][1]
    assert g2["passed"] is False and not g2["flagged"]     # ~0.2% >= 0.05%
    assert g2["detail"].startswith("spread 0.19")


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

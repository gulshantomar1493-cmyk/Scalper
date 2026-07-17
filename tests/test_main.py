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


# ------------------------------------------------------- full composition


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

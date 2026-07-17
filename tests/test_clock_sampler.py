"""Tests for the clock-offset sampler (roadmap P0.11, Decision D6/A12).

Offset math is pure; sampling behavior is verified against a local aiohttp
server speaking the /api/v3/time payload shape — real sockets, no mocks.
"""

from __future__ import annotations

import asyncio

import aiohttp
from aiohttp import web

from marketscalper.providers.binance import (
    CLOCK_FAILURE_LIMIT,
    CLOCK_OFFSET_LIMIT_S,
    ClockOffsetSampler,
    compute_offset_s,
)


# --------------------------------------------------------------- offset math


def test_offset_zero_when_server_matches_midpoint():
    # request sent at 100.0, received at 100.2 -> midpoint 100.1
    assert compute_offset_s(100_100, 100.0, 100.2) == 0.0


def test_offset_sign_and_latency_neutralization():
    # server 3s ahead of midpoint, regardless of a slow (2s) round trip
    assert compute_offset_s(104_000, 100.0, 102.0) == 3.0
    # server 1.5s behind midpoint
    assert compute_offset_s(98_600, 100.0, 100.2) == -1.5


def test_in_sync_boundaries():
    s = ClockOffsetSampler()
    assert s.offset_s is None and s.in_sync is False        # unknown -> not in sync
    s._offset_s = CLOCK_OFFSET_LIMIT_S                      # exactly 2.0s
    assert s.in_sync is True                                # gate is |offset| > 2s
    s._offset_s = -CLOCK_OFFSET_LIMIT_S
    assert s.in_sync is True
    s._offset_s = CLOCK_OFFSET_LIMIT_S + 0.001
    assert s.in_sync is False


# ------------------------------------------------------- sampling behavior


async def _serve(handler) -> tuple[web.AppRunner, str]:
    app = web.Application()
    app.router.add_get("/api/v3/time", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


async def test_sample_once_measures_offset_within_tolerance():
    import time as _time

    async def handler(request):
        return web.json_response({"serverTime": int(_time.time() * 1000)})

    runner, base = await _serve(handler)
    try:
        sampler = ClockOffsetSampler(base_url=base)
        async with aiohttp.ClientSession() as session:
            offset = await sampler.sample_once(session)
        # local server shares our clock: offset must be ~0 (sub-second)
        assert offset is not None and abs(offset) < 0.5
        assert sampler.offset_s == offset and sampler.in_sync is True
    finally:
        await runner.cleanup()


async def test_sample_once_detects_skewed_server():
    import time as _time

    async def handler(request):
        return web.json_response({"serverTime": int((_time.time() + 5) * 1000)})

    runner, base = await _serve(handler)
    try:
        sampler = ClockOffsetSampler(base_url=base)
        async with aiohttp.ClientSession() as session:
            offset = await sampler.sample_once(session)
        assert offset is not None and 4.5 < offset < 5.5
        assert sampler.in_sync is False                     # > 2s -> out of sync
    finally:
        await runner.cleanup()


async def test_consecutive_failures_reset_offset_to_unknown():
    sampler = ClockOffsetSampler(base_url="http://127.0.0.1:1")  # nothing listens
    sampler._offset_s = 0.1                                  # previously known
    timeout = aiohttp.ClientTimeout(total=0.5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for i in range(CLOCK_FAILURE_LIMIT):
            assert await sampler.sample_once(session) is None
    assert sampler.offset_s is None and sampler.in_sync is False


async def test_success_resets_failure_count():
    import time as _time

    async def handler(request):
        return web.json_response({"serverTime": int(_time.time() * 1000)})

    runner, base = await _serve(handler)
    try:
        sampler = ClockOffsetSampler(base_url=base)
        sampler._failures = CLOCK_FAILURE_LIMIT - 1          # one away from unknown
        async with aiohttp.ClientSession() as session:
            assert await sampler.sample_once(session) is not None
        assert sampler._failures == 0 and sampler.in_sync is True
    finally:
        await runner.cleanup()


async def test_start_stop_lifecycle_runs_periodic_samples():
    import time as _time
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return web.json_response({"serverTime": int(_time.time() * 1000)})

    runner, base = await _serve(handler)
    try:
        sampler = ClockOffsetSampler(base_url=base, interval_s=0.05)
        await sampler.start()
        await asyncio.sleep(0.3)
        await sampler.stop()
        assert calls >= 3 and sampler.in_sync is True        # sampled repeatedly
        n = calls
        await asyncio.sleep(0.15)
        assert calls == n                                    # stopped means stopped
    finally:
        await runner.cleanup()

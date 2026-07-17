"""Proves pytest-asyncio wiring works (asyncio_mode = "auto").

Everything from P0.9 onward is asyncio code; this smoke test guarantees the
harness can run coroutine tests before any of that lands.
"""

from __future__ import annotations

import asyncio


async def test_event_loop_runs_coroutine_tests():
    await asyncio.sleep(0)
    value = await _echo("harness-ok")
    assert value == "harness-ok"


async def _echo(value: str) -> str:
    await asyncio.sleep(0)
    return value

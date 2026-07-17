"""Determinism harness v0 (roadmap P0.26; Architecture §10).

Identical replay -> identical output: the exact same stored dataset is
replayed twice through ReplayFeed and the emitted normalized event stream
is reduced to a canonical byte string and hashed (sha256). Both hashes must
be byte-identical; any difference fails the build.

The hash is built ONLY from the normalized emitted events, in emission
order — field values and event type, nothing else. No internal state, no
timings, no object ids, no counters. Runs inside the normal pytest step of
scripts/ci.sh (which is why ci.sh itself is untouched).
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone

from conftest import TxPool

from marketscalper import db
from marketscalper.core.bus import EventBus
from marketscalper.providers.base import Candle
from marketscalper.providers.replay import ReplayFeed

UTC = timezone.utc
M0 = datetime(2026, 7, 14, 19, 0, tzinfo=UTC)  # minute bucket divisible by 5

# Deterministic dataset: two symbols, two full 5m windows, plus a gap that
# exercises the partial-window discard path — every replay code path emits.
_MINUTES = list(range(10)) + [17, 18, 19]


def _candle(symbol: str, minute: int, base: float) -> Candle:
    ts = M0 + timedelta(minutes=minute)
    o = base + minute
    return Candle(symbol=symbol, tf="1m", ts=ts, o=o, h=o + 2.25, l=o - 1.5,
                  c=o + 0.75, v=1.5 + minute * 0.25, qv=o * 1.5,
                  n_trades=3 + minute, taker_buy_v=0.5 + minute * 0.125)


DATASET = [_candle("BTCUSDT", m, 100.0) for m in _MINUTES] + \
          [_candle("ETHUSDT", m, 3500.0) for m in _MINUTES]

RANGE = (M0, M0 + timedelta(minutes=20))


def canonical_event(e: Candle) -> str:
    """One event -> canonical text: type + every normalized field, exactly.

    repr() gives exact float bytes; isoformat gives exact timestamps.
    Nothing timing-dependent, no ids, no internal state."""
    return "|".join((
        type(e).__name__, e.symbol, e.tf, e.ts.isoformat(),
        repr(e.o), repr(e.h), repr(e.l), repr(e.c),
        repr(e.v), repr(e.qv), repr(e.n_trades), repr(e.taker_buy_v),
    ))


def stream_hash(events: list[Candle]) -> str:
    """sha256 over the canonical stream, in emission order."""
    payload = "\n".join(canonical_event(e) for e in events)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _replay_once(db_conn) -> list[Candle]:
    bus = EventBus()
    events: list[Candle] = []

    async def collect(e):
        events.append(e)

    bus.subscribe(Candle, collect)
    feed = ReplayFeed(["BTCUSDT", "ETHUSDT"], bus, TxPool(db_conn),
                      RANGE[0], RANGE[1], speed="max")
    await feed.start()
    for _ in range(500):
        if feed._task is not None and feed._task.done():
            break
        await asyncio.sleep(0.01)
    await feed.stop()
    return events


# ------------------------------------------------------------------- gate


async def test_identical_replay_produces_byte_identical_hash(db_conn):
    """The §10 non-negotiable: same input candles -> byte-identical output.
    A difference here means a repaint/nondeterminism bug somewhere."""
    await db.insert_candles(
        db_conn,
        [(c.symbol, c.tf, c.ts, c.o, c.h, c.l, c.c, c.v, c.qv,
          c.n_trades, c.taker_buy_v) for c in DATASET],
    )

    first = await _replay_once(db_conn)
    second = await _replay_once(db_conn)

    assert len(first) > len(DATASET)          # 1m stream plus 5m closes emitted
    assert stream_hash(first) == stream_hash(second)


def test_hash_is_sensitive_to_any_stream_difference():
    """The gate itself must be able to fail: value, order and length changes
    all alter the hash."""
    base = [_candle("BTCUSDT", m, 100.0) for m in range(3)]
    assert stream_hash(base) == stream_hash(list(base))            # stable

    changed_value = [base[0], _candle("BTCUSDT", 1, 100.000001), base[2]]
    assert stream_hash(changed_value) != stream_hash(base)

    reordered = [base[1], base[0], base[2]]
    assert stream_hash(reordered) != stream_hash(base)

    truncated = base[:2]
    assert stream_hash(truncated) != stream_hash(base)

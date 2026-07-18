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


# ------------------------------------------------- harness v1 (roadmap P1.21)
# Grown per Part D note 4: the object stream — every engine payload the
# composition publishes (pivots+labels, trend, BOS/CHOCH, trendlines,
# channels, liquidity pools/levels/sweeps) — must be byte-identical across a
# double replay. The stream is the composition's own JSON payload,
# canonicalized with sorted keys.

import json  # noqa: E402


# Session-crossing window (LONDON 08:00 observed from its boundary,
# completing at 13:00) so level promotion and session bookkeeping are part
# of the hashed object stream. The tail (minutes 300+) walks the P1.11
# flip-journey shape, empirically verified through the real pipelines to
# fire a WEAK BOS (displacement False -> no OB: the qualification is in
# the hash) and then a displacement BOS DOWN that creates an order block.
V1_M0 = datetime(2026, 7, 14, 7, 30, tzinfo=UTC)
V1_MINUTES = 335                                   # 07:30 -> 13:05 UTC

_V1_SHAPE = [(10, 9), (11, 10), (12, 11), (15, 14), (12, 11), (11, 10),
             (10, 9), (11, 10), (12, 11), (13, 12), (17, 16), (14, 13),
             (13, 12), (12, 11), (13, 12), (14, 13), (15, 14), (18, 17),
             (10, 9), (9, 8), (8, 7), (9, 8), (10, 9), (9, 8), (8, 7),
             (7, 6), (6, 5)]


def _v1_tail(offset: int) -> tuple:
    """(o, h, l, c) relative to base for tail minute `offset`."""
    if offset < len(_V1_SHAPE):
        h, l = _V1_SHAPE[offset]
        if offset == 17:                           # BOS-UP bar, fat body
            return (13.2, h, 13.0, h)
        if offset == 26:                           # displacement crash bar
            return (6.0, 6.0, 0.8, 1.0)
        return (l, h, l, h)                        # full-body bullish
    return (1.0, 1.5, 0.5, 1.0)                    # benign pad past 13:00


def _v1_candle(symbol: str, minute: int, base: float) -> Candle:
    """Oscillating dataset with a tie-breaking drift: pivots on both
    chains, labels, trend states, pools, session levels, BOS and order
    blocks all emit."""
    if minute < 300:
        o = base + ((minute * 7) % 13) - 6 + minute * 0.01
        h = o + ((minute * 5) % 7) + 1
        l = o - ((minute * 3) % 5) - 1
        c = o + ((minute * 2) % 3) - 1
    else:
        ro, rh, rl, rc = _v1_tail(minute - 300)
        o, h, l, c = base + ro, base + rh, base + rl, base + rc
    ts = V1_M0 + timedelta(minutes=minute)
    return Candle(symbol=symbol, tf="1m", ts=ts, o=o, h=h, l=l, c=c,
                  v=1.0, qv=o, n_trades=2, taker_buy_v=0.5)


V1_DATASET = [_v1_candle("BTCUSDT", m, 100.0) for m in range(V1_MINUTES)] + \
             [_v1_candle("ETHUSDT", m, 3500.0) for m in range(V1_MINUTES)]
V1_RANGE = (V1_M0, V1_M0 + timedelta(minutes=V1_MINUTES))


async def _replay_object_stream_once(db_conn) -> list[str]:
    """Replay through the REAL composition pipelines; canonicalize every
    published structure payload (per symbol, per closed candle)."""
    from marketscalper.core.state import StateStore
    from marketscalper.main import _wire_structure_engines

    bus = EventBus()
    store = StateStore(bus)
    _wire_structure_engines(bus, store, ["BTCUSDT", "ETHUSDT"])
    stream: list[str] = []

    async def collect(candle: Candle) -> None:      # subscribed AFTER engines
        state = store.snapshot(candle.symbol)
        if state is not None and state.structure is not None:
            stream.append(candle.symbol + "|" +
                          json.dumps(state.structure, sort_keys=True))

    bus.subscribe(Candle, collect)
    feed = ReplayFeed(["BTCUSDT", "ETHUSDT"], bus, TxPool(db_conn),
                      V1_RANGE[0], V1_RANGE[1], speed="max")
    await feed.start()
    for _ in range(500):
        if feed._task is not None and feed._task.done():
            break
        await asyncio.sleep(0.01)
    await feed.stop()
    return stream


async def test_v1_object_stream_byte_identical_across_double_replay(db_conn):
    await db.insert_candles(
        db_conn,
        [(c.symbol, c.tf, c.ts, c.o, c.h, c.l, c.c, c.v, c.qv,
          c.n_trades, c.taker_buy_v) for c in V1_DATASET],
    )
    first = await _replay_object_stream_once(db_conn)
    second = await _replay_object_stream_once(db_conn)
    assert len(first) >= len(V1_DATASET)            # every close published
    joined = "\n".join(first)
    assert '"pivots": [{' in joined                 # objects actually emitted
    assert '"trend": "' in joined                   # trend classified
    assert '"LONDON_H"' in joined                   # session level promoted
    assert '"pools": [{' in joined                  # liquidity pools emitted
    assert '"premium_discount": "' in joined        # 5m external range live
    assert '"displacement": false' in joined        # weak BOS hashed...
    assert '"displacement": true' in joined         # ...and a qualified one
    assert '"blocks": [{' in joined                 # OB content in the hash
    assert '"fvgs": [{' in joined                   # FVG content in the hash
    h1 = hashlib.sha256(joined.encode()).hexdigest()
    h2 = hashlib.sha256("\n".join(second).encode()).hexdigest()
    assert h1 == h2                                 # §10, non-negotiable


def test_v1_canonicalization_is_sensitive():
    a = json.dumps({"trend": "BULLISH", "pivots": [{"price": 100.0}]},
                   sort_keys=True)
    b = json.dumps({"trend": "BULLISH", "pivots": [{"price": 100.000001}]},
                   sort_keys=True)
    assert hashlib.sha256(a.encode()).hexdigest() != \
        hashlib.sha256(b.encode()).hexdigest()

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


# ------------------------- harness v2 (roadmap P1.21 -> P2.23: all objects)
# Grown per Part D note 4: the object stream — every engine payload the
# composition publishes (pivots+labels, trend, BOS/CHOCH, trendlines,
# channels, liquidity pools/levels/sweeps/shifts, order blocks/breakers,
# FVGs, confluence, qualification, volume) — must be byte-identical across
# a double replay. The stream is the composition's own JSON payload,
# canonicalized with sorted keys. Two datasets: V1 (session-crossing +
# flip-tail) and V2 (the P2.24-A gate episodes, which fire the object
# families V1 cannot: CHOCH, sweep+shift, breakers, channels).

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


async def _replay_object_stream_once(db_conn, replay_range=None,
                                     symbols=("BTCUSDT", "ETHUSDT"),
                                     seed_candles=None) -> list[str]:
    """Replay through the REAL composition pipelines; canonicalize every
    published structure payload (per symbol, per closed candle).

    seed_candles (D19.2): the same RVOL seed the live composition / F2
    replay apply — passed directly to the wiring (not the DB), so a
    seeded stream (V4) can reach TRADEABLE and carry recommendations."""
    from marketscalper.core.state import StateStore
    from marketscalper.main import _wire_structure_engines

    start, end = replay_range or V1_RANGE
    symbols = list(symbols)
    bus = EventBus()
    store = StateStore(bus)
    _wire_structure_engines(bus, store, symbols, seed_candles=seed_candles)
    stream: list[str] = []

    async def collect(candle: Candle) -> None:      # subscribed AFTER engines
        state = store.snapshot(candle.symbol)
        if state is not None and state.structure is not None:
            stream.append(candle.symbol + "|" +
                          json.dumps(state.structure, sort_keys=True))

    bus.subscribe(Candle, collect)
    feed = ReplayFeed(symbols, bus, TxPool(db_conn), start, end, speed="max")
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
    assert '"confluence": [{' in joined             # D15 zones in the hash
    assert '"volume": {' in joined                  # D19 content in the hash
    assert '"anchored_vwap": 1' in joined           # a real A8-anchored value
    assert '"rvol": null' in joined                 # unseeded warm-up (D7)
    assert '"htf_magnet": true' in joined           # a real 3+ stack hashed
    assert '"verdict": "NO_SIGNAL"' in joined       # G1 warming era...
    assert '"verdict": "BELOW_THRESHOLD"' in joined  # ...and scored era
    assert 'rules aligned"' in joined               # A14 display string
    assert '"trendlines": [{' in joined             # kept lines in the hash
    assert '"sweeps": [{' in joined                 # sweep events in the hash
    h1 = hashlib.sha256(joined.encode()).hexdigest()
    h2 = hashlib.sha256("\n".join(second).encode()).hexdigest()
    assert h1 == h2                                 # §10, non-negotiable


async def test_v2_object_stream_byte_identical_across_double_replay(db_conn):
    """P2.23: the object families V1 cannot fire — CHOCH, sweep+shift,
    breakers, channels — carried with real content in the hashed stream,
    byte-identical across a double replay (the gate-episode dataset)."""
    from gate_dataset import GATE_M0, gate_candles

    v2 = (gate_candles(6, "BTCUSDT", 100.0) +
          gate_candles(6, "ETHUSDT", 3500.0))
    await db.insert_candles(
        db_conn,
        [(c.symbol, c.tf, c.ts, c.o, c.h, c.l, c.c, c.v, c.qv,
          c.n_trades, c.taker_buy_v) for c in v2],
    )
    v2_range = (GATE_M0, GATE_M0 + timedelta(minutes=len(v2) // 2))
    first = await _replay_object_stream_once(db_conn, v2_range)
    second = await _replay_object_stream_once(db_conn, v2_range)
    assert len(first) >= len(v2)                    # every close published
    joined = "\n".join(first)
    assert '"choch": [{' in joined                  # CHOCH events hashed
    assert '"shifts": [{' in joined                 # sweep+shift hashed
    assert '"breakers": [{' in joined               # breaker zones hashed
    assert '"channels": [{' in joined               # channels hashed
    assert '"trend": "BEARISH"' in joined           # full trend vocabulary...
    assert '"trend": "BULLISH"' in joined
    # D20.2 reversal check at composition level: every gate episode pairs
    # a LOW sweep with a DOWN (continuation) CHOCH — S1 must refuse all
    # of them, and S2/S3 cannot fire unseeded (D20.6) -> no signals ever,
    # hence no recommendations either (D21.2).
    assert '"signals": [{' not in joined
    assert '"recommendations": [{' not in joined
    h1 = hashlib.sha256(joined.encode()).hexdigest()
    h2 = hashlib.sha256("\n".join(second).encode()).hexdigest()
    assert h1 == h2                                 # §10, non-negotiable


async def test_v3_signal_stream_byte_identical_across_double_replay(db_conn):
    """P3.12-P3.16 (the P3.20 signals guard): the engineered S1 reversal
    dataset fires strategy S1 through the complete composition chain via
    the real DB replay path, with the signal content byte-identical
    across a double replay. S2/S3 stay silent (rvol unseeded, D20.6)."""
    from s1_dataset import S1_M0, S1_MINUTES, s1_candles

    v3 = s1_candles()
    await db.insert_candles(
        db_conn,
        [(c.symbol, c.tf, c.ts, c.o, c.h, c.l, c.c, c.v, c.qv,
          c.n_trades, c.taker_buy_v) for c in v3],
    )
    v3_range = (S1_M0, S1_M0 + timedelta(minutes=S1_MINUTES))
    first = await _replay_object_stream_once(db_conn, v3_range)
    second = await _replay_object_stream_once(db_conn, v3_range)
    assert len(first) >= len(v3)                    # every close published
    joined = "\n".join(first)
    assert '"signals": [{' in joined                # signal content hashed
    assert '"strategy": "S1"' in joined
    assert '"direction": "LONG"' in joined
    assert 'swept ASIA_L (LOW)' in joined           # §8 fact trace hashed
    assert '"strategy": "S2"' not in joined         # unseeded -> silent
    assert '"strategy": "S3"' not in joined
    # D21.7/D21.8: the recommendations key is carried (structural) but
    # stays empty — the S1 bar scores 52.5 (BELOW_THRESHOLD): Structure
    # forfeits the opposing-CHOCH +20 (its own reversal CHOCH), Liquidity
    # loses the +40 pool item (ASIA_L is a key level, not a pool), and
    # unseeded Volume loses the +40 rvol item and the +30 delta item (the
    # synthetic 50/50 taker split zeroes delta). The recommendation-
    # carrying stream is P3.20's milestone (seeded rvol).
    assert '"recommendations": [' in joined
    assert '"recommendations": [{' not in joined
    # exactly one signal, with the D20.2 geometry invariants
    final = json.loads(first[-1].split("|", 1)[1])
    assert len(final["signals"]) == 1
    sig = final["signals"][0]
    assert sig["sl"] < 96.40                        # beyond the sweep wick
    assert 97.0 < sig["entry"] < 97.90              # inside the entry zone
    assert sig["tp1"] > 104.4                       # the EQH pool target
    assert sig["tp1"] >= sig["entry"] + (sig["entry"] - sig["sl"])  # 1R
    assert sig["invalid_after_bars"] == 5
    h1 = hashlib.sha256(joined.encode()).hexdigest()
    h2 = hashlib.sha256("\n".join(second).encode()).hexdigest()
    assert h1 == h2                                 # §10, non-negotiable


async def test_v4_recommendation_stream_byte_identical_across_double_replay(
        db_conn):
    """P3.20: the recommendation-carrying stream. The seeded rec_dataset
    reaches TRADEABLE and emits a real S1 recommendation through the
    complete composition (qualification -> planner -> admission), with
    the recommendation CONTENT byte-identical across a double replay —
    no payload family is structurally-guarded-only anymore (P2.23)."""
    from rec_dataset import REC_M0, REC_MINUTES, rec_candles, rec_seed

    v4 = rec_candles("BTCUSDT")
    await db.insert_candles(
        db_conn,
        [(c.symbol, c.tf, c.ts, c.o, c.h, c.l, c.c, c.v, c.qv,
          c.n_trades, c.taker_buy_v) for c in v4],
    )
    v4_range = (REC_M0, REC_M0 + timedelta(minutes=REC_MINUTES))
    seed = {"BTCUSDT": rec_seed("BTCUSDT")}
    first = await _replay_object_stream_once(
        db_conn, v4_range, symbols=("BTCUSDT",), seed_candles=seed)
    second = await _replay_object_stream_once(
        db_conn, v4_range, symbols=("BTCUSDT",), seed_candles=seed)
    joined = "\n".join(first)
    # non-empty recommendation content IS in the hashed stream
    assert '"recommendations": [{' in joined
    assert '"verdict": "TRADEABLE"' in joined
    assert '"net_rr_tp1":' in joined
    assert 'move SL to break-even' in joined         # §7 guidance text hashed
    # the admitted recommendation's geometry (D21.2 + §7)
    final = None
    for line in first:
        payload = json.loads(line.split("|", 1)[1])
        if payload["recommendations"]:
            final = payload["recommendations"][-1]
    assert final is not None
    assert final["strategy"] == "S1" and final["direction"] == "LONG"
    assert final["tp1"] > 104.0                      # the EQH pool target
    assert final["net_rr_tp1"] >= 1.0                # planner floor (D17)
    assert final["sl"] < final["entry"] < final["tp1"]
    assert len(final["guidance"]) == 4               # §7 management lines
    h1 = hashlib.sha256(joined.encode()).hexdigest()
    h2 = hashlib.sha256("\n".join(second).encode()).hexdigest()
    assert h1 == h2                                  # §10, non-negotiable


class _CapturingRecorder:
    """A SignalRecorder-shaped stand-in that serializes the rows it would
    persist (D21.1/D21.2 fields) into canonical strings instead of hitting
    a database — so a double run's persisted output can be compared."""

    def __init__(self):
        self.rows: list[str] = []

    async def record(self, symbol, records, payload):
        snapshot = json.dumps(payload, sort_keys=True) if payload else None
        for signal, qual, plan, rec in records:
            self.rows.append(json.dumps({
                "kind": "signal", "ts": signal.created_ts.isoformat(),
                "symbol": symbol, "tf": "1m", "strategy": signal.strategy,
                "direction": signal.direction, "score": qual.score,
                "gates": {"verdict": qual.verdict,
                          "integrity": qual.data_integrity},
                "components": qual.components,
                "snapshot_len": len(snapshot) if snapshot else 0,
            }, sort_keys=True))
            if rec is not None:
                self.rows.append(json.dumps({
                    "kind": "recommendation", "ts": signal.created_ts.isoformat(),
                    "direction": signal.direction, "entry": plan.entry,
                    "sl": plan.sl, "tp1": plan.tp1, "tp2": plan.tp2,
                    "qty": plan.qty, "risk_amt": plan.risk_amt,
                    "est_fees": plan.qty * plan.fee_per_unit,
                    "net_rr_tp1": plan.net_rr_tp1,
                }, sort_keys=True))


async def _record_rows_once() -> str:
    """Drive the seeded rec_dataset through the real composition with a
    capturing recorder (the wiring's own on_candle hop persists) and
    return the newline-joined serialized rows."""
    from rec_dataset import rec_candles, rec_seed
    from marketscalper.core.state import StateStore
    from marketscalper.main import _wire_structure_engines

    bus = EventBus()
    store = StateStore(bus)
    cap = _CapturingRecorder()
    _wire_structure_engines(bus, store, ["BTCUSDT"], recorder=cap,
                            seed_candles={"BTCUSDT": rec_seed()})
    win = []
    for candle in rec_candles("BTCUSDT"):
        await bus.publish(candle)
        win.append(candle)
        if len(win) == 5:                            # feed the 5m context
            w = win
            await bus.publish(Candle(
                symbol=w[0].symbol, tf="5m", ts=w[0].ts, o=w[0].o,
                h=max(c.h for c in w), l=min(c.l for c in w), c=w[-1].c,
                v=sum(c.v for c in w), qv=sum(c.qv for c in w),
                n_trades=sum(c.n_trades for c in w),
                taker_buy_v=sum(c.taker_buy_v for c in w)))
            win = []
    return "\n".join(cap.rows)


async def test_persisted_signal_and_recommendation_rows_byte_identical():
    """P3.20 / §10 ('byte-identical signals table'): the persisted rows
    themselves — not just the payload — are deterministic. Two independent
    composition passes over the seeded rec_dataset, each serializing every
    (signal, recommendation) row from the SignalRecorder's own fields, must
    be byte-identical. Proves the records the persistence path receives are
    deterministic (the SignalRecorder's serialization itself is covered by
    the test_recorder round-trips)."""
    first = await _record_rows_once()
    second = await _record_rows_once()
    assert first, "expected at least one persisted row"
    assert '"kind": "signal"' in first               # a real signal row
    assert '"strategy": "S1"' in first
    assert '"kind": "recommendation"' in first       # a real rec row
    assert first == second                           # byte-identical rows


def test_v1_canonicalization_is_sensitive():
    a = json.dumps({"trend": "BULLISH", "pivots": [{"price": 100.0}]},
                   sort_keys=True)
    b = json.dumps({"trend": "BULLISH", "pivots": [{"price": 100.000001}]},
                   sort_keys=True)
    assert hashlib.sha256(a.encode()).hexdigest() != \
        hashlib.sha256(b.encode()).hexdigest()

"""Automated Phase-2 validation gate (roadmap P2.24-A; replaces the
manual 50-sweep/50-OB audit with 100%-coverage automated verification).

Scope (owner-approved): implementation correctness, determinism (via the
harness in test_determinism.py), lifecycle integrity, and conformance to
the frozen D12/D13/D14 rules. It does NOT evaluate trading effectiveness
or the market quality of the rules — that belongs to later strategy/
performance phases and final pre-release market validation.

Structure: `capture_run` drives the REAL composition over the gate
dataset and records every emitted sweep/shift/CHOCH/BOS/OB/breaker/FVG
with its full per-bar status history. Pure verifier functions then check
EVERY captured object against independent recomputation from the raw
candles (the P1.1 independent-verifier precedent) and against the legal
lifecycle grammars. Verifiers return violation lists; gate tests assert
emptiness; self-tests corrupt copies of a captured run and assert the
verifiers FAIL — proving the gate is not vacuous. Minimum-count floors
(>= the original manual gate's 50+50) prove density materialized.

Validation-only: no production code is imported beyond the composition
entry points every composition test already uses.
"""

from __future__ import annotations

import asyncio
import copy

import pytest

from gate_dataset import gate_candles
from marketscalper.core.bus import EventBus
from marketscalper.core.state import StateStore
from marketscalper.main import _wire_structure_engines

SWEEP_WICK_RATIO = 0.6          # frozen D12.4 literal, restated for the
OB_LOOKBACK = 20                # independent verifiers (D13.1)
SHIFT_WINDOW = (1, 3)           # D12.5: CHOCH within bars +1..+3


class GateCapture:
    """Everything the composition emitted over one run, by identity."""

    def __init__(self):
        self.candles = []
        self.bar_of_ts = {}
        self.sweeps = {}            # ts -> {record, bar, prev_pools, prev_levels}
        self.shifts = {}            # (sweep_ts, ts) -> record
        self.choch_ts = set()
        self.bos = {}               # ts -> record
        self.blocks = {}            # (direction, created_ts) -> {bar: status}
        self.breakers = {}          # (direction, created_ts) -> {bar: status}
        self.fvgs = {}              # (direction, created_ts) -> {bar: status}


def capture_run(candles, symbol="BTCUSDT"):
    async def run():
        bus = EventBus()
        store = StateStore(bus)
        _wire_structure_engines(bus, store, [symbol])
        cap = GateCapture()
        prev_pools, prev_levels = [], {}
        for bar, candle in enumerate(candles):
            await bus.publish(candle)
            cap.candles.append(candle)
            cap.bar_of_ts[candle.ts.isoformat()] = bar
            st = store.snapshot(symbol).structure
            liq = st["liquidity"]
            for s in liq["sweeps"]:
                if s["ts"] not in cap.sweeps:
                    cap.sweeps[s["ts"]] = {
                        "record": s, "bar": cap.bar_of_ts.get(s["ts"]),
                        "prev_pools": list(prev_pools),
                        "prev_levels": dict(prev_levels)}
            for s in liq["shifts"]:
                cap.shifts[(s["sweep_ts"], s["ts"])] = s
            for e in st["choch"]:
                cap.choch_ts.add(e["ts"])
            for e in st["bos"]:
                cap.bos.setdefault(e["ts"], e)
            for b in st["orderblocks"]["blocks"]:
                cap.blocks.setdefault(
                    (b["direction"], b["created_ts"]), {})[bar] = b
            for b in st["orderblocks"]["breakers"]:
                cap.breakers.setdefault(
                    (b["direction"], b["created_ts"]), {})[bar] = b
            for g in st["fvgs"]:
                cap.fvgs.setdefault(
                    (g["direction"], g["created_ts"]), {})[bar] = g
            prev_pools, prev_levels = liq["pools"], liq["levels"]
        return cap
    return asyncio.run(run())


# ------------------------------------------------- independent verifiers


def verify_sweeps(cap):
    """Every sweep: D12.4 wick geometry recomputed from the raw candle +
    the swept target existed (pool or promoted level) entering the bar."""
    out = []
    for ts, entry in cap.sweeps.items():
        rec, bar = entry["record"], entry["bar"]
        if bar is None:
            out.append(f"sweep {ts}: no candle at its timestamp")
            continue
        c = cap.candles[bar]
        rng = c.h - c.l
        price = rec["price"]
        if rng <= 0:
            out.append(f"sweep {ts}: zero-range candle")
            continue
        if rec["side"] == "HIGH":
            wick = c.h - max(c.o, c.c)
            geom = c.h > price and c.c < price
        else:
            wick = min(c.o, c.c) - c.l
            geom = c.l < price and c.c > price
        if not geom:
            out.append(f"sweep {ts}: wick/close geometry violated")
        if not wick > SWEEP_WICK_RATIO * rng:
            out.append(f"sweep {ts}: wick {wick:.3f} <= 60% of range {rng:.3f}")
        if rec["target"] in ("EQH", "EQL"):
            if not any(p["kind"] == rec["target"] and p["price"] == price
                       for p in entry["prev_pools"]):
                out.append(f"sweep {ts}: no {rec['target']} pool at {price}")
        else:
            if entry["prev_levels"].get(rec["target"]) != price:
                out.append(f"sweep {ts}: level {rec['target']} != {price}")
    seen_pairs = {}
    for ts, entry in cap.sweeps.items():
        pair = (entry["record"]["target"], entry["record"]["price"])
        if pair in seen_pairs:
            out.append(f"sweep {ts}: duplicate of {seen_pairs[pair]} {pair}")
        seen_pairs[pair] = ts
    return out


def verify_shifts(cap):
    """Every sweep+shift: its sweep exists, a CHOCH fired at the tag bar,
    and the distance is inside the frozen +1..+3 window (D12.5)."""
    out = []
    for (sweep_ts, ts), _rec in cap.shifts.items():
        if sweep_ts not in cap.sweeps:
            out.append(f"shift {ts}: unknown sweep {sweep_ts}")
            continue
        if ts not in cap.choch_ts:
            out.append(f"shift {ts}: no CHOCH at the tagging bar")
        gap = cap.bar_of_ts[ts] - cap.bar_of_ts[sweep_ts]
        if not SHIFT_WINDOW[0] <= gap <= SHIFT_WINDOW[1]:
            out.append(f"shift {ts}: distance {gap} outside +1..+3")
    return out


def verify_obs(cap):
    """Every order block: created on a displacement-True BOS of matching
    direction, zone recomputed from the last opposite-color candle
    within the 20-bar lookback (D13.1, S4.5 verbatim)."""
    out = []
    for (direction, created_ts), bars in cap.blocks.items():
        bar = cap.bar_of_ts.get(created_ts)
        tag = f"OB {direction} {created_ts}"
        if bar is None:
            out.append(f"{tag}: no candle at creation ts")
            continue
        bos = cap.bos.get(created_ts)
        if bos is None or bos["displacement"] is not True:
            out.append(f"{tag}: no displacement-True BOS at creation bar")
            continue
        if bos["direction"] != ("UP" if direction == "BULL" else "DOWN"):
            out.append(f"{tag}: BOS direction mismatch")
        source = None
        for j in range(bar - 1, max(-1, bar - 1 - OB_LOOKBACK), -1):
            cj = cap.candles[j]
            if (cj.c < cj.o) if direction == "BULL" else (cj.c > cj.o):
                source = cj
                break
        if source is None:
            out.append(f"{tag}: no opposite-color source in lookback")
            continue
        first = bars[min(bars)]
        zone = ((source.l, source.o) if direction == "BULL"
                else (source.o, source.h))
        if (first["lo"], first["hi"]) != zone:
            out.append(f"{tag}: zone {first['lo'], first['hi']} != {zone}")
        if first["status"] != "active" or min(bars) != bar:
            out.append(f"{tag}: not active at its creation bar")
    return out


def verify_fvgs(cap):
    """Every FVG: the 3-candle imbalance recomputed from raw candles
    (D14.1 strict inequalities, zone bounds verbatim)."""
    out = []
    for (direction, created_ts), bars in cap.fvgs.items():
        bar = cap.bar_of_ts.get(created_ts)
        tag = f"FVG {direction} {created_ts}"
        if bar is None or bar < 2:
            out.append(f"{tag}: no 3-candle window")
            continue
        c1, c3 = cap.candles[bar - 2], cap.candles[bar]
        first = bars[min(bars)]
        if direction == "BULL":
            ok = c1.h < c3.l and (first["lo"], first["hi"]) == (c1.h, c3.l)
        else:
            ok = c1.l > c3.h and (first["lo"], first["hi"]) == (c3.h, c1.l)
        if not ok:
            out.append(f"{tag}: imbalance/zone mismatch")
    return out


# ---------------------------------------------------- lifecycle grammars


def _lifecycle_violations(identity_bars, legal_next, tag):
    out = []
    bars = sorted(identity_bars)
    if bars != list(range(bars[0], bars[-1] + 1)):
        out.append(f"{tag}: presence gap (resurrection)")
    prev = None
    for b in bars:
        status = identity_bars[b]["status"]
        if prev is not None and status != prev and status not in legal_next.get(prev, ()):
            out.append(f"{tag}: illegal transition {prev} -> {status}")
        prev = status
    return out


def verify_ob_lifecycle(cap):
    """active -> mitigated only, contiguous presence, breakers born from
    a same-zone opposite-direction block vanishing that bar (D13.2/3)."""
    out = []
    for (direction, created_ts), bars in cap.blocks.items():
        out += _lifecycle_violations(bars, {"active": ("mitigated",)},
                                     f"OB {direction} {created_ts}")
    for (direction, created_ts), bars in cap.breakers.items():
        tag = f"breaker {direction} {created_ts}"
        out += _lifecycle_violations(bars, {"active": ("mitigated",)}, tag)
        birth = min(bars)
        zone = (bars[birth]["lo"], bars[birth]["hi"])
        opposite = "BEAR" if direction == "BULL" else "BULL"
        died = False
        for (bdir, bts), bbars in cap.blocks.items():
            if bdir != opposite or birth - 1 not in bbars or birth in bbars:
                continue
            b = bbars[birth - 1]
            if (b["lo"], b["hi"]) == zone:
                died = True
                break
        if not died:
            out.append(f"{tag}: no same-zone {opposite} block died at birth")
    return out


def verify_fvg_lifecycle(cap):
    out = []
    for (direction, created_ts), bars in cap.fvgs.items():
        out += _lifecycle_violations(bars, {"active": ("ce_tested",)},
                                     f"FVG {direction} {created_ts}")
    return out


ALL_VERIFIERS = (verify_sweeps, verify_shifts, verify_obs, verify_fvgs,
                 verify_ob_lifecycle, verify_fvg_lifecycle)


# ------------------------------------------------------------ the gate


@pytest.fixture(scope="module")
def gate_run():
    return capture_run(gate_candles(60))


def test_gate_density_floors(gate_run):
    """>= the original manual gate's sample sizes — but at 100% verified
    coverage instead of eyeballed sampling."""
    assert len(gate_run.sweeps) >= 50
    assert len(gate_run.blocks) >= 50
    assert len(gate_run.breakers) >= 40
    assert len(gate_run.fvgs) >= 100
    assert len(gate_run.choch_ts) >= 50
    assert len(gate_run.shifts) >= 50
    assert sum(1 for b in gate_run.bos.values()
               if b["displacement"] is True) >= 50


def test_gate_full_conformance_and_lifecycle(gate_run):
    for verifier in ALL_VERIFIERS:
        assert verifier(gate_run) == [], verifier.__name__


# ------------------------------------------------- edge / empty datasets


def test_gate_empty_dataset():
    cap = capture_run([])
    assert not cap.sweeps and not cap.blocks and not cap.fvgs
    for verifier in ALL_VERIFIERS:
        assert verifier(cap) == []


def test_gate_tiny_flat_dataset():
    cap = capture_run(gate_candles(1)[:5])
    assert not cap.sweeps and not cap.blocks and not cap.breakers
    for verifier in ALL_VERIFIERS:
        assert verifier(cap) == []


# ------------------------------- self-tests: the gate itself can fail


@pytest.fixture(scope="module")
def small_run():
    return capture_run(gate_candles(3))


def _corrupt(run):
    return copy.deepcopy(run)


def test_selftest_sweep_price_corruption_is_caught(small_run):
    bad = _corrupt(small_run)
    ts = next(iter(bad.sweeps))
    bad.sweeps[ts]["record"]["price"] += 1.0
    assert verify_sweeps(bad) != []


def test_selftest_sweep_duplicate_target_is_caught(small_run):
    bad = _corrupt(small_run)
    ts = next(iter(bad.sweeps))
    clone = copy.deepcopy(bad.sweeps[ts])
    bad.sweeps["1999-01-01T00:00:00+00:00"] = clone   # same (target, price)
    assert any("duplicate" in v for v in verify_sweeps(bad))


def test_selftest_shift_window_corruption_is_caught(small_run):
    bad = _corrupt(small_run)
    (sweep_ts, ts) = next(iter(bad.shifts))
    far = bad.candles[0].ts.isoformat()               # distance far outside
    bad.shifts[(far, ts)] = dict(bad.shifts[(sweep_ts, ts)], sweep_ts=far)
    assert verify_shifts(bad) != []


def test_selftest_ob_zone_corruption_is_caught(small_run):
    bad = _corrupt(small_run)
    key = next(iter(bad.blocks))
    bars = bad.blocks[key]
    bars[min(bars)]["hi"] += 0.5
    assert verify_obs(bad) != []


def test_selftest_ob_weak_bos_is_caught(small_run):
    bad = _corrupt(small_run)
    key = next(iter(bad.blocks))
    bad.bos[key[1]]["displacement"] = False
    assert verify_obs(bad) != []


def test_selftest_fvg_bounds_corruption_is_caught(small_run):
    bad = _corrupt(small_run)
    key = next(iter(bad.fvgs))
    bars = bad.fvgs[key]
    bars[min(bars)]["lo"] -= 0.25
    assert verify_fvgs(bad) != []


def test_selftest_illegal_status_regression_is_caught(small_run):
    bad = _corrupt(small_run)
    key, bars = next(iter(bad.blocks.items()))
    b0 = min(bars)
    bars[b0]["status"] = "mitigated"
    bars[b0 + 1] = dict(bars[b0], status="active")     # mitigated -> active
    assert any("illegal transition" in v for v in verify_ob_lifecycle(bad))


def test_selftest_resurrection_is_caught(small_run):
    bad = _corrupt(small_run)
    key, bars = next(iter(bad.blocks.items()))
    b0 = min(bars)
    bars[b0 + 5] = dict(bars[b0])                      # gap then reappear
    for b in list(bars):
        if b0 + 1 <= b <= b0 + 4:
            del bars[b]
    assert any("resurrection" in v for v in verify_ob_lifecycle(bad))


def test_selftest_orphan_breaker_is_caught(small_run):
    bad = _corrupt(small_run)
    key, bars = next(iter(bad.breakers.items()))
    b0 = min(bars)
    clone = dict(bars[b0], lo=bars[b0]["lo"] + 0.33,
                 hi=bars[b0]["hi"] + 0.33)             # zone nobody vacated
    bad.breakers[(key[0], "1999-01-01T00:00:00+00:00")] = {b0: clone}
    assert any("no same-zone" in v for v in verify_ob_lifecycle(bad))
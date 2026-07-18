"""Automated Phase-3 validation gate (roadmap P3.21; the P2.24 precedent).

Roadmap P3 gate: "90-day replay -> recommendations generated;
top-20/bottom-20 score-ordering review; docs updated." Following P2.24
(owner-approved automated-gate methodology), the AUTOMATABLE parts are
verified here at 100% coverage by independent recomputation:

  1. Recommendations are GENERATED end-to-end (strategies -> qualification
     -> planner -> admission) over a replay of the composition.
  2. Score ORDERING makes sense as a checkable invariant set: the score is
     the exact frozen weighted sum of its components; verdicts partition
     by the frozen thresholds; data-integrity and agreement are consistent;
     component dominance implies score dominance; and the top-scored setups
     carry strictly more rule agreement than the bottom-scored ones.
  3. Every generated recommendation is VALID: its bar cleared the verdict
     threshold, the plan geometry is sound, and the net RR meets the floor.

Out of scope (deferred to Final Pre-Release Validation, with P1.22 and the
P2.24 rule-quality residual): whether a human trader agrees the high-scored
setups are genuinely better trades. That is not automatable; the gate
verifies the machinery, not the market edge.

Determinism of signals + recommendations is the separate P3.20 gate
(test_determinism.py V4 + persisted rows). Validation-only module.
"""

from __future__ import annotations

import asyncio
import copy

from marketscalper.core.bus import EventBus
from marketscalper.core.state import StateStore
from marketscalper.engines.qualification import (
    SCORE_A_PLUS,
    SCORE_TRADEABLE,
    WEIGHTS,
    verdict_of,
)
from marketscalper.main import _wire_structure_engines
from rec_dataset import rec_candles, rec_seed

_EPS = 1e-9


class QualGateCapture:
    """Every qualification result + unique signal/recommendation the
    composition emitted over one run, by (symbol, bar)."""

    def __init__(self):
        self.quals = []          # {symbol, bar, verdict, score, components,
                                 #  gates, agreement, data_integrity, aligned,
                                 #  evaluable, reasons}
        self.recs = {}           # (symbol, created_ts) -> rec dict
        self.signals = {}        # (symbol, strategy, created_ts) -> signal


def capture_run(streams):
    """streams: dict symbol -> (candles, seed). Drives the REAL seeded
    composition and captures per-bar qualification + recommendations."""
    async def run():
        bus = EventBus()
        store = StateStore(bus)
        symbols = list(streams)
        seed = {s: streams[s][1] for s in symbols}
        _wire_structure_engines(bus, store, symbols, seed_candles=seed)
        cap = QualGateCapture()
        # interleave the per-symbol streams by index (deterministic)
        length = max(len(streams[s][0]) for s in symbols)
        wins = {s: [] for s in symbols}
        for i in range(length):
            for s in symbols:
                candles = streams[s][0]
                if i >= len(candles):
                    continue
                candle = candles[i]
                await bus.publish(candle)
                wins[s].append(candle)
                if len(wins[s]) == 5:
                    w = wins[s]
                    await bus.publish(_fold_5m(w))
                    wins[s] = []
                st = store.snapshot(s).structure
                q = st["qualification"]
                cap.quals.append({
                    "symbol": s, "bar": i, "verdict": q["verdict"],
                    "score": q["score"], "components": q["components"],
                    "gates": q["gates"], "agreement": q["agreement"],
                    "data_integrity": q["data_integrity"],
                    "reasons": q["reasons"]})
                for sig in st["signals"]:
                    cap.signals.setdefault(
                        (s, sig["strategy"], sig["created_ts"]), sig)
                for rec in st["recommendations"]:
                    cap.recs[(s, rec["created_ts"])] = dict(rec, symbol=s)
        return cap
    return asyncio.run(run())


def _fold_5m(w):
    from marketscalper.providers.base import Candle
    return Candle(symbol=w[0].symbol, tf="5m", ts=w[0].ts, o=w[0].o,
                  h=max(c.h for c in w), l=min(c.l for c in w), c=w[-1].c,
                  v=sum(c.v for c in w), qv=sum(c.qv for c in w),
                  n_trades=sum(c.n_trades for c in w),
                  taker_buy_v=sum(c.taker_buy_v for c in w))


# --------------------------------------------- independent verifiers


def verify_score_formula(quals):
    """Every scored bar: score == the exact frozen weighted sum."""
    out = []
    for q in quals:
        if q["score"] is None:
            continue
        c = q["components"]
        expect = sum(WEIGHTS[k] * c[k] for k in WEIGHTS)
        if abs(expect - q["score"]) > _EPS:
            out.append((q["symbol"], q["bar"], q["score"], expect))
    return out


def verify_verdict_partition(quals):
    """Verdict follows the frozen thresholds; a gate fail -> NO_SIGNAL."""
    out = []
    for q in quals:
        gate_fail = any(not g["passed"] for g in q["gates"])
        if gate_fail:
            if q["verdict"] != "NO_SIGNAL" or q["score"] is not None \
                    or q["components"] is not None:
                out.append((q["symbol"], q["bar"], "gate-fail", q["verdict"]))
        else:
            if q["verdict"] != verdict_of(q["score"]):
                out.append((q["symbol"], q["bar"], q["score"], q["verdict"]))
    return out


def verify_integrity_and_agreement(quals):
    """data_integrity == PASS iff G1&G2; agreement == the exact
    '{aligned} of 14 rules aligned' string (14 evaluable items — the
    composition always attaches the Volume Engine, D21.3), or the
    gate-fail sentinel when unscored."""
    out = []
    for q in quals:
        g = {x["name"]: x for x in q["gates"]}
        want = "PASS" if g["G1"]["passed"] and g["G2"]["passed"] else "DEGRADED"
        if q["data_integrity"] != want:
            out.append((q["symbol"], q["bar"], "integrity", q["data_integrity"]))
            continue
        if q["score"] is None:
            if q["agreement"] != "gates failed — no score":
                out.append((q["symbol"], q["bar"], "agree-fail", q["agreement"]))
        else:
            aligned = sum(1 for r in q["reasons"] if r.startswith("✓"))
            if q["agreement"] != f"{aligned} of 14 rules aligned":
                out.append((q["symbol"], q["bar"], "agree", q["agreement"]))
    return out


def verify_component_dominance(quals):
    """Independent 'ordering makes sense' check: if bar A's four components
    each >= reference B's, then score(A) >= score(B). The reference B is
    the least-evidence scored bar (min component SUM) — chosen by
    components, so a corrupted score can never move it."""
    scored = [q for q in quals if q["score"] is not None]
    if not scored:
        return []
    base = min(scored, key=lambda q: sum(q["components"].values()))
    out = []
    for q in scored:
        if all(q["components"][k] >= base["components"][k] for k in WEIGHTS):
            if q["score"] + _EPS < base["score"]:
                out.append((q["symbol"], q["bar"], q["score"], base["score"]))
    return out


def verify_recommendations(cap):
    """Every recommendation: its bar cleared the threshold and the plan is
    geometrically valid with net RR at/above the floor."""
    out = []
    for key, r in cap.recs.items():
        if r["score"] is None or r["score"] + _EPS < SCORE_TRADEABLE:
            out.append((key, "below-threshold", r["score"]))
        if r["verdict"] not in ("TRADEABLE", "A_PLUS"):
            out.append((key, "verdict", r["verdict"]))
        long = r["direction"] == "LONG"
        ok = (r["sl"] < r["entry"] < r["tp1"] if long
              else r["tp1"] < r["entry"] < r["sl"])
        if not ok:
            out.append((key, "geometry", r))
        if r["tp2"] is not None:
            beyond = r["tp2"] > r["tp1"] if long else r["tp2"] < r["tp1"]
            if not beyond:
                out.append((key, "tp2", r))
        if r["net_rr_tp1"] is None or r["net_rr_tp1"] + _EPS < 1.0:
            out.append((key, "net-rr", r["net_rr_tp1"]))
        r_dist = abs(r["entry"] - r["sl"])
        one_r = (r["entry"] + r_dist if long else r["entry"] - r_dist)
        if long and r["tp1"] + _EPS < one_r:
            out.append((key, "1R", r))
        if not long and r["tp1"] - _EPS > one_r:
            out.append((key, "1R", r))
    return out


def top_bottom_evidence_ordering(quals, k):
    """Top-k scored bars carry more aligned rules than the bottom-k, and
    the top-k scores strictly exceed the bottom-k (the automatable form of
    the roadmap's top-20/bottom-20 review)."""
    scored = sorted((q for q in quals if q["score"] is not None),
                    key=lambda q: q["score"])
    if len(scored) < 2 * k:
        k = len(scored) // 2
    bottom, top = scored[:k], scored[-k:]

    def aligned(q):
        return sum(1 for r in q["reasons"] if r.startswith("✓"))
    top_min = min(q["score"] for q in top)
    bot_max = max(q["score"] for q in bottom)
    top_evi = sum(aligned(q) for q in top) / k
    bot_evi = sum(aligned(q) for q in bottom) / k
    return top_min, bot_max, top_evi, bot_evi, k


# ------------------------------------------------------------- the gate


def _streams():
    return {"BTCUSDT": (rec_candles("BTCUSDT"), rec_seed("BTCUSDT")),
            "ETHUSDT": (rec_candles("ETHUSDT"), rec_seed("ETHUSDT"))}


def test_p3_gate_recommendations_generated_and_ordered():
    cap = capture_run(_streams())

    # (1) the P3 chain generated recommendations end-to-end
    assert len(cap.recs) >= 2, "no recommendations generated"
    assert cap.signals, "no signals generated"
    assert all(r["strategy"] in ("S1", "S2", "S3") for r in cap.recs.values())

    # (2) score ordering / formula integrity — 100% coverage
    assert verify_score_formula(cap.quals) == []
    assert verify_verdict_partition(cap.quals) == []
    assert verify_integrity_and_agreement(cap.quals) == []
    assert verify_component_dominance(cap.quals) == []

    # (3) every generated recommendation is valid
    assert verify_recommendations(cap) == []

    # verdict spectrum present (warming + scored + tradeable eras)
    verdicts = {q["verdict"] for q in cap.quals}
    assert {"NO_SIGNAL", "BELOW_THRESHOLD", "TRADEABLE"} <= verdicts

    # top vs bottom evidence ordering (the automatable review)
    top_min, bot_max, top_evi, bot_evi, k = top_bottom_evidence_ordering(
        cap.quals, 20)
    assert k >= 5                                   # enough scored bars
    assert top_min > bot_max                        # clean score separation
    assert top_evi > bot_evi                        # more evidence up top


def test_p3_gate_density_floor():
    """The replay produced a substantial, verified qualification stream —
    the P2.24 'coverage over sample' discipline (every bar checked)."""
    cap = capture_run(_streams())
    scored = [q for q in cap.quals if q["score"] is not None]
    assert len(cap.quals) >= 1000                   # ~590 bars x 2 symbols
    assert len(scored) >= 500                       # most bars scored
    assert max(q["score"] for q in scored) >= SCORE_TRADEABLE
    # the frozen weights sum to 1.0 and the max is the recorded 97.0 ceiling
    assert abs(sum(WEIGHTS.values()) - 1.0) < _EPS
    assert all(q["score"] <= 97.0 + _EPS for q in scored)


# ------------------------------------------------ non-vacuousness self-tests


def test_selftest_score_formula_catches_corruption():
    cap = capture_run(_streams())
    corrupt = copy.deepcopy([q for q in cap.quals if q["score"] is not None])
    assert corrupt, "need a scored bar"
    corrupt[0]["score"] = corrupt[0]["score"] + 5.0      # break the sum
    assert verify_score_formula(corrupt) != []


def test_selftest_verdict_partition_catches_corruption():
    cap = capture_run(_streams())
    scored = [q for q in cap.quals if q["score"] is not None]
    tradeable = next((q for q in scored
                      if q["verdict"] == "TRADEABLE"), None)
    assert tradeable is not None
    bad = copy.deepcopy(tradeable)
    bad["verdict"] = "A_PLUS"                             # wrong threshold
    assert verify_verdict_partition([bad]) != []


def test_selftest_recommendation_validity_catches_corruption():
    cap = capture_run(_streams())
    assert cap.recs
    bad_cap = QualGateCapture()
    key = next(iter(cap.recs))
    bad = dict(cap.recs[key])
    bad["net_rr_tp1"] = 0.5                               # below the floor
    bad_cap.recs = {key: bad}
    assert verify_recommendations(bad_cap) != []


def test_selftest_dominance_catches_corruption():
    cap = capture_run(_streams())
    scored = [q for q in cap.quals if q["score"] is not None]
    base = min(scored, key=lambda q: sum(q["components"].values()))
    # a bar that component-dominates the reference but has a lower score
    bad = copy.deepcopy(base)
    bad["components"] = {k: base["components"][k] + 10.0 for k in WEIGHTS}
    bad["score"] = base["score"] - 1.0
    assert verify_component_dominance([base, bad]) != []

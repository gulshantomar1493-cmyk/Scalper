"""Config-sweep calibration tooling (roadmap P5.3; unblocked by the D9
config-plumbing).

Offline / read-only. Replays historical 1m candles from Postgres through
the REAL composition once per candidate regime/momentum config, collecting
each admitted recommendation's hypothetical (candle-geometry) outcome, and
reports per-config fees-included (NET) expectancy so the owner can COMPARE
calibrations side by side. Per D9 the tool only REPORTS; the calibration /
TRUSTED decision stays the owner's (the §0-rule-4 / P5.6-P5.8 doctrine —
never automated, never a "live unlock").

Provider-blind (P0.19 import boundary): the concrete ReplayFeed class and
the composition wiring (_wire_structure_engines) are INJECTED by the caller
(the create_app / main.py pattern), so this module imports no provider and
no composition root. The pure comparison core (config_stats / rank_sweep)
is a plain function of dicts — testable without a database.
"""

from __future__ import annotations

_WIN = ("tp1", "tp2")                 # hypothetical winning outcomes
_EVALUATED = "evaluated"              # the only lifecycle status with an R


class _CaptureRecorder:
    """Recorder-shaped in-memory capture for ONE composition run (no DB).

    Mirrors the SignalRecorder surface the pipeline calls — record /
    record_lifecycle — so the sweep drives the EXACT production chain, but
    keeps everything in memory: each admitted recommendation's identity +
    round-trip fee-in-R, and its terminal lifecycle outcome. The pipeline
    is unaware it is talking to a capture rather than the DB recorder."""

    def __init__(self) -> None:
        # (created_ts_iso, strategy) -> {"fee_r": float, "direction": str}
        self.admitted: dict = {}
        # same key -> {"status": str, "outcome": str|None, "eval_r": float|None}
        self.terminal: dict = {}

    async def record(self, symbol, records, payload) -> None:
        for signal, qual, plan, rec in records:
            if rec is None:               # non-admitted signal: no plan/outcome
                continue
            risk = plan.risk_amt
            est_fees = plan.qty * plan.fee_per_unit
            fee_r = est_fees / risk if risk else 0.0
            self.admitted[(signal.created_ts.isoformat(), signal.strategy)] = {
                "fee_r": fee_r, "direction": signal.direction}

    async def record_lifecycle(self, symbol, events) -> None:
        # ev.rec_key = (created_ts_isoformat, strategy) — already a string
        # pair (the SignalRecorder keys its rows the same way, D20.1), so it
        # matches the admitted key built in record() above.
        for ev in events:
            self.terminal[(ev.rec_key[0], ev.rec_key[1])] = {
                "status": ev.status,
                "outcome": ev.outcome.outcome if ev.outcome else None,
                "eval_r": ev.outcome.eval_r if ev.outcome else None,
            }


def config_stats(admitted: dict, terminal: dict) -> dict:
    """Pure per-config outcome stats: join admitted recs to their terminal
    lifecycle outcomes and compute the fees-included (NET) expectancy — the
    number the sweep ranks on. Only 'evaluated' recs (SL/TP actually touched)
    count toward expectancy; 'invalidated'/'expired' never filled and are
    excluded, matching the analytics read-model and §0 rule 4 (net of the
    round-trip taker fee, expressed in R)."""
    evaluated = []
    for key, info in admitted.items():
        term = terminal.get(key)
        if (term and term["status"] == _EVALUATED
                and term["eval_r"] is not None):
            evaluated.append((term["eval_r"], info["fee_r"],
                              term["outcome"] in _WIN))
    n = len(evaluated)
    wins = sum(1 for _, _, w in evaluated if w)
    return {
        "n_admitted": len(admitted),
        "n_evaluated": n,
        "gross_expectancy": (sum(er for er, _, _ in evaluated) / n)
                            if n else None,
        "net_expectancy": (sum(er - fr for er, fr, _ in evaluated) / n)
                          if n else None,
        "win_rate": (wins / n) if n else None,
    }


def rank_sweep(results: list, min_evaluated: int = 1) -> dict:
    """Pure ranking of a config sweep. `results` = [{"label": str, "stats":
    <config_stats>}]. Ranks by NET expectancy (desc) among configs that
    reached >= min_evaluated evaluated trades; ties keep input order (Python
    sort is stable). The DECISION stays the owner's — the tool only reports
    (D9). A config below the sample floor is listed but never 'best'."""
    eligible = [r for r in results
                if r["stats"]["n_evaluated"] >= min_evaluated
                and r["stats"]["net_expectancy"] is not None]
    ranked = sorted(eligible, key=lambda r: r["stats"]["net_expectancy"],
                    reverse=True)
    return {
        "min_evaluated": min_evaluated,
        "n_configs": len(results),
        "n_eligible": len(eligible),
        "results": results,          # every config's stats, input order
        "ranked": ranked,            # eligible subset, best net expectancy first
        "best_label": ranked[0]["label"] if ranked else None,
        "note": ("no config reached the minimum evaluated-trade sample — "
                 "calibration inconclusive" if not ranked else
                 "reported only; the calibration / TRUSTED decision is the "
                 "owner's (D9, §0 rule 4)"),
    }


async def run_config(pool, symbol, start, end, *, replay_cls, wiring,
                     regime_cfg=None, shift_accel_atr_ratio=0.1,
                     seed_candles=None) -> _CaptureRecorder:
    """Drive ONE composition run over [start, end) DB candles under the given
    D9 config, returning the in-memory outcome capture. replay_cls (a
    ReplayFeed) and wiring (_wire_structure_engines) are injected to keep
    this module provider-blind and composition-root-blind (P0.19)."""
    from marketscalper.core.bus import EventBus
    from marketscalper.core.state import StateStore

    bus = EventBus()
    store = StateStore(bus)
    capture = _CaptureRecorder()
    wiring(bus, store, [symbol], recorder=capture, regime_cfg=regime_cfg,
           shift_accel_atr_ratio=shift_accel_atr_ratio,
           seed_candles=seed_candles)
    feed = replay_cls([symbol], bus, pool, start, end, speed="max")
    await feed.start()
    # ReplayFeed exposes completion only through its internal task (it
    # auto-idles when the stored range is exhausted — P0.25). Await it to
    # natural completion, then stop() for idempotent cleanup. Same access
    # the determinism harness uses; NOT an import-boundary issue (attribute
    # access on an injected instance, no provider import).
    if feed._task is not None:
        await feed._task
    await feed.stop()
    return capture


async def sweep(pool, symbol, start, end, combos, *, replay_cls, wiring,
                seed_candles=None, min_evaluated: int = 1) -> dict:
    """Run the composition once per combo and rank by NET expectancy.

    `combos` = [{"label": str, "regime_cfg": RegimeConfig|None,
    "shift_accel_atr_ratio": float}]. Runs are sequential (each is a full
    replay) and deterministic given the same candles + seed. Returns the
    rank_sweep report."""
    results = []
    for combo in combos:
        capture = await run_config(
            pool, symbol, start, end, replay_cls=replay_cls, wiring=wiring,
            regime_cfg=combo.get("regime_cfg"),
            shift_accel_atr_ratio=combo.get("shift_accel_atr_ratio", 0.1),
            seed_candles=seed_candles)
        results.append({"label": combo["label"],
                        "stats": config_stats(capture.admitted,
                                              capture.terminal)})
    return rank_sweep(results, min_evaluated)

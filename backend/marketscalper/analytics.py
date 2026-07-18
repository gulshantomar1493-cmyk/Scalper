"""Analytics read-model (Architecture §11 P4; roadmap P4.11).

A thin SQL join over the persisted signals + recommendations + journal,
feeding a PURE aggregation: manual results (win rate, avg R, expectancy),
hypothetical-evaluator stats (win rate, avg R, MAE/MFE), and the
system-vs-actual comparison — overall and per strategy / per session.

The aggregation is a pure function of plain row dicts (testable without a
database); `compute_analytics` is the thin fetch that maps the Decimal
columns to floats and delegates. Read-only — never writes.
"""

from __future__ import annotations

from marketscalper.engines.liquidity import session_of

_HYP_WIN = ("tp1", "tp2")          # hypothetical terminal outcomes = win
_HYP_TERMINAL = ("tp1", "tp2", "sl")   # 'none' = un-resolved, excluded

ANALYTICS_SQL = (
    "SELECT s.strategy, r.ts, r.eval_outcome, r.eval_r, r.eval_mae,"
    " r.eval_mfe, r.status, j.taken, j.result, j.actual_r"
    " FROM recommendations r"
    " JOIN signals s ON s.id = r.signal_id"
    " LEFT JOIN journal j ON j.recommendation_id = r.id"
    " ORDER BY r.ts"
)


def _ratio(num: int, den: int):
    return None if den == 0 else num / den


def _mean(values: list):
    return None if not values else sum(values) / len(values)


def _stats(rows: list) -> dict:
    """All metrics for one group of recommendation rows (pure)."""
    # -- hypothetical evaluator (candle-based, §7): terminal outcomes only
    evaluated = [r for r in rows if r["eval_outcome"] in _HYP_TERMINAL]
    hwins = sum(1 for r in evaluated if r["eval_outcome"] in _HYP_WIN)
    hlosses = sum(1 for r in evaluated if r["eval_outcome"] == "sl")
    hyp_r = [r["eval_r"] for r in evaluated if r["eval_r"] is not None]
    hypothetical = {
        "n_evaluated": len(evaluated),
        "wins": hwins, "losses": hlosses,
        "win_rate": _ratio(hwins, hwins + hlosses),
        "avg_r": _mean(hyp_r),
        "expectancy": _mean(hyp_r),          # expected R per evaluated trade
        "avg_mae": _mean([r["eval_mae"] for r in evaluated
                          if r["eval_mae"] is not None]),
        "avg_mfe": _mean([r["eval_mfe"] for r in evaluated
                          if r["eval_mfe"] is not None]),
    }

    # -- manual results (owner's journal, taken trades)
    taken = [r for r in rows
             if r["taken"] and r["result"] in ("win", "loss", "be")]
    mwins = sum(1 for r in taken if r["result"] == "win")
    mlosses = sum(1 for r in taken if r["result"] == "loss")
    mbe = sum(1 for r in taken if r["result"] == "be")
    man_r = [r["actual_r"] for r in taken if r["actual_r"] is not None]
    manual = {
        "n_taken": len(taken),
        "wins": mwins, "losses": mlosses, "be": mbe,
        "win_rate": _ratio(mwins, mwins + mlosses),
        "avg_r": _mean(man_r),
        "expectancy": _mean(man_r),
    }

    # -- system vs actual: taken AND hypothetically-evaluated, both R known
    both = [r for r in rows
            if r["taken"] and r["actual_r"] is not None
            and r["eval_r"] is not None
            and r["eval_outcome"] in _HYP_TERMINAL]
    mean_eval = _mean([r["eval_r"] for r in both])
    mean_actual = _mean([r["actual_r"] for r in both])
    system_vs_actual = {
        "n": len(both),
        "mean_eval_r": mean_eval,
        "mean_actual_r": mean_actual,
        "delta": (None if mean_eval is None or mean_actual is None
                  else mean_actual - mean_eval),
    }

    return {"n": len(rows), "hypothetical": hypothetical,
            "manual": manual, "system_vs_actual": system_vs_actual}


def aggregate(rows: list) -> dict:
    """Overall + per-strategy + per-session breakdown (pure). Rows are
    plain dicts with keys strategy/ts/eval_outcome/eval_r/eval_mae/
    eval_mfe/status/taken/result/actual_r; `ts` is a datetime."""
    by_strategy: dict = {}
    by_session: dict = {}
    for r in rows:
        by_strategy.setdefault(r["strategy"], []).append(r)
        by_session.setdefault(session_of(r["ts"].hour), []).append(r)
    return {
        "n_recommendations": len(rows),
        "overall": _stats(rows),
        "by_strategy": {k: _stats(v) for k, v in sorted(by_strategy.items())},
        "by_session": {k: _stats(v) for k, v in sorted(by_session.items())},
    }


# P5.2 — MAE-distribution buckets (R units, adverse = negative). Each is
# an inclusive-low, exclusive-high [lo, hi) band; the last catches the tail.
MAE_BUCKETS = ((0.0, -0.5), (-0.5, -1.0), (-1.0, -1.5), (-1.5, None))


def _mae_histogram(maes: list) -> list:
    """Counts of MAE values (≤ 0, in R) per band — deterministic order."""
    counts = [0] * len(MAE_BUCKETS)
    for v in maes:
        for i, (lo, hi) in enumerate(MAE_BUCKETS):
            if v <= lo and (hi is None or v > hi):
                counts[i] += 1
                break
    return [{"band": (f"<={lo:.1f}R" if hi is None
                      else f"{lo:.1f}..{hi:.1f}R"),
             "count": counts[i]} for i, (lo, hi) in enumerate(MAE_BUCKETS)]


def _sorted_winner_maes(rows: list) -> list:
    winners = [r for r in rows
               if r["eval_outcome"] in _HYP_WIN and r["eval_mae"] is not None]
    return sorted(r["eval_mae"] for r in winners)      # ascending (worst first)


def _preserve_stop(winner_maes: list, keep: float):
    """The tightest SL (in R) that would still have preserved `keep`
    fraction of the winners — the P5.2 SL-tuning signal. Winner MAEs are
    negative; the value below which only (1-keep) of winners fall."""
    if not winner_maes:
        return None
    idx = int((1.0 - keep) * len(winner_maes))         # index into ascending
    idx = max(0, min(idx, len(winner_maes) - 1))
    return winner_maes[idx]


def mae_distribution(rows: list) -> dict:
    """P5.2: per-strategy MAE/MFE distribution over EVALUATED trades plus
    the SL-tuning summary (the worst/median winner MAE and the stop levels
    that would have preserved 90% / 75% of winners). Pure. Rows are the
    analytics row dicts."""
    by_strategy: dict = {}
    for r in rows:
        by_strategy.setdefault(r["strategy"], []).append(r)
    out: dict = {}
    for strat in sorted(by_strategy):
        rs = by_strategy[strat]
        evaluated = [r for r in rs if r["eval_outcome"] in _HYP_TERMINAL
                     and r["eval_mae"] is not None]
        winner_maes = _sorted_winner_maes(rs)
        out[strat] = {
            "n_evaluated": len(evaluated),
            "n_winners": len(winner_maes),
            "mae_histogram": _mae_histogram([r["eval_mae"] for r in evaluated]),
            "avg_mfe": _mean([r["eval_mfe"] for r in evaluated
                              if r["eval_mfe"] is not None]),
            "winner_mae_worst": winner_maes[0] if winner_maes else None,
            "winner_mae_median": (winner_maes[len(winner_maes) // 2]
                                  if winner_maes else None),
            "sl_preserve_90": _preserve_stop(winner_maes, 0.90),
            "sl_preserve_75": _preserve_stop(winner_maes, 0.75),
        }
    return out


async def compute_mae_distribution(conn) -> dict:
    """Fetch + MAE distribution (P5.2). Read-only."""
    return mae_distribution(await _analytics_rows(conn))


def _f(v):
    return None if v is None else float(v)


JOURNAL_LIST_SQL = (
    "SELECT r.id, r.ts, s.strategy, r.direction, r.entry_px, r.sl, r.tp1,"
    " r.tp2, r.status, r.eval_outcome, r.eval_r, j.reason_text, j.taken,"
    " j.result, j.actual_r, j.notes, j.tags"
    " FROM recommendations r"
    " JOIN signals s ON s.id = r.signal_id"
    " LEFT JOIN journal j ON j.recommendation_id = r.id"
    " ORDER BY r.ts DESC LIMIT $1"
)


async def journal_list(conn, limit: int = 100) -> list:
    """Recent recommendations + their journal context, newest first —
    the P4.12 journal tab. Read-only."""
    rows = await conn.fetch(JOURNAL_LIST_SQL, limit)
    return [{
        "id": r["id"], "ts": r["ts"].isoformat(), "strategy": r["strategy"],
        "direction": r["direction"], "entry": _f(r["entry_px"]),
        "sl": _f(r["sl"]), "tp1": _f(r["tp1"]), "tp2": _f(r["tp2"]),
        "status": r["status"], "eval_outcome": r["eval_outcome"],
        "eval_r": _f(r["eval_r"]), "reason_text": r["reason_text"],
        "taken": r["taken"], "result": r["result"],
        "actual_r": _f(r["actual_r"]), "notes": r["notes"],
        "tags": list(r["tags"]) if r["tags"] is not None else None,
    } for r in rows]


async def _analytics_rows(conn) -> list:
    """The shared thin fetch + Decimal->float map for the read-model."""
    rows = await conn.fetch(ANALYTICS_SQL)
    return [{
        "strategy": r["strategy"], "ts": r["ts"],
        "eval_outcome": r["eval_outcome"], "eval_r": _f(r["eval_r"]),
        "eval_mae": _f(r["eval_mae"]), "eval_mfe": _f(r["eval_mfe"]),
        "status": r["status"], "taken": r["taken"], "result": r["result"],
        "actual_r": _f(r["actual_r"]),
    } for r in rows]


async def compute_analytics(conn) -> dict:
    """Fetch + aggregate (the thin DB layer). Read-only."""
    return aggregate(await _analytics_rows(conn))

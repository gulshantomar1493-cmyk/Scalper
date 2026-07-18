"""Validation-campaign tooling (Architecture §11 P5; roadmap P5.5 + P5.7).

Read-only analysis the owner runs DURING the live campaign:

- `data_quality_audit` (P5.5): integrity checks over the persisted tables
  — 1m candle gaps, recommendations whose lifecycle never terminalised,
  and evaluated/eval_* consistency — surfaced as a violation list (empty =
  clean), so a weekly audit catches data problems the expectancy report
  would otherwise silently include.
- `expectancy_report` (P5.7): the fees-included expectancy summary per
  strategy over the campaign — the exact number the P5.8 TRUSTED gate
  reads (positive expectancy after fees over >=200 recommendations). Pure
  formatting over the analytics read-model + the recorded fee estimates.

The live campaign itself (>=200 recommendations, the kill/keep decisions,
the TRUSTED verdict) is owner-operated; these are the tools for it. Pure /
read-only — never writes.
"""

from __future__ import annotations

from datetime import timedelta

_TERMINAL = ("invalidated", "expired", "evaluated")
_HYP_TERMINAL = ("tp1", "tp2", "sl")


async def data_quality_audit(conn, *, gap_grace_days: int = 1) -> dict:
    """P5.5 weekly data-quality audit. Returns {"violations": [...],
    "clean": bool, counts...}. Read-only."""
    violations: list = []

    # 1) 1m candle gaps per symbol (a missing closed minute inside the
    #    observed range — the feed/backfill should leave none)
    gap_rows = await conn.fetch(
        "SELECT symbol, count(*) AS n, min(ts) AS lo, max(ts) AS hi"
        " FROM candles WHERE tf = '1m' GROUP BY symbol")
    for r in gap_rows:
        span_minutes = int((r["hi"] - r["lo"]).total_seconds() // 60) + 1
        # allow a grace tail (partial current day) — flag only sizeable holes
        missing = span_minutes - r["n"]
        if missing > gap_grace_days * 1440:
            violations.append(
                f"candle gap: {r['symbol']} missing {missing} 1m candles "
                f"across [{r['lo'].isoformat()}, {r['hi'].isoformat()}]")

    # 2) recommendations stuck non-terminal past a full evaluation horizon
    #    (240 bars ~ 4h; anything older still 'active' means the lifecycle
    #    never ran on it — a persistence or forward-run defect)
    stuck = await conn.fetch(
        "SELECT id, ts FROM recommendations WHERE status = 'active'"
        " AND ts < (SELECT max(ts) FROM recommendations) - $1::interval",
        timedelta(hours=6))
    for r in stuck:
        violations.append(
            f"stuck recommendation: id={r['id']} still 'active' at "
            f"{r['ts'].isoformat()} (past the evaluation horizon)")

    # 3) evaluated/eval_* consistency: status 'evaluated' <=> a terminal
    #    eval_outcome with an eval_r; neither half-written
    bad_eval = await conn.fetch(
        "SELECT id, status, eval_outcome, eval_r FROM recommendations"
        " WHERE (status = 'evaluated') <> "
        " (eval_outcome IN ('tp1','tp2','sl') AND eval_r IS NOT NULL)")
    for r in bad_eval:
        violations.append(
            f"eval inconsistency: id={r['id']} status={r['status']} "
            f"outcome={r['eval_outcome']} eval_r={r['eval_r']}")

    # 4) orphan journal rows (should be impossible via the FK, but a
    #    campaign audit states the invariant it relies on)
    orphans = await conn.fetchval(
        "SELECT count(*) FROM journal j WHERE NOT EXISTS"
        " (SELECT 1 FROM recommendations r WHERE r.id = j.recommendation_id)")
    if orphans:
        violations.append(f"orphan journal rows: {orphans}")

    n_recs = await conn.fetchval("SELECT count(*) FROM recommendations")
    return {"violations": violations, "clean": not violations,
            "n_recommendations": n_recs, "n_violations": len(violations)}


# ------------------------------------------------------- expectancy report


TRUSTED_MIN_RECOMMENDATIONS = 200      # §0 rule 4 / §11 P5.8


def expectancy_report(analytics: dict) -> dict:
    """P5.7: the fees-included expectancy summary per strategy — the P5.8
    TRUSTED gate's number. `analytics` is compute_analytics output; the
    hypothetical/manual expectancies are already R multiples (fee-adjusted
    at plan time via the D17 net RR, then realized by the candle geometry).
    A strategy is TRUSTED-eligible iff it has >= 200 recommendations AND
    positive hypothetical expectancy after fees. Pure."""
    strategies = {}
    for strat, s in sorted(analytics.get("by_strategy", {}).items()):
        n = s["n"]
        hyp_exp = s["hypothetical"]["expectancy"]
        man_exp = s["manual"]["expectancy"]
        enough = n >= TRUSTED_MIN_RECOMMENDATIONS
        positive = hyp_exp is not None and hyp_exp > 0
        strategies[strat] = {
            "n": n,
            "hypothetical_expectancy": hyp_exp,
            "manual_expectancy": man_exp,
            "hypothetical_win_rate": s["hypothetical"]["win_rate"],
            "system_vs_actual_delta": s["system_vs_actual"]["delta"],
            "sample_sufficient": enough,
            "positive_after_fees": positive,
            "trusted_eligible": enough and positive,
        }
    return {
        "n_recommendations": analytics.get("n_recommendations", 0),
        "trusted_threshold": TRUSTED_MIN_RECOMMENDATIONS,
        "strategies": strategies,
        "any_trusted_eligible": any(v["trusted_eligible"]
                                    for v in strategies.values()),
    }

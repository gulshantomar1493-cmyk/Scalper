"""Forward-run ops helpers (Architecture §11 P4; roadmap P4.13).

Pure utilities for the live forward-run: feed-gap detection (alert when a
symbol's closed-candle stream stalls) and the daily stats-snapshot
formatter (one summary line per strategy from the analytics read-model).
Composition (main.py) drives the async watchdog / daily task; these
helpers stay pure and testable. Live-only — never run in replay/tests.
"""

from __future__ import annotations

from datetime import datetime

# §11 P4.13 thresholds — uncalibrated, ops-owned.
FEED_GAP_ALERT_S = 180        # 3 min without a closed 1m candle -> alert
FEED_WATCHDOG_INTERVAL_S = 60


def feed_gap_alerts(last_seen: dict, now: datetime,
                    threshold_s: float = FEED_GAP_ALERT_S) -> list:
    """Symbols whose latest closed 1m candle is older than the threshold.

    last_seen: symbol -> the ts of its latest closed 1m candle (or None if
    never seen — a symbol still warming up is NOT alerted). Returns
    [(symbol, gap_seconds)] sorted by symbol, deterministic."""
    out = []
    for symbol in sorted(last_seen):
        ts = last_seen[symbol]
        if ts is None:
            continue
        gap = (now - ts).total_seconds()
        if gap > threshold_s:
            out.append((symbol, gap))
    return out


def _pct(v):
    return "—" if v is None else f"{v * 100:.0f}%"


def _r(v):
    return "—" if v is None else f"{v:+.2f}R"


def format_daily_summary(analytics: dict) -> str:
    """A compact daily stats snapshot from compute_analytics output — the
    §11 P4.13 daily log line (one row per strategy: n, hyp/manual win rate
    and expectancy). Pure formatting."""
    n = analytics.get("n_recommendations", 0)
    lines = [f"daily stats snapshot: {n} recommendation"
             f"{'' if n == 1 else 's'}"]
    by_strategy = analytics.get("by_strategy", {})
    if not by_strategy:
        lines.append("  (no recommendations today)")
    for strat in sorted(by_strategy):
        s = by_strategy[strat]
        h, m = s["hypothetical"], s["manual"]
        lines.append(
            f"  {strat}: n={s['n']} "
            f"hyp_win={_pct(h['win_rate'])} hyp_exp={_r(h['expectancy'])} "
            f"man_win={_pct(m['win_rate'])} man_exp={_r(m['expectancy'])} "
            f"sys_vs_actual={_r(s['system_vs_actual']['delta'])}")
    return "\n".join(lines)

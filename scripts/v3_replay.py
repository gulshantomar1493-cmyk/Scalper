#!/usr/bin/env python
"""V3 P4 replay runner (owner-operated, offline, read-only).

Replays a historical range through the FULL V3 stack and prints the objective
performance report (win rate, expectancy, profit factor, drawdown, per-grade /
per-session splits, false + missed trades). Writes the full JSON next to cwd.

Usage (repo root; MARKETSCALPER_DB_DSN or backend/config.yaml set):
    python scripts/v3_replay.py --symbol BTCUSDT --days 7
    python scripts/v3_replay.py --symbol ETHUSDT --start 2026-06-01 --end 2026-07-01
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent
                       / "backend"))

from marketscalper import db                                     # noqa: E402
from marketscalper.config import load_config                     # noqa: E402
from marketscalper.core.chart_service import ChartService        # noqa: E402
from marketscalper.v3.replay import ReplayEngine                 # noqa: E402


def _fmt(a: dict) -> str:
    if not a or a.get("n", 0) == 0:
        return f"n=0 (expired {a.get('expired', 0)})"
    return (f"n={a['n']:<3} win={a['win_rate']:.0%} exp={a['expectancy']:+.2f}R "
            f"PF={a['profit_factor']} maxDD={a['max_drawdown']}R "
            f"hold={a['avg_hold_bars']}bars totR={a['total_r']:+.1f} "
            f"tp2={a['tp2_rate']:.0%} (expired {a['expired']})")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--start")
    ap.add_argument("--end")
    args = ap.parse_args()

    end = (datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
           if args.end else datetime.now(timezone.utc))
    start = (datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
             if args.start else end - timedelta(days=args.days))

    config = load_config()
    if not config.database.dsn:
        print("no DSN configured (MARKETSCALPER_DB_DSN / config.yaml)")
        return 2
    pool = await db.create_pool(config.database.dsn)
    try:
        engine = ReplayEngine(ChartService(pool, provider=None))
        t0 = time.perf_counter()

        def progress(done, total):
            pct = 100.0 * done / max(total, 1)
            print(f"\r  replaying {args.symbol} … {pct:5.1f}%", end="", flush=True)

        report = await engine.run(args.symbol, start, end, progress=progress)
        secs = time.perf_counter() - t0
        print()
        if "error" in report:
            print("ERROR:", report["error"])
            return 1

        print(f"\n=== V3 REPLAY {args.symbol} "
              f"{start.date()} → {end.date()} ({report['bars_5m']} × 5m bars, "
              f"{secs:.1f}s) ===")
        print(f"issued setups : {report['issued']}")
        print(f"OVERALL       : {_fmt(report['overall'])}")
        print("\nby grade:")
        for g, a in report["by_grade"].items():
            print(f"  {g:<3}: {_fmt(a)}")
        print("by session:")
        for s, a in report["by_session"].items():
            print(f"  {str(s)[:34]:<34}: {_fmt(a)}")
        print("by direction:")
        for d_, a in report["by_direction"].items():
            print(f"  {d_:<5}: {_fmt(a)}")
        print(f"\nfalse trades (SL before +1R): {len(report['false_trades'])}")
        for t in report["false_trades"][:5]:
            print(f"  {t['created_ts']} {t['direction']} {t['grade']} "
                  f"@{t['entry']} | {str(t['zone'])[:60]}")
        print(f"missed trades (ARMED, unissued, ran ≥2R): {report['missed_count']}")
        for m in report["missed_trades"][:5]:
            print(f"  {m['direction']} [{m['lo']}..{m['hi']}] "
                  f"+{m['would_have_made_r']}R | {str(m['reason_not_issued'])[:56]}")

        out = (f"v3_replay_{args.symbol}_{start.date()}_{end.date()}.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=1)
        print(f"\nfull report → {out}")
        return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

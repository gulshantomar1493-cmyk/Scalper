#!/usr/bin/env python
"""P5.3 config-sweep calibration runner (owner-operated, offline).

Replays a stored candle range through the REAL composition once per
candidate regime/momentum config and prints the per-config fees-included
(NET) expectancy, ranked. This is the operable front-end for
marketscalper.calibration.sweep; it lives in scripts/ (not the package) so
it may inject the concrete ReplayFeed without tripping the P0.19 import
boundary — the same reason main.py is the only in-package composition root.

Per D9 the tool only REPORTS. Reading the sweep and deciding a calibration
(or marking a strategy TRUSTED) stays the owner's judgment — this never
tunes anything automatically (§0 rule 4).

Usage (from repo root, with MARKETSCALPER_DB_DSN or config.yaml set):

    python scripts/calibration_sweep.py --symbol BTCUSDT \\
        --start 2026-05-01T00:00 --end 2026-05-08T00:00

The date range must already be present in the candles table (bootstrap or
a forward run populates it). Defaults sweep expansion_ratio and
shift_accel_atr_ratio around the frozen D9 pins.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone

# repo layout: scripts/ -> backend/ is the package root on sys.path
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent
                       / "backend"))

from marketscalper import db                                    # noqa: E402
from marketscalper.calibration import sweep                     # noqa: E402
from marketscalper.config import load_config                    # noqa: E402
from marketscalper.engines.momentum import RegimeConfig         # noqa: E402
from marketscalper.main import _row_to_candle, _wire_structure_engines  # noqa: E402
from marketscalper.providers.replay import ReplayFeed           # noqa: E402


def _default_combos() -> list[dict]:
    """A small illustrative grid around the frozen D9 pins. The owner edits
    this for a real calibration campaign."""
    combos = [{"label": "d9-default"}]                          # 1.5 / 0.1
    for exp in (1.3, 1.7, 2.0):
        combos.append({"label": f"expansion={exp}",
                       "regime_cfg": RegimeConfig(0.6, exp, 240)})
    for ratio in (0.05, 0.2):
        combos.append({"label": f"shift_accel={ratio}",
                       "shift_accel_atr_ratio": ratio})
    return combos


def _parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _main(args: argparse.Namespace) -> int:
    config = load_config()
    if not config.database.dsn:
        print("error: database DSN not configured "
              "(set MARKETSCALPER_DB_DSN or config.yaml)", file=sys.stderr)
        return 2
    start, end = _parse_dt(args.start), _parse_dt(args.end)
    if end <= start:
        print("error: --end must be after --start", file=sys.stderr)
        return 2

    pool = await db.create_pool(config.database.dsn)
    try:
        seed = None
        if args.seed_days > 0:                     # D19.2 RVOL bucket seed
            async with pool.acquire() as conn:
                rows = await db.select_candles(
                    conn, args.symbol, "1m",
                    start - timedelta(days=args.seed_days), start)
            seed = {args.symbol: [_row_to_candle(r) for r in rows]}
            print(f"seed: {len(seed[args.symbol])} candles "
                  f"({args.seed_days}d before start)")

        combos = _default_combos()
        print(f"sweeping {len(combos)} configs over {args.symbol} "
              f"[{start.isoformat()}, {end.isoformat()})...")
        report = await sweep(
            pool, args.symbol, start, end, combos,
            replay_cls=ReplayFeed, wiring=_wire_structure_engines,
            seed_candles=seed, min_evaluated=args.min_evaluated)
    finally:
        await pool.close()

    print(f"\nconfigs: {report['n_configs']}  eligible "
          f"(>= {report['min_evaluated']} evaluated): {report['n_eligible']}")
    print(f"{'label':<20} {'admitted':>8} {'evaluated':>9} "
          f"{'net_exp':>9} {'gross_exp':>9} {'win_rate':>8}")
    for r in report["results"]:
        s = r["stats"]
        def _f(v):
            return "  --  " if v is None else f"{v:8.3f}"
        print(f"{r['label']:<20} {s['n_admitted']:>8} {s['n_evaluated']:>9} "
              f"{_f(s['net_expectancy'])} {_f(s['gross_expectancy'])} "
              f"{_f(s['win_rate'])}")
    print(f"\nbest (by net expectancy): {report['best_label']}")
    print(report["note"])
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="P5.3 config-sweep calibration")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--start", required=True, help="ISO datetime (UTC)")
    p.add_argument("--end", required=True, help="ISO datetime (UTC)")
    p.add_argument("--seed-days", type=int, default=20,
                   help="RVOL seed window before start (0 = unseeded)")
    p.add_argument("--min-evaluated", type=int, default=30,
                   help="minimum evaluated trades for a config to rank")
    return asyncio.run(_main(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

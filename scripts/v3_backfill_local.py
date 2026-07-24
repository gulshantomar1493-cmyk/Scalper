#!/usr/bin/env python
"""Local 1m backfill for replay validation (owner-operated, offline).

Fills missing 1m candles in the LOCAL dev DB from Binance public klines so the
V3 replay can run over long ranges locally (prod already holds 9 years).
Read-only against Binance; append-only inserts (existing days are skipped by
day-level coverage check; duplicate rows are skipped one-by-one).

Usage: python scripts/v3_backfill_local.py --days 400
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone

import aiohttp

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent
                       / "backend"))

from marketscalper import db                                     # noqa: E402
from marketscalper.config import load_config                     # noqa: E402

KLINES = "https://api.binance.com/api/v3/klines"
SYMBOLS = ("BTCUSDT", "ETHUSDT")


async def fetch_day(session, symbol: str, day_start: datetime) -> list[tuple]:
    rows, cur = [], day_start
    day_end = day_start + timedelta(days=1)
    while cur < day_end:
        params = {"symbol": symbol, "interval": "1m",
                  "startTime": int(cur.timestamp() * 1000),
                  "endTime": int(day_end.timestamp() * 1000) - 1,
                  "limit": 1000}
        async with session.get(KLINES, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()
        if not data:
            break
        for k in data:
            ts = datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc)
            rows.append((symbol, "1m", ts, float(k[1]), float(k[2]),
                         float(k[3]), float(k[4]), float(k[5]), float(k[7]),
                         int(k[8]), float(k[9])))
        cur = datetime.fromtimestamp(data[-1][0] / 1000, tz=timezone.utc) \
            + timedelta(minutes=1)
        if len(data) < 1000:
            break
    return rows


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=400)
    args = ap.parse_args()
    config = load_config()
    if not config.database.dsn:
        print("no DSN configured")
        return 2
    pool = await db.create_pool(config.database.dsn)
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                             microsecond=0)
    start = end - timedelta(days=args.days)
    try:
        async with pool.acquire() as conn:
            await db.ensure_partitions(conn, p_from=start,
                                       months_ahead=args.days // 28 + 2)
        async with aiohttp.ClientSession() as session:
            for symbol in SYMBOLS:
                filled = skipped = 0
                async with pool.acquire() as conn:
                    counts = await conn.fetch(
                        "SELECT date_trunc('day', ts) AS d, count(*) AS n"
                        " FROM candles WHERE symbol=$1 AND tf='1m'"
                        " AND ts >= $2 AND ts < $3 GROUP BY 1", symbol, start, end)
                have = {r["d"]: r["n"] for r in counts}
                day = start
                while day < end:
                    if have.get(day, 0) >= 1380:          # ~full day present
                        skipped += 1
                    else:
                        rows = await fetch_day(session, symbol, day)
                        if rows:
                            async with pool.acquire() as conn:
                                try:                       # fast path: whole day
                                    await db.insert_candles(conn, rows)
                                except Exception:          # partial day exists
                                    for r in rows:
                                        try:
                                            await db.insert_candles(conn, [r])
                                        except Exception:
                                            pass
                            filled += 1
                        await asyncio.sleep(0.15)          # gentle rate limit
                    day += timedelta(days=1)
                    if (filled + skipped) % 25 == 0:
                        print(f"\r{symbol}: {filled} fetched, {skipped} present "
                              f"({day.date()})", end="", flush=True)
                print(f"\n{symbol}: DONE — {filled} days fetched, "
                      f"{skipped} already present", flush=True)
        print("BACKFILL DONE", flush=True)
        return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

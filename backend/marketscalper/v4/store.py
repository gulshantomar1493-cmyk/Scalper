"""Persistence for V4 recommendations (migration 008).

Error doctrine, inherited from the V1 recorder: a database failure is logged and
counted, never allowed to kill the analysis chain.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def _ts(v):
    return datetime.fromtimestamp(int(v), tz=timezone.utc)


class V4Store:
    def __init__(self, pool):
        self._pool = pool
        self.written = 0
        self.updated = 0
        self.errors = 0

    @staticmethod
    def key(s: dict) -> str:
        return f"{s['strategy_id']}|{s['symbol']}|{s['decision_ts']}|{s['direction']}"

    async def record(self, setups: list[dict]) -> list[dict]:
        """Insert new setups, returning only the ones that were actually new.
        Idempotent on setup_key — a re-seen setup returns nothing, so callers
        (alerts) never fire twice for the same setup."""
        if not setups or self._pool is None:
            return []
        sql = """INSERT INTO v4_recommendations
                 (setup_key,strategy_id,symbol,direction,level_source,level_tf,
                  filters_passed,entry,stop,target,risk_pct,rr,reason,
                  decision_ts,valid_until_ts)
                 VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                 ON CONFLICT (setup_key) DO NOTHING"""
        new: list[dict] = []
        try:
            async with self._pool.acquire() as c:
                for s in setups:
                    r = await c.execute(sql, self.key(s), s["strategy_id"], s["symbol"],
                                        int(s["direction"]), s["level_source"], s["level_tf"],
                                        int(s["filters_passed"]), float(s["entry"]),
                                        float(s["stop"]), float(s["target"]),
                                        s.get("risk_pct"), s.get("rr"), s.get("reason"),
                                        _ts(s["decision_ts"]), _ts(s["valid_until_ts"]))
                    if r and r.endswith("1"):
                        new.append(s)
            self.written += len(new)
        except Exception as exc:
            self.errors += 1
            log.warning("v4 store.record failed: %s", exc)
        return new

    async def apply_fill(self, setup_key: str, o) -> bool:
        """Promote OPEN -> FILLED: the resting order triggered and this is now a
        LIVE position, not a recommendation.

        Without this a triggered trade stayed 'OPEN' in the database until it
        finally hit its target or stop — so the trader had no record that they
        were in a trade at all, sometimes for three days.

        Guarded on status='OPEN' so it transitions exactly once: the recorder
        re-derives the fill from the tape every 60s, and the returned bool is
        what gates the "entry triggered" alert. That makes the alert survive a
        restart without re-firing, which an in-memory set cannot do.
        """
        if self._pool is None or o.fill_price is None:
            return False
        sql = """UPDATE v4_recommendations
                    SET status='FILLED', fill_price=$2, filled_ts=$3
                  WHERE setup_key=$1 AND status='OPEN'"""
        try:
            async with self._pool.acquire() as c:
                r = await c.execute(sql, setup_key, float(o.fill_price),
                                    _ts(o.filled_ts) if o.filled_ts else None)
        except Exception as exc:
            self.errors += 1
            log.warning("v4 store.apply_fill failed: %s", exc)
            return False
        if r and r.endswith("1"):
            self.updated += 1
            return True
        return False

    async def apply_outcome(self, setup_key: str, o) -> bool:
        if self._pool is None:
            return False
        sql = """UPDATE v4_recommendations SET
                   status=$2, fill_price=$3, exit_price=$4, gross_r=$5, fee_r=$6,
                   funding_r=$7, net_r=$8, mae_r=$9, mfe_r=$10, hold_minutes=$11,
                   filled_ts=$12, closed_ts=$13
                 WHERE setup_key=$1"""
        if o.status == "CANCELLED":
            # CANCELLED means "the window elapsed and the level never broke".
            # It is re-derived from the 1m tape every cycle, so a gap in that
            # tape over the fill bar would make an already-filled trade look
            # like one that never triggered — silently rewriting a real trade
            # out of the record. A row that has a fill can never go back.
            sql += " AND fill_price IS NULL"
        try:
            async with self._pool.acquire() as c:
                r = await c.execute(sql, setup_key, o.status, o.fill_price, o.exit_price,
                                    o.gross_r, o.fee_r, o.funding_r, o.net_r, o.mae_r,
                                    o.mfe_r, o.hold_minutes,
                                    _ts(o.filled_ts) if o.filled_ts else None,
                                    _ts(o.closed_ts) if o.closed_ts else None)
        except Exception as exc:
            self.errors += 1
            log.warning("v4 store.apply_outcome failed: %s", exc)
            return False
        if r and r.endswith("1"):
            self.updated += 1
            return True
        return False

    async def query(self, *, symbol=None, strategy=None, status=None, limit=200) -> list[dict]:
        """`status` accepts one value or a comma-separated set — a live position
        is 'FILLED' and a waiting order is 'OPEN', and several callers need both
        without paying for two round trips."""
        if self._pool is None:
            return []
        where, args = [], []
        if symbol:
            args.append(symbol); where.append(f"symbol=${len(args)}")
        if strategy:
            args.append(strategy); where.append(f"strategy_id=${len(args)}")
        if status:
            wanted = [s.strip().upper() for s in str(status).split(",") if s.strip()]
            args.append(wanted); where.append(f"status = ANY(${len(args)})")
        args.append(int(limit))
        sql = ("SELECT * FROM v4_recommendations"
               + (" WHERE " + " AND ".join(where) if where else "")
               + f" ORDER BY decision_ts DESC LIMIT ${len(args)}")
        try:
            async with self._pool.acquire() as c:
                rows = await c.fetch(sql, *args)
        except Exception as exc:
            self.errors += 1
            log.warning("v4 store.query failed: %s", exc)
            return []
        out = []
        for r in rows:
            d = dict(r)
            for k in ("decision_ts", "valid_until_ts", "filled_ts", "closed_ts", "created_at"):
                if d.get(k):
                    d[k] = int(d[k].timestamp())
            out.append(d)
        return out

    async def open_setups(self) -> list[dict]:
        """Everything still being tracked against the tape.

        FILLED belongs here: those are live positions still hunting a target or
        a stop. Querying OPEN alone would drop a trade out of the advance loop
        the moment it filled, and it could then never be resolved.
        """
        return await self.query(status="OPEN,FILLED", limit=500)

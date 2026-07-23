"""V3 L1 — Chart Read Engine: one timeframe, read like a trader.

Pure deterministic fold over closed candles → TfRead dict:
swings → structure (trend/BOS/CHOCH) → trendlines (lifecycle) → zones
(S/R, supply–demand, order blocks, FVG, trendline bands — all with lifecycle)
→ liquidity map (ranked pools, swept state, PDH/PDL/PWH/PWL/sessions)
→ premium/discount.

No indicators drive anything. No wall clock. No randomness. Fold(candles) is a
pure function — same candles, same read (the determinism contract).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from marketscalper.v3 import domain as d
from marketscalper.v3.config import V3Config, DEFAULT


# ---------------------------------------------------------------- helpers

def _parse_ts(v) -> int:
    """Candle ts (ISO string or epoch) → epoch seconds (UTC)."""
    if isinstance(v, (int, float)):
        return int(v)
    dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _utc(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


class _Atr:
    """Wilder ATR — incremental, closed candles only."""

    def __init__(self, period: int):
        self.period = period
        self.value: float | None = None
        self._prev_close: float | None = None
        self._seed: list = []

    def update(self, h: float, lo: float, c: float) -> None:
        tr = (h - lo) if self._prev_close is None else max(
            h - lo, abs(h - self._prev_close), abs(lo - self._prev_close))
        self._prev_close = c
        if self.value is None:
            self._seed.append(tr)
            if len(self._seed) >= self.period:
                self.value = sum(self._seed) / len(self._seed)
        else:
            self.value = (self.value * (self.period - 1) + tr) / self.period


# ---------------------------------------------------------------- engine

class ChartReadEngine:
    """Folds one timeframe's candles into a complete trader's read."""

    def __init__(self, symbol: str, tf: str, cfg: V3Config = DEFAULT):
        self.symbol, self.tf, self.cfg = symbol, tf, cfg
        self._ids = 0

    def _id(self, kind: str) -> str:
        self._ids += 1
        return f"{self.symbol}:{self.tf}:{kind}:{self._ids}"

    # ---- public --------------------------------------------------------

    def read(self, candles: list[dict]) -> dict:
        """The full fold. `candles` = closed candles ascending (ChartService)."""
        cfg = self.cfg
        n = len(candles)
        C = [{"ts": _parse_ts(c["ts"]), "o": float(c["o"]), "h": float(c["h"]),
              "l": float(c["l"]), "c": float(c["c"]), "v": float(c.get("v") or 0)}
             for c in candles]

        atr = _Atr(cfg.atr_period)
        atr_series: list[float | None] = []

        swings: list[d.Swing] = []
        last_high: d.Swing | None = None
        last_low: d.Swing | None = None
        structure = d.Structure()
        zones: list[d.Zone] = []
        pools: list[d.LiquidityPool] = []

        # day / week / session tracking
        day_key = None
        day = {"h": None, "l": None}
        prev_day: dict | None = None
        week_key = None
        week = {"h": None, "l": None}
        prev_week: dict | None = None
        sess_state: dict = {}          # name -> {"h","l"} for the CURRENT day

        def spawn_zone(kind, lo, hi, ts, origin, flipped_from=None) -> d.Zone:
            pad = (atr.value or 0.0) * cfg.zone_pad_atr
            z = d.Zone(id=self._id(f"zone.{kind.lower()}"), symbol=self.symbol,
                       tf=self.tf, created_at=ts, state=d.FRESH,
                       kind=kind, lo=min(lo, hi) - pad, hi=max(lo, hi) + pad,
                       origin=origin, flipped_from=flipped_from)
            z._log(ts, "created", origin)
            zones.append(z)
            return z

        def spawn_pool(kind, price, ts, side, members=(), session=None,
                       reason="") -> d.LiquidityPool:
            p = d.LiquidityPool(
                id=self._id(f"pool.{kind.lower()}"), symbol=self.symbol,
                tf=self.tf, created_at=ts, state=d.UNSWEPT, kind=kind,
                price=price, side=side,
                priority=cfg.pool_priorities.get(kind, 1),
                member_ids=tuple(members), session=session)
            p._log(ts, "created", reason or kind)
            pools.append(p)
            return p

        # ---- the bar loop ---------------------------------------------
        for i, bar in enumerate(C):
            ts, o, h, lo, c = bar["ts"], bar["o"], bar["h"], bar["l"], bar["c"]
            atr.update(h, lo, c)
            atr_series.append(atr.value)
            a = atr.value
            body = abs(c - o)
            displaced = a is not None and body > cfg.displacement_atr * a

            # -- day / week / session rollovers (UTC, from candle ts only)
            dt = _utc(ts)
            dk = dt.date()
            if dk != day_key:
                if day_key is not None:
                    prev_day = {"h": day["h"], "l": day["l"], "date": day_key}
                    # PDH/PDL pools refresh each day
                    pools[:] = [p for p in pools if p.kind not in ("PDH", "PDL")]
                    spawn_pool("PDH", prev_day["h"], ts, d.BUYSIDE,
                               reason=f"previous day high ({day_key})")
                    spawn_pool("PDL", prev_day["l"], ts, d.SELLSIDE,
                               reason=f"previous day low ({day_key})")
                    # session pools for the finished day
                    for name, ss in sess_state.items():
                        if ss["h"] is not None:
                            pools[:] = [p for p in pools
                                        if not (p.kind == "SESSION_H" and p.session == name)]
                            pools[:] = [p for p in pools
                                        if not (p.kind == "SESSION_L" and p.session == name)]
                            spawn_pool("SESSION_H", ss["h"], ts, d.BUYSIDE,
                                       session=name, reason=f"{name} session high")
                            spawn_pool("SESSION_L", ss["l"], ts, d.SELLSIDE,
                                       session=name, reason=f"{name} session low")
                day_key, day = dk, {"h": h, "l": lo}
                sess_state = {}
            else:
                day["h"] = max(day["h"], h)
                day["l"] = min(day["l"], lo)
            wk = dt.isocalendar()[:2]
            if wk != week_key:
                if week_key is not None:
                    prev_week = {"h": week["h"], "l": week["l"]}
                    pools[:] = [p for p in pools if p.kind not in ("PWH", "PWL")]
                    spawn_pool("PWH", prev_week["h"], ts, d.BUYSIDE,
                               reason="previous week high")
                    spawn_pool("PWL", prev_week["l"], ts, d.SELLSIDE,
                               reason="previous week low")
                week_key, week = wk, {"h": h, "l": lo}
            else:
                week["h"] = max(week["h"], h)
                week["l"] = min(week["l"], lo)
            # session running H/L (UTC hours from config)
            hour = dt.hour + dt.minute / 60.0
            for name, (s0, s1) in (("ASIA", cfg.session_asia_utc),
                                   ("LONDON", cfg.session_london_utc),
                                   ("NY", cfg.session_ny_utc)):
                if s0 <= hour < s1:
                    ss = sess_state.setdefault(name, {"h": h, "l": lo})
                    ss["h"] = max(ss["h"], h)
                    ss["l"] = min(ss["l"], lo)

            # -- swing confirmation at j = i - k ------------------------
            k = cfg.swing_k
            j = i - k
            if j >= k:
                win = C[j - k: j + k + 1]
                pj = C[j]
                if all(pj["h"] > w["h"] for wi, w in enumerate(win) if wi != k):
                    label = None
                    if last_high is not None:
                        label = "HH" if pj["h"] > last_high.price else "LH"
                    sw = d.Swing(id=self._id("swing"), symbol=self.symbol,
                                 tf=self.tf, created_at=pj["ts"],
                                 state="CONFIRMED", kind=d.HIGH,
                                 price=pj["h"], ts=pj["ts"], confirmed_ts=ts,
                                 label=label)
                    sw._log(ts, "confirmed", f"swing high {pj['h']} ({label})")
                    swings.append(sw)
                    last_high = sw
                    self._on_swing(sw, swings, zones, pools, spawn_zone,
                                   spawn_pool, a, ts)
                if all(pj["l"] < w["l"] for wi, w in enumerate(win) if wi != k):
                    label = None
                    if last_low is not None:
                        label = "HL" if pj["l"] > last_low.price else "LL"
                    sw = d.Swing(id=self._id("swing"), symbol=self.symbol,
                                 tf=self.tf, created_at=pj["ts"],
                                 state="CONFIRMED", kind=d.LOW,
                                 price=pj["l"], ts=pj["ts"], confirmed_ts=ts,
                                 label=label)
                    sw._log(ts, "confirmed", f"swing low {pj['l']} ({label})")
                    swings.append(sw)
                    last_low = sw
                    self._on_swing(sw, swings, zones, pools, spawn_zone,
                                   spawn_pool, a, ts)

            # -- structure: trend + BOS/CHOCH ---------------------------
            self._structure(structure, swings, last_high, last_low, c, ts,
                            displaced)
            # order block on a displaced structure event this bar
            ev = structure.events[-1] if structure.events else None
            if ev is not None and ev.ts == ts and ev.displaced:
                self._order_block(C, i, ev, spawn_zone, a)

            # -- supply / demand base→impulse ---------------------------
            if a is not None and body >= cfg.impulse_body_atr * a and i >= 1:
                base_lo, base_hi, nb = None, None, 0
                for b in range(i - 1, max(i - 1 - cfg.base_max_candles, -1), -1):
                    bb = C[b]
                    if abs(bb["c"] - bb["o"]) < cfg.base_body_atr * a:
                        base_lo = bb["l"] if base_lo is None else min(base_lo, bb["l"])
                        base_hi = bb["h"] if base_hi is None else max(base_hi, bb["h"])
                        nb += 1
                    else:
                        break
                if nb >= 1:
                    kind = d.DEMAND if c > o else d.SUPPLY
                    if not any(z.kind == kind and z.state in (d.FRESH, d.TESTED)
                               and not (z.hi < base_lo or z.lo > base_hi)
                               for z in zones):
                        spawn_zone(kind, base_lo, base_hi, ts,
                                   f"{nb}-candle base before "
                                   f"{'up' if kind == d.DEMAND else 'down'} impulse "
                                   f"(body {body:.2f} ≥ {cfg.impulse_body_atr}×ATR)")

            # -- FVG ----------------------------------------------------
            if i >= 2 and a is not None:
                c1, c3 = C[i - 2], C[i]
                if c1["h"] < c3["l"] and (c3["l"] - c1["h"]) >= cfg.fvg_min_atr * a:
                    spawn_zone(d.FVG, c1["h"], c3["l"], ts,
                               f"bullish 3-candle imbalance {c1['h']}→{c3['l']}")
                elif c1["l"] > c3["h"] and (c1["l"] - c3["h"]) >= cfg.fvg_min_atr * a:
                    spawn_zone(d.FVG, c3["h"], c1["l"], ts,
                               f"bearish 3-candle imbalance {c3['h']}→{c1['l']}")

            # -- zone lifecycle fold ------------------------------------
            for z in zones:
                if z.created_at >= ts or z.state in (d.BROKEN, d.RETIRED):
                    continue
                if (ts - z.created_at) > self._bars_to_secs(cfg.zone_max_age_bars):
                    z._transition(ts, d.RETIRED, "max age reached")
                    continue
                if h >= z.lo and lo <= z.hi:               # bar touched the band
                    closed_through = c > z.hi or c < z.lo
                    if closed_through and displaced:
                        z._transition(ts, d.BROKEN,
                                      f"displaced close through at {c}")
                        if z.kind in (d.DEMAND, d.SUPPLY, d.SR) and not z.flipped_from:
                            flip = d.SUPPLY if z.kind == d.DEMAND else \
                                d.DEMAND if z.kind == d.SUPPLY else d.SR
                            spawn_zone(flip, z.lo, z.hi, ts,
                                       f"role-flip of broken {z.kind.lower()} {z.id}",
                                       flipped_from=z.id)
                        continue
                    reaction = "PIERCED" if closed_through else "HELD"
                    z.touches.append((ts, reaction))
                    if z.touch_count >= cfg.zone_weak_touches:
                        z._transition(ts, d.WEAK,
                                      f"touch #{z.touch_count} — orders consumed")
                    elif z.state == d.FRESH:
                        z._transition(ts, d.TESTED, f"first touch ({reaction})")

            # -- liquidity swept fold + post-sweep resolution -----------
            for p in pools:
                if p.state == d.UNSWEPT and p.created_at < ts:
                    if (p.side == d.BUYSIDE and h > p.price) or \
                       (p.side == d.SELLSIDE and lo < p.price):
                        p.swept_at = ts
                        p._sweep_idx = i
                        p._transition(ts, d.SWEPT, f"wick through {p.price}")
                elif p.state == d.SWEPT and p.post_sweep == "PENDING" and \
                        getattr(p, "_sweep_idx", None) is not None and \
                        (i - p._sweep_idx) >= cfg.sweep_resolve_bars:
                    # judged N bars later: did the raid reverse or continue?
                    if p.side == d.BUYSIDE:
                        p.post_sweep = "REVERSED" if c < p.price else "CONTINUED"
                    else:
                        p.post_sweep = "REVERSED" if c > p.price else "CONTINUED"
                    p._log(ts, "post_sweep", p.post_sweep)

        # ---- post-pass: trendlines + premium/discount ------------------
        trendlines = self._trendlines(C, swings, atr_series)
        for tl in trendlines:                       # trendline zones (VALID+)
            if tl.state in (d.TL_VALID, d.TL_STRONG):
                px = tl.price_at(n - 1)
                tol = (atr_series[-1] or 0.0) * self.cfg.tl_touch_tol_atr
                z = spawn_zone(d.TRENDLINE_ZONE, px - tol, px + tol,
                               C[-1]["ts"],
                               f"{tl.side.lower()} trendline {tl.id} "
                               f"({len(tl.touches)} touches)")
                z.touches = [(t, "HELD") for t, _ in tl.touches]
                if z.touch_count:
                    z.state = d.TESTED

        rng = self._dealing_range(swings)
        last_close = C[-1]["c"] if C else None
        pd_state = None
        if rng and last_close is not None:
            pd_state = "DISCOUNT" if last_close < rng["eq"] else "PREMIUM"

        return self._payload(C, atr_series, swings, structure, trendlines,
                             zones, pools, rng, pd_state, prev_day, prev_week,
                             sess_state)

    # ---- swing side-effects: EQ pools + SR clusters --------------------

    def _on_swing(self, sw, swings, zones, pools, spawn_zone, spawn_pool,
                  a, ts) -> None:
        cfg = self.cfg
        if a is None:
            return
        same = [s for s in swings[:-1] if s.kind == sw.kind]
        # equal highs / lows → liquidity pool
        eq = [s for s in same[-8:] if abs(s.price - sw.price) <= cfg.eq_pool_atr * a]
        if eq:
            kind = "EQH" if sw.kind == d.HIGH else "EQL"
            members = [s.id for s in eq] + [sw.id]
            mean = sum(s.price for s in eq + [sw]) / (len(eq) + 1)
            existing = next((p for p in pools if p.kind == kind
                             and p.state == d.UNSWEPT
                             and abs(p.price - mean) <= cfg.eq_pool_atr * a), None)
            if existing:
                existing.member_ids = tuple(set(existing.member_ids) | set(members))
                existing.price = mean
                existing._log(ts, "extended", f"now {len(existing.member_ids)} equal swings")
            else:
                spawn_pool(kind, mean, ts,
                           d.BUYSIDE if sw.kind == d.HIGH else d.SELLSIDE,
                           members=members,
                           reason=f"{len(members)} equal "
                                  f"{'highs' if sw.kind == d.HIGH else 'lows'}")
        # S/R cluster → zone
        cluster = [s for s in same[-10:] if abs(s.price - sw.price) <= cfg.sr_cluster_atr * a]
        if len(cluster) + 1 >= cfg.sr_min_members:
            prices = [s.price for s in cluster] + [sw.price]
            lo, hi = min(prices), max(prices)
            if not any(z.kind == d.SR and z.state not in (d.BROKEN, d.RETIRED)
                       and not (z.hi < lo or z.lo > hi) for z in zones):
                spawn_zone(d.SR, lo, hi, ts,
                           f"{len(prices)} swing "
                           f"{'highs' if sw.kind == d.HIGH else 'lows'} clustered")

    # ---- structure fold -------------------------------------------------

    def _structure(self, st, swings, last_high, last_low, close, ts,
                   displaced) -> None:
        highs = [s for s in swings if s.kind == d.HIGH][-2:]
        lows = [s for s in swings if s.kind == d.LOW][-2:]
        trend = d.RANGE
        if len(highs) == 2 and len(lows) == 2:
            if highs[-1].label == "HH" and lows[-1].label == "HL":
                trend = d.BULLISH
            elif highs[-1].label == "LH" and lows[-1].label == "LL":
                trend = d.BEARISH
        st.trend = trend
        # break events (close beyond the last confirmed swing, once per swing)
        if last_high is not None and close > last_high.price and \
                not getattr(last_high, "_broken", False):
            last_high._broken = True
            kind = "CHOCH" if trend == d.BEARISH else "BOS"
            ev = d.StructureEvent(kind, "UP", ts, displaced, last_high.id)
            st.events.append(ev)
            if kind == "BOS":
                st.last_bos = ev
            else:
                st.last_choch = ev
        if last_low is not None and close < last_low.price and \
                not getattr(last_low, "_broken", False):
            last_low._broken = True
            kind = "CHOCH" if trend == d.BULLISH else "BOS"
            ev = d.StructureEvent(kind, "DOWN", ts, displaced, last_low.id)
            st.events.append(ev)
            if kind == "BOS":
                st.last_bos = ev
            else:
                st.last_choch = ev

    def _order_block(self, C, i, ev, spawn_zone, a) -> None:
        want_down = ev.direction == "UP"     # bull OB = last down candle
        for b in range(i - 1, max(i - 11, -1), -1):
            bb = C[b]
            if (bb["c"] < bb["o"]) == want_down and abs(bb["c"] - bb["o"]) > 0:
                if want_down:
                    spawn_zone(d.ORDER_BLOCK, bb["l"], bb["o"], ev.ts,
                               f"last down candle before displaced {ev.kind} UP")
                else:
                    spawn_zone(d.ORDER_BLOCK, bb["o"], bb["h"], ev.ts,
                               f"last up candle before displaced {ev.kind} DOWN")
                return

    # ---- trendlines -----------------------------------------------------

    def _trendlines(self, C, swings, atr_series) -> list:
        cfg = self.cfg
        n = len(C)
        if n < 5:
            return []
        idx_of = {}
        ci = 0
        for s in swings:
            while ci < n and C[ci]["ts"] < s.ts:
                ci += 1
            idx_of[s.id] = ci if ci < n and C[ci]["ts"] == s.ts else max(ci - 1, 0)
            ci = 0  # reset (swings are ordered but restart to stay exact)
        # (simple exact lookup)
        ts_to_idx = {bar["ts"]: k for k, bar in enumerate(C)}
        for s in swings:
            idx_of[s.id] = ts_to_idx.get(s.ts, idx_of.get(s.id, 0))

        out = []
        for side, kind in (("RESISTANCE", d.HIGH), ("SUPPORT", d.LOW)):
            pts = [s for s in swings if s.kind == kind][-cfg.tl_anchor_swings:]
            cands = []
            for ai in range(len(pts)):
                for bi in range(ai + 1, len(pts)):
                    A, B = pts[ai], pts[bi]
                    ia, ib = idx_of[A.id], idx_of[B.id]
                    if ib <= ia:
                        continue
                    la, lb = math.log(A.price), math.log(B.price)
                    slope = (lb - la) / (ib - ia)
                    if side == "SUPPORT" and slope <= 0:
                        continue                       # rising support only
                    if side == "RESISTANCE" and slope >= 0:
                        continue                       # falling resistance only
                    intercept = la - slope * ia
                    tl = d.Trendline(
                        id=self._id("tl"), symbol=self.symbol, tf=self.tf,
                        created_at=B.ts, state=d.TL_NEW, side=side,
                        anchor_ids=(A.id, B.id), slope=slope,
                        intercept=intercept, a_idx=ia, b_idx=ib)
                    if self._validate_line(tl, C, pts, idx_of, atr_series):
                        cands.append(tl)
            cands.sort(key=lambda t: (
                {d.TL_STRONG: 3, d.TL_VALID: 2, d.TL_NEW: 1,
                 d.TL_WEAK: 0, d.TL_BROKEN: -1}.get(t.state, 0),
                len(t.touches), t.b_idx), reverse=True)
            kept, used = [], []
            for t in cands:                            # de-dup near-parallel
                if any(abs(t.slope - u.slope) < abs(u.slope) * 0.1 + 1e-9 and
                       abs(t.price_at(n - 1) - u.price_at(n - 1)) <
                       (atr_series[-1] or 1.0) * 0.3 for u in used):
                    continue
                kept.append(t)
                used.append(t)
                if len(kept) >= cfg.tl_keep_per_side:
                    break
            out.extend(kept)
        return out

    def _validate_line(self, tl, C, pts, idx_of, atr_series) -> bool:
        """Touches + lifecycle for one candidate line. False = geometrically
        invalid (price closed through it between the anchors)."""
        cfg = self.cfg
        n = len(C)
        sup = tl.side == "SUPPORT"
        # between anchors no close beyond the line
        for k in range(tl.a_idx, tl.b_idx + 1):
            a = atr_series[k] or atr_series[-1] or 0.0
            tol = a * cfg.tl_touch_tol_atr
            line = tl.price_at(k)
            if sup and C[k]["c"] < line - tol:
                return False
            if not sup and C[k]["c"] > line + tol:
                return False
        # touches: anchor swings + any swing near the line
        for s in pts:
            k = idx_of[s.id]
            if k < tl.a_idx:
                continue
            a = atr_series[k] or atr_series[-1] or 0.0
            if abs(s.price - tl.price_at(k)) <= a * cfg.tl_touch_tol_atr:
                tl.touches.append((s.ts, s.price))
        tl.touches.sort()
        tl.last_touch_idx = tl.b_idx
        nt = len(tl.touches)
        if nt >= 4:
            tl._transition(C[tl.b_idx]["ts"], d.TL_STRONG, f"{nt} touches")
        elif nt >= 3:
            tl._transition(C[tl.b_idx]["ts"], d.TL_VALID, f"{nt} touches")
        # after the second anchor: violated / broken?
        for k in range(tl.b_idx + 1, n):
            a = atr_series[k] or 0.0
            line = tl.price_at(k)
            beyond = (line - C[k]["c"]) if sup else (C[k]["c"] - line)
            if beyond > a * cfg.tl_break_atr:
                tl.broken_at = C[k]["ts"]
                tl._transition(C[k]["ts"], d.TL_BROKEN,
                               f"decisive close through at {C[k]['c']}")
                break
            if beyond > a * cfg.tl_touch_tol_atr:
                tl._transition(C[k]["ts"], d.TL_WEAK,
                               f"close beyond tolerance at {C[k]['c']}")
        if tl.state not in (d.TL_BROKEN,) and \
                (n - 1 - tl.last_touch_idx) > cfg.tl_max_age_bars:
            tl._transition(C[-1]["ts"], d.TL_INVALID, "stale — no recent touch")
        return True

    # ---- premium / discount --------------------------------------------

    def _dealing_range(self, swings) -> dict | None:
        pts = swings[-self.cfg.range_swings:]
        highs = [s.price for s in pts if s.kind == d.HIGH]
        lows = [s.price for s in pts if s.kind == d.LOW]
        if not highs or not lows:
            return None
        hi, lo = max(highs), min(lows)
        return {"high": hi, "low": lo, "eq": (hi + lo) / 2.0}

    # ---- payload --------------------------------------------------------

    def _bars_to_secs(self, bars: int) -> int:
        secs = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600,
                "4h": 14400, "1d": 86400}.get(self.tf, 3600)
        return bars * secs

    def _payload(self, C, atr_series, swings, structure, trendlines, zones,
                 pools, rng, pd_state, prev_day, prev_week, sess_state) -> dict:
        cfg = self.cfg
        n = len(C)

        def _hist(o):
            return [{"ts": t, "event": e, "reason": r} for t, e, r in o.history[-6:]]

        live_zones = [z for z in zones if z.state not in (d.RETIRED,)]
        live_zones.sort(key=lambda z: z.created_at, reverse=True)
        ev_out = [{"kind": e.kind, "direction": e.direction, "ts": e.ts,
                   "displaced": e.displaced} for e in structure.events[-10:]]
        return {
            "symbol": self.symbol,
            "tf": self.tf,
            "asof_ts": C[-1]["ts"] if C else None,
            "last_close": C[-1]["c"] if C else None,
            "bars": n,
            "atr": atr_series[-1] if atr_series else None,
            "trend": structure.trend,
            "structure": {
                "trend": structure.trend,
                "last_bos": ev_out and next((e for e in reversed(ev_out)
                                             if e["kind"] == "BOS"), None) or None,
                "last_choch": ev_out and next((e for e in reversed(ev_out)
                                               if e["kind"] == "CHOCH"), None) or None,
                "events": ev_out,
            },
            "swings": [{
                "id": s.id, "kind": s.kind, "price": s.price, "ts": s.ts,
                "label": s.label,
            } for s in swings[-cfg.max_swings_out:]],
            "trendlines": [{
                "id": t.id, "side": t.side, "state": t.state,
                "touches": len(t.touches), "role_flipped": t.role_flipped,
                # endpoints for direct chart drawing (ts + price)
                "a": {"ts": C[t.a_idx]["ts"], "price": t.price_at(t.a_idx)},
                "b": {"ts": C[-1]["ts"], "price": t.price_at(n - 1)},
                "broken_at": t.broken_at,
                "history": _hist(t),
            } for t in trendlines],
            "zones": [{
                "id": z.id, "kind": z.kind, "state": z.state,
                "lo": z.lo, "hi": z.hi, "touches": z.touch_count,
                "created_at": z.created_at, "origin": z.origin,
                "flipped_from": z.flipped_from,
                "history": _hist(z),
            } for z in live_zones[:cfg.max_zones_out]],
            "liquidity": sorted([{
                "id": p.id, "kind": p.kind, "price": p.price, "side": p.side,
                "priority": p.priority, "state": p.state,
                "swept_at": p.swept_at, "post_sweep": p.post_sweep,
                "session": p.session,
                "history": _hist(p),
            } for p in pools[-cfg.max_pools_out * 2:]],
                key=lambda x: (-x["priority"], x["price"]))[:cfg.max_pools_out],
            "dealing_range": rng,
            "premium_discount": pd_state,
            "context": {
                "prev_day": prev_day and {"high": prev_day["h"], "low": prev_day["l"]},
                "prev_week": prev_week,
                "sessions_today": {k: dict(v) for k, v in sess_state.items()},
            },
        }

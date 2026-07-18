"""MarketScalper entrypoint — the composition root (roadmap P0.27).

The ONLY place components are constructed and wired together, with plain
config-driven selection (Architecture Part-D rule; no DI framework, no
plugins). No business logic lives here.

Wiring (all components pre-existing):
    EventBus · StateStore (constructed BEFORE the app so bus ordering makes
    state_diff contain the announced candle) · CandleBuilder · CandleWriter ·
    KlineReconciler (built candles via a bus subscription; reference klines
    via BinanceFeed's explicit callback) · ClockOffsetSampler · FastAPI app
    (ReplayFeed injected as the replay provider) · uvicorn server.

Launch settings come from the environment (config layer 3, per D3):
    MARKETSCALPER_API_TOKEN  required — refuses to start without it
    MARKETSCALPER_API_HOST   default 127.0.0.1
    MARKETSCALPER_API_PORT   default 8000
    MARKETSCALPER_FEED       default "binance" (provider selection, Part D)
plus the existing config chain (symbols, DB DSN, logging).

Lifecycle duties owned by the composition root (Decision D2): ensure candle
partitions at startup and after each UTC midnight.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections import deque
from datetime import datetime, timedelta, timezone
from math import exp

import uvicorn

from marketscalper import db
from marketscalper.api.app import create_app
from marketscalper.config import Config, load_config
from marketscalper.core.bus import EventBus
from marketscalper.core.candle_builder import CandleBuilder
from marketscalper.core.candle_writer import CandleWriter
from marketscalper.core.reconciler import KlineReconciler
from marketscalper.core.recorder import SignalRecorder, engine_version_stamp
from marketscalper.core.state import StateStore
from marketscalper.engines.confluence import confluence_zones
from marketscalper.engines.fvg import FvgEngine
from marketscalper.engines.liquidity import LiquidityEngine, SweepEvent
from marketscalper.engines.orderblock import OrderBlockEngine
from marketscalper.engines.momentum import (IncrementalATR, MomentumState,
                                            RegimeClassifier)
from marketscalper.analytics import compute_analytics
from marketscalper.engines.lifecycle import RecommendationLifecycle
from marketscalper.engines.psychology import PsychologyGuard
from marketscalper.ops import (FEED_WATCHDOG_INTERVAL_S, feed_gap_alerts,
                               format_daily_summary)
from marketscalper.engines.qualification import (QualificationEngine,
                                                 spread_pct_of)
from marketscalper.engines.risk import management_guidance, plan_trade
from marketscalper.engines.strategy import StrategyEngine
from marketscalper.engines.structure import (BosDetector, ChochDetector,
                                             PivotDetector, PivotLabeler,
                                             TrendState)
from marketscalper.engines.trendline import TrendlineBook, TrendlineDetector
from marketscalper.engines.volume import VolumeEngine
from marketscalper.logging_setup import setup_logging
from marketscalper.providers.base import BookTicker, Candle
from marketscalper.providers.binance import BinanceFeed, ClockOffsetSampler
from marketscalper.providers.replay import ReplayFeed

log = logging.getLogger(__name__)

_FEEDS = {"binance": BinanceFeed}  # provider selection map (Part D: plain config)

# D21.5: display-scaling default for §7 suggested qty (qty/risk_amt are
# display-only; net RR is equity-independent). Live main() overrides via
# MARKETSCALPER_EQUITY_USD; replay/tests use this constant — replay ≡ live.
DEFAULT_EQUITY_USD = 10000.0


class _StructurePipeline:
    """One symbol's 1m analysis chain (P1.19 composition, frozen engines,
    pinned cadence). Publishes a JSON-ready payload into the StateStore
    after every closed 1m candle; the existing WS diff carries it (§9).
    R1: no pool — engines persist nothing in Phase 1. 5m engine instances
    arrive with their first consumer (P2, A8)."""

    _PIVOTS_SHOWN = 30      # marker history depth in the payload
    _EVENTS_SHOWN = 10      # BOS/CHOCH label history depth
    _ZONES_SHOWN = 10       # confluence display cap (D15.3)

    def __init__(self, symbol: str, store: StateStore,
                 clock_provider=None, volume_seed=None,
                 equity=DEFAULT_EQUITY_USD, psych_guard=None) -> None:
        self._symbol = symbol
        self._psych_guard = psych_guard            # D23 (P4.9), live-only
        self._store = store
        self._clock_provider = clock_provider      # D16.2 G1 (live only)
        self._spread_pct = None                    # latest G2 input
        self._equity = equity                      # D21.5 (display scaling)
        self._atr = IncrementalATR()
        self._atr_5m = IncrementalATR()            # regime input (D16.5)
        self._momentum = MomentumState(self._atr)
        self._regime = RegimeClassifier(symbol, self._atr, self._atr_5m)
        self._volume = VolumeEngine(symbol, self._atr)
        if volume_seed:                            # D19.2: 20-day bucket seed
            self._volume.seed(volume_seed)
        self._detector = PivotDetector(symbol, "1m")
        self._labeler = PivotLabeler()
        self._trend = TrendState()
        self._bos = BosDetector(self._trend, self._atr)
        self._choch = ChochDetector(self._trend)
        self._tl_detector = TrendlineDetector(self._atr)
        self._book = TrendlineBook(self._tl_detector, self._atr,
                                   rvol_provider=lambda: self._volume.rvol)
        self._liq = LiquidityEngine(symbol, self._atr,
                                    rvol_provider=lambda: self._volume.rvol)
        self._ob = OrderBlockEngine(symbol)
        self._fvg = FvgEngine(symbol, self._atr)
        self._detector_5m = PivotDetector(symbol, "5m")   # first 5m consumer:
        self._labeler_5m = PivotLabeler()                 # A8 range (D12.6)
        self._trend_5m = TrendState()                     # D20.2: S1/S2 context
        self._qual = QualificationEngine(symbol, self._atr, self._trend,
                                         self._momentum, self._regime,
                                         volume=self._volume)   # D21.3 seam
        self._strategy = StrategyEngine(symbol, self._atr)   # D20 (P3.12)
        self._lifecycle = RecommendationLifecycle(symbol)    # D22 (P4.2)
        self._pivots: deque = deque(maxlen=self._PIVOTS_SHOWN)
        self._signals: deque = deque(maxlen=self._EVENTS_SHOWN)
        self._recommendations: deque = deque(maxlen=self._EVENTS_SHOWN)
        self._records: list = []       # (signal, qual, plan, rec|None) —
        self._lifecycle_events: list = []  # drained every bar (D21.6/P4.5)
        self._last_payload = None
        self._bos_events: deque = deque(maxlen=self._EVENTS_SHOWN)
        self._choch_events: deque = deque(maxlen=self._EVENTS_SHOWN)
        self._sweep_events: deque = deque(maxlen=self._EVENTS_SHOWN)
        self._shift_events: deque = deque(maxlen=self._EVENTS_SHOWN)
        self._bar = -1          # positional axis, lockstep with the engines
        # Freeze-audit fix: the reconnect path can emit a stale pre-gap
        # bucket AFTER its backfilled successors (accepted D7 residual).
        # The engines assume chronological candles, so the composition
        # drops out-of-order candles here — one guard for every engine.
        self._last_ts = None
        self._last_ts_5m = None

    def step(self, candle: Candle) -> None:
        """The pinned per-closed-candle cadence, engines in §1 order."""
        if self._last_ts is not None and candle.ts <= self._last_ts:
            log.warning("engines: dropped out-of-order 1m candle %s %s "
                        "(last %s)", self._symbol, candle.ts, self._last_ts)
            return
        self._last_ts = candle.ts
        self._bar += 1
        self._atr.update(candle)
        self._momentum.update(candle)              # P1.2: ATR first
        self._regime.update()                      # after both ATRs (D16.5)
        self._volume.update(candle)                # D19.8 phase 1: rvol is
        self._tl_detector.update(candle)           # ready for all consumers
        for pivot in self._detector.update(candle):
            labeled = self._labeler.label(pivot)
            self._pivots.append(labeled)
            self._trend.on_pivot(labeled)
            self._bos.on_pivot(labeled)
            self._choch.on_pivot(labeled)
            self._tl_detector.on_pivot(labeled)
            self._liq.on_pivot(labeled)
            self._strategy.on_pivot(labeled)       # D20.5: S2/S3 legs
        self._trend.update(candle)
        bos_event = self._bos.update(candle)
        if bos_event is not None:
            self._bos_events.append(bos_event)
            self._choch.on_bos(bos_event)
            self._ob.on_bos(bos_event)             # D13.5 cadence
        choch_event = self._choch.update(candle)
        if choch_event is not None:
            self._choch_events.append(choch_event)
        tl_events = self._book.refresh(candle)
        if choch_event is not None:                # D12.7: CHOCH before liq
            self._liq.on_choch(choch_event)
        liq_events = self._liq.update(candle)
        for event in liq_events:
            if isinstance(event, SweepEvent):
                self._sweep_events.append(event)
            else:
                self._shift_events.append(event)
        self._volume.classify(candle, self._liq.key_levels,   # D19.8
                              self._liq.pools,                # phase 2
                              self._liq.running_extremes)
        self._ob.update(candle)                    # after liquidity (D13.5)
        self._fvg.update(candle)                   # after order blocks (D14.3)
        zones = confluence_zones(                  # D15.3: after fvg
            blocks=self._ob.blocks, breakers=self._ob.breakers,
            gaps=self._fvg.gaps, lines=self._book.active,
            pools=self._liq.pools, key_levels=self._liq.key_levels,
            atr=self._atr.value, bar_index=self._bar)
        psych = (self._psych_guard.evaluate(candle.ts, self._symbol)
                 if self._psych_guard is not None else None)   # D23.5
        qual = self._qual.update(                  # D16.5
            candle, bos_event=bos_event, choch_event=choch_event,
            tl_events=tl_events, liq_events=liq_events, zones=zones,
            spread_pct=self._spread_pct,
            clock=(self._clock_provider()
                   if self._clock_provider is not None else None),
            psych=psych)
        new_recs = []
        bar_signals = []
        for signal in self._strategy.evaluate(     # D20.5: last consumer
                candle,
                trend_5m=self._trend_5m.state, bos_event=bos_event,
                choch_event=choch_event, tl_events=tl_events,
                liq_events=liq_events, zones=zones,
                blocks=self._ob.blocks, gaps=self._fvg.gaps,
                pools=self._liq.pools, levels=self._liq.key_levels,
                premium_discount=self._liq.premium_discount,
                session_vwap=self._volume.session_vwap,
                rvol=self._volume.rvol):
            self._signals.append(signal)
            bar_signals.append((signal.strategy, signal.direction))
            plan, rec = self._admit(signal, qual)  # D21.2 (§6→§7 flow)
            if rec is not None:
                self._recommendations.append(rec)
                new_recs.append(rec)
            self._records.append((signal, qual, plan, rec))
        # P4.2 lifecycle: advance PRE-EXISTING recs (this candle appended)
        # BEFORE registering the ones just created (their creation bar is
        # this candle — never advanced on their own creation bar).
        g1_ok = qual.gates[0].passed
        events = self._lifecycle.update(candle, opposite_signals=bar_signals,
                                        g1_ok=g1_ok)
        for ev in events:
            self._apply_lifecycle_event(ev)
        self._lifecycle_events.extend(events)
        for rec in new_recs:
            rec["status"] = "active"               # D22.1 initial state
            self._lifecycle.on_recommendation(rec, candle)
        payload = self._payload(candle, zones, qual)
        self._last_payload = payload               # D21.1 state_snapshot
        self._store.set_structure(self._symbol, payload)

    def _apply_lifecycle_event(self, ev) -> None:
        """Reflect a terminal transition on the payload's recommendation
        (status + eval_*), so the UI sees the current lifecycle state
        (P4.2). The deque is bounded — a rec pushed out is DB-only."""
        for rec in self._recommendations:
            if (rec["created_ts"], rec.get("strategy")) == ev.rec_key:
                rec["status"] = ev.status
                rec["status_reason"] = ev.reason
                if ev.outcome is not None:
                    rec["eval_outcome"] = ev.outcome.outcome
                    rec["eval_r"] = ev.outcome.eval_r
                    rec["eval_mae"] = ev.outcome.eval_mae
                    rec["eval_mfe"] = ev.outcome.eval_mfe
                break

    def drain_lifecycle(self) -> list:
        """This bar's lifecycle transitions for the recorder (P4.5);
        empties the buffer."""
        events, self._lifecycle_events = self._lifecycle_events, []
        return events

    def snapshot_payload(self) -> dict | None:
        """The last published payload — the signal row's state_snapshot."""
        return self._last_payload

    def _admit(self, signal, qual):
        """§7 planning + the D21.2 recommendation admission — pure, runs
        in every wiring so payloads are identical with or without a
        recorder. Returns (plan, rec_dict | None)."""
        plan = plan_trade(direction=signal.direction, entry=signal.entry,
                          sl=signal.sl, tp1=signal.tp1, tp2=signal.tp2,
                          equity=self._equity)
        if (qual.verdict not in ("TRADEABLE", "A_PLUS")
                or plan.status != "suggested" or not plan.rr_floor_ok):
            return plan, None                      # signal row only (D21.2)
        rec = {
            "id": None,                            # P4.7: the recorder fills
            "strategy": signal.strategy, "direction": signal.direction,
            "created_ts": signal.created_ts.isoformat(),
            "entry": plan.entry, "sl": plan.sl,
            "tp1": plan.tp1, "tp2": plan.tp2,
            "qty": plan.qty, "risk_amt": plan.risk_amt,
            "est_fees": plan.qty * plan.fee_per_unit,
            "net_rr_tp1": plan.net_rr_tp1, "net_rr_tp2": plan.net_rr_tp2,
            "guidance": list(management_guidance(plan)),
            "score": qual.score, "verdict": qual.verdict,
            "invalid_after_bars": signal.invalid_after_bars,   # D22.1a
        }
        return plan, rec

    def drain_records(self) -> list:
        """This bar's (signal, qual, plan, rec) tuples for the recorder
        (D21.6); empties the buffer. Recorder-less wirings simply never
        call it — the bounded payload deques carry the display state."""
        records, self._records = self._records, []
        return records

    def step_5m(self, candle: Candle) -> None:
        """5m closed candle: pivots feed the A8 external range (D12.6)."""
        if self._last_ts_5m is not None and candle.ts <= self._last_ts_5m:
            log.warning("engines: dropped out-of-order 5m candle %s %s "
                        "(last %s)", self._symbol, candle.ts, self._last_ts_5m)
            return
        self._last_ts_5m = candle.ts
        self._atr_5m.update(candle)                # regime input (D16.5)
        for pivot in self._detector_5m.update(candle):
            labeled = self._labeler_5m.label(pivot)
            self._liq.on_external_pivot(labeled)
            self._volume.on_anchor(labeled)        # D19.4 anchor intake
            self._trend_5m.on_pivot(labeled)       # D20.2: 5m context
            self._strategy.on_external_pivot(labeled)   # D20.2: S1 TP2
        self._trend_5m.update(candle)              # D10 cadence, tf-generic

    def on_book_ticker(self, ticker: BookTicker) -> None:
        """Latest spread for the G2 gate (D16.2; live feeds only)."""
        self._spread_pct = spread_pct_of(ticker.bid_px, ticker.ask_px)

    def _payload(self, candle: Candle, zones, qual) -> dict:
        """Everything the overlays draw — pre-serialized, no frontend math
        beyond rendering (line endpoints are projected here)."""
        cur = self._bar
        lines = []
        for line in self._book.active:
            lines.append({
                "side": line.side, "touches": line.touches,
                "x1": line.a_pivot.ts.isoformat(), "y1": line.a_pivot.price,
                "x2": candle.ts.isoformat(),
                "y2": exp(line.intercept + line.slope * (cur - line.a_index)),
            })
        channels = []
        for ch in self._book.channels():
            start_index = max(ch.support.a_index, ch.resistance.a_index)
            start_pivot = (ch.support.a_pivot
                           if ch.support.a_index >= ch.resistance.a_index
                           else ch.resistance.a_pivot)
            channels.append({
                "x1": start_pivot.ts.isoformat(),
                "y1": exp(ch.mid_value(start_index)),
                "x2": candle.ts.isoformat(),
                "y2": exp(ch.mid_value(cur)),
            })
        return {
            "trend": self._trend.state,
            "pivots": [{"ts": p.ts.isoformat(), "kind": p.kind,
                        "price": p.price, "label": p.label}
                       for p in self._pivots],
            "bos": [{"ts": e.ts.isoformat(), "direction": e.direction,
                     "close": e.close, "displacement": e.displacement}
                    for e in self._bos_events],
            "choch": [{"ts": e.ts.isoformat(), "direction": e.direction,
                       "close": e.close}
                      for e in self._choch_events],
            "trendlines": lines,
            "channels": channels,
            "liquidity": {
                "pools": [{"kind": p.kind, "price": p.price, "size": p.size,
                           "strength": p.strength}
                          for p in self._liq.pools],
                "levels": self._liq.key_levels,
                "premium_discount": self._liq.premium_discount,
                "sweeps": [{"ts": e.ts.isoformat(), "side": e.side,
                            "target": e.target, "price": e.target_price}
                           for e in self._sweep_events],
                "shifts": [{"sweep_ts": e.sweep.ts.isoformat(),
                            "ts": e.ts.isoformat()}
                           for e in self._shift_events],
            },
            "orderblocks": {
                "blocks": [{"direction": b.direction, "lo": b.zone_lo,
                            "hi": b.zone_hi, "status": b.status,
                            "created_ts": b.created_ts.isoformat()}
                           for b in self._ob.blocks],
                "breakers": [{"direction": b.direction, "lo": b.zone_lo,
                              "hi": b.zone_hi, "status": b.status,
                              "created_ts": b.created_ts.isoformat()}
                             for b in self._ob.breakers],
            },
            "fvgs": [{"direction": g.direction, "lo": g.lo, "hi": g.hi,
                      "ce": g.ce, "status": g.status,
                      "created_ts": g.created_ts.isoformat()}
                     for g in self._fvg.gaps],
            "volume": {
                "rvol": self._volume.rvol,
                "session_vwap": self._volume.session_vwap,
                "band_1_up": self._volume.band_1_up,
                "band_1_dn": self._volume.band_1_dn,
                "band_2_up": self._volume.band_2_up,
                "band_2_dn": self._volume.band_2_dn,
                "anchored_vwap": self._volume.anchored_vwap,
                "anchor_ts": (self._volume.anchor_ts.isoformat()
                              if self._volume.anchor_ts else None),
                "delta": self._volume.delta,
                "cum_delta": self._volume.cum_delta,
                "spike": self._volume.spike,
                "absorption": (None if self._volume.absorption is None else
                               {"level": self._volume.absorption.level,
                                "price": self._volume.absorption.price,
                                "delta_sign": self._volume.absorption.delta_sign,
                                "ts": self._volume.absorption.ts.isoformat()}),
                "exhaustion": self._volume.exhaustion,
            },
            "confluence": [{"kind": z.kind, "direction": z.direction,
                            "lo": z.lo, "hi": z.hi, "count": z.count,
                            "members": list(z.members),
                            "htf_magnet": z.htf_magnet,
                            "created_ts": z.created_ts.isoformat()}
                           for z in zones[:self._ZONES_SHOWN]],
            "signals": [{"strategy": s.strategy, "direction": s.direction,
                         "entry": s.entry, "sl": s.sl, "tp1": s.tp1,
                         "tp2": s.tp2,
                         "created_ts": s.created_ts.isoformat(),
                         "invalid_after_bars": s.invalid_after_bars,
                         "facts": list(s.facts)}
                        for s in self._signals],
            "recommendations": list(self._recommendations),   # D21.7
            "qualification": {
                "gates": [{"name": g.name, "passed": g.passed,
                           "flagged": g.flagged, "detail": g.detail}
                          for g in qual.gates],
                "data_integrity": qual.data_integrity,
                "components": qual.components,
                "score": qual.score,
                "verdict": qual.verdict,
                "agreement": qual.agreement,
                "reasons": list(qual.reasons),
            },
        }


def _row_to_candle(r) -> Candle:
    """Stored candle row -> normalized Candle (D19.2 seed reads)."""
    return Candle(
        symbol=r["symbol"], tf=r["tf"], ts=r["ts"],
        o=float(r["o"]), h=float(r["h"]), l=float(r["l"]), c=float(r["c"]),
        v=float(r["v"]), qv=float(r["qv"]),
        n_trades=r["n_trades"], taker_buy_v=float(r["taker_buy_v"]),
    )


def _wire_structure_engines(bus: EventBus, store: StateStore,
                            symbols, clock_provider=None,
                            seed_candles=None, recorder=None,
                            equity=DEFAULT_EQUITY_USD,
                            psych_guard=None) -> None:
    """P1.19: subscribe the per-symbol pipelines to closed 1m candles.
    Must be wired AFTER the StateStore and BEFORE create_app so the WS
    broadcast's diff already contains the just-computed structure.
    clock_provider: live main() passes the D6 sampler surface for G1;
    replay/tests leave it None (flagged pass, D16.2).
    seed_candles: dict symbol -> historical 1m candles for the D19.2
    RVOL bucket seed (the 20 days preceding the stream start); None ->
    unseeded (rvol warms from the stream).
    recorder: live main()'s SignalRecorder (D21.6); replay/tests pass
    None — payloads are identical either way, only persistence differs."""
    pipelines = {
        symbol: _StructurePipeline(
            symbol, store, clock_provider,
            volume_seed=(seed_candles or {}).get(symbol),
            equity=equity, psych_guard=psych_guard)
        for symbol in symbols}

    async def on_candle(candle: Candle) -> None:
        pipeline = pipelines.get(candle.symbol)
        if pipeline is None:
            return
        if candle.tf == "1m":
            pipeline.step(candle)
            records = pipeline.drain_records()     # always drained (bounded)
            events = pipeline.drain_lifecycle()    # P4.5 status/eval writes
            if recorder is not None and records:
                await recorder.record(candle.symbol, records,
                                      pipeline.snapshot_payload())
            if recorder is not None and events:
                await recorder.record_lifecycle(candle.symbol, events)
        elif candle.tf == "5m":
            pipeline.step_5m(candle)

    async def on_book_ticker(ticker: BookTicker) -> None:
        pipeline = pipelines.get(ticker.symbol)
        if pipeline is not None:                   # G2 input (D16.2)
            pipeline.on_book_ticker(ticker)

    bus.subscribe(Candle, on_candle)
    bus.subscribe(BookTicker, on_book_ticker)


def main() -> int:
    config = load_config()
    setup_logging(level=config.app.log_level, log_dir=config.app.log_dir)

    token = os.environ.get("MARKETSCALPER_API_TOKEN", "")
    if not token:
        log.error("MARKETSCALPER_API_TOKEN is not set — refusing to start (D3)")
        return 2
    if not config.database.dsn:
        log.error("database DSN is not configured — refusing to start")
        return 2
    feed_name = os.environ.get("MARKETSCALPER_FEED", "binance")
    if feed_name not in _FEEDS:
        log.error("unknown feed provider %r (available: %s)",
                  feed_name, ", ".join(_FEEDS))
        return 2
    # D21.5: equity is validated at startup (the D3 refuse-to-start
    # pattern) — a non-positive value would make plan_trade geometry-
    # reject every plan, silently killing all recommendations.
    raw_equity = os.environ.get("MARKETSCALPER_EQUITY_USD",
                                str(DEFAULT_EQUITY_USD))
    try:
        equity = float(raw_equity)
    except ValueError:
        log.error("MARKETSCALPER_EQUITY_USD=%r is not a number — refusing "
                  "to start (D21.5)", raw_equity)
        return 2
    if not equity > 0:
        log.error("MARKETSCALPER_EQUITY_USD must be positive (got %s) — "
                  "refusing to start (D21.5)", equity)
        return 2

    host = os.environ.get("MARKETSCALPER_API_HOST", "127.0.0.1")
    port = int(os.environ.get("MARKETSCALPER_API_PORT", "8000"))

    log.info(
        "MarketScalper starting — decision support only (never executes trades); "
        "feed=%s symbols=%s api=%s:%d",
        feed_name, ",".join(config.symbols), host, port,
    )
    asyncio.run(_run(config, _FEEDS[feed_name], token, host, port, equity))
    log.info("MarketScalper stopped")
    return 0


async def _run(config: Config, feed_cls, token: str, host: str, port: int,
               equity: float = DEFAULT_EQUITY_USD) -> None:
    pool = await db.create_pool(config.database.dsn)
    async with pool.acquire() as conn:
        created = await db.ensure_partitions(conn)         # D2: startup
        log.info("partitions ensured at startup (%d created)", created)

    bus = EventBus()
    store = StateStore(bus)                                # before create_app
    CandleBuilder(bus)
    CandleWriter(bus, pool)
    reconciler = KlineReconciler()

    async def to_built(candle: Candle) -> None:            # truth 1m -> reconciler
        if candle.tf == "1m":
            reconciler.on_built(candle)

    bus.subscribe(Candle, to_built)
    sampler = ClockOffsetSampler()                 # before wiring: G1 input
    # D19.2 (owner-approved): seed the RVOL buckets from the 20 days
    # preceding the stream start — composition owns the read, the engine
    # stays database-unaware. Empty history -> unseeded warm-up.
    seed_end = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)
    seed_start = seed_end - timedelta(days=20)
    seed_candles: dict[str, list[Candle]] = {}
    async with pool.acquire() as conn:
        for symbol in config.symbols:
            rows = await db.select_candles(conn, symbol, "1m",
                                           seed_start, seed_end)
            seed_candles[symbol] = [_row_to_candle(r) for r in rows]
            log.info("volume seed: %s — %d candles [%s .. %s)",
                     symbol, len(rows), seed_start, seed_end)
    recorder = SignalRecorder(pool, engine_version_stamp())   # D21.6 (live)
    psych_guard = PsychologyGuard()                # D23 (P4.9), live-only
    _wire_structure_engines(
        bus, store, config.symbols,
        clock_provider=lambda: (sampler.offset_s, sampler.in_sync),
        seed_candles=seed_candles, recorder=recorder, equity=equity,
        psych_guard=psych_guard)

    feed = feed_cls(config.symbols, bus,
                    on_reference_candle=reconciler.on_reference)
    app = create_app(bus, store, pool, token, replay_provider=ReplayFeed,
                     replay_wiring=_wire_structure_engines,   # F2: full chain
                     psych_guard=psych_guard)                 # D23.5 (P4.9)

    await feed.start()
    await sampler.start()
    rollover = asyncio.create_task(_daily_ops(pool), name="daily-ops")
    watchdog = asyncio.create_task(                    # P4.13 feed-gap alert
        _feed_gap_watchdog(store, config.symbols), name="feed-gap-watchdog")

    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info"))
    # The composition root owns process lifecycle: route SIGTERM/SIGINT to a
    # graceful uvicorn stop so serve() returns and cleanup below runs (without
    # this, uvicorn restores the default handler and re-raises the captured
    # signal after shutdown, killing the process before cleanup — exit -15).
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, server.handle_exit, sig, None)
        except NotImplementedError:  # Windows dev machine — no asyncio signal
            break                    # handlers; uvicorn falls back to
                                     # signal.signal() itself (Ctrl+C only)
    try:
        await server.serve()                               # until SIGTERM/SIGINT
    finally:
        rollover.cancel()
        watchdog.cancel()
        await asyncio.gather(rollover, watchdog, return_exceptions=True)
        await sampler.stop()
        await feed.stop()
        await pool.close()


async def _daily_ops(pool) -> None:
    """D2 partition re-ensure + P4.13 daily stats snapshot just after each
    UTC midnight."""
    while True:
        now = datetime.now(tz=timezone.utc)
        next_midnight = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        await asyncio.sleep((next_midnight - now).total_seconds() + 60)
        try:
            async with pool.acquire() as conn:
                created = await db.ensure_partitions(conn)
                analytics = await compute_analytics(conn)
            log.info("partitions ensured at UTC rollover (%d created)", created)
            log.info(format_daily_summary(analytics))       # P4.13 snapshot
        except Exception as exc:                            # keep the loop alive
            log.error("daily ops failed: %s", exc)


async def _feed_gap_watchdog(store, symbols) -> None:
    """P4.13: alert when a symbol's closed 1m candle stream stalls (feed
    outage / stale connection). Logs a structured ALERT; never mutates
    state — the reconnect/backfill machinery (P0.10/P0.15) does the healing."""
    while True:
        await asyncio.sleep(FEED_WATCHDOG_INTERVAL_S)
        now = datetime.now(tz=timezone.utc)
        last_seen = {}
        for symbol in symbols:
            state = store.snapshot(symbol)
            candle = state.last_candle_1m if state is not None else None
            last_seen[symbol] = candle.ts if candle is not None else None
        for symbol, gap in feed_gap_alerts(last_seen, now):
            log.warning("ALERT feed gap: %s — no closed 1m candle for %.0fs",
                        symbol, gap)


if __name__ == "__main__":
    raise SystemExit(main())

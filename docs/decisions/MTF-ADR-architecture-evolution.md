# DECISION D26 — ADR: Multi-Timeframe Charting (Architecture Evolution)

**Date:** 2026-07-19 · **Status:** Owner-APPROVED (architecture); Phase 0 record ·
**Type:** Architecture evolution (display layer only) · **Supersedes:** nothing —
**additive**. `docs/Architecture.md` v1.2 and `docs/IMPLEMENTATION_PLAN.md` remain
FROZEN and UNMODIFIED. This ADR is the authoritative record for the multi-timeframe
(MTF) charting feature and the owner's frozen decisions governing it.

Basis: a 5-agent read-only forensic review (data layer, ingestion/build/replay,
API/WS/composition, frontend, decision-engine impact). Companion decisions:
[[MTF-calendar-alignment]] (D27) and [[MTF-completeness-policy]] (D28).

---

## D26.1 — What this is (and is NOT)

MarketScalper's **chart display** expands from 1m/5m to nine timeframes:
`1m, 5m, 15m, 30m, 1H, 4H, 1D, 1W, 1M`. Higher timeframes (15m and above) are
**derived on demand** by a new, isolated `ChartService`.

This is a **charting / display** evolution. It is **NOT** an analysis-scope change:

> **The decision engine stays EXACTLY 1m/5m and FROZEN.** No higher timeframe may
> influence scoring, gates, recommendations, the structure engine, the liquidity
> engine, or replay determinism — ever — unless a future architecture revision
> explicitly re-approves it (owner decision #2).

## D26.2 — Project scale (owner decision, frozen)

1–2 concurrent users maximum. **NOT a SaaS.** Optimize for **simplicity,
correctness, determinism, maintainability** — never for hundreds/thousands of
users. When two designs perform similarly, choose the simpler one. Do not
over-engineer.

## D26.3 — The isolation invariant (the load-bearing guarantee)

The forensic proof (Review 6): the decision engine's entire input surface is
`_wire_structure_engines.on_candle`, gated by `if tf=="1m" … elif tf=="5m"`; every
other tf falls through and is ignored. No higher-TF producer exists. The frozen
weighted score (0.30/0.30/0.25/0.15) and gates G1–G6 contain no timeframe branch.
Therefore MTF is provably non-affecting **iff all three clauses hold**:

- **(a)** An HTF candle is **NEVER** published onto an engine-carrying `EventBus`.
  (If it were, engines would ignore it — but `CandleWriter` persists *every* bus
  candle unconditionally and the WS would broadcast it → **CRITICAL** corruption
  of the canonical store. HTF must never reach the bus.)
- **(b)** HTF data is **NEVER** written into the `_payload()` / `structure` dict.
  (The determinism harness V1–V4 hashes `state.structure`; injecting HTF there
  breaks every hash — **HIGH**. HTF context, if ever surfaced, uses a *separate*
  `SymbolState` field — see D26.8.)
- **(c)** HTF rows are **NOT** persisted under an engine-read tf. Compute-on-read
  only (owner decision #4) — no HTF rows in `candles` at all.

These three clauses are the acceptance contract for every phase.

## D26.4 — Owner's frozen architecture decisions (verbatim intent)

1. **Thin client** — frontend never aggregates, caches, builds candles, owns
   replay state, or owns market correctness. Backend owns everything.
2. **Decision engine frozen** at 1m/5m (D26.1).
3. **ChartService** owns historical aggregation, the Chart API, timeframe
   conversion, and historical retrieval; fully isolated from the decision engine.
4. **Compute-on-read.** Do NOT materialize HTF candles. Do NOT introduce Redis,
   background aggregation workers, TimescaleDB, or continuous aggregates. Canonical
   DB = **1m candles**; everything else is derived on demand.
5. **Chart API** — `GET /api/chart?symbol&timeframe&from&to` → `{candles, metadata,
   overlays}`. Only 1m/5m return overlays; higher TFs return candles only.
6. **Historical source of truth** — DB first, provider second. On a missing range,
   fetch canonical **1m** from the provider, store it (append-only), then return the
   aggregated result. **Never fetch higher-TF candles from the provider.**
7. **Replay** is 100% database-driven; never requests historical data from the
   exchange; uses only stored canonical candles.
8. **Timeframes:** the nine listed above.
9. **UI** — segmented timeframe buttons (not a dropdown); remember the last TF;
   preserve zoom / drawings / crosshair / replay position across a switch.
10. **Provider independence** — ChartService must never import Binance-specific
    code; historical retrieval goes through the existing `FeedProvider` abstraction
    (P0.19 import boundary); a future Delta provider must work with zero ChartService
    changes.
11. **DB philosophy** — single source of truth = canonical 1m; everything else
    derived; never duplicate historical storage.
12. **Implementation order** — Phases 0–4 (D26.9).

## D26.5 — Component design (as approved)

**`ChartService`** (new read-model, `backend/marketscalper/core/chart_service.py`,
OUTSIDE `engines/`). Two responsibilities, both pure recompute-from-1m:

- **Historical aggregation** — for a `(symbol, tf, from, to)` request:
  - `1m` / `5m` → served directly from the canonical stored rows
    (`db.select_candles`), no aggregation.
  - `15m…1M` → aggregated from stored 1m via guarded SQL (D26.6), closed buckets
    only, bucketing per [[MTF-calendar-alignment]], completeness per
    [[MTF-completeness-policy]].
- **Historical gap-fill** (owner decision #6) — DB first; for any missing 1m
  sub-range, fetch **1m** through an **injected `FeedProvider`** (never a concrete
  provider import; never HTF from the provider), persist it (append-only), then
  aggregate. Reuses the existing reconnect-backfill pattern.
- (Phase 3, optional) **Live forming bar** — an in-memory O(1) fold of *closed* 1m
  candles for the active TF, surfaced transport-only. Never a bus `Candle`.

**Injection** — `create_app(..., chart_service=None)` keyword seam (exact
`replay_provider`/`psych_guard` precedent; `None` → 503). Constructed in
`main.py::_run` after the pool; subscribed to the bus *before* `create_app` only if
Phase 3's forming bar needs closed-1m observation (Phases 1–2 need only the pool).

## D26.6 — Determinism guards (mandatory in every aggregation query)

1. Ordered open/close — `(array_agg(o ORDER BY ts))[1]` / `…DESC)[1]`.
2. Closed-buckets-only — exclude the currently-forming bucket (no-repaint).
3. Completeness handling per [[MTF-completeness-policy]].
4. Forced UTC — `date_trunc(field, ts, 'UTC')`; `date_bin` grouping is TZ-independent.

## D26.7 — Chart API contract (owner decision #5)

`GET /api/chart?symbol&timeframe&from&to` (Bearer, reuses `require_token`):
```json
{ "candles": [ {"ts":"…ISO…","o":0,"h":0,"l":0,"c":0,"v":0} ],
  "metadata": {"symbol":"…","timeframe":"15m","from":"…","to":"…","count":0,
               "source_tf":"1m","aggregated":true,"last_closed_ts":"…"},
  "overlays": null }
```
`overlays` is non-null ONLY for `1m`/`5m`. `/candles` (the existing bare-array
`tf∈{1m,5m}` endpoint) is **left untouched** — extending or wrapping it is HIGH-risk
(breaks a pinned contract + the thin-client bootstrap).

## D26.8 — Explicitly deferred (NOT in this feature)

Higher-TF *context* as read-only annotation (daily bias / 4H trend / weekly
structure) is **out of scope** here. If ever added, it attaches to a **separate
`SymbolState` field** (never `structure`), is display-only, and must NOT enter the
score or gates without a new decision record + an `ENGINE_VERSION` bump (owner
decision #2). Recorded so the boundary is explicit, not silently crossed.

## D26.9 — Implementation order (owner decision #12)

- **Phase 0** — ADRs (this doc + D27 + D28) + architecture documentation → **owner
  approval** (this gate).
- **Phase 1** — `ChartService` + Chart API + aggregation + gap-fill + unit tests.
  Backend only. Acceptance: aggregation independently recomputed vs raw 1m; 5m
  short-circuit == stored 5m; double-query byte-identical; determinism V1–V4
  **unchanged** (hash-neutral by construction — clause (b)).
- **Phase 2** — Frontend segmented selector, parametrized history load, zoom/drawing/
  crosshair preservation, overlays gated to {1m,5m}, last-TF memory in `ui.js`, and
  a new purity-test guard **banning client-side aggregation** (closes the forensic
  enforcement gap).
- **Phase 3** — Replay integration (HTF over replay ranges + optional live forming
  bar via an additive WS `forming` key or client-fold) + regression tests.
- **Phase 4** — Final QA + performance verification (<300ms cached ranges) +
  architecture audit against clauses (a)/(b)/(c).

Each phase: green CI + unchanged V1–V4 hashes + a freeze audit. The feature is
additive at every phase — rollback removes the endpoint/service/buttons and restores
current behavior exactly.

## D26.10 — What must never be touched

`candle_builder` (`_WINDOW=5` non-parametric), `providers/*` (`_TF_MS` not widened;
never fetch HTF from an exchange), `/candles`, the `structure`/`_payload` dict, the
engine bus, and the frozen engines/score/gates. Every HIGH/CRITICAL risk in the
forensic review stems from reusing this frozen 1m/5m machinery for derived MTF data
instead of giving MTF its own parallel, read-only, transport-only lane.

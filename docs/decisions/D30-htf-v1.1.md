# D30 — Higher-Timeframe Intelligence (HTF V1.1)

**Status:** Implemented + deployed (2026-07-20)
**Scope:** owner-approved HTF V1.1 — an ADDITIVE, ISOLATED context layer. No
change to the frozen 1m/5m decision engine, the architecture, or execution.

## What was built

A full Smart-Money-Concepts read of **15m / 1h / 4h / 1d**, per timeframe:
trend, market structure, BOS, CHOCH, swing high/low, liquidity (pools),
liquidity sweep, supply, demand, support, resistance, trendlines, EMA
alignment, momentum, and a per-tf **HTF score + bias**. Plus an **overall** HTF
score, bias, confidence, market story, and explanation.

Surfaced through a new `GET /api/htf?symbol=` and an always-visible **HTF panel**
in the Live rail (overall bias + score + confidence, market story, four
per-timeframe cards, and an **alignment badge** vs the current 1m/5m signal).

## Isolation invariant (D26.3) — preserved and proven

HTF **reuses the frozen analysis engines** (instantiated on aggregated candles,
never modified) but is completely off the canonical path:

- it never `bus.publish`es a candle (so it can't enter the v0 event-hash stream),
- it never writes the `structure` payload / `store.set_structure` (so it can't
  enter the v1–v4 object-hash stream),
- it never persists a candle or an analysis row.

It is a compute-on-read read-model exactly like ChartService, surfaced only via a
separate REST path. **Determinism V1–V4 stayed byte-identical** across the whole
implementation (verified by the §10 self-consistency gate on every CI run).

## Key design decisions

- **Reuse, don't duplicate.** The isolated `_Pipeline` mirrors the production
  `main.py` `step()` cadence (ATR → momentum → trendline detector → pivot fan-out
  → trend → BOS → CHOCH → book.refresh → liquidity → order blocks) using the
  frozen engine classes.
- **PivotDetector borrows k=2** (the `"5m"` depth). Its `K_BY_TF` gate only knows
  1m/5m and raises otherwise; `Pivot.tf` is cosmetic to everything HTF reads, so a
  uniform 5-bar swing definition is used across HTF tfs without touching the file.
- **Displayed trend is derived from structure (HH/HL vs LH/LL) + EMA**, not the
  frozen `TrendState` — which is a memoryless band classifier that reads RANGE on
  most closed HTF bars (correct for driving BOS/CHOCH intra-run, useless as a
  displayed trend). `TrendState` still drives BOS/CHOCH internally.
- **Per-tf score** = a weighted signed sum (trend 3 / EMA 2 / BOS 1.5 / CHOCH 1 /
  momentum 0.5 / demand-supply 1 / ema200 0.5) mapped to 0–100 (50 neutral) + a
  bias. **Overall** = tf-weighted roll-up (1d 4 / 4h 3 / 1h 2 / 15m 1); confidence
  = the tf-weight fraction agreeing with the overall bias; market story = a
  deterministic top-down (Daily → 15M) narrative.
- **HtfService** caches per symbol for a short TTL (the analysis only changes when
  an HTF candle closes ≥ 15 min) and fetches only RECENT ranges (so ChartService
  never triggers a deep gap-fill).
- **Integration = display-layer, additive.** The frozen recommendation / score /
  verdict is untouched. The panel compares the HTF bias with the live signal
  direction (a plain string compare, no engine math) and shows an aligned /
  conflicting badge. Execution stays 1m/5m; HTF only adds context and confidence.

## Milestones + verification

1. **M1** analysis + scoring core (`core/htf.py`) — 9 unit tests; RCA'd + fixed two
   QA issues (displayed-trend derivation; stable range S/R).
2. **M2** `HtfService` + `GET /api/htf` — 6 tests; verified on prod with real
   9-year data.
3. **M3+M4** HTF panel UI + signal alignment (`frontend/htf.js`, pure renderer) —
   2 contract tests; browser-QA'd (aligned / conflicting / null / not-ready).

Full CI green throughout; determinism V1–V4 byte-identical. Deployed to
scalper.aismartscan.in and verified (endpoint 200, assets served).

## Out of scope (unchanged)

No order placement / execution; HTF never mutates the frozen recommendation or
the determinism stream; no persistence of HTF data; the frozen engines and
Architecture v1.2 are untouched.

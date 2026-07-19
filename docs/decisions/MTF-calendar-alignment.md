# DECISION D27 — MTF higher-timeframe bucket alignment

**Date:** 2026-07-19 · **Status:** Proposed (Phase 0; approve with [[MTF-ADR-architecture-evolution]]) ·
**Scope:** the exact bucket boundaries the `ChartService` uses to aggregate canonical
1m → higher timeframes. Pins a detail the aggregation SQL depends on; changes no
architecture. Dependent code: Phase 1 aggregation.

## Context

`ChartService` derives 15m…1M from stored 1m on read (compute-on-read, D26). The nine
timeframes split into two bucketing families. Boundaries MUST match the conventions
the frozen engines already use for day/week/month rollovers (the A9/D12.1 liquidity
map + the `ensure_candle_partitions` month helper), so the chart and the analysis
never disagree about where a day/week/month begins. All boundaries are **UTC**
(the platform's only clock; determinism requires a pinned TZ).

## D27.1 — Fixed-width timeframes: 5m, 15m, 30m, 1H(60m), 4H(240m)

Epoch-anchored fixed-minute buckets via PostgreSQL `date_bin(make_interval(mins=>N),
ts, TIMESTAMPTZ '1970-01-01 00:00:00+00')`. Equivalent to the frozen builder's
`bucket - (bucket % N)` integer math. Because 5/15/30/60/240 all divide 1440 and the
UTC-epoch origin is a UTC midnight, every one of these tiles the UTC day evenly
(e.g. 4H → 00:00/04:00/08:00/12:00/16:00/20:00 UTC). `date_bin` grouping is defined in
absolute time, so it is session-timezone-independent — no TZ pin needed for this family.

Note: **5m is served from the canonical stored 5m rows, not aggregated** (D28), so the
5m entry here is only relevant if a future path ever needs to recompute it; the stored
5m rows are themselves epoch-aligned by the frozen builder, i.e. identical boundaries.

## D27.2 — Calendar timeframes: 1D, 1W, 1M (`date_trunc(field, ts, 'UTC')`, PG16)

Naive epoch-modular arithmetic is WRONG for these:
- **1W** — `bucket % 10080` would start weeks on **Thursday** (epoch 1970-01-01 was a
  Thursday). Must be **ISO week = Monday 00:00 UTC**, matching the liquidity engine's
  ISO-week rollover. → `date_trunc('week', ts, 'UTC')` (Postgres `week` is ISO/Monday).
- **1M** — calendar months are variable length (28–31 days); no fixed modulus works.
  → `date_trunc('month', ts, 'UTC')` — the exact semantics `ensure_candle_partitions`
  already uses for monthly partitions.
- **1D** — `date_trunc('day', ts, 'UTC')` = **00:00 UTC** day boundary, matching the
  liquidity engine's day rollover (PDH/PDL) and the A9 session day.

The `'UTC'` third argument is **mandatory** (a PG16 feature): `date_trunc` on a
`timestamptz` is otherwise session-TZ-dependent, which would make replay ≠ live and
break determinism.

## D27.3 — Consequences

- Chart day/week/month boundaries are identical to the analysis engines' rollovers —
  no cross-surface disagreement.
- Deterministic: every boundary is a pure function of `candle.ts` and a pinned UTC
  origin; no `now()` in the bucketing.
- Phase 1 tests pin representative boundaries per family (a 4H bucket at 08:00 UTC; an
  ISO-week starting Monday; a calendar month; a UTC day), including a DST-agnostic
  check (UTC has no DST, so this is inherently stable).

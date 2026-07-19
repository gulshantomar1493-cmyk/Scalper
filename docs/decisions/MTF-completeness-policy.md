# DECISION D28 — MTF higher-timeframe completeness / gap policy

**Date:** 2026-07-19 · **Status:** Proposed (Phase 0; approve with [[MTF-ADR-architecture-evolution]]) ·
**Scope:** how the `ChartService` handles missing/partial underlying 1m data when
aggregating a higher-TF display candle. A DISPLAY-layer policy only — the frozen
decision engine (1m/5m, F1 strict) is untouched. Dependent code: Phase 1 aggregation.

## The question

A higher-TF bucket (e.g. a 1D candle = 1440 one-minute candles) may have missing
sub-minutes. Trade-less minutes produce no 1m candle at all (the frozen builder emits
no synthetic gap candles), so on BTC/ETH a genuine 1m gap is rare but possible. Two
philosophies:

- **Strict-void** (the frozen builder's F1 rule for 5m): emit an HTF candle ONLY if
  every expected sub-minute is present; otherwise omit the whole bucket. Simple, but a
  single missing minute leaves a **visible hole** in the daily/weekly chart.
- **Best-effort**: emit an HTF candle for every closed bucket that has ≥1 underlying
  1m candle, aggregating whatever 1m exist. No chart holes on minor gaps.

## D28.1 — Decision (recommended): best-effort for derived TFs; canonical rows for 1m/5m

1. **1m and 5m are served from the canonical STORED rows** (`db.select_candles`),
   never aggregated. This is exact, matches the engines' own view byte-for-byte, and
   **sidesteps any "on-demand-5m must equal materialized-5m" consistency question**.
2. **15m…1M are derived on read** from 1m with **best-effort** aggregation:
   - Emit a candle for every bucket whose full time window lies **strictly in the
     past** (the no-repaint / closed-bucket requirement, D26.6) **and** that contains
     **≥ 1** underlying 1m candle.
   - Aggregate the 1m candles that ARE present (first open, max high, min low, last
     close, summed volumes — all with the ordered/UTC guards of D26.6).
   - **Drop only a fully-empty bucket** (zero 1m candles = a true data void — and
     genuine 1m voids are healed FIRST by the DB-first→provider gap-fill of owner
     decision #6, so a remaining empty bucket means the exchange itself had no trades).
   - **Per-candle completeness metadata (owner rule 5, Phase-1 lock):** each
     aggregated candle carries `complete` (bool) and `n` (count of underlying 1m
     candles present). `complete = (n == expected_minutes(tf, bucket_ts))` where
     expected = N for fixed TFs, 1440 for 1D, 10080 for 1W, and (days-in-month×1440)
     for 1M. Native `1m`/`5m` rows are canonical → `complete: true`. This lets the
     frontend render incomplete candles differently (e.g. dimmed) while the backend
     stays a pure deterministic function of the stored 1m. `metadata.count` also
     reports the number of candles returned.

## D28.2 — Why best-effort (given the owner's priorities)

- **Determinism (priority #1):** best-effort is still a **pure, deterministic
  function** of the stored 1m rows — same 1m in, same HTF out. Determinism does not
  require completeness; it requires reproducibility, which holds either way.
- **Correctness for a DISPLAY chart:** a daily candle summarizing 1438 of 1440 real
  minutes is a truthful, useful bar; a hole is not. This is display, not analysis —
  the frozen F1 strictness exists to protect the decision engine, which never sees
  these TFs (the D26.3 isolation invariant).
- **Simplicity (owner directive):** no completeness threshold to tune, no per-candle
  flag, no strict-void special-casing of the many trade-less minutes on 1M/1W. When
  two designs perform the same, the simpler wins — best-effort is the simpler *and*
  more useful design here.

## D28.3 — Consequences & the escape hatch

- The daily/weekly/monthly chart stays continuous across minor 1m gaps.
- If the owner later wants strict visual honesty, a `complete` flag per candle (from
  the `count(*) = expected` check already computable in the aggregation) is a trivial,
  additive extension — recorded here, not built now.
- The gap-fill (D26.6 / owner decision #6) remains the primary data-quality mechanism:
  real gaps are filled with canonical 1m from the provider before aggregation, so
  best-effort rarely has to paper over anything.

## D28.4 — Alternative on file

Strict-void (omit any incomplete bucket) is the more frozen-faithful mirror of the F1
5m rule and would be defensible if MTF candles were ever fed to analysis — but they are
not (D26.3), and it produces a worse chart. Chosen against for display; re-openable via
a one-line owner instruction if visual gap-honesty is later preferred over continuity.

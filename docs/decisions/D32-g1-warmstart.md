# D32 — G1 warm-start: seed the continuity window on a live restart

**Status:** Accepted (owner decision, 2026-07-20)
**Engine:** `engines/qualification.py` — additive `seed()` method (**no `ENGINE_VERSION` bump**)
**Composition:** `main.py` — `warm_g1` flag, live-only

## Context

§6 gate **G1** (data integrity) passes only when the engine has seen **30
contiguous closed 1m candles** and the clock is in sync. The 30 timestamps live
in an in-memory `deque(maxlen=30)` (`_ts_window`) filled one-per-closed-candle.

On every deploy the process restarts, so `_ts_window` starts **empty**. The
startup 20-day history read (D19.2) seeds only the **RVOL volume buckets**, not
the structure/qualification chain — so after each deploy G1 reported
`"warming: N/30 candles"` → `data_integrity = DEGRADED` → `NO_SIGNAL` for
**~30 minutes** until 30 live candles closed.

Diagnosed live (2026-07-20): the VPS clock is NTP-synced and the Binance offset
is ~0.01s (`in_sync=True` from the first sample, taken at t=0), so the clock is
**not** a factor — the 30-candle in-memory warm-up was the whole cause. It
self-heals, but the owner wants the post-deploy blackout removed.

## Decision

**On a live restart, seed the G1 continuity window from the recent stored
candles** — the same 20-day history already read for the RVOL seed. Mirrors the
D19.2 "composition owns the storage read; the engine stays database-unaware"
pattern.

- `QualificationEngine.seed(candles)` — additive method (like `VolumeEngine.seed()`).
  Fills `_ts_window` with the trailing minute-**contiguous** run (≤ maxlen) of the
  provided candles. A gap anywhere in the tail seeds **only the run nearest now** —
  it can never manufacture a false "contiguous". Touches ONLY `_ts_window` (not
  `_bar`, the recency windows, `update`, `_gates`, or scoring).
- `main.py`: a `warm_g1` flag on `_StructurePipeline` / `_wire_structure_engines`.
  **Only live `_run` passes `warm_g1=True`** (reusing the RVOL `seed_candles`).
  The F2 replay path (`app.py`) and the determinism harness call the wiring
  **without** it → default `False` → cold warm-up unchanged.

## Rationale / safety

1. **Honest, not a cheat.** The 30 seeded candles are real, contiguous, confirmed
   rows already in the DB. G1's claim "the last 30 candles are contiguous" is
   **true** after seeding — consistent with §0.4 "validate before trust" (we are
   trusting *complete* stored history, not a *partial* live window — the D7 concern).
2. **No false pass.** If the seed→live boundary has a real gap (the missing minute
   is not backfilled), G1's own contiguity check catches it and warms normally —
   the seed only ever *helps* the common (contiguous) case. Unit-tested.
3. **Boundary is bridged by P0.15.** The seed ends at the last stored candle; the
   reconnect/backfill fetches the gap up to the live edge before live processing
   resumes, so the common case is a clean contiguous boundary → G1 passes on the
   **first** live candle (~1 min post-deploy, since the clock is already known).
4. **Determinism-safe by construction.** `warm_g1` is passed only by live `main()`.
   Replay/tests/harness never pass it, so V1–V4 and their content assertions
   (e.g. "29 G1-warming bars") stay **byte-identical** (verified). Only live
   startup behavior changes.
5. **No `ENGINE_VERSION` bump.** `seed()` is composition-provided *initial state*,
   not a change to the per-candle logic — the same reasoning as the RVOL seed
   (which left Volume's version untouched) and the D23.7 inert-when-unwired rule.
   The git-hash prefix in the D1 stamp already distinguishes the code era.

## Scope

Fixes only the G1 (data-integrity) warm-up. The weighted **score** still ramps
as ATR / regime / structure / liquidity warm from the live stream — but the hard
`NO_SIGNAL` block and the DEGRADED badge clear immediately, which was the owner's
pain point. A full "warm start" (replaying history through the entire chain) was
considered (audit Option B) and **not** taken — it is heavier and riskier and was
not needed for the reported problem.

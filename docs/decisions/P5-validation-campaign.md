# Phase 5 — Validation Campaign: discipline + process (P5.1, P5.4)

**Date:** 2026-07-19 · **Status:** Accepted (process/discipline records;
the campaign itself is owner-operated). Covers the roadmap's non-code
Phase-5 tasks and records the scope split for the rest.

Phase 5 is the **validation campaign** — running the platform live and
making data-backed kill/keep decisions per strategy. Its essence is
operational, not implementational: the software (Phases 0–4) is complete;
Phase 5 is about generating ≥200 real recommendations and judging
expectancy. This document pins the two process disciplines (P5.1 holdout,
P5.4 weekly ritual) and records what is autonomously built vs
owner-operated.

## P5.1 — Out-of-sample holdout

Calibration must never see the data it is judged on. The discipline:

- **Lock one calendar month as the holdout** — excluded from ALL
  calibration and threshold tuning. Recommended: the most recent complete
  month at the START of the campaign, fixed for the campaign's duration
  and never re-picked.
- Every parameter change (SL formula, thresholds, the D9 regime/momentum
  constants) is tuned ONLY on in-sample data (everything except the
  holdout) and then **verified on the holdout** before it is kept. A
  change that improves in-sample but not holdout is overfitting — reject
  it.
- The holdout month is recorded in the weekly review log (P5.4) so it is
  auditable and immutable. The threshold-calibration harness (P5.3) must
  restrict its replay sweeps to the in-sample range; that harness depends
  on the D9 config-plumbing task (see below) and is not yet built.

## P5.4 — Weekly review ritual

Every week of the campaign, run and record:

1. **Data-quality audit** — `GET /campaign/audit` (P5.5). Any violation is
   fixed before trusting that week's numbers.
2. **Determinism re-run** — the determinism harness (V1–V4 + the persisted
   rows) already runs on every CI invocation; a weekly full `scripts/ci.sh`
   confirms no repaint/nondeterminism crept in.
3. **Expectancy read** — `GET /campaign/expectancy` (P5.7) + the MAE
   distribution (`GET /analytics/mae`, P5.2) per strategy.
4. **Kill/keep + parameter change log** — for each strategy, a one-line
   decision (keep / watch / kill) with the data behind it, and any
   parameter change with its in-sample tuning + holdout verification
   (P5.1). The log is append-only — the campaign's audit trail.

The kill/keep checkpoint at ~100 recommendations (P5.6) and the final
expectancy report at ≥200 (P5.7 data / P5.8 gate) are entries in this same
log, made by the owner from the tool output.

## Scope split (recorded)

**Built autonomously (the tooling the ritual uses):**
- P5.2 — MAE-distribution SL-tuning tool (`GET /analytics/mae`).
- P5.5 — data-quality audit (`GET /campaign/audit`).
- P5.7 — expectancy report generator (`GET /campaign/expectancy`), the
  P5.8 gate's number (fees-included expectancy + ≥200-sample sufficiency +
  positive-after-fees → `trusted_eligible`).

**Owner-operated (live campaign — real market data + calendar time):**
- P5.6 — the ~100-recommendation kill/keep checkpoint.
- P5.7 (data) / P5.8 — the ≥200-recommendation TRUSTED gate. The
  `expectancy_report` computes `trusted_eligible` mechanically; the
  DECISION to mark a strategy TRUSTED is the owner's, from real campaign
  data. §0 rule 4 stands: a strategy is TRUSTED only after ≥200 logged
  recommendations with positive fees-included expectancy — the platform
  never auto-trusts, and never executes trades regardless.

**Blocked on a prerequisite:**
- P5.3 — the threshold-calibration harness needs engine constants to be
  config-driven (the D9 config-plumbing obligation, recorded at P1.3/D9 as
  "its own task"). The engines currently hardcode their pins; a config
  surface is a deliberate, separate change to the frozen engines, not
  folded into P5. Until it lands, calibration is manual (edit a constant,
  re-run the in-sample replay, verify on the holdout) — the harness
  automates that loop once the config surface exists.

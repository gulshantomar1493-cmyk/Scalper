# DECISION D18 — Verified-defect fix milestone (F1–F4)

**Date:** 2026-07-18 · **Status:** Accepted (owner-directed fix milestone
after the milestone audit + beyond-doubt verification pass) · **Scope:**
fixes for the four VERIFIED findings only. No feature work, no refactor.

## F1 — 5m completeness (Genuine Production Correctness Defect — frozen code reopened)

**Reopened:** `core/candle_builder.py` and the mirrored fold in
`providers/replay.py` (both P0-frozen Foundation) — under the standing
rule that frozen code may be modified only for a genuine production
correctness defect, which this was (verified with reproductions: windows
entered mid-way after restarts/gaps, holes inside windows, and stale
reconnect partials all published and persisted wrong 5m candles that
permanently diverged from replay).

**Rule now enforced (D7, uniformly):** a 5m candle publishes ONLY when
its window is complete — seeded at the window head and folded
contiguously through all five minutes (closed 1m candles arrive in
strictly increasing bucket order, so head + contiguity ⇒ all five
present at the boundary). Any incomplete window is discarded with a
WARNING and never persisted. The P0.13-era "publish with what it
received" behavior (which two tests pinned) is superseded; those tests
now pin the D7 rule, with regressions for every verified scenario.
Builder↔replay fold equivalence continues to be enforced by the
pipeline-identity test.

## F2 — Replay isolation (Integration Defect — composition/API/frontend only)

Each replay session now runs on its **own EventBus** with its own
StateStore and — via the new `create_app(..., replay_wiring=...)`
parameter, which composition supplies as `_wire_structure_engines` — its
own fresh engine pipelines (the determinism harness's exact shape).
Consequences: replay drives the complete engine chain again regardless
of live progress; the live bus never sees replay candles (no
out-of-order drops, no duplicate CandleWriter inserts, no reconciler
pending leakage); live processing continues untouched underneath. While
a replay is active the live WS push is suppressed (diffs stay consumed)
and resumes automatically at completion/stop. Frontend: replay start
clears the chart and polls `/replay/status` (existing endpoint, now
consumed); completion or stop re-bootstraps the live chart. No payload
or protocol change; no engine logic touched; the live out-of-order guard
is unchanged.

## F3 — CI gate integrity (Infrastructure)

`scripts/ci.sh` now **fails (exit 1)** when `MARKETSCALPER_DB_DSN` is
unset, because the mandatory §10 determinism harness, the conformance
suite, and the DB gates cannot run without it — a vacuous green is
refused. The two stale TODO comments describing already-delivered gates
were corrected in the same file. No test weakened; the pytest
skip-without-DSN behavior for ad-hoc developer runs is unchanged.

## F4 — WebSocket backpressure (Infrastructure)

The bus-side broadcast no longer performs network sends: each WS client
gets a bounded queue (256 payloads) drained by its own sender task. A
slow or blocked client fills its queue and is disconnected (close 1013,
fire-and-forget); the thin client's existing reconnect + REST bootstrap
recovers it. Feed reading, candle building, persistence, the EventBus,
and the engine chain can no longer stall on any browser socket. Payload
shape and protocol unchanged.

## Records superseded

The P1.19 status nuance "replay-while-live interleaves streams into the
same engine instances (harmless)" and the P0-CLOSURE notes about replay
duplicate-insert/reconciler noise are superseded by F2's isolation (the
scenarios no longer occur). The P0.13 status line "partial windows across
gaps discarded" is now fully true rather than partially true (F1).

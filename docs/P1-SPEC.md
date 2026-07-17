# P1 Implementation Specification — Structure + Trendlines

**Status:** DRAFT for owner approval · **Date:** 2026-07-17
**Derived strictly from:** Architecture v1.2 (FROZEN) §0–§4, §9–§12 · Roadmap v2.0 Part B (A4–A8) + Part C Phase 1 + Part D · Decisions D1–D8 · `docs/P0-CLOSURE.md`.
**Rule:** nothing here changes the architecture. Where the blueprint is silent, this spec pins the detail (or routes it to the phase's [DECISION] tasks) so that implementation makes **zero** new design decisions.

---

## 1. Objectives

1. Implement the shared momentum utilities (§4.7): incremental ATR(14) on 1m + 5m, velocity, acceleration, momentum-shift flag, body dominance, and the regime classifier (§4.2) — consumed by every later engine.
2. Implement the **Structure Engine** (§4.2): k-bar-confirmed pivots, HH/HL/LH/LL labeling, BULLISH/BEARISH/RANGE trend state, BOS with displacement classification, CHOCH with confirmed-flip logic — all repaint-free (`ts` + `confirmed_ts`).
3. Implement the **Trendline Engine** (§4.3): candidate generation from confirmed pivots, touch validation, scoring + cluster dedup (max 3+3), TOUCH/BREAK/FAKE-BREAK classification, lifecycle + channels.
4. Surface both engines on the chart via LWC v5 primitives and extend the replay audit workflow (jump-to-random-trendline, accept/reject tally).
5. Grow the determinism harness to v1: pivots + levels byte-identical across double replay.
6. Pass the **P1 gate** (P1.22): ≥80% agreement on 50 randomly audited trendlines over a 30-day replay; determinism pass; docs updated.

## 2. Scope

Roadmap tasks **P1.1–P1.22**, in order. New code lives in `backend/marketscalper/engines/` (currently an intentionally empty placeholder package). Three [DECISION] documents (P1.3, P1.7, P1.12) land in `docs/decisions/` **before** their dependent code tasks. Persistence targets the existing `pivots` and `levels` tables (migration 002 — no schema changes).

### Planned minimal touchpoints to existing P0 modules (additive wiring only — not refactoring)

| P0 module | Planned P1 change | Why it is legitimate |
|---|---|---|
| `core/state.py` | `SymbolState` gains structure/trendline/momentum fields; `diff()` covers them | StateStore is *defined* as the single source of truth engines write to (§1); P0.20 built only what P0 produced |
| `api/app.py` | `_diff_json` generalizes from candle-only values to engine-state values | §9 mandates WS pushes `{candle, state_diff, …}`; P0.21 serialized the only state that existed |
| `db.py` | **No new helpers expected** — `insert_pivot`, `select_pivots`, `insert_level`, `update_level_lifecycle`, `select_levels` already shipped at P0.7 and are P0.8-tested; P1 verifies sufficiency and adds a select variant only if a concrete need appears | The append-only guard already covers pivots (no update path exists) and levels (`update_level_lifecycle` touches exactly `touches`/`status`/`status_ts`) |
| `tests/test_determinism.py` | Grows to hash pivots + levels (P1.21) | Pre-planned growth, recorded at P0.26 ("grows to structure objects at P1.21") |
| `main.py` | Compose utilities + engines (subscription order = execution order) | `main()` is the only composition point (locked convention) |
| `frontend/app.js` + new overlay file(s) | Consume state_diff; draw primitives | P1.19 deliverable per roadmap |

Each touchpoint is extended, never restructured; every P0 test keeps passing untouched except where a new field legitimately appears in a contract test.

## 3. Out of Scope (explicitly)

- **RVOL and everything volume** — P2. P1.16's BREAK rule needs a volume ratio: ships behind a flagged placeholder interface (roadmap P1.16), swapped at P2.2 with a determinism re-run.
- Liquidity, SMC, VWAP, sessions, delta (P2); gates/scoring/strategies/planner/reasoning (P3); recommendation lifecycle/journal/analytics (P4); validation campaign (P5); DeltaFeed (P6).
- Any provider change, any DB migration, any deployment/VPS work, anything on the v1.2 permanent exclusion list (execution etc.).
- No new event types, no wrapper events, no DI, no plugins, no new processes.

## 4. Dependencies (external to P1)

- **P0.28 passed** (all P0 modules live and frozen; tag `v1.0.0-foundation`).
- Data: 90-day 1m bootstrap for BTCUSDT+ETHUSDT in the dev DB; BTCUSDT 5m populated by the gate's replay; **ETHUSDT 5m is sparse** — the P1.22 audit gate runs on replay (which folds 5m internally), so this does not block the gate; a one-off ETH replay can backfill it anytime.
- Test DB `marketscalper_test` (schema-only) for the suite; per-test rollback discipline (P0.8).
- Frontend LWC v5 pinned @5.0.0 (primitives/plugins API available).

## 5. Deliverables

1. `engines/momentum.py` — shared utilities module (explicitly **not** an engine class — §4.7).
2. `engines/structure.py` — Structure Engine.
3. `engines/trendline.py` — Trendline Engine.
4. `docs/decisions/P1.3-regime-constants.md` (A7), `docs/decisions/P1.7-range-and-external-structure.md` (A6+A8), `docs/decisions/P1.12-trendline-row-and-tolerance.md` (A4+A5).
5. `state.py` extended `SymbolState`; composition wiring in `main.py` (DB helpers already exist from P0.7 — see §2 table).
6. Frontend: trendline/channel primitives, pivot markers, BOS/CHOCH labels; replay audit controls (random-trendline jump + accept/reject tally display).
7. Determinism harness v1 (`tests/test_determinism.py` extended: canonical pivot+level stream hashed).
8. Unit/integration tests per module; repaint test; docs + per-task milestone commits (rule 8 — back to strict per-task commits in P1).

## 6. Module-by-module implementation order (with per-task rationale)

Order is the roadmap's, which follows Part D: **utilities → structure → trendlines → overlays/gate** (displacement, tolerance and min-size filters all consume ATR — ordering, not redesign).

### 6a. Momentum utilities

| Task | Why it exists | Later dependents | Must be complete first |
|---|---|---|---|
| **P1.1** ATR(14) 1m+5m, incremental; unit tests vs reference | Every threshold in §4.2/§4.3/§4.5 is ATR-scaled; nothing downstream can be built without it | P1.2, P1.4, P1.9, P1.13–P1.17; P2 (FVG min-size, EQ clustering); P3 (S1–S3 SL buffers) | P0.28 |
| **P1.2** Velocity (EMA-5 of close-to-close Δ), acceleration (Δvelocity), momentum-shift flag (velocity sign flip with \|accel\| > threshold), body-dominance (avg body/range, last 5) | §4.7 conviction/impulse metrics consumed by scoring and regime context | P3.9 (Momentum scorer); P1.4 context; threshold value pinned by P1.3 | P1.1 |
| **P1.3** [DECISION] A7: regime constants as config; log regime distribution in replay for calibration. Also pins the §4.7 momentum-shift \|acceleration\| threshold (one consolidated config-constant set — a deliberate, stated extension of A7's original scope) | §4.2's compression formula (ATR1m < 0.6×ATR5m) is suspected mis-calibrated; constants must be config, calibrated from the 90-day replay distribution — not hard-coded guesses | P1.2/P1.4 (use the constants); P5.3 (calibration harness sweeps them) | P1.1 |
| **P1.4** Regime classifier: coil / normal / expansion (§4.2 formulas, A7 constants; expansion median window proposal: 240 bars) | Regime feeds scoring context and the §4.2 compression/expansion states shown on structure | P3 scorers; display | P1.3 |

### 6b. Structure Engine

| Task | Why it exists | Later dependents | Must be complete first |
|---|---|---|---|
| **P1.5** Pivot detection: k-bar confirmation (k=3 on 1m, k=2 on 5m); store `ts` + `confirmed_ts`; persist to `pivots` | The atomic unit of all structure; k-bar rule is the no-repaint foundation (swing at bar i confirms at i+k — lag accepted, repaint rejected) | P1.6–P1.10, P1.13 (trendline candidates), P2.9 (EQ clustering), A8 external structure | P0.28 (+ persistence-policy ruling, §12 below) |
| **P1.6** HH/HL/LH/LL label state machine (label vs last same-kind pivot price) | Turns raw pivots into the market-structure vocabulary every later rule uses | P1.8, P2.12, P3.7 | P1.5 |
| **P1.7** [DECISION] A6 (exact RANGE formula) + A8 (external structure = 5m-confirmed pivots, k=2; 1m pivots internal) | RANGE is vague in §4.2 ("alternating / overlap > 60%…") and "external structure" is referenced by premium/discount, anchored VWAP and S1 TP2 without a definition — one definition, three consumers | P1.8; P2.4, P2.12; P3 S1 TP2 | P1.6 |
| **P1.8** Trend-state machine: BULLISH (HH+HL sequence) / BEARISH (LH+LL) / RANGE (A6 formula) | The Q1 answer; context input for S2 and premium/discount | P1.9–P1.10; P2.12; P3.12–P3.14 | P1.7 |
| **P1.9** BOS detection (1m close beyond last confirmed swing in trend direction) + displacement classification (breaking body > 1.2×ATR(14) → displacement BOS, else weak) | Continuation signal + the impulse/drift filter; displacement additionally triggers P2 OB detection | P1.10; P2.14 (OB on displacement BOS); P3.7 | P1.8, P1.1 |
| **P1.10** CHOCH detection (first close beyond last confirmed swing *against* trend) + confirmed-flip logic (CHOCH alone ≠ reversal; CHOCH + opposite BOS = confirmed flip) | Reversal warning; S1's confirmation trigger; sweep-and-shift tagging in P2 | P2.11; P3.13 (S1 CONFIRM) | P1.9 |
| **P1.11** Unit tests: all structure transitions; **repaint test** (nothing visible before `confirmed_ts`) | §0 rule 1 enforced mechanically; the repaint test is the phase's conscience | P1.21/P1.22 (gate); every later phase inherits the pattern | P1.10 |

### 6c. Trendline Engine

| Task | Why it exists | Later dependents | Must be complete first |
|---|---|---|---|
| **P1.12** [DECISION] A4 + A5 + trendline detail set. A4 (p1 = price at t1, p2 = price at t2, slope redundant) is **already frozen verbatim in migration 002's comment** — the doc restates it for completeness. Genuinely decided here: A5 log-space tolerance (`tol_log = 0.15×ATR/price` at the evaluation candle; one unit-tested helper); **scoring constants** (`age_penalty` definition, `span_bars` definition, whether the two anchor pivots count toward ≥3 touches); **archive semantics** (recommended: `status='archived'` added to the app-layer status vocabulary — no CHECK constraint exists, validation is app-layer by design — archiving when 300 bars have elapsed since the line's last touch); **timeframe pinning** (candidates from 1m confirmed pivots, tolerance uses ATR(14) 1m, `levels.tf='1m'` — 5m pivots serve A8 external structure, not trendlines) | §4.3 leaves these five details open; deciding them ad-hoc during coding is exactly what this spec exists to prevent | P1.13–P1.17 all consume every item | P1.5 |
| **P1.13** Step 1 — candidate generation: last N=12 confirmed same-kind pivots; line through every (a,b) pair, b newer; log-price space; direction-validity filter (support from lows must not cut closes between a,b) | §4.3's candidate universe; bounded O(N²)=66 pairs per side, recomputed **only when a new confirmed pivot arrives** (not per candle) — determinism + cost pin | P1.14 | P1.12 |
| **P1.14** Step 2 — touch validation: tolerance 0.15×ATR (via A5 helper); touches = candles whose low/high comes within tolerance **without close crossing**; keep lines with ≥3 touches | Separates real market geometry from coincidence | P1.15 | P1.13 |
| **P1.15** Step 3 — scoring (`touches×2 + span_bars/20 − age_penalty`, constants per P1.12) + cluster dedup (slope Δ<10%, intercept Δ<0.3×ATR → keep best per cluster); cap **3 support + 3 resistance**; persist to `levels` | Prevents line spam; the cap is the sanity guarantee for the audit gate and the UI | P1.16–P1.17, P1.19, P2.18 (confluence), P3.15 (S3) | P1.14, P1.12 |
| **P1.16** Step 4 — break classification: TOUCH (within tolerance, no close beyond) / BREAK (1m close beyond + body > 0.8×ATR **+ RVOL ≥ 1.5 via flagged placeholder interface**) / FAKE BREAK (close beyond, then close back inside within 3 candles) | The scalper-critical event set; FAKE BREAK feeds S3 | P1.17; P3.15; placeholder swapped at P2.2 with determinism re-run | P1.15 |
| **P1.17** Step 5 — lifecycle: broken → `status='broken'` + role-flip candidate; archive per the P1.12 ruling (recommended: 300 bars since last touch → `status='archived'`); channel detection (parallel pair, slope Δ<8%, both ≥3 touches → channel object, mid-line reference) | Keeps the working set current; channels feed S3 targets | P1.18, P1.21; P3.15 | P1.16 |
| **P1.18** Unit tests: candidate/touch/dedup/break/lifecycle on synthetic pivot fixtures | Every §4.3 step verified in isolation before integration | P1.21, P1.22 | P1.17 |

### 6d. Overlays, audit, determinism, gate

| Task | Why it exists | Later dependents | Must be complete first |
|---|---|---|---|
| **P1.19** LWC primitives: trendlines/channels + BOS/CHOCH labels + pivot markers | §9 terminal layout; the audit gate is *visual* — no overlays, no audit | P1.20, P1.22; P2.20 reuses the primitive layer | P1.11 (structure objects), P1.15, P0.23 |
| **P1.20** Replay audit tool: jump-to-random-trendline + accept/reject tally | Makes the ≥80% agreement gate executable and honest (random selection beats cherry-picking); tally is session-local UI state, recorded by the owner in the gate doc | P1.22; P2.22 extends it | P1.19, P0.25 |
| **P1.21** Determinism harness v1: pivots + levels byte-identical; wired into CI | §0 rule 2 grows with the phase (Part D note 4: never retrofitted) | P1.22; P2.23 extends | P1.17, P0.26 |
| **P1.22** **P1 GATE**: 50 random trendlines audited on 30-day replay, ≥80% "I would draw this line too"; determinism pass; docs updated | Sign-off that the first analytical layer matches a trader's eye before more layers stack on it | All P2 | All P1 |

## 7. Public interfaces (signatures pinned now; bodies at implementation)

```python
# engines/momentum.py — plain utilities, no engine class (§4.7)
class IncrementalATR:                     # one instance per (symbol, tf)
    def __init__(self, period: int = 14) -> None: ...
    def update(self, candle: Candle) -> float | None    # None until warm (period+1 candles)
    @property
    def value(self) -> float | None

class MomentumState:                      # velocity/accel/shift/body-dominance, per (symbol, tf)
    def update(self, candle: Candle) -> None
    velocity: float | None; acceleration: float | None
    momentum_shift: bool; body_dominance: float | None

def classify_regime(atr_1m: float, atr_5m: float, atr_median: float,
                    cfg: RegimeConfig) -> Regime        # 'coil'|'normal'|'expansion' (P1.3 constants)
```

```python
# engines/structure.py
class StructureEngine:                    # subscribes to Candle (1m + 5m) at composition
    def __init__(self, bus, store, pool, atr_by_tf) -> None: ...
    async def on_candle(self, candle: Candle) -> None
# Emits nothing new on the bus. Writes SymbolState.structure and persists
# confirmed pivots. All outputs carry ts + confirmed_ts.
```

```python
# engines/trendline.py
class TrendlineEngine:
    def __init__(self, bus, store, pool, atr_1m) -> None: ...
    async def on_candle(self, candle: Candle) -> None   # touch/break eval per candle
    def on_confirmed_pivot(self, pivot) -> None         # candidate regeneration only here
# on_confirmed_pivot is a DIRECT callback wired in main() (the established
# P0 on_reference_candle pattern) — no new event types on the bus.
```

- **Execution order** (§1 "engines run sequentially"): guaranteed by the P0.9 EventBus (sequential delivery in subscription order) — composition subscribes utilities-update first, then StructureEngine, then TrendlineEngine, then StateStore broadcast. No scheduler, no new mechanism.
- **ATR reference definition pinned:** Wilder's smoothing (RMA), TR = max(h−l, |h−prev_c|, |l−prev_c|); unit-tested against hand-computed reference vectors.
- **StateStore additions** (`SymbolState`): `atr_1m`, `atr_5m`, `regime`, `momentum`, `pivots_recent` (confirmed only), `trend_state`, `last_bos`, `last_choch`, `trendlines_active` (≤6), `channels`. Diff mechanism unchanged: changed-fields-since-last-call, collapsing.

## 8. Data flow

```
Candle close (live builder or ReplayFeed — identical events)
  → bus (sequential, deterministic order):
      1. momentum utilities update (ATR/velocity/regime state per symbol+tf)
      2. StructureEngine: confirm pivots at i+k → label → trend state → BOS/CHOCH
           → persist confirmed pivots (policy per §12) → SymbolState.structure
      3. TrendlineEngine: on new confirmed pivot → regenerate candidates;
           every 1m close → touch/break/fake-break eval, lifecycle
           → persist level transitions (policy per §12) → SymbolState.trendlines
      4. StateStore → diff → WS {candle, state_diff} → frontend primitives redraw diffs only
```

Replay uses the exact same path (pipeline cannot tell replay from live — P0.24 invariant); the audit tool drives ReplayFeed and reads the same WS stream.

## 9. Database impact

- **No migrations.** Tables `pivots` and `levels` exist (migration 002, exact §3 schema, A4 comments included).
- `pivots`: append-only, INSERT of confirmed pivots only (id bigserial; symbol, tf, ts, confirmed_ts, kind, price, label).
- `levels`: INSERT on new trendline/channel acceptance; UPDATEs restricted to lifecycle columns only — `touches`, `status`, `status_ts` (active → broken/archived; matches migration-002's app-layer discipline note). Core geometry columns (kind, p1, p2, t1, t2, slope, created_ts) are immutable after insert.
- `db.py` already provides the helpers (P0.7): `insert_pivot`, `select_pivots`, `insert_level`, `update_level_lifecycle` (touches/status/status_ts only — the exact permitted set), `select_levels`; the append-only guard already enforces the policy above. P1 adds nothing to `db.py` unless a concrete select variant proves necessary.
- `levels.status` vocabulary: migration 002 documents `'active','swept','mitigated','broken'`; the archive state requires one added app-layer value (`'archived'`), pinned in the P1.12 decision doc — legitimate because validation is app-layer by design (no CHECK constraints, per the migration's own note).
- Volume: ~a few pivots/hour/symbol on 1m; ≤6 active trendlines/symbol — negligible.

## 10. Testing strategy

1. **Unit** (per module, synthetic fixtures): ATR vs reference vectors incl. warm-up; velocity/accel/shift edge cases; regime boundaries at exact thresholds; pivot confirmation timing (confirms exactly at bar i+k, never earlier); every label/trend transition; BOS/CHOCH matrices incl. displacement boundary (body exactly 1.2×ATR); trendline steps 1–5 each isolated (P1.18 list).
2. **Repaint test** (P1.11, the phase's centerpiece): feed a candle sequence incrementally; after every single candle, assert no structure object exists whose `confirmed_ts` is in the future — and replay any prefix of the sequence, asserting the prefix output is a strict prefix of the full output (no retroactive change).
3. **Determinism v1** (P1.21): double replay over the same 30-day range → canonical serialization of the pivot and level object streams (sorted field order, fixed float formatting) → sha256 must match byte-for-byte; plus a sensitivity self-test (a deliberately perturbed input must change the hash).
4. **Integration**: engines wired on a real bus with ReplayFeed over the test DB (tx-rollback isolation, P0.8 pattern); WS state_diff carries structure fields end-to-end (extends P0.21 API tests additively).
5. **Boundary intact**: the P0.19 AST import-boundary gate already fails any engine importing a concrete provider — engines import only `providers.base` types, `core.*`, `db`.
6. Everything runs inside `scripts/ci.sh` unchanged (single pytest step).

## 11. Acceptance criteria (P1.22 gate — restated verbatim + operationalized)

- 30-day replay with overlays; audit tool selects **50 random** validated trendlines; owner judges each ("I would draw this line too"); **≥80% accept** required. Tally + date recorded in the gate record doc.
- Determinism harness v1 green (double replay byte-identical pivots+levels).
- Repaint test green; full suite green via `scripts/ci.sh`.
- Docs updated (CLAUDE.md status, decision docs P1.3/P1.7/P1.12, gate record) — milestone commit per completed task throughout the phase (rule 8).

## 12. Risks & open details (each pinned or routed — none left to improvise during coding)

| # | Risk / open detail | Handling |
|---|---|---|
| R1 | **Engine-output persistence during replay**: replay re-runs engines over historical ranges; naive always-on persistence would duplicate pivot/level rows on every replay session (serial PKs — no natural dedup, unlike candles). The roadmap is silent. | **Proposed ruling (needs owner sign-off inside the P1.5 task plan, before code):** persistence subscribers are wired only for the live feed in `main()`; replay sessions keep engine output in StateStore/WS (which is all P1.20's audit needs). Determinism/testing writes stay tx-isolated (P0.8 pattern). §10's "byte-identical signals table" is satisfied at P3 via isolated determinism runs. |
| R2 | A7 compression constant likely mis-calibrated (1m ATR is *typically* already < 0.6×5m ATR) | By design: P1.3 makes constants config + logs the replay distribution before P1.4 locks the classifier; P5.3 re-calibrates |
| R3 | 1m noise → pivot spam | k=3 confirmation + ATR-scaled tolerances everywhere (§12 risk register mitigation, already designed) |
| R4 | Trendline recompute cost | Bounded: candidates only on new confirmed pivots (≤66 pairs/side); touch eval is O(active lines)=≤6 per candle |
| R5 | RVOL placeholder dishonesty (BREAK classified without real volume) | Placeholder flagged in code + docs per roadmap; P2.2 swap re-runs P1 determinism |
| R6 | Audit-gate subjectivity | Random selection (no cherry-picking) + fixed judgment phrasing + tally recorded; 30-day window per gate text |
| R7 | Determinism hazards (float formatting, iteration order) | Canonical serialization rules pinned in §10.3; engines use only ordered structures; no wall-clock/randomness in engine code (replay-first discipline) |
| R8 | WS payload growth from state_diff | Diffs stay collapsing/changed-fields-only (P0.20 semantics); overlays redraw diffs only (§9) |
| R9 | ETH 5m sparse in dev DB | Gate audits on replay (self-folding 5m); optional one-off ETH replay backfill anytime |

## 13. Rollback considerations

- **Per-task milestone commits** (rule 8) make every task individually revertable; P1 returns to strict per-task commits.
- Engines are **additive**: rollback of any engine = remove its composition wiring in `main()` (single point) + revert its module — P0 pipeline continues untouched, as proven by the P0.28 gate.
- DB: pivots/levels rows from a reverted task can be deleted by plain SQL (dev DB only; append-only discipline governs the application, not owner-run maintenance); no schema to roll back.
- The three [DECISION] docs are additive; reverting one reverts its dependent code task with it.
- Frontend overlays are an isolated layer over the P0.23 chart; reverting them restores the bare live chart.

## 14. What implementation approval looks like (process)

Per the locked process rules: each task begins with a short task plan (what/files/tests), owner approval, implementation, tests, docs, milestone commit — one task at a time, in the §6 order. The first plan presented will be **P1.1**, plus the R1 persistence ruling folded into the **P1.5** plan when its turn comes.

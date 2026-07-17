# MarketScalper — Implementation Roadmap v2.0

**Derived strictly from:** `docs/Architecture.md` — Blueprint **v1.2 (FROZEN)**
**Date:** 2026-07-14 | **Status:** Scope Simplification applied (owner-approved, 2026-07-14)
**Rule:** No architecture changes. Every item implements what v1.2 already specifies. Where the blueprint is silent on a detail, the item is flagged **[DECISION]** — a parameter/detail choice inside the frozen design, never a redesign.

**v2.0 Scope Simplification (supersedes Roadmap v1.1):** MarketScalper is a **deterministic market analysis and decision-support platform** — NOT an automated trading platform, NOT an order-execution platform, NOT a broker terminal. The user always executes trades manually on their exchange. The platform's responsibility ends at a high-quality trade recommendation; outcomes are logged manually. Consequences for this roadmap:

- **Removed entirely:** ExecutionProvider interface, PaperBroker, DeltaBroker, order placement/modification, position management & sync, kill switch, emergency flatten, broker auth/2FA, REST order APIs, live-trading gates & micro-trade validation, automated trade management (trailing/partials/time-exit automation).
- **Kept unchanged:** FeedProvider abstraction (Binance, Replay, Delta market-data — capability-flagged: `supports_live_data`, `supports_historical_data`, `supports_orderbook`, `supports_trades`), Candle Builder, all analysis engines, Strategy/Qualification/Planning, RR math, entries/SL/targets, reasoning, invalidation logic, Replay (learning/validation/testing only — no replay broker), journal (now recommendation-based), analytics, UI, PostgreSQL, FastAPI, WebSocket, Lightweight Charts.
- **Manual workflow (the whole pipeline):** FeedProvider → normalized events → Candle Builder → Analysis Engines → Strategy → Qualification → Planning → **Trade Recommendation** → manual execution by user → manual outcome logging → Analytics. Nothing beyond this.
- Engines never know the provider; no engine consumes raw Binance/Delta JSON — normalized internal objects only, enforced by a CI import-boundary check.
- Simplicity constraints unchanged: single VPS, single process, single codebase, plain-Python interfaces wired in `main()` — no DI frameworks, no plugins, no microservices.

Tasks are renumbered cleanly per phase (v2.0 numbering). **P0.1 was completed 2026-07-14 under v1.x and keeps its ID.**

---

## Part A — Consistency Verdict (Architecture v1.2)

Blueprint v1.2 remains internally consistent after the scope simplification: the runtime chain (Feed → Analysis → Strategy → Recommendation → manual execution → Journal) is respected in every section; score weights still sum to 1.00; replay-first is intact (ReplayFeed shares the FeedProvider interface, and the determinism gate now covers recommendations + hypothetical outcomes); the data model matches the new flow (`recommendations` + manual `journal`, both append-only in spirit — recommendation core immutable, only status/eval columns transition); validation thresholds are consistent (§0 rule 4 "validate before trust" ↔ P4 gate ≥60 ↔ P5 gate 200+ → TRUSTED). No execution remnants remain in v1.2.

---

## Part B — Ambiguities & Missing Implementation Details

Carried forward from the v1.x analysis, updated for v1.2. Each is a parameter-level decision inside the frozen design.

| # | Gap | Where it bites | Proposed resolution (within v1.2) |
|---|-----|----------------|-----------------------------------|
| A1 | Dual kline sources, no declared authority (tick-built candles vs `kline_1m`). | P0 | Tick-built candles = runtime truth; closed `kline_1m` = reconciliation reference + backfill source. Log every mismatch. |
| A2 | 5m roll boundary off-by-one in §4.1. | P0 | Emit CANDLE_CLOSE_5M when closed 1m bucket satisfies `(bucket+1) % 5 == 0`, epoch-aligned. Pin in unit test. |
| A3 | Initial history bootstrap depth unspecified (RVOL needs 20 days; replay wants 90). | P0 | Bootstrap ≥90 days 1m klines per symbol; engines refuse to run with <20 days. |
| A4 | `levels` row semantics for TRENDLINE (p1=p2 comment fits horizontals only). | P1 | kind='TRENDLINE': p1 = price at t1, p2 = price at t2, slope redundant. |
| A5 | Log-price line fitting vs price-space ATR tolerance. | P1 | `tol_log = 0.15×ATR/price` at evaluation candle; one unit-tested helper. |
| A6 | RANGE state definition vague. | P1 | Exact formula fixed in [DECISION] before coding (alternating labels OR ≥60% bodies inside last swing band). |
| A7 | Compression threshold likely mis-calibrated (1m ATR is typically already <0.6× 5m ATR); expansion median window unspecified. | P1 | Keep formulas; constants become config; calibrate from 90-day replay distribution. Median window proposal: 240 bars. |
| A8 | "External structure" never formally defined (premium/discount, anchored VWAP, S1 TP2 depend on it). | P1–P2 | External swings = 5m-confirmed pivots (k=2); 1m pivots internal. One definition, three consumers. |
| A9 | Session map hole: 21:00–00:00 UTC unassigned; G3 + session levels need it. | P2–P3 | "LATE" bucket 21–00 UTC; G3 default = no strategy allowed in LATE unless config says otherwise. |
| A10 | G2 spread gate has no data source (feed spec = aggTrade + kline). | P0/P3 | Add `bookTicker` to the existing Binance combined stream (same provider, capability `supports_orderbook`). |
| A11 | G4 news calendar source unspecified. | P3 | Manual `events.yaml`, owner-maintained weekly. |
| A12 | G1 clock-sync measurement undefined. | P0/P3 | Offset vs Binance `/api/v3/time` every 5 min; G1 fails if |offset| > 2s. |
| A13 | Trendline/SMC score routing (§6 weights only 4 components). | P3 | Trendline events → Structure rubric; OB/FVG/confluence → Liquidity rubric. Full rubric doc before coding. |
| A14 | "9 of 11 rules aligned" display vs weighted score. | P3 | Display = count of boolean rubric items scoring >0 over total evaluated. |
| A15 | S2 "impulse" needs a formula. | P3 | Impulse = BOS leg: last confirmed opposite swing → BOS-breaking close, on 1m. |
| A16 | Hypothetical outcome evaluation policy: same-candle SL+TP ambiguity, evaluation horizon, gap-through handling. | P4 | Worst-case-first: SL wins same-candle ambiguity; gap-through fills at candle open; horizon = strategy expiry + management guidance window. Deterministic in replay and forward-run. |
| A17 | Chart snapshot when browser is closed (journal auto-context needs LWC screenshot). | P4 | Persist state snapshot; render PNG lazily on first journal view; `snapshot_pending` fallback. |
| A18 | Binance-analysis vs Delta price basis — now display-only. | P6 (opt.) | DeltaFeed divergence display is informational (helps manual execution on Delta). No reconciliation logic needed — nothing executes. |
| A19 | `engine_version` derivation undefined. | P0 | Short git hash + per-engine semantic version constant, stamped per signal. |
| A20 | Ops details: partition automation, systemd, UI auth, test framework. | P0 | Cron/pg_partman DDL job; one systemd unit; single-user token auth; pytest + pytest-asyncio. |

---

## Part C — Implementation Roadmap

Mirrors Architecture v1.2 §11 (P0–P5 + optional P6). Every task ≤ 1 hour. **[DECISION]** tasks are ≤30-min written decisions in `docs/decisions/`, always before their dependent code. Every engine phase ends with unit tests + determinism run + docs update.

---

### Phase 0 — Spine (§11 P0) — 28 tasks

**0a. Repo & scaffolding**

| ID | Task (≤1h) | Depends on |
|----|------------|-----------|
| P0.1 | ✅ **DONE 2026-07-14** — root README.md + CLAUDE.md populated; `.md.txt` double extensions fixed | — |
| P0.2 | Python 3.12 project scaffold: `pyproject.toml`, package layout under `backend/` (incl. `providers/` boundary), config loader (YAML + env), logging setup | — |
| P0.3 | pytest + pytest-asyncio harness; CI script skeleton in `scripts/` (will host determinism + import-boundary gates) | P0.2 |
| P0.4 | [DECISION] A19 + A20: engine_version scheme, partition automation, auth approach, systemd plan | — |

**0b. Database**

| ID | Task | Depends on |
|----|------|-----------|
| P0.5 | Migration 001: `candles` + monthly range partitions + partition-automation job | P0.4 |
| P0.6 | Migration 002: `pivots`, `levels`, `signals`, `recommendations`, `journal` (exact §3 v1.2 schema, incl. A4 TRENDLINE comments) | P0.5 |
| P0.7 | Async DB access layer (asyncpg): insert/select helpers; append-only guard (no UPDATE on signals; recommendations: only status/eval columns may transition) | P0.6 |
| P0.8 | Unit tests: schema round-trip, partition routing, append-only enforcement | P0.7 |

**0c. Feed layer (provider abstraction)**

| ID | Task | Depends on |
|----|------|-----------|
| P0.9 | Define `FeedProvider` interface: live trades/ticks stream, historical-candle fetch, order-book/best-bid-ask stream, WS connect/reconnect contract, REST-fallback hook + **capability flags** (`supports_live_data`, `supports_historical_data`, `supports_orderbook`, `supports_trades`) + normalized Tick/Trade/BookTicker dataclasses + asyncio EventBus. Engines subscribe to normalized events only — no raw provider JSON past this layer | P0.2 |
| P0.10 | `BinanceFeed` implementing FeedProvider: combined WS (`aggTrade` + `kline_1m` + `bookTicker` per A10), auto-reconnect w/ backoff, heartbeat monitor | P0.9 |
| P0.11 | [DECISION] A1 (candle authority) + A12 (clock sync); implement clock-offset sampler | P0.10 |
| P0.12 | Candle Builder: aggTrade → 1m buckets (o/h/l/c/v/qv/n_trades/taker_buy_v); emit CANDLE_CLOSE_1M | P0.9 |
| P0.13 | 5m aggregation with A2 boundary rule; emit CANDLE_CLOSE_5M; boundary unit tests | P0.12 |
| P0.14 | Kline reconciliation: tick-built candle vs closed `kline_1m`; mismatch logging (A1 policy) | P0.11, P0.13 |
| P0.15 | Gap-safe backfill: on reconnect, fetch missing 1m klines via `FeedProvider.fetch_historical_candles()` *before* resuming engine flow | P0.14 |
| P0.16 | Bootstrap job (A3): ≥90 days 1m klines for BTCUSDT+ETHUSDT via the same historical interface; <20-days refuse-to-run guard | P0.7, P0.10 |
| P0.17 | Candle persistence writer: EventBus subscriber → batched inserts | P0.7, P0.13 |
| P0.18 | Unit tests: candle builder (tick sequences, gaps, taker_buy_v semantics), backfill ordering | P0.15 |
| P0.19 | Provider-boundary enforcement: shared FeedProvider conformance suite (identical tests pass for BinanceFeed and ReplayFeed) + CI import-boundary check — no engine/strategy/planner/journal module imports a concrete provider | P0.18, P0.24 |

**0d. StateStore + API + UI**

| ID | Task | Depends on |
|----|------|-----------|
| P0.20 | StateStore: per-symbol dataclass container (single source of truth); diff-generation helper | P0.9 |
| P0.21 | FastAPI app: WS endpoint pushing `{candle, state_diff}` + REST (history, health); single-user token auth | P0.20 |
| P0.22 | Frontend shell: vanilla JS + LWC v5, TradeOS dark tokens, WS client with reconnect | P0.21 |
| P0.23 | Live 1m chart (diff updates only) + symbol switcher (BTC/ETH) + 5m context strip | P0.22 |

**0e. Replay skeleton + gate**

| ID | Task | Depends on |
|----|------|-----------|
| P0.24 | `ReplayFeed` implementing the same FeedProvider interface (historical candles from Postgres; `supports_orderbook`=false); identical normalized CANDLE_CLOSE events at ×{1,10,60,max} — pipeline cannot tell replay from live | P0.9, P0.16 |
| P0.25 | Replay CLI/UI hook: start/stop/speed/date-range | P0.24, P0.23 |
| P0.26 | Determinism harness v0: double replay → byte-identical candle stream hash | P0.24, P0.3 |
| P0.27 | systemd unit + deploy script (single Linux server, provider-agnostic); prod config | P0.21 |
| P0.28 | **P0 GATE:** live BTCUSDT 1m chart in browser; 90-day replay playback; zero candle mismatch vs official klines; FeedProvider conformance suite green for both providers; docs updated | all P0 |

---

### Phase 1 — Structure + Trendlines (§11 P1) — 22 tasks

*(No provider-facing tasks here or in P2–P3: engines consume normalized events/StateStore only.)*

**1a. Shared momentum utilities (§4.7)**

| ID | Task | Depends on |
|----|------|-----------|
| P1.1 | ATR(14) 1m + 5m, incremental; unit tests vs reference | P0.28 |
| P1.2 | Velocity (EMA-5), acceleration, momentum-shift flag, body-dominance(5) | P1.1 |
| P1.3 | [DECISION] A7: regime constants as config; log regime distribution in replay for calibration | P1.1 |
| P1.4 | Regime classifier (coil/normal/expansion, §4.2 formulas) | P1.3 |

**1b. Structure Engine (§4.2)**

| ID | Task | Depends on |
|----|------|-----------|
| P1.5 | Pivot detection: k-bar confirmation (k=3 1m, k=2 5m); ts + confirmed_ts; persist to `pivots` | P0.28 |
| P1.6 | HH/HL/LH/LL label state machine | P1.5 |
| P1.7 | [DECISION] A6 (RANGE formula) + A8 (external structure) | P1.6 |
| P1.8 | Trend-state machine: BULLISH/BEARISH/RANGE | P1.7 |
| P1.9 | BOS detection + displacement classification (body > 1.2×ATR) | P1.8, P1.1 |
| P1.10 | CHOCH detection + confirmed-flip logic | P1.9 |
| P1.11 | Unit tests: all structure transitions; repaint test (nothing visible before confirmed_ts) | P1.10 |

**1c. Trendline Engine (§4.3)**

| ID | Task | Depends on |
|----|------|-----------|
| P1.12 | [DECISION] A4 (TRENDLINE row semantics) + A5 (log-space tolerance helper) | P1.5 |
| P1.13 | Step 1: candidate generation (12 same-kind pivots, log-price, validity filters) | P1.12 |
| P1.14 | Step 2: touch validation (0.15×ATR tolerance, close-cross exclusion, ≥3 touches) | P1.13 |
| P1.15 | Step 3: scoring + cluster dedup; cap 3+3; persist to `levels` | P1.14 |
| P1.16 | Step 4: break classification — TOUCH / BREAK / FAKE BREAK. (Volume ratio placeholder behind RVOL interface until P2, flagged in code) | P1.15 |
| P1.17 | Step 5: lifecycle (role-flip, 300-bar archive) + channel detection | P1.16 |
| P1.18 | Unit tests: candidate/touch/dedup/break/lifecycle on synthetic pivots | P1.17 |

**1d. Overlays + gate**

| ID | Task | Depends on |
|----|------|-----------|
| P1.19 | LWC primitives: trendlines/channels + BOS/CHOCH labels + pivot markers | P1.15, P0.23 |
| P1.20 | Replay audit tool: jump-to-random-trendline + accept/reject tally | P1.19, P0.25 |
| P1.21 | Determinism harness v1: pivots + levels byte-identical; wire into CI | P1.17, P0.26 |
| P1.22 | **P1 GATE:** 50 random trendlines audited ≥80% agreement on 30-day replay; determinism pass; docs updated | all P1 |

---

### Phase 2 — Liquidity + SMC + Volume (§11 P2) — 24 tasks

**2a. Volume Engine (§4.6) — first; downstream formulas consume RVOL**

| ID | Task | Depends on |
|----|------|-----------|
| P2.1 | RVOL time-of-day normalized (minute-of-day median, 20 days); incremental cache | P1.22 |
| P2.2 | Swap P1.16 placeholder → real RVOL; re-run P1 determinism | P2.1 |
| P2.3 | Session VWAP (00:00 UTC reset) + ±1σ/±2σ bands | P1.22 |
| P2.4 | Anchored VWAP at last confirmed major swing (A8) | P2.3, P1.7 |
| P2.5 | Delta metrics: per-candle delta, session cum_delta; absorption detector | P2.1 |
| P2.6 | Volume spike (rvol≥2.0) + exhaustion | P2.5 |
| P2.7 | Unit tests: RVOL bucketing, VWAP math, delta/absorption | P2.6 |

**2b. Liquidity Engine (§4.4)**

| ID | Task | Depends on |
|----|------|-----------|
| P2.8 | [DECISION] A9: session map incl. LATE bucket | P1.22 |
| P2.9 | EQH/EQL clustering (0.1×ATR), pool strength; persist | P1.22 |
| P2.10 | Key levels: PDH/PDL, PWH/PWL, session H/L, day H/L; daily refresh | P2.8 |
| P2.11 | Sweep detection + "sweep + shift" tagging (CHOCH within 3 candles) | P2.9, P2.10, P1.10 |
| P2.12 | Premium/discount on external-structure range | P1.7 |
| P2.13 | Unit tests: clustering, sweep true/false positives, midnight refresh | P2.11 |

**2c. Smart Money Engine (§4.5)**

| ID | Task | Depends on |
|----|------|-----------|
| P2.14 | Order Block detection on displacement BOS | P1.9 |
| P2.15 | OB lifecycle: active → mitigated → broken | P2.14 |
| P2.16 | FVG detection (min 0.3×ATR) + fill tracking | P1.1 |
| P2.17 | Breaker logic | P2.15 |
| P2.18 | Confluence stacking: zone_quality overlap count within 0.3×ATR | P2.15, P2.16, P1.15, P2.9, P2.3, P2.10 |
| P2.19 | Unit tests: OB/FVG/breaker lifecycle, confluence counting | P2.18 |

**2d. Overlays + gate**

| ID | Task | Depends on |
|----|------|-----------|
| P2.20 | LWC overlays: OB/FVG boxes, EQH/EQL + session lines, sweep markers | P2.18, P1.19 |
| P2.21 | VWAP + bands overlay + premium/discount shading | P2.3, P2.12 |
| P2.22 | Replay audit extension: random sweep/OB jump + tally | P2.20, P1.20 |
| P2.23 | Determinism harness v2: all objects byte-identical | P2.19, P1.21 |
| P2.24 | **P2 GATE:** 50 sweeps + 50 OBs audited; false-positive rate acceptable; lifecycle transitions verified; docs updated | all P2 |

---

### Phase 3 — Scoring + Plan + Reasoning (§11 P3) — 21 tasks

**3a. Hard gates (§6 stage 1)**

| ID | Task | Depends on |
|----|------|-----------|
| P3.1 | [DECISION] A10/A11/A12 wiring: spread from bookTicker, `events.yaml` format, clock-sync threshold | P2.24 |
| P3.2 | G1 data-integrity + G2 spread gates | P3.1 |
| P3.3 | G3 session filter (A9 map) + G4 news blackout | P3.1 |
| P3.4 | G5 risk budget (daily logged loss from journal, active-recommendation count, revenge flag input) + G6 RR floor; gate-fail → NO SIGNAL | P3.2 |
| P3.5 | Unit tests: gate matrix; score never emitted on gate fail | P3.4 |

**3b. Scoring (§6 stage 2)**

| ID | Task | Depends on |
|----|------|-----------|
| P3.6 | [DECISION] A13 + A14: full 4-component rubric + rules-aligned display formula | P2.24 |
| P3.7 | Structure component scorer | P3.6 |
| P3.8 | Liquidity component scorer (§6 rubric) | P3.6 |
| P3.9 | Volume + Momentum scorers | P3.6 |
| P3.10 | Weighted aggregate (0.30/0.30/0.25/0.15); ≥75 tradeable, ≥85 A+; two-tier display payload | P3.7–P3.9 |
| P3.11 | Unit tests: rubric scoring, weights, thresholds | P3.10 |

**3c. Strategies (§5)**

| ID | Task | Depends on |
|----|------|-----------|
| P3.12 | Strategy template: CONTEXT→SETUP→CONFIRM→ENTRY/SL/TP/INVALID state machine | P3.10 |
| P3.13 | S1 Liquidity Sweep Reversal (full §5 spec) | P3.12, P2.11 |
| P3.14 | S2 Trend Pullback Continuation ([DECISION] A15 inline) | P3.12 |
| P3.15 | S3 Trendline Fake-Break Trap | P3.12, P1.17 |
| P3.16 | Unit tests: trigger + non-trigger scenarios per strategy | P3.13–P3.15 |

**3d. Planner, reasoning, UI, gate**

| ID | Task | Depends on |
|----|------|-----------|
| P3.17 | Trade planner (§7): 0.5% risk → **suggested qty** (display only), fee-adjusted net RR, reject net RR(TP1)<1.0 | P3.13 |
| P3.18 | Rule-trace reasoning + persistence: immutable `signals` row + `recommendations` row (entry/SL/TP1/TP2, suggested qty, status='active') | P3.17, P0.7 |
| P3.19 | Quality panel UI: score gauge, gates, components + trade-plan rail + reason trace | P3.18, P2.21 |
| P3.20 | Determinism harness v3: signals + recommendations byte-identical — §10 CI gate complete | P3.18, P2.23 |
| P3.21 | **P3 GATE:** 90-day replay → recommendations generated; top-20/bottom-20 score-ordering review; docs updated | all P3 |

---

### Phase 4 — Recommendation Lifecycle + Journal + Analytics (§11 P4) — 14 tasks

*(Replaces the former Paper-Execution phase. No brokers, no fills, no order management — lifecycle, hypothetical evaluation, manual logging, analytics.)*

| ID | Task | Depends on |
|----|------|-----------|
| P4.1 | [DECISION] A16 (outcome-evaluation policy: SL-first worst case, gap-through at open, horizon) + A17 (snapshot fallback) | P3.21 |
| P4.2 | Recommendation lifecycle engine: active → invalidated (strategy INVALID rules, opposite signal, G1 fail) → expired (time window); status events to UI + DB | P4.1 |
| P4.3 | Hypothetical outcome evaluator: candle-based SL/TP first-touch per A16, eval_r + eval_mae/eval_mfe from candles; identical in forward-run and replay | P4.2 |
| P4.4 | Unit tests: lifecycle transitions + evaluator edge cases (SL+TP same candle, gap-through, expiry-before-entry) | P4.3 |
| P4.5 | Persistence: recommendation status/eval transitions (core stays immutable) | P4.2, P0.7 |
| P4.6 | Auto-context capture at recommendation: chart PNG (A17 fallback), rule-trace, state snapshot → journal row seed | P4.5 |
| P4.7 | Manual quick-log UI: Taken/Skipped, Win/Loss/BE, actual entry/exit (optional), notes, tags — one-tap form on the recommendation card | P4.5 |
| P4.8 | Journal REST endpoints: manual fields writable by owner; recommendation core + auto-context immutable | P4.7 |
| P4.9 | Psychology guards: overtrade warn >6 taken/day, hard lock >8; revenge flag (<5 min after logged loss) → feeds G5 | P4.8, P3.4 |
| P4.10 | Recommendation panel UI: active card + entry/SL/TP rail, invalidation timer, suggested-management guidance (display-only text) | P4.2, P3.19 |
| P4.11 | Analytics queries: manual results (win rate, avg R, expectancy) + hypothetical evaluator stats + **system-vs-actual comparison**, per-strategy/per-session | P4.3, P4.8 |
| P4.12 | Analytics dashboard UI + journal tab (list, snapshot, rule-trace, outcomes, tags) | P4.11, P4.6 |
| P4.13 | Forward-run ops: systemd hardening, feed-gap alerting, daily stats snapshot | P4.2, P0.27 |
| P4.14 | **P4 GATE:** 2-week forward run; ≥60 recommendations; evaluator outcome recorded for all; manual log complete for all taken; docs updated | all P4 |

---

### Phase 5 — Validation Campaign (§11 P5) — 8 tasks

**Frozen rule: strategies 4–6 only if S1–S3 show positive expectancy.**

| ID | Task | Depends on |
|----|------|-----------|
| P5.1 | Out-of-sample holdout: lock one month excluded from all calibration | P4.14 |
| P5.2 | MAE-distribution analysis tool (evaluator data): per-strategy histograms → SL-tuning report | P4.11 |
| P5.3 | Threshold calibration harness: replay sweeps over config constants, in-sample only | P5.1 |
| P5.4 | Weekly review ritual doc: kill/keep per strategy, parameter change log (every change re-validated on holdout) | P5.2 |
| P5.5 | Campaign ops: continuous forward-run; weekly determinism re-run + data-quality audit | P4.14 |
| P5.6 | Kill/keep checkpoint at ~100 recommendations (documented, data-backed) | P5.4 |
| P5.7 | Final expectancy report: fees-included expectancy per strategy over 200+ recommendations (hypothetical basis + manual comparison), holdout verification | P5.6 |
| P5.8 | **P5 GATE:** positive expectancy after fees on ≥1 strategy over 200 recommendations → strategy marked **TRUSTED**. Fail → strategy not trusted, period. | all P5 |

---

### Phase 6 (OPTIONAL) — Delta Market Data (§11 P6) — 3 tasks

*(Public market data only — no auth, no orders, no execution. Purely a manual-execution helper for trading on Delta.)*

| ID | Task | Depends on |
|----|------|-----------|
| P6.1 | Delta public WS market-data client: trades/mark price streams, reconnect + REST fallback (public endpoints) | P5.8 |
| P6.2 | `DeltaFeed` implementing FeedProvider (capability flags set honestly; `supports_historical_data` per public API availability); passes the P0.19 conformance suite | P6.1, P0.9 |
| P6.3 | Binance↔Delta price-divergence display on the recommendation card (informational only, A18) + docs update | P6.2 |

---

## Part D — Sequencing Notes

1. Momentum utilities (P1.1–P1.4) precede the Structure Engine — displacement, trendline tolerance, and min-size filters all need ATR. Ordering, not redesign.
2. Volume Engine leads Phase 2 (sweep + trendline BREAK consume RVOL). P1 ships a flagged placeholder behind the same interface (P1.16), swapped in P2.2 with a determinism re-run.
3. Every [DECISION] lands before its dependent code task, as a short doc in `docs/decisions/`.
4. The determinism harness grows per phase: candles (P0) → structure objects (P1) → all objects (P2) → signals + recommendations (P3) → evaluator outcomes (P4, via A16's deterministic policy). Never retrofitted.
5. Task counts (v2.0): P0=28, P1=22, P2=24, P3=21, P4=14, P5=8, P6=3 (optional) → **117 core + 3 optional = 120 tasks**, each ≤1h. (v1.1 had 135; 21 execution-related tasks removed, 6 simpler analysis-side tasks added.)
6. Provider boundary is mechanical: engines/strategies/planner/journal import only the FeedProvider base types and EventBus events; the P0.19 CI check fails the build if any engine imports `BinanceFeed`, `ReplayFeed`, or `DeltaFeed`. Provider selection = plain config in `main()`. No DI container, no plugin framework. Single VPS, single process, single codebase.

**Approved scope. Implementation proceeds one task at a time, plan-first, per project rules. Next task: P0.2.**

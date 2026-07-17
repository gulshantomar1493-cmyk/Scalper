# Phase 0 Foundation — Closure Report

**Date:** 2026-07-17 · **Milestone:** P0.28 acceptance gate PASSED (local acceptance) · **Tag:** `v1.0.0-foundation`
Authority: Architecture v1.2 (FROZEN) · Roadmap v2.0 · Decisions D1–D8 (`docs/decisions/`).

## 1. Final architecture summary (as implemented)

Single Python 3.12 asyncio process, composed only in `main.py`: FeedProvider (BinanceFeed live / ReplayFeed historical — identical interface, capability flags) → normalized events on a type-keyed sequential EventBus → CandleBuilder (aggTrades → deterministic 1m candles, discard-first-bucket startup rule D7; internal A2-boundary 5m fold) → CandleWriter (immediate per-candle persistence) and StateStore (per-symbol truth + diffs) → FastAPI app (Bearer-token REST + WS per D3, CORS per D8, replay control endpoints) → vanilla-JS LWC v5 terminal. KlineReconciler pairs tick-built truth against official closed klines (D5, raw-count n_trades); ClockOffsetSampler measures drift vs Binance time (D6); partitioned append-only PostgreSQL 16 schema (migrations 001–002) with `ensure_candle_partitions` (D2) called at startup, UTC midnight, and bootstrap. Engines never see raw provider JSON (AST-enforced import boundary). No order execution exists anywhere, by scope.

## 2. Implemented modules

**Backend** (`backend/marketscalper/`): `config.py`, `logging_setup.py`, `db.py`, `bootstrap.py`, `main.py`; `providers/base.py`, `providers/binance.py`, `providers/replay.py`; `core/bus.py`, `core/candle_builder.py`, `core/candle_writer.py`, `core/reconciler.py`, `core/state.py`; `api/app.py`; `engines/` (intentionally empty until P1).
**Frontend:** `frontend/index.html`, `app.js`, `styles.css` (standalone static, LWC v5 pinned @5.0.0).
**Database:** migrations `001_candles` (partitioned candles + partition helper), `002_analysis_and_journal` (pivots, levels, signals, recommendations, journal).
**Ops:** `deployment/marketscalper.service`, `env.example`, `deploy.sh` (D4); `scripts/ci.sh`.
**Tests:** 22 files (21 test modules + `conftest.py`), 200 tests (199 pass + 1 documented POSIX-only skip on Windows).

## 3. Acceptance results (P0.28, 2026-07-17)

Acceptance criteria are labeled AC1–AC4 (not "G1–G4" — Architecture §6 owns those labels for the runtime qualification gates).

AC1 **PASS** — live BTCUSDT 1m chart in browser (WS closes every minute, history bootstrap, BTC/ETH switch, 5m strip, reconnect).
AC2 **PASS** — zero candle mismatch: 28/28 reconciler pairs clean over the evidence window; 2 startup discards per D7.
AC3 **PASS** — 90-day replay: 129,599 candles ×max through the full pipeline + WS, started/observed via the UI, auto-idle on completion.
AC4 **PASS** — `scripts/ci.sh`: 199 passed / 1 skipped; provider conformance green (both providers); determinism harness green.
Four defects were discovered by the gate and fixed with owner approval (Windows signals, CORS, n_trades semantics, startup candle) — full record in `docs/decisions/P0.28-acceptance-gate.md`.

## 4. Repository status

`main` at the P0.28 milestone commit (the commit introducing this report), tagged `v1.0.0-foundation`; working tree clean once it lands. No secrets in git (DSN/token via env or git-ignored local config). Local dev environment: dedicated Docker PostgreSQL 16 (`marketscalper-pg`, localhost:5437) holding the 90-day bootstrap plus a schema-only `marketscalper_test` database for the suite.

## 5. Deferred items (explicitly outside P0 — no action implied)

Production/VPS deployment (Phase 2 of the owner's execution order — includes provisioning a **PostgreSQL 16** instance per the locked Architecture §2 stack (the target server's existing PostgreSQL is 18.x, which serves other projects and does not satisfy the locked version as-is; resolution is owner-gated), reverse-proxy setup, and systemd install) · ETHUSDT 5m historical backfill via replay (BTCUSDT was the gate scope; identical run available anytime) · all P1–P5 engine/strategy/journal work · optional P6 DeltaFeed · everything on the v1.2 permanent exclusion list (execution, brokers, order APIs).

## 6. Technical debt

Deliberate, documented, all by-design: replay over already-persisted ranges logs one duplicate-insert rejection per candle (P0.15/P0.17 no-dedup design — noisy logs, no data effect) · reconciler keeps unpaired candles pending indefinitely (no expiry policy by design; replay feeds it unpairable entries — bounded by run length, restart clears) · reconnect-mid-minute partial candles remain possible and are surfaced via reconciliation logs (accepted at the D7 ruling; startup case is fixed) · thin client does not render historical replay timestamps into a live chart (§9 thin-client scope). None blocks P1.

## 7. Recommended starting point for P1

**P1.1 — ATR(14) on 1m + 5m, incremental, with unit tests vs a reference implementation** (roadmap 1a; P1.5 pivot detection also unblocks directly off P0.28, but P1.1 precedes it per Part D sequencing — displacement, trendline tolerance, and min-size filters all consume ATR). Per process rules: implementation plan first, owner approval before any code; determinism harness grows at P1.21.

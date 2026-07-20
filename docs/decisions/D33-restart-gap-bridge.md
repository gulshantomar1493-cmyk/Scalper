# D33 — Restart-gap bridge: no candle lost across a deploy (completes D32)

**Status:** Accepted (owner decision, 2026-07-20 — "fix it whatever it takes")
**Reopens (frozen-Foundation exception, F1/F2/F4 precedent):** `providers/binance.py`
**Composition:** `main.py` — live-only priming of the feed
**Builds on:** [[D32-g1-warmstart]] (the G1 continuity-window seed)

## Context — why D32 alone didn't work (verified on prod)

D32 seeded the G1 continuity window from the DB so a restart wouldn't re-warm G1
for ~30 min. Prod verification (restart at 11:48:00 UTC) showed G1 still failing —
now `"gap in last 30 candles"` — and the prod DB confirmed the cause: candles
**11:47 and 11:48 were genuinely missing** (11:46 → gap → 11:49).

A restart **inescapably drops 1–2 candles**, given the frozen design:

1. **Teardown minute(s)** — the old process stops (for `git pull`/reinstall) before
   the new one starts; the minutes in between are never captured.
2. **Connect minute** — the new feed connects mid-minute, builds a *partial* candle,
   and the frozen `CandleBuilder` **discards** it (D7 "correctness over completeness").
   The frozen `_backfill_gaps` runs only on *reconnect* (never first-connect) and can't
   fetch the connect-minute anyway (it's still open at connect).

So the seed ends at the last stored candle, live resumes one/two minutes later, and
that **boundary gap poisons the seeded window for the same ~30 min**. Neither the
seed (D32) nor a full replay-through-chain (audit Option B) avoids it — the gap is
created by the restart itself. It also leaves a **permanent 1–2 min hole in the DB**
every deploy (a data-quality issue P5.5's audit would flag).

## Decision

**Make the restart stop losing candles**, so seed + backfill + live are contiguous:

1. **`main.py` (live only):** `feed.prime_last_closed({sym: db_latest_ts})` — seed the
   feed's per-symbol last-stored 1m ts from the DB (reusing the D19.2 20-day read).
   `ReplayFeed` has no such method; replay/tests never prime → byte-identical.
2. **First-connect backfill (`binance.py`):** priming populates `_last_closed_ts`, so
   the existing `_backfill_gaps` now fills the **teardown** gap on the first connect
   (the frozen "first connection → nothing" skip only made sense before the DB era).
3. **Connect-minute bridge (`binance.py`):** a concurrent one-shot task waits for the
   connect-minute to close, then fetches it from REST and publishes it (truth → bus,
   D5). It runs **alongside** the live loop; the builder's first live candle lags a
   full minute (it closes on the next bucket's first trade), so the bridged candle
   lands well before it.

Net: `seed(→ db_latest)` + `backfill(teardown)` + `bridge(connect-minute)` +
`live(next minute →)` is fully contiguous → **G1 passes on the first live candle
(~1–2 min post-deploy)**, and the recurring DB holes are filled.

## Safety / rationale

1. **Graceful worst case, never corruption.** If the bridge were ever late, the closed
   candle is simply dropped by the composition out-of-order guard (F1) → falls back to
   the normal warm-up. A duplicate (bridge + a later reconnect-backfill) is likewise
   dropped. A real market gap (no trades that minute) → REST returns nothing → G1
   correctly warms. No path produces a wrong or duplicated candle.
2. **No blocking, no buffering.** The bridge is a separate task; the live read loop is
   never paused, so the socket/heartbeat and message flow are untouched.
3. **Live only → determinism untouched.** `prime_last_closed` is called only by live
   `main()`. `ReplayFeed`/tests never prime, so `_bridge_pending` stays False and the
   first-connection behavior is byte-identical → V1–V4 unchanged. The `_now` clock is
   injectable purely for tests.
4. **Reconnects unchanged.** The bridge arms once (first connect after a primed start);
   subsequent reconnects use the existing `_backfill_gaps` exactly as before.
5. **Frozen-Foundation reopen** — permitted with care + audit under the F1/F2/F4
   precedent (the analysis engines stay untouched). The change is additive: a new
   method + a first-connect branch + an injectable clock; the reconnect path, the
   normalization, and the REST fetch are unchanged.

## Freeze audit (adversarial, 10-point trace across binance/main/candle_builder/bus/state/writer/guard)

**Verdict: SOUND, no blockers.** The ordering is guaranteed by construction — the
bridge publishes the connect-minute at ~M+1:02 while the builder physically cannot
close its first live candle before ~M+2:00 (its next-bucket-close invariant), a hard
~57 s margin — and every adverse path degrades gracefully via the existing `<=`
out-of-order guard (drop, never corruption/duplicate). Concurrency confirmed safe
(`pipeline.step()` is synchronous/atomic; `CandleWriter` acquires a fresh pool
connection per call). Isolation confirmed (`prime_last_closed` live-only → V1–V4
byte-identical). Two minor findings were hardened post-audit; one accepted:

- **M1 (hardened):** multi-symbol REST fetches were sequential, so a second symbol
  under simultaneous ~30 s REST latency could publish its bridge candle after its
  first live candle (a transient StateStore/WS regression, self-correcting, no
  backend corruption). Now fetched **concurrently** (`asyncio.gather`) → each symbol
  publishes by ~M+1:32, safely before ~M+2:00.
- **M3 (hardened):** the bridge is a detached task; its publish loop is now wrapped
  in a top-level `except` so it can never die unretrieved (theoretical — no Candle
  handler raises).
- **M2 (accepted):** if arming lands in the final ~1 ms of a minute but the first
  trade lands in the next, that one minute is neither bridged nor built → a residual
  gap that G1 warms through normally (identical to pre-D33 behavior, no corruption).
  Negligible for BTC/ETH (trades every few ms); not worth complicating the code.

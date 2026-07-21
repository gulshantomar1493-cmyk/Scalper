# MarketScalper V2 — Forensic Research Report

**Status:** Draft (research phase — no code written yet)
**Author:** engine research, 2026-07-21
**Scope:** professional discretionary trade-setup construction for BTCUSDT / ETHUSDT
perps + XAUUSD / PAXG (gold), decision-support only (no execution).

> This report is the mandated "research first" deliverable. It (1) records the
> infrastructure reality that constrains the build, (2) synthesises how experienced
> discretionary traders actually construct setups, and (3) derives a *practical*
> engine design from that synthesis — reusing the frozen V1 engines rather than
> re-implementing them. The reuse map, bug root-causes, and phased plan are in
> §8–§10 (completed once the code audit lands).

---

## 1. Method & guiding principle

I did not "pick a strategy." Experienced discretionary traders across the SMC/ICT,
Wyckoff, supply/demand, and classical-price-action schools disagree on vocabulary but
**converge on the same underlying behaviour**: they read *where liquidity sits*, wait
for the market to *take* that liquidity, and enter on *evidence that the move has
shifted* — always with the higher-timeframe context deciding direction and the lower
timeframe deciding timing. The engine is designed around that convergent behaviour,
not any one guru's ruleset. Indicators (EMA, RSI, ATR) are **confirmation and
measurement only** — never a trigger.

The engine's job is **not prediction**. It is: understand structure → locate the
high-probability zone → wait for the trigger → explain the reasoning → or say *"no
high-probability setup."* Quality over quantity is enforced structurally (a setup must
clear multiple independent gates before it is shown).

---

## 2. Infrastructure findings (verified — this constrains the build)

**Critical discrepancy with the directive.** The directive states Delta Exchange API
credentials "have already been provided in the project's `.env` file." That is not the
case in this repository:

| Claim | Reality (verified) |
|---|---|
| `.env` with Delta credentials | **No `.env` file exists anywhere** in the repo (`find . -name .env*` → empty). |
| Existing Delta API client | **None.** `providers/` has only `binance.py`, `replay.py`, `base.py`. Every "delta" match in code is order-flow *delta* (buy−sell volume) or `timedelta`. |
| Delta referenced in project | Only as a **future, optional** idea: Architecture §597 — *"DeltaFeed: Delta Exchange **public** WS market data (no auth, no orders)."* README — *"Optional: Delta Exchange public market data."* |
| Current live data source | **Binance spot** (`aggTrade` + `kline_1m` + `bookTicker`) for **BTCUSDT + ETHUSDT** only; 9-year 1m backfill in Postgres. |

**Instrument reality (verified via Delta's own docs/site):**
- **BTCUSDT / ETHUSDT on Delta are *perpetual futures*** (funding, mark-price basis) —
  different instruments from the Binance *spot* the app runs on today.
- **XAUUSD and PAXG are Delta-only gold perpetuals** — they do **not** exist on Binance
  spot. Supporting them is *impossible* without a Delta market-data feed.
- Delta's **market-data endpoints are public** (OHLCV across 1m…1w, tickers, order book,
  funding rate, mark price, open-interest history) — **no credentials required** for the
  read-only data this engine needs.

**Consequences (these are design constraints, not opinions):**
1. **No credentials are actually needed** for the public data the engine consumes, so the
   missing `.env` is not itself a blocker for *analysis*. What is missing is the **DeltaFeed
   integration** (a real, if modest, build) — which is Architecture "Phase 6, optional."
2. **Hard safety/architecture boundary — non-negotiable:** the engine stays
   decision-support only. **No live execution. No authenticated / account / order APIs.
   Public market data only.** This is both my operating rule and the frozen architecture's
   own definition of a DeltaFeed. Paper trading remains 100 % locally simulated in our
   Postgres (as V1 already does) — it must *never* touch a real Delta account.
3. The build therefore splits cleanly into **Delta-independent** work (everything for
   BTC/ETH on the existing Binance data) and **Delta-dependent** work (XAUUSD/PAXG,
   perp funding/OI, cross-venue basis) that requires the DeltaFeed first.

---

## 3. How professional discretionary traders actually build a setup

The convergent workflow, top-down:

**(a) Establish the higher-timeframe narrative first.** Daily/4H define *bias* — is price
making higher-highs/higher-lows (bullish structure) or lower-highs/lower-lows (bearish)?
Where is price within its dealing range (premium vs discount)? What obvious liquidity
pools sit above/below (equal highs/lows, prior day/week high/low, round numbers)? No
lower-timeframe entry is considered until this story is coherent.

**(b) Locate liquidity and the "draw."** Markets move *to* liquidity. Buy-side liquidity
rests above equal highs / swing highs (stop-losses of shorts + breakout buys); sell-side
below equal lows. The "draw on liquidity" is the magnet the HTF narrative points at. A
setup that trades *away* from the obvious draw is low-probability.

**(c) Wait for the raid (sweep), then the shift.** The high-probability entry is not the
level itself — it is a **liquidity sweep** (price spikes through the pool, grabbing stops)
**followed by a Change of Character (CHOCH)** or Break of Structure (BOS) on the entry
timeframe. Sweep = *fuel*; CHOCH/BOS = *confirmation the intent flipped*. Entering on the
sweep alone is a knife-catch; entering after the shift is the trade.

**(d) Refine entry into an imbalance or block.** After the shift, price typically retraces
into a **Fair Value Gap (FVG)**, **Order Block** (the last opposing candle before the
impulsive move), **Breaker** (a failed OB that flipped), or **Mitigation Block**. That
retracement zone — ideally in the *discount* half of the range for longs, *premium* for
shorts — is the entry. This is where indicators *confirm* (declining pullback volume,
momentum divergence, EMA alignment) but never *decide*.

**(e) Risk is defined by structure, not by a fixed number.** Stop goes *beyond the
sweep wick / invalidation of the block* — the price that would prove the read wrong.
Targets are the *next liquidity pool* (opposing EQH/EQL, prior session extreme, HTF swing).
This yields an R:R that is *discovered*, not imposed; if structure doesn't offer ≥~1.5–2R
to the next pool, there is no trade.

**(f) Confluence gating.** A professional does not take a setup because one thing lines
up. They stack independent evidence: HTF bias + liquidity draw + sweep + shift + refined
zone in the right half of the range + acceptable R:R + no opposing HTF wall in the way.
Missing pillars → smaller size or *no trade*. This is the single most important behaviour
to encode: **confluence, and the discipline to pass.**

**Wyckoff & order-flow overlay.** Wyckoff's accumulation/distribution schematics
(spring = sell-side sweep at range low → sign of strength; upthrust = buy-side sweep at
range high → sign of weakness) are the *same behaviour* in different words, and give the
engine a way to label the *phase* (accumulation / markup / distribution / markdown).
Order-flow (CVD / delta, absorption, exhaustion) is confirmation of *who won* the sweep —
already computed by the V1 volume engine.

---

## 4. Concept reference (what each means operationally, for the engine)

- **Market structure / HH-HL-LH-LL** — the swing skeleton. Bullish = HH+HL; bearish =
  LH+LL; mixed = range. Computed on *confirmed* swings only (no repaint).
- **BOS (Break of Structure)** — close beyond the last confirmed swing *in the trend
  direction* = continuation.
- **CHOCH (Change of Character)** — first close beyond the last *counter-trend* swing =
  earliest evidence of a reversal; the trigger that arms most reversal setups.
- **Liquidity / sweeps** — pools above EQH / below EQL and at session/day/week extremes;
  a *sweep* is a wick through the pool that fails to hold (the raid).
- **Equal Highs / Equal Lows** — clustered highs/lows = engineered liquidity; a magnet
  and a common reversal origin after they're taken.
- **Order Block** — last opposing-color candle before a displacement move; institutional
  footprint; a retest zone. **Breaker** — an OB that price broke *through*, then flips to
  act as support/resistance from the other side. **Mitigation Block** — an OB retested
  without full invalidation.
- **Fair Value Gap (imbalance)** — 3-candle gap where wicks don't overlap; price tends to
  return to "rebalance" it; a precise entry refinement.
- **Supply / Demand** — zones of prior imbalance origin; the classical framing of the
  same OB/imbalance idea.
- **Premium / Discount** — the 50 % of the dealing range (fib equilibrium). Buy in
  discount, sell in premium; a setup on the wrong side of equilibrium is penalised.
- **Trend / EMA alignment / momentum** — *confirmation* layer. EMA stack (e.g. 20>50>200)
  and momentum agree-or-disagree with the structural read; they raise/lower confidence,
  never trigger.
- **ATR / volatility** — sizing, stop-distance sanity, and displacement thresholds
  (a "BOS" on a doji in dead volatility is not displacement).

---

## 5. Instrument-specific behaviour

- **BTCUSDT / ETHUSDT perps** — 24/7, high liquidity, strong session character
  (Asia range → London expansion → NY continuation/reversal). Funding + open interest
  matter: rising OI into a sweep = fresh positioning (trend fuel); falling OI on a break =
  short/long covering (fade risk). Perp *mark* can diverge from spot around funding —
  relevant if we ever fill paper trades against Delta perp marks.
- **XAUUSD / PAXG (gold)** — gold is *not* crypto: it respects macro sessions (London/NY
  fixes), is lower daily-range % than BTC, and reacts to DXY/rates/risk events. PAXG is a
  gold-backed token; XAUUSD on Delta is a synthetic gold perp — they track spot gold with
  a basis. **SMC structure concepts transfer, but calibration does not:** ATR-scaled
  thresholds, EQH/EQL tolerance, and "displacement" sizing must be *relative* (ATR-normalised,
  which the V1 engines already are) rather than absolute. Gold's cleaner session structure
  actually suits liquidity-sweep setups well.

---

## 6. Design principles the research forces on the engine

1. **Top-down or nothing.** Compute the HTF story first; a lower-timeframe trigger is only
   valid *with* the HTF bias behind it (or explicitly flagged counter-trend + lower
   confidence).
2. **Price action decides, indicators confirm.** Structure/liquidity/zones generate the
   setup; EMA/RSI/volume only move the confidence score. No indicator-only trades — ever.
3. **Sweep → shift → refined zone** is the canonical high-probability pattern; encode it
   explicitly and prefer it.
4. **Structure-defined risk.** SL = structural invalidation (beyond the sweep/zone);
   TP = next liquidity pool. R:R is discovered; reject setups that can't reach ≥ the
   configured floor to the next pool.
5. **Confluence gating + the confident "no trade."** Multiple independent pillars required;
   if they're absent, return *"No high-probability setup available"* rather than a weak one.
6. **Full explainability.** Every setup carries *why it exists*, *why now*, *why the stop
   is there*, *why the target is there*, its invalidation, and its risk level — as
   human-readable reasons, reconstructable from the analysis (no black box, no fabricated
   confidence %).
7. **Reuse, don't re-implement.** The V1 engines already detect structure, BOS/CHOCH,
   liquidity/sweeps, OB, FVG, volume, momentum, premium/discount — bit-for-bit and
   no-repaint. V2 is an *orchestration + narrative + setup-selection* layer over them
   across 6 timeframes (the HTF V1.1 module already does this for 4 TFs — V2 extends the
   pattern), never a parallel re-detection.

---

## 7. Engine shape (derived — detail pinned in the plan, §10)

```
For each symbol:
  MTF analysis  (Daily,4H,1H,15m,5m,1m)  ── reuse frozen engines on aggregated candles
        │              per-TF: trend, structure, BOS/CHOCH, swings, liquidity, sweeps,
        │              EQH/EQL, OB/breaker/mitigation, FVG, supply/demand, S/R,
        │              trendlines, premium/discount, EMA, volume, momentum
        ▼
  Market Story  (top-down narrative + HTF bias + draw-on-liquidity + phase)
        ▼
  Setup search  (only in the direction the story allows):
        sweep(entry-TF pool) → CHOCH/BOS shift → refined zone (FVG/OB/breaker,
        premium/discount-correct) → next-pool target → R:R check → confluence gate
        ▼
  Setup | "No high-probability setup"
        (direction, entry, SL, TP1, TP2, R:R, confidence, market bias, HTF bias,
         reasons[], invalidation, risk level — all explained)
```

Confidence = weighted agreement of *independent structural pillars* (HTF-aligned,
sweep present, shift confirmed, zone quality, premium/discount-correct, R:R, volume
confirmation) — **never** a probability and never fabricated. This mirrors V1's
"weighted rule agreement" discipline (Architecture §0.3).

---

## 8. Reuse map — existing subsystems V2 builds on (audited)

- **`core/htf.py` (HTF V1.1)** — already the V2 pattern: re-instantiates the frozen
  engines on aggregated candles (engine-isolated) across **1d/4h/1h/15m** and emits
  per-tf {trend, structure, BOS, CHOCH, swings, liquidity, sweep, supply/demand, S/R,
  trendlines, EMA, momentum} + `overall {score, bias, confidence, market_story,
  explanation}` via `GET /api/htf`. **Gaps vs the directive:** no 5m/1m, no explicit
  EQH/EQL, no premium/discount, no FVG in the HTF read. → V2 extends this, it doesn't
  replace it.
- **`core/chart_service.py`** — 9-TF read-model (`1m…1M`), engine-isolated, `GET
  /api/chart` with candles + indicators + display-only HTF context. V2's analyzer pulls
  candles from here.
- **`engines/*` (frozen, reuse as-is)** — structure (pivots/HH-HL-LH-LL/BOS/CHOCH),
  trendline, liquidity (pools/sweeps/session levels), orderblock, fvg, volume (rvol/
  VWAP/delta/absorption/exhaustion), momentum (ATR/velocity/regime), confluence,
  qualification (gate rubric), strategy (S1/S2/S3 signals), risk (`plan_trade` →
  entry/SL/TP1/TP2/net-RR with fees), evaluator, lifecycle, psychology.
- **Paper trading** — `papertrade.py` (pure netting/PnL/liquidation math), `paper_service.py`
  (DB), `/api/paper*`, tables (migrations 004/005). Positions are **per-symbol** (one
  net position/symbol); mark = last **closed** 1m candle close.
- **Frontend** — `app.js` is the **sole** owner of `fetch`/`WebSocket`; `drawing.js`
  (trendline/hline/rect/fib/text), reused single chart (never recreated on switch).
  **Pure-consumer contract is grep-enforced**: every JS module except `app.js` is banned
  from network + engine-math; V2 keeps all math server-side and renders via `textContent`.

## 9. Reported bugs — symptoms → root cause → impact → permanent fix (audited)

| # | Symptom (as reported) | Verdict & **root cause** | Permanent fix |
|---|---|---|---|
| **B1** | "Trades belong to the timeframe, not the symbol" | **Not real.** Positions/orders/marks are keyed purely by `symbol`; no timeframe column or param exists anywhere. The confusion is **per-symbol netting** (BUY then SELL nets/flips one position) + the on-chart widget only showing `activeSymbol`. | UX clarity: label the netted position honestly; show all open positions. No data change needed. |
| **B2** | "Trades disappear / funds reset after refresh" | **Partly real (UX), not a data reset.** Persistence is correct (Postgres); no load path resets funds. Real causes: (a) after F5 `activeSymbol` resets to BTCUSDT, so an ETH position vanishes **from the chart** (still in DB); (b) `get_state` runs `_sync`, which legitimately closes a position whose SL/TP/liq was crossed while away (10× default, no SL → liquidation). | Persist `activeSymbol` across reload; surface *why* a position closed (liq/SL/TP) in history; keep `_sync` (it's correct). |
| **B3** | "Total P&L resets / is incorrect" | **Real gap.** There is **no persistent lifetime realized-P&L** field — it's implicit as `balance − starting_balance`, and `reset_wallet` rewrites `starting_balance`, orphaning prior realized history. Trade `history` is capped at 100 rows, so any client-side "total" truncates. | Add a persistent lifetime realized-PnL counter (own column, survives wallet reset); expose it in `portfolio`; never sum the capped history for totals. |
| **B4** | "Invalid / unrealistic execution prices" | **Real.** Market orders fill at `last_candle_1m.c` (up to ~60 s **stale**), while the UI shows the live forming price. The live tick exists server-side (`LiveBarTracker.FormingBar.c`) but is **broadcast-only, never in `StateStore`**, so paper can't read it. Stops also fill at the stale mark, not `stop_price`. No bid/ask/slippage/range-bounding. | Source the paper mark from the **live forming bar**; bound fills to the candle range; fill stops at `stop_price` (±slippage); add a small realistic bid/ask + slippage model. |
| **B5** | "Changing timeframe/symbol/refresh removes drawings" | **Real for refresh only.** Drawings are an in-memory array with **no persistence** → F5 discards them. tf/symbol switch *keeps* them, but they're **not symbol-scoped**, so a BTC trendline renders at BTC price on the ETH chart (off-screen → "gone"). | Persist drawings (backend `drawings` table + API, since `app.js`/`drawing.js` are storage-banned) **scoped per symbol**; restore on load; show only the active symbol's drawings. |

_All fixes are root-cause (no band-aids); each ships with a regression test._

## 10. Phased implementation plan + the Delta decision

**Principle:** reuse the frozen engines + HTF; build V2 as additive, engine-isolated
layers (never touch the determinism stream); BTC/ETH first (unblocked), gold/Delta
gated on the decision below.

- **Phase 0 — Research + plan** *(this document — done).*
- **Phase 1 — Paper Trading V2 correctness** *(BTC/ETH, no Delta).* B4 live-forming-bar
  marks + range-bounding + realistic stop/slippage/bid-ask; B3 persistent lifetime P&L;
  B2 persist `activeSymbol` + closure-reason in history; B1 netting-UX clarity. + regression.
- **Phase 2 — Professional chart + drawing persistence** *(frontend, no Delta).* B5
  backend drawings persistence scoped per symbol (survive refresh/tf/symbol); fullscreen;
  extra tools (ray/vline/arrow/RR); zoom/pan/crosshair/selection polish. + regression.
- **Phase 3 — Trade Engine V2 (setup engine)** *(BTC/ETH, no Delta).* Extend the HTF
  analyzer to 6 TFs (add 5m/1m) + the missing concepts (EQH/EQL, premium/discount, FVG);
  new engine-isolated `core/setup_engine.py`: market story → setup search (sweep→shift→
  refined zone→next-pool target→R:R→confluence gate) → `TradeSetupV2` with full
  explainability, or a confident "No high-probability setup." Reuses `strategy`/`risk`/
  `confluence`. `GET /api/setups?symbol=`; frontend setups panel + on-chart viz. + regression.
- **Phase 4 — Gold + Delta** *(BLOCKED — decision required).* Build a **public** DeltaFeed
  (market data only — OHLCV/tickers/order book/funding/OI; **no auth, no orders**, per
  Architecture §597) to add **XAUUSD / PAXG** (Delta-only) + perp funding/OI/basis; widen
  the symbol set; gold calibration is inherited free (V1 engines are ATR-normalised).
  This is net-new integration ("Phase 6, optional" in the frozen roadmap) + a scope
  amendment → needs an explicit owner GO.

**The Delta decision (the one thing that can't be resolved automatically):**
XAUUSD/PAXG are impossible without a Delta feed, and bringing BTC/ETH onto Delta *perp*
data (vs the current Binance *spot*) is an architectural change. Options: **(A)** build
the public DeltaFeed now (enables gold; public data only); **(B)** ship Phases 1–3 on
BTC/ETH first, DeltaFeed as a follow-up; **(C)** both venues in parallel (Binance for
BTC/ETH history, Delta for gold). Recommendation: **B → then A** — deliver the unblocked
high-value V2 (fixes + setup engine) immediately, then add the public DeltaFeed for gold.
Everything in Phases 1–3 proceeds automatically under AUTO MODE; only Phase 4 waits.

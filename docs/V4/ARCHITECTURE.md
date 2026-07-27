# MarketScalper V4 — Architecture

**Status:** DESIGN + BUILD. Supersedes the V1/V2/V3 *strategy* layers.
Written 2026-07-27, after a full quantitative research programme
(`RESEARCH-JOURNAL.md`, 24 experiments, 9 years of BTC/ETH 1m data).

---

## 0. Why V4 exists — the evidence

| Layer | Evidence | Decision |
|---|---|---|
| V3 sweep-reversal (currently live) | gross-negative on **16/16** research cells; live prod 222 recs = **−141R**, only **4 target hits** in 191 resolved trades | **REMOVE** |
| V1 strategies S1/S2/S3 + qualification | never validated; the TRUSTED gate never passed | **REMOVE** |
| V2 `setup_engine` | orphaned — zero frontend callers | **REMOVE** |
| Infrastructure: candle store, ChartService, DB, paper-trade engine, feed | never the problem; 9y clean data, aggregation verified | **KEEP** |

**The strategy layer is replaced. The plumbing is kept.**

---

## 1. What the research actually proved

These are the only rules with statistical support. V4 implements exactly these.

**The winning structure:**
```
LEVEL (from a higher timeframe)
   + TREND FILTER (price above/below EMA, structure agrees)
   + RESTING STOP ORDER at the level      <- latency-immune
   + STOP = 5 x ATR(5m)                   <- keeps fee/R ~0.09
   + TARGET = 10R                         <- winners must run
```

**Why each piece exists (not arbitrary):**

| Piece | Reason | Evidence |
|---|---|---|
| Resting **stop** order (not market) | market entry decays 36% in 1 minute and inverts by 15 min; a resting order at a known level is unaffected by delay | +0.128 → +0.130 with a **4-hour** delay |
| Stop = **5×ATR(5m)** | fee/R must stay below ~0.12 or costs eat the edge; tighter stops (2×ATR5m ⇒ fee/R 0.27) turn it negative | −0.374 at 2×ATR vs +0.191 at 5×ATR |
| Target **10R** | expectancy rises monotonically with target; the edge lives in the tail | rr 5 → 10 → 15 all improve |
| **Trend filter** | the only confluence family that helps | +0.19…+0.68 lift per filter |
| **BREAK not BOUNCE** | fading a level loses on every level type, both symbols | bounce −0.19…−0.36 (t up to −3.95) |

**Explicitly rejected (do NOT reintroduce):**
premium/discount and liquidity-sweep filters (−0.23…−0.45 lift — they are
mean-reversion logic fighting a breakout system) · mean reversion in any form
(gross-negative in every regime, including CHOP) · SMC sweep→CHOCH→OB retest
(not significant even at **zero** fees) · trendline bounce (negative at zero fees) ·
time-of-day seasonality (42–46% era-to-era sign stability) · BTC→ETH lead-lag.

---

## 2. Strategy catalogue shipped in V4

Each is an independent, separately-tracked strategy. The UI compares them.

| id | Symbol | Level source | Filter | Trades/yr | net R | t |
|---|---|---|---|---|---|---|
| `eth_1h_fast` | ETH | 1H donchian-20 | need ≥1 | **251** | +0.196 | 3.17 |
| `eth_4h_core` | ETH | 4H donchian-20 | need ≥3 | 100 | **+0.474** | 4.50 |
| `eth_4h_wide` | ETH | 4H donchian-20 | need ≥1 | 153 | +0.372 | 4.60 |
| `eth_1d_swing` | ETH | 1D swing | need ≥3 | 54 | +0.643 | 4.08 |
| `eth_pdhl` | ETH | prior-day H/L | need ≥3 | 112 | +0.334 | 3.49 |
| `btc_4h_core` | BTC | 4H donchian-20 | need ≥1 | 159 | +0.204 | 2.61 |

**Honesty rule:** every strategy card shows its *own* backtest stats AND its live
paper stats side by side. No strategy is presented as validated for the modern era —
2021+ significance was NOT established (t ≈ 0.7–1.7). The UI states this.

---

## 3. Module layout

```
backend/marketscalper/v4/
  config.py      frozen, validated parameters — one dataclass per strategy
  levels.py      level detection: donchian / swing / PDH-PDL / round, any TF
  filters.py     trend filters: EMA vs price, structure trend
  signals.py     setup construction (level + filter -> Setup)
  outcome.py     honest outcome tracking: fees BOTH sides, funding, real exits
  service.py     compute-on-read orchestration + TTL cache (ChartService source)
  store.py       persistence (migration 008)
```

**Non-negotiables carried from the research engine:**
1. **No lookahead** — a signal uses only bars whose close ≤ decision time.
2. **Fees on both sides of every trade**, always, in the reported R.
3. **Funding charged** on holding period.
4. **Real exits only** — a horizon exit is a market close that pays fees, never an
   unrealized mark. `TIME` exits are reported separately from `TP`/`SL`.
5. **Losses are not floored at −1R** — a gap through the stop is charged in full.
6. **Deterministic** — same candles in, same setups out.

---

## 4. API (cut over — the V1/V2/V3 endpoints are gone)

```
GET  /api/v4/strategies                     catalogue + backtest evidence
POST /api/v4/strategies/{id}/enabled        owner on/off switch (runtime)
GET  /api/v4/setups?symbol=&strategy=       current actionable setups
GET  /api/v4/quotes                         last price per symbol
GET  /api/v4/levels?symbol=&tf=             levels to draw on the chart
GET  /api/v4/history                        recommendation history (filter/sort/CSV)
GET  /api/v4/performance                    per-strategy equity, expectancy, drawdown
```

Kept from the old stack because they are strategy-independent infrastructure:
`/api/chart` (multi-timeframe read-model), `/api/htf` (higher-timeframe context),
`/api/journal` (the owner's own journal, full CRUD), `/api/paper/*` (the
simulation engine), `/ops`, `/settings/*`, `/candles`, `/ws`, `/replay/*`.

Removed with their strategy layer: `/api/v3/*`, `/api/setups` (V2), `/journal/{id}`
(the V1 per-recommendation journal), `/analytics`, `/analytics/mae`, `/campaign/*`.

---

## 5. UI — six screens, each with one job

| Screen | Job | Key content |
|---|---|---|
| **Today** | "what do I do right now" | active setups as cards: symbol, direction, entry / stop / target, R:R, which strategy, why it triggered, one-click *Take (paper)* |
| **Chart** | see the setup on price | candles + level lines + entry/stop/target drawn; strategy selector; TF switch |
| **Strategies** | compare and choose | one row per strategy: backtest stats vs live paper stats, equity curve, trades/yr, enable/disable toggle |
| **History** | learn from outcomes | every recommendation ever issued, outcome, R, MAE/MFE, hold time, filter + CSV |
| **Paper** | track the simulated book | open positions, closed trades, equity curve, per-strategy attribution |
| **Journal** | your own record | free-form entries (independent of what the system said): prices, emotion, mistakes, lessons, tags — create / edit / delete / search |

**UI principles (from the audit of the old tool):**
* No fake confidence percentages — grade is a named factor count or nothing.
* Every number traceable to a rule; every setup shows *why*.
* Backtest stats and live stats are never mixed in the same figure.
* "No setup right now" is a valid, common, clearly-displayed state.

---

## 6. Build order

1. `config.py` + `levels.py` + `filters.py` + `signals.py` + unit tests ← **core**
2. `outcome.py` + `store.py` + migration 008
3. `service.py` + API endpoints
4. UI: Today → Chart → Strategies → History → Paper
5. Cut over: remove V1/V2/V3 strategy code and their endpoints ✅ **DONE**

### What the cutover removed

| Removed | Lines | Why |
|---|---|---|
| `v3/` package + `/api/v3/*` + replay runner | ~2,800 | the rejected sweep-reversal strategy |
| `core/setup_engine.py` + `/api/setups` | ~350 | V2, orphaned — zero callers |
| `engines/{strategy,qualification,confluence,risk,lifecycle,evaluator,psychology}.py`, `core/recorder.py` | ~1,700 | V1 S1/S2/S3 + the score/gate/plan/journal chain that never passed its own TRUSTED gate |
| `analytics.py`, `campaign.py`, `calibration.py` + their endpoints | ~640 | reporting over V1 recommendations, which no longer exist |
| the old frontend (23 files) | ~5,100 | it *was* the V1/V2/V3 client; V4 replaces it |

Then a second pass removed what the first left orphaned — the owner's call once
it was clear nothing read it:

| Also removed | Lines | Why |
|---|---|---|
| `engines/` (structure, liquidity, order blocks, FVG, trendlines, volume, momentum) | ~2,600 | the V4 UI never connected to `/ws`, so the whole 1m chain computed for nobody |
| `core/htf.py` + `/api/htf` | ~530 | same: no client after the old frontend went |
| `/ws` + the broadcast machinery, `/replay/*`, `LiveIndicatorTracker`, `StateStore.structure` | ~450 | they existed only to carry that payload to a browser |

**Kept:** the candle pipeline (builder, writer, reconciler, backfill, providers),
the DB, ChartService, paper trading, the owner journal, ops / settings / alerts,
and `ReplayFeed` itself (the determinism gate and the provider-conformance suite
both drive it). `LiveBarTracker` stays too — paper-trade fills mark against it.

**What the determinism gate now guarantees.** It used to hash the analysis
payload and the signal/recommendation stream. Those are gone, so it hashes what
still matters: the CANDLE stream V4 reads is byte-identical across a double
replay, with a sensitivity self-test proving that is not vacuous. V4's own
determinism is covered by `test_v4.py::test_deterministic`.

Backend: 14,258 -> ~5,500 lines. Frontend: 5,100 -> ~1,800.

# MarketScalper V3 — Virtual Trader Engine (scratch design)

Status: DESIGN — awaiting owner approval before Phase 1 code.
Supersedes the V1/V2 analytical core (engines/, setup_engine, htf, strategies,
qualification). Infra is kept: feeds, candle store, ChartService, DB, API,
frontend shell, paper trading, deploy.

---

## 0. Root cause — why the current engine never gives a trade

`/api/setups` requires ALL of, on the SAME 1m bar:

1. a frozen StrategyEngine Signal (S1 sweep→shift / S2 pullback / S3 fake-break)
   — fires a handful of times per WEEK on 1m;
2. the signal still inside its 5-bar validity window (≤5 minutes old);
3. qualification data-integrity PASS;
4. HTF not ≥50% convinced against;
5. sweep+shift present in the last bars;
6. premium/discount or OB/FVG at entry;
7. net R:R ≥ 1.5 to the next pool.

The joint probability is ~zero → permanent "No high-probability setup".
The design flaw: **setups are born from rare 1m event chains** instead of from
the thing a real trader watches all day — **price approaching pre-mapped
multi-TF zones**. V3 inverts this: map the chart first, then wait at the zones.

---

## 1. What V3 is

A **virtual professional crypto-futures trader**: it reads every timeframe the
way a human does, keeps a marked-up chart (trendlines, zones, liquidity) per
TF, knows where reversals / breakouts / breakdowns are likely, waits for price
to reach those places, confirms with price action, and only then issues a
complete setup — sized by session quality (IST timing guide). Decision-support
only; never executes.

Pipeline (5 layers):

```
candles (ChartService, all TFs)
   │
   ▼
L1  CHART READ  — per TF: swings → structure → trendlines → zones → liquidity
   │
   ▼
L2  MARKET MAP  — merge TFs: stacked zones, liquidity targets, bias per TF + overall
   │
   ▼
L3  VIRTUAL TRADER — watch price vs map: WATCHING → ARMED → TRIGGERED setups
   │                 (reversal / breakout / breakdown), entry/SL/TP/RR/grade
   ▼
L4  SESSION TIMING — IST windows (owner's guide) modify grade / block dead zones
   │
   ▼
L5  DELIVERY — /api/v3/* + chart overlays per active TF + setup card + paper
```

---

## 2. L1 — Chart Read Engine (per timeframe)

Runs on TFs: **5m, 15m, 1h, 4h, 1d** (read TFs) + **1m** (confirmation only).
Compute-on-read over ChartService candles (same pattern as the old HtfService),
cached per (symbol, tf), refreshed when that TF prints a new closed candle.

Per TF, in order:

1. **Swings** — fractal pivots (k=2 per TF), labeled HH/HL/LH/LL.
2. **Structure** — trend (bullish = HH+HL chain, bearish = LH+LL, else range);
   BOS (close beyond last same-side swing); CHOCH (first close beyond the last
   opposite swing). Displacement flag = body > 1.2× ATR(tf).
3. **Trendlines** — connect ≥2 swing highs (resistance line) / lows (support
   line) in log space; validity = touches (≥2, 3+ = strong), age, respected-%.
   Channels when parallel pair. Broken line → role-flip candidate.
4. **Zones** (each with price band [lo,hi], kind, strength, touches, fresh/tested):
   - **S/R levels** — swing-price clusters (≥2 swings within 0.25×ATR band);
   - **Supply / Demand** — base (≤3 small candles) before an impulse
     (displacement move ≥1.5×ATR) up = demand, down = supply; fresh until tapped;
   - **Order Blocks** — last opposite candle before displacement BOS;
   - **FVG** — 3-candle imbalance ≥0.3×ATR, tracked to CE/fill;
   - **Trendline zone** — the projected line ± 0.15×ATR band at current bar.
5. **Liquidity map** —
   - equal highs/lows pools (≥2 swings within 0.1×ATR) = resting stops;
   - PDH/PDL, PWH/PWL, session high/low of ASIA / LONDON / NY;
   - above/below trendlines (stop clusters);
   - each pool: side (buy-side above / sell-side below), price, swept? when?
6. **Premium/Discount** — equilibrium of the active TF range; longs only from
   discount, shorts only from premium (for reversal setups).

Output contract per TF: `TfRead { trend, swings[], structure_events[],
trendlines[], channels[], zones[], liquidity[], equilibrium, atr }`.

**Chart rendering (owner requirement):** `GET /api/v3/analysis?symbol&tf`
returns that TF's `TfRead`; the frontend draws that TF's own trendlines +
zones + liquidity levels, and re-draws on every TF switch. What you see on 4h
is the 4h read; switch to 15m → the 15m read.

---

## 3. L2 — Market Map (the merged picture)

One object per symbol, rebuilt whenever any TF read updates:

- **Stacked zones** — zones from different TFs that overlap (within 0.3×ATR of
  the higher TF) merge into one map-zone with `tf_stack` (e.g. 4h demand + 1h
  OB + 1h trendline = stack of 3). Stack depth = zone weight.
- **Bias ladder** — trend per TF (1d → 5m) + overall bias = weighted vote
  (1d:4, 4h:3, 1h:2, 15m:1) from STRUCTURE ONLY (indicators never vote).
- **Liquidity targets** — nearest unswept pools above & below current price,
  ordered; these are the draw (where price is being pulled).
- **Next decision points** — the ordered list of map-zones above & below price
  with distance; this is the trader's "agar yahan aaya to kya karunga" list.

---

## 4. L3 — Virtual Trader (setup generation)

A state machine per (symbol, map-zone). No rare-event dependence — zones are
always there; the trader is always watching.

```
IDLE ──price within 1.5×ATR of zone──▶ WATCHING  (shown in UI as "upcoming")
WATCHING ──price enters zone band──▶ ARMED
ARMED ──confirmation on 1m/5m──▶ TRIGGERED → issue TradeSetup
ARMED ──zone violated (close through + displacement)──▶ FAILED (may arm breakout the other way)
TRIGGERED ──entry filled/expired/invalidated──▶ resolved → archive
```

Three setup archetypes:

**A. REVERSAL (at a stacked zone)**
- Where: price into a map-zone WITH the HTF bias (pullback) or at 1d/4h
  extreme against exhausted move; must be discount (long) / premium (short).
- Extra weight if a liquidity pool just got swept INTO the zone (stop hunt →
  reversal fuel).
- Confirmation (1m/5m, any one): CHOCH toward trade · rejection wick ≥60% of
  bar through zone and back · engulfing close inside zone.
- Entry: zone 50% (or CE of FVG). SL: beyond zone edge + 0.25×ATR (beyond the
  sweep wick if swept). TP1: nearest opposing liquidity pool. TP2: next map-zone.

**B. BREAKOUT (through resistance / trendline)**
- Where: compression against a level/trendline (≥3 touches, tightening range,
  falling 1m ranges) OR a strong displacement close through it.
- Confirmation: break candle displacement ≥1.2×ATR, OR retest-hold (price
  returns to the broken level, holds it as support, LTF CHOCH up).
- Entry: retest of broken level (preferred) or break close. SL: back inside
  (beyond the broken level ∓ 0.25×ATR). TP1: next pool above. TP2: next map-zone.

**C. BREAKDOWN** — mirror of B through support / trendline / equal-lows shelf.

Every `TradeSetup`:
`{ direction, archetype, entry, sl, tp1, tp2, rr_net (fees ×2, min 1.5),
grade, grade_reason, confluences[], avoid_reasons[], invalidation,
management[], session_window, tf_stack, state, created_ts }`

**Grade = confluence count (never a %):**
counted from: HTF bias aligned · zone stack ≥2 TFs · liquidity swept into zone ·
trendline confluence · fresh (untested) zone · clean confirmation displacement ·
session window ≥4⭐. A+ ≥5 · A ≥3 · B ≥2 (below 2 → not issued).

**Honesty rules (kept from V2):** avoid_reasons always populated; "No Setup"
is a valid, common answer; every number traceable to a rule; no probabilities.

---

## 5. L4 — Session Timing Engine (owner's IST guide, verbatim)

Windows (IST) with rating → effect on the trader:

| IST window          | Rating | Effect |
|---------------------|--------|--------|
| 03:30–05:30         | ⭐ ❌   | BLOCK all setups (fake-breakout zone) |
| 05:30–08:30         | ⭐⭐⭐⭐  | normal (Tokyo momentum) |
| 08:30–11:30         | ⭐⭐⭐⭐  | normal |
| 11:30–13:30         | ⭐⭐ ❌  | WARN + downgrade one grade (Asian lunch chop) |
| 13:30–14:30         | ⭐⭐⭐⭐  | normal (pre-London prep; flag "London open soon") |
| 14:30–17:30         | ⭐⭐⭐⭐⭐ | boost: counts as a confluence (London open) |
| 17:30–19:30         | ⭐⭐⭐⭐⭐ | boost (London peak) |
| 19:30–22:30         | ⭐⭐⭐⭐⭐⭐| boost (LDN+NY overlap — best window) |
| 22:30–00:30         | ⭐⭐⭐⭐  | normal |
| 00:30–02:00         | ⭐⭐⭐   | strong setups only: issue A+/A, suppress B |
| 02:00–03:30         | ⭐ ❌   | BLOCK |
| Sunday (full day)   | ❌     | WARN + downgrade one grade (erratic structure) |

Session high/low of ASIA (05:30–13:30 IST), LONDON (14:30–19:30), NY
(19:30–02:00) feed the L1 liquidity map. All windows in config, not code.

---

## 6. L5 — Delivery

- `GET /api/v3/analysis?symbol&tf` → the TF's `TfRead` (chart draws it; redraw
  on TF switch).
- `GET /api/v3/map?symbol` → Market Map (stacked zones, bias ladder, liquidity
  targets, session state).
- `GET /api/v3/setups?symbol` → `{ active[], watching[], session, message }` —
  watching = the ARMED/WATCHING pipeline ("setup ban raha hai"), active =
  TRIGGERED.
- Frontend: zones/trendlines/liquidity rendered per active TF; setup card +
  strip driven by v3; one-click paper bracket unchanged.
- Old engine: disabled at composition (flag `MARKETSCALPER_ENGINE=v3`), code
  quarantined, deleted at Phase 5.

---

## 7. Non-negotiables carried forward

closed-candle-only (no repaint) · deterministic pure folds (replay-safe) ·
config over magic numbers (every threshold in `v3.*` config) · explainable
(every setup lists its rules) · decision-support only (no execution) ·
append-only DB.

---

## 8. Roadmap (each phase: design→implement→test→regression→perf→docs→commit→STOP)

- **P1 — Chart Read Engine.** L1 for 5m/15m/1h/4h/1d + `/api/v3/analysis` +
  frontend per-TF rendering of trendlines/zones/liquidity. Old overlays off.
  Gate: visually correct on real BTC/ETH charts across TFs (owner review).
- **P2 — Market Map.** Zone stacking, bias ladder, liquidity targets,
  `/api/v3/map` + map summary in UI (strip rewired to v3).
  Gate: map matches what a trader would mark on the same chart.
- **P3 — Virtual Trader + Session Timing.** State machine, 3 archetypes,
  grading, `/api/v3/setups`, setup card + watchlist UI, paper hookup.
  Gate: setups on historical days match discretionary reads; dead windows block.
- **P4 — Validation.** Replay over historical data (multi-month), false-pos/neg
  review, expectancy report per archetype/session, edge cases, perf (<300ms
  per refresh), stress.
  Gate: owner reviews the validation report.
- **P5 — Production cutover.** v3 default, old engine code + dead endpoints
  removed, docs final, deploy, prod verify, monitor.

STOP after every phase for owner approval.

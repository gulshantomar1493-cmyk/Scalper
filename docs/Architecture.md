# MarketScalper — Concrete Architecture Blueprint v1.2

**Deterministic Market Analysis & Decision-Support Terminal**
Timeframes: 1m primary, 5m context. Markets (V1): **BTCUSDT + ETHUSDT only.**
Gold/Indices architecture se removed — engine stable hone ke baad same engines reuse honge, naya design nahi.
Owner: Gulshan | Status: **FROZEN v1.2** — ab koi engine add/remove nahi. Focus = analysis quality + validation.

> **v1.2 Scope Simplification (2026-07-14, owner-approved — engines untouched):**
> MarketScalper automated trading / order-execution / broker terminal **NAHI** hai.
> Ye deterministic market analysis + decision-support platform hai. Trades hamesha
> user khud manually execute karega (Delta Exchange ya koi bhi exchange par).
> Platform ki zimmedari high-quality trade recommendation generate karne par khatam
> hoti hai: **no order placement, no trade management, no position sync, no broker
> auth.** Journal recommendation-based hai (outcome user manually log karta hai).
> Feed providers, saare analysis engines, scoring, planning, replay, journal,
> analytics — sab v1.1 jaisa hi. Sirf execution layer scope se removed.

---

## 0. Core Philosophy (Locked)

System har closed candle par 6 questions ka high-quality answer dega:

| # | Question | Engine Responsible |
|---|----------|-------------------|
| Q1 | Market kis structure me hai? | Structure Engine |
| Q2 | Liquidity kahan hai? | Liquidity Engine |
| Q3 | Strength kis side hai? | Volume + Momentum Engine |
| Q4 | Risk/Reward favorable hai? | Trade Qualification Engine |
| Q5 | Exit plan kya hai? | Trade Planning Engine |
| Q6 | Ye setup tested edge ka hissa hai? | Analytics + Journal Engine |

**Non-negotiable rules:**
1. **No repaint.** Har structural element (swing, BOS, trendline, OB) sirf confirmed closed candles par compute hoga. Jo cheez replay mein nahi dikhti, wo live mein exist nahi karti.
2. **Replay-first.** Koi bhi engine "done" nahi hai jab tak wo historical replay par bit-identical output na de.
3. **No fake confidence %.** Score = weighted rule agreement, probability nahi. Two-tier display: Data Integrity + Signal Agreement (TradeOS philosophy carry-forward).
4. **Validate before trust.** Minimum 200 logged recommendations + positive expectancy stats (fees included) se pehle koi strategy **TRUSTED** mark nahi hogi. Execution hamesha manual hai — "live execution unlock" ka concept v1.2 me exist hi nahi karta.

---

## 1. System Architecture (Runtime View)

**Runtime truth: Feed → Analysis → Strategy → Recommendation → (Manual Execution by user) → Journal.** Bas. Neeche ka diagram isi ka detail hai, isse zyada kuch nahi:

```
┌─────────────────────────────────────────────────────────────┐
│  FEED PROVIDERS (pluggable — same interface, capabilities)   │
│  BinanceFeed │ DeltaFeed(market-data only) │ ReplayFeed      │
└──────────────┬──────────────────────────────────────────────┘
               │ normalized Tick / Trade events
               ▼
┌─────────────────────────────────────────────────────────────┐
│  CANDLE BUILDER                                              │
│  ticks → 1m candles → 5m candles (aggregated, gap-safe)      │
│  emits: CANDLE_CLOSE_1M, CANDLE_CLOSE_5M, TICK               │
└──────────────┬──────────────────────────────────────────────┘
               │ EventBus (in-process asyncio pub/sub)
               ▼
┌─────────────────────────────────────────────────────────────┐
│  FEATURE ENGINES  (run sequentially on CANDLE_CLOSE)         │
│  1. Structure   (swings, HH/HL/LH/LL, BOS, CHOCH, range)     │
│  2. Trendline   (auto lines, channels, break/fake-break)     │
│  3. Liquidity   (EQH/EQL, pools, sweeps, session/day levels) │
│  4. SmartMoney  (OB, FVG, breaker, imbalance, premium/disc)  │
│  5. Volume      (RVOL time-normalized, VWAP+bands, spike,    │
│                  delta/absorption)                           │
│  + shared utilities: ATR regime, velocity, body dominance    │
│    (separate engine NAHI — sab engines inhe consume karte)   │
│  → writes to StateStore (single source of truth per symbol)  │
└──────────────┬──────────────────────────────────────────────┘
               ▼
┌─────────────────────────────────────────────────────────────┐
│  DECISION LAYER                                              │
│  Strategy Engine → Qualification (hard gates + score)        │
│  → Trade Plan (entry/SL/TP/size/fees/RR)                     │
│  → Reasoning (rule-trace, human-readable)                    │
└──────────────┬──────────────────────────────────────────────┘
               ▼
┌─────────────────────────────────────────────────────────────┐
│  RECOMMENDATION LAYER (no execution — manual workflow)       │
│  Lifecycle: active → invalidated / expired                   │
│  Hypothetical outcome evaluator (candle-based, deterministic)│
│  User executes manually on apna exchange → outcome quick-log │
└──────────────┬──────────────────────────────────────────────┘
               ▼
┌─────────────────────────────────────────────────────────────┐
│  PERSISTENCE + UI                                            │
│  PostgreSQL (candles, signals, trades, journal — append-only)│
│  FastAPI + WebSocket → Browser (Lightweight Charts v5)       │
└─────────────────────────────────────────────────────────────┘
```

**Kyun ye design:** ReplayFeed aur BinanceFeed same FeedProvider interface implement karte hain (capability flags: supports_live_data, supports_historical_data, supports_orderbook, supports_trades), isliye backtest = live pipeline. Engines kabhi nahi jaante data kis provider se aaya — normalized internal objects only, koi raw Binance/Delta JSON engine tak nahi pahunchta. Zero code duplication, zero "backtest worked but live didn't" bugs from pipeline mismatch. Naya provider = same interface implement karo, engines untouched.

---

## 2. Tech Stack (Locked Decisions)

| Component | Choice | Why |
|-----------|--------|-----|
| Language | Python 3.12 + asyncio | 1m/5m scalping mein decision candle-close par hota hai — millisecond HFT nahi. Python kaafi fast hai; aapki existing skill stack se match. |
| Backend framework | FastAPI (WebSocket + REST) | Native async, WS support built-in. Flask nahi — Flask ka WS story weak hai. |
| Feed (crypto) | Binance combined stream (`aggTrade` + `kline_1m`) — free, no auth | Fastest iteration, 24×7 market = engines din-raat test honge |
| Execution | **Manual — user khud, apne exchange par** (Delta ya koi bhi) | Platform recommendation-only. Broker auth / order APIs scope se bahar (v1.2). Delta **public** WS market-data optional (price-divergence display ke liye, no auth) |
| DB | PostgreSQL 16 (self-hosted, existing Linux server) | Partitioned candle tables; TimescaleDB optional later, abhi overkill |
| Hot state | In-process Python objects (dataclasses) | Redis NAHI Phase 1 mein — single process, single symbol set. Premature infra = death for solo builder |
| Frontend | Vanilla JS + TradingView Lightweight Charts v5 | Already adopted in TradeOS, Apache 2.0. Custom primitives API se trendlines/boxes draw honge |
| Charts overlays | LWC v5 Plugins (Custom Series + Primitives) | Trendlines, OB boxes, FVG zones, sweep markers — sab primitives se |
| Deployment | Single Linux server, single process (systemd service) — serverless/request-scoped platforms NAHI | Persistent WS connections chahiye; request-scoped serverless environments me ye break hota hai. Self-hosted Linux VPS par chalao — hosting provider = implementation decision, architecture decision nahi |
| Reasoning | Deterministic rule-trace, Phase 3. LLM (Gemini free-tier) optional Phase 5, sirf narrative polish | Ye AI system nahi hai — Deterministic Market Analysis Engine hai. Decision kabhi LLM se nahi aayega |

---

## 3. Data Model (PostgreSQL)

```sql
-- Append-only discipline (TradeOS pattern carry-forward)

CREATE TABLE candles (
  symbol      text NOT NULL,
  tf          text NOT NULL,          -- '1m' | '5m'
  ts          timestamptz NOT NULL,   -- candle open time UTC
  o numeric, h numeric, l numeric, c numeric,
  v numeric,                          -- base volume
  qv numeric,                         -- quote volume
  n_trades int,
  taker_buy_v numeric,                -- delta/aggression proxy
  PRIMARY KEY (symbol, tf, ts)
) PARTITION BY RANGE (ts);            -- monthly partitions

CREATE TABLE pivots (
  id bigserial PRIMARY KEY,
  symbol text, tf text,
  ts timestamptz,                     -- pivot candle time
  confirmed_ts timestamptz,           -- jab confirm hua (repaint audit)
  kind text,                          -- 'H' | 'L'
  price numeric,
  label text                          -- 'HH','HL','LH','LL'
);

CREATE TABLE levels (                 -- liquidity + SMC objects
  id bigserial PRIMARY KEY,
  symbol text, tf text,
  kind text,        -- 'EQH','EQL','PDH','PDL','SESSION_H','SESSION_L',
                    -- 'OB_BULL','OB_BEAR','FVG_BULL','FVG_BEAR','TRENDLINE'
  p1 numeric, p2 numeric,             -- zone top/bottom (line ke liye p1=p2)
  t1 timestamptz, t2 timestamptz,     -- trendline anchors
  slope numeric,                      -- trendline only
  touches int DEFAULT 0,
  status text DEFAULT 'active',       -- 'active','swept','mitigated','broken'
  created_ts timestamptz, status_ts timestamptz
);

CREATE TABLE signals (                -- immutable, append-only
  id bigserial PRIMARY KEY,
  ts timestamptz, symbol text, tf text,
  strategy text,
  direction text,                     -- 'LONG'|'SHORT'
  score numeric,
  gates jsonb,                        -- har gate ka pass/fail
  components jsonb,                   -- structure:91, liquidity:95 ...
  state_snapshot jsonb,               -- full StateStore dump (forensics)
  engine_version text                 -- hash-freeze discipline
);

CREATE TABLE recommendations (        -- core immutable; sirf status/eval columns update hote
  id bigserial PRIMARY KEY,
  signal_id bigint REFERENCES signals(id),
  ts timestamptz,
  direction text,                     -- 'LONG'|'SHORT'
  entry_px numeric, sl numeric, tp1 numeric, tp2 numeric,
  suggested_qty numeric, risk_amt numeric, est_fees numeric,
  net_rr_tp1 numeric,
  status text DEFAULT 'active',       -- 'active','invalidated','expired','evaluated'
  status_ts timestamptz, status_reason text,
  -- hypothetical outcome: candle-based evaluator (execution NAHI — pure analysis)
  eval_outcome text,                  -- 'tp1','tp2','sl','none'
  eval_r numeric,
  eval_mae numeric, eval_mfe numeric  -- aapka MAE/MFE analysis pattern, candles se
);

CREATE TABLE journal (                -- recommendation-based; outcome MANUAL entry
  recommendation_id bigint PRIMARY KEY REFERENCES recommendations(id),
  reason_text text,                   -- rule-trace explanation (AUTO)
  chart_snapshot_path text,           -- PNG at recommendation (AUTO)
  taken boolean,                      -- Taken / Skipped   (MANUAL)
  result text,                        -- 'win','loss','be' (MANUAL; NULL if skipped)
  actual_entry numeric, actual_exit numeric,  -- (MANUAL, optional)
  actual_pnl numeric, actual_r numeric,       -- (MANUAL, optional)
  rule_violations jsonb,              -- psychology layer
  notes text,                         -- user notes (MANUAL)
  tags text[]
);
```

---

## 4. Engine Logic — Actual Algorithms

### 4.1 Candle Builder

```
on aggTrade(price, qty, ts, is_buyer_maker):
    bucket = floor(ts / 60s)
    if bucket != current.bucket:
        emit CANDLE_CLOSE_1M(current)      # only place candles close
        if bucket % 5 boundary: roll 5m aggregate, emit CANDLE_CLOSE_5M
        open new candle
    update o/h/l/c, v += qty, taker_buy_v += qty if !is_buyer_maker

Gap safety: agar WS drop ho, reconnect ke baad missing 1m klines
REST se backfill karo BEFORE resuming engines (stale-state poison prevention).
```

### 4.2 Structure Engine (Q1)

**Pivot detection — k-bar confirmation (k=3 for 1m, k=2 for 5m):**

```
candle[i] is SWING HIGH iff:
    high[i] > high[i-1..i-k]  AND  high[i] > high[i+1..i+k]

⇒ Swing high at bar i CONFIRMS at bar i+k. Lag accepted; repaint rejected.
Store both ts (pivot location) and confirmed_ts (jab actionable hua).
```

**Label state machine:**

```
on new confirmed swing:
    if kind == H:  label = HH if price > last_H.price else LH
    if kind == L:  label = HL if price > last_L.price else LL

TREND STATE:
    BULLISH  = last two labels contain HH + HL sequence
    BEARISH  = LH + LL sequence
    RANGE    = alternating / overlap > 60% of last 20-bar range

BOS  (continuation): 1m CLOSE beyond last confirmed swing in trend direction
CHOCH (reversal warn): first CLOSE beyond last confirmed swing AGAINST trend
    → CHOCH alone ≠ reversal. CHOCH + opposite BOS = confirmed flip.

DISPLACEMENT filter (impulse vs drift):
    breaking candle body > 1.2 × ATR(14)  → "displacement BOS" (strong)
    else "weak BOS" (lower score weight)

Compression: ATR(14) < 0.6 × ATR(14, 5m context)  → coil state
Expansion:   ATR(14) > 1.5 × rolling median ATR   → active state
```

### 4.3 Trendline Engine (aapka explicit core requirement)

Auto-drawn, validated, break-classified. Algorithm:

```
INPUT: last N=12 confirmed swing pivots (same kind — highs for
       descending resistance, lows for ascending support)

STEP 1 — Candidate generation:
    for every pair (pivot_a, pivot_b) where b newer than a:
        line = through(a, b)          # log-price space for crypto
        if direction invalid, skip    # support line slope from lows
                                      # must not cut through closes between a,b

STEP 2 — Touch validation:
    tolerance = 0.15 × ATR(14)
    touches = count of candles whose low (support) / high (resistance)
              comes within tolerance of line WITHOUT close crossing it
    keep lines with touches ≥ 3

STEP 3 — Scoring & dedup:
    line_score = touches × 2 + span_bars/20 − age_penalty
    cluster near-parallel lines (slope Δ < 10%, intercept Δ < 0.3×ATR)
    → keep best per cluster, max 3 active support + 3 resistance

STEP 4 — Break classification (scalper-critical):
    TOUCH      : price within tolerance, no close beyond      → bounce setup
    BREAK      : 1m close beyond line + body > 0.8×ATR
                 + RVOL ≥ 1.5                                 → breakout setup
    FAKE BREAK : close beyond line, then within 3 candles
                 close back inside                            → sweep/trap setup
    → FAKE BREAK is often the BEST scalp signal (liquidity grab)

STEP 5 — Lifecycle:
    broken lines → status='broken', role-flip candidate (old support = new resistance)
    lines older than 300 bars without touch → archive

CHANNELS: agar parallel support+resistance pair (slope Δ<8%) dono ≥3 touches
    → channel object; mid-line = mean-reversion reference
```

### 4.4 Liquidity Engine (Q2)

```
EQUAL HIGHS/LOWS:
    cluster confirmed pivots: |p_i − p_j| < 0.1 × ATR(14)
    2+ pivots in cluster = liquidity pool (stops resting beyond it)
    pool_strength = cluster size × recency weight

KEY LEVELS (auto-tracked, refreshed daily):
    PDH/PDL (previous day), PWH/PWL (week),
    session H/L (crypto sessions: Asia 00-08 UTC, London 08-13, NY 13-21),
    current day H/L

SWEEP DETECTION (the money pattern):
    given active pool/level P (say a high):
    SWEEP = candle high > P
            AND close < P                      # wick through, body rejected
            AND (RVOL ≥ 1.5 OR wick > 60% of candle range)
    within next 3 candles: agar CHOCH confirm ho jaaye
    → "sweep + shift" = A+ reversal context

PREMIUM/DISCOUNT:
    range = last confirmed swing high ↔ swing low (external structure)
    price in upper 50% = premium (longs discouraged, shorts favored)
    price in lower 50% = discount (mirror)
```

### 4.5 Smart Money Engine

```
ORDER BLOCK:
    on displacement BOS (body > 1.2×ATR breaking structure):
    OB = last opposite-color candle before the impulse leg
    zone = [OB.open, OB.high] (bearish OB) / [OB.low, OB.open] (bullish)
    status: active → mitigated (first revisit) → broken (close through)
    Sirf UNMITIGATED OB tradeable. Mitigated = weight 0.

FVG (3-candle imbalance):
    bullish FVG: candle1.high < candle3.low  → gap = [c1.high, c3.low]
    bearish FVG: candle1.low  > candle3.high
    minimum gap size: 0.3 × ATR (noise filter — 1m par ye critical hai)
    fill tracking: 50% fill = "CE tested", full fill = archived

BREAKER: OB that failed (broken through) → flips role, becomes
    opposite-direction zone on retest.

CONFLUENCE STACKING (scoring input):
    zone_quality = overlap count of {OB, FVG, trendline, EQH/EQL,
                   VWAP band, session level} within 0.3×ATR band
    3+ overlapping objects = "HTF magnet zone"
```

### 4.6 Volume Engine (Q3a)

```
RVOL (time-of-day normalized — aapka TradeOS pattern, 1m version):
    rvol[i] = volume[i] / median(volume at same minute-of-day, last 20 days)
    (crypto 24×7: minute-of-day buckets; indices: minute-of-session)

VWAP: session-anchored (daily reset 00:00 UTC crypto / 09:15 IST indices)
    vwap = Σ(typical_price × vol) / Σ(vol)
    bands at ±1σ, ±2σ (σ of price-vwap deviation, volume-weighted)

ANCHORED VWAP: auto-anchor at last confirmed major swing (external structure)

DELTA/AGGRESSION (Binance gives taker_buy_volume free):
    delta[i] = taker_buy_v − (v − taker_buy_v)
    cum_delta per session
    ABSORPTION: high volume + high |delta| + small candle range
                (< 0.5×ATR) at a key level = passive player absorbing
                → reversal warning at that level

VOLUME SPIKE: rvol ≥ 2.0  |  EXHAUSTION: spike + long wick + at range extreme
```

### 4.7 Momentum Utilities (shared metrics — separate engine NAHI)

```
ATR(14) on 1m + 5m; regime = expansion / normal / compression (§4.2)
velocity     = EMA(close-to-close change, 5)
acceleration = Δ velocity
momentum_shift = velocity sign flip WITH |acceleration| > threshold
Body dominance = avg(body/range, last 5 candles)  → conviction proxy

Ye ek utility module hai jise Structure/Trendline/Volume/Qualification
sab consume karte hain. Context Engine (funding/OI/liquidations) v1 se
REMOVED — queued for post-P6, same plug interface par.
```

---

## 5. Strategy Engine — Launch Set (3 strategies, not 10)

Solo builder rule: **3 strategies deeply validated > 10 shallow.** Baaki 7 aapke doc se Phase 5+ mein add honge, same template par.

### S1 — Liquidity Sweep Reversal (flagship)

```
CONTEXT (5m): trend ya range extreme par ho
SETUP:  sweep of EQH/EQL/PDH/PDL/session-level detected (§4.4)
CONFIRM: within 3×1m candles → CHOCH on 1m
         + entry zone confluence ≥ 2 (OB/FVG/VWAP band overlap)
ENTRY:  limit at OB/FVG 50% level, ya market on CHOCH-candle close
SL:     beyond sweep wick + 0.25×ATR buffer
TP1:    nearest opposing liquidity pool (1R minimum required)
TP2:    external structure target
INVALID: agar entry 5 candles mein fill na ho → cancel
```

### S2 — Trend Pullback Continuation

```
CONTEXT (5m): confirmed trend (BOS chain) + price above/below session VWAP
SETUP:  1m pullback into: unmitigated OB ∪ FVG ∪ VWAP ∪ trendline
        pullback depth 30–70% of last impulse (fib zone equivalent)
        pullback on DECLINING rvol (healthy)
CONFIRM: 1m close back in trend direction with body > 0.8×ATR + rvol ≥ 1.2
ENTRY:  confirm-candle close
SL:     below pullback low − 0.25×ATR
TP1:    previous impulse high (1R min) | TP2: 1.618 extension / next pool
```

### S3 — Trendline Fake-Break Trap

```
SETUP:  validated trendline (≥3 touches) par FAKE BREAK classified (§4.3)
CONFIRM: re-entry close + rvol ≥ 1.5 + no opposing HTF level within 1R
ENTRY:  re-entry candle close
SL:     beyond fake-break extreme + 0.25×ATR
TP:     opposite side of channel / last swing
```

---

## 6. Trade Qualification Engine (Q4) — Scoring Math

**Stage 1 — HARD GATES (binary, koi score compensate nahi kar sakta):**

```
G1  Data integrity: feed live, no gap in last 30 candles, clock sync < 2s
G2  Spread/liquidity: spread < 0.05% (crypto majors)
G3  Session filter: strategy-allowed session only
G4  News blackout: ±5 min of high-impact scheduled events (crypto: FOMC/CPI)
G5  Risk budget: daily logged loss < limit (journal se), active recommendations < max, no revenge-trade flag
G6  RR floor: plan RR to TP1 ≥ 1.0, to TP2 ≥ 1.5
ANY FAIL → NO SIGNAL. Score kabhi display nahi hota gate-fail par.
```

**Stage 2 — WEIGHTED SCORE (0–100):**

```
score = 0.30×Structure + 0.30×Liquidity + 0.25×Volume + 0.15×Momentum
        (Context component removed with engine — weights rebalanced)

Har component ki apni rubric, e.g. Liquidity:
    sweep of multi-touch pool        +40
    sweep with CHOCH confirm         +30
    entry zone confluence ≥2 objects +20
    opposing pool distance ≥ 1.5R    +10

DISPLAY (two-tier, no fake %):
    Data Integrity : PASS/DEGRADED  (gates G1–G2)
    Signal Agreement: 87/100 — "9 of 11 rules aligned"
Tradeable threshold: ≥ 75. A+ setup: ≥ 85.
```

---

## 7. Trade Planning + Recommendation Lifecycle (Q5)

```
PLANNING (suggested values — user apne exchange par manually lagayega):
    risk_amt  = equity × 0.5%              # scalping: 0.25–0.5% per trade
    qty       = risk_amt / |entry − sl|    # SUGGESTED qty (display only)
    fees      = qty × entry × taker_fee × 2   # Delta ~0.05% taker/side
    net RR    = (tp − entry − fee_per_unit) / (entry − sl + fee_per_unit)
    reject if net RR(TP1) < 1.0            # fees included — sach wala RR

RECOMMENDATION LIFECYCLE (execution NAHI — analysis only):
    INVALIDATION → strategy ka INVALID rule (e.g. entry zone 5 candles me
                   touch na ho), opposite-direction signal, ya G1 data-
                   integrity fail → status='invalidated' + reason
    EXPIRY       → 15 min (1m setups) me thesis play na ho → 'expired'
    OUTCOME EVAL → hypothetical, candle-based: SL ya TP pehle touch hua?
                   same-candle ambiguity = SL-first (worst case, conservative)
                   eval_r / eval_mae / eval_mfe candles se — deterministic,
                   replay aur live-forward dono me bit-identical

SUGGESTED MANAGEMENT (display-only guidance — user manually kare):
    +1R par SL→BE | TP1 par 50% off, baaki trail at last confirmed 1m swing
    15-min time stop | opposite displacement candle > 1.5×ATR par exit
    (recommendation card par as text; platform kuch execute nahi karta)
```

---

## 8. Reasoning + Journal + Psychology (Q6)

```
REASONING = deterministic rule-trace (LLM decision path mein NAHI):
    "LONG BTCUSDT @ 67,215 | S1 Sweep Reversal | Score 88
     ✓ Swept Asia session low (3-touch pool) with 68% wick
     ✓ CHOCH confirmed +2 candles, displacement 1.4×ATR
     ✓ Entry = bullish OB ∩ FVG ∩ VWAP −1σ (3-object confluence)
     ✓ RVOL 2.3 on confirm candle | ✓ ATR regime: expansion
     Risk: SL 0.31% below sweep wick | Net RR 1.7 to TP1"
    (Phase 5 optional: Gemini free-tier isko narrative polish de — facts
     sirf rule-trace se, grounding guardrail AdmissionOS pattern)

JOURNAL (recommendation-based — context AUTO, outcome MANUAL):
    recommendation par AUTO: chart PNG snapshot (LWC takeScreenshot →
    server save), full rule-trace, state_snapshot JSON, score
    trade ke baad user MANUALLY log kare: Taken/Skipped, Win/Loss/BE,
    actual entry/exit (optional), notes, tags
    evaluator alag se hypothetical outcome record karta hai →
    "system kya keh raha tha" vs "user ne actually kya kiya" comparison

PSYCHOLOGY GUARDS (rule-based, judgmental nahi):
    revenge flag   : new signal < 5 min after logged loss, same symbol → G5 fail
    overtrade flag : > 6 taken-trades/day (journal se) → warn; > 8 → hard lock till next day
    violation log  : recommendation guidance se manual deviation → journal tag (self-report)
```

---

## 9. Frontend (Lightweight Charts v5 + Vanilla JS)

```
LAYOUT (MarketScalper terminal — TradeOS design DNA):
┌────────────────────────────────────────────┬───────────────┐
│  1m CANDLE CHART (LWC v5)                  │ QUALITY PANEL │
│  overlays via Primitives plugin API:       │  Score gauge  │
│   • trendlines/channels (custom primitive) │  (arc, count- │
│   • OB/FVG boxes (rect primitive)          │   up — reuse  │
│   • EQH/EQL + session levels (price lines) │   TradeOS)    │
│   • sweep markers, BOS/CHOCH labels        │  Gates list   │
│   • VWAP + bands (line series)             │  Components   │
│   • entry/SL/TP rail on active reco        ├───────────────┤
│  5m mini-chart (context strip) below       │ TRADE PLAN    │
├────────────────────────────────────────────┤  rail (reuse  │
│  ACTIVE RECOMMENDATION BAR: entry | SL |   │  TradeOS Rail)│
│  TP1/TP2 | status | invalidation timer     │               │
├────────────────────────────────────────────┼───────────────┤
│  QUICK LOG (manual journal, one-tap):      │ REASON TRACE  │
│  Taken/Skipped | Win/Loss/BE | px | notes  │ + journal tab │
└────────────────────────────────────────────┴───────────────┘

DATA FLOW: FastAPI WS pushes {candle, state_diff, signal, recommendation}
    → frontend renders diffs only (no full redraws)
DESIGN TOKENS: TradeOS locked set reuse — #0A0F1E surfaces, hairline
    rgba(255,255,255,0.14), cyan #22D3EE accent, semantic green/red,
    tabular mono for every number, radius 12px.
```

---

## 10. Replay Engine (build FIRST, not last)

```
ReplayFeed implements same FeedProvider interface as live providers:
    reads historical 1m klines (Binance REST bulk download → Postgres)
    emits CANDLE_CLOSE events at speed × {1, 10, 60, max}
    → ENTIRE pipeline (engines, scoring, recommendations, outcome
      evaluator) runs unchanged — no replay broker, no replay execution

USES (learning, validation, testing, strategy improvement — bas):
    1. Engine validation: run 90 days, eyeball 200 random trendlines/OBs/sweeps
    2. Strategy stats: expectancy, win rate, PF, MAE/MFE distribution
       (hypothetical outcome evaluator se — execution nahi)
    3. UI replay mode: candle-by-candle step-through with AI decisions
       overlaid — "learning mode" from your Layer 17

DETERMINISM TEST (CI gate): same input candles → byte-identical
signals table. Fail = repaint bug somewhere. Non-negotiable.
```

---

## 11. Phased Execution Plan (Sign-off Gates)

**P0 — Spine (Week 1–2)**
Binance WS feed adapter + candle builder + gap-safe backfill + Postgres schema + FastAPI WS + live 1m chart in LWC v5 + ReplayFeed skeleton.
✅ Gate: live BTCUSDT 1m chart browser mein, replay mode 90 days playback, zero candle mismatch vs Binance official klines.

**P1 — Structure + Trendlines (Week 3–4)**
Pivot detection, HH/HL labeling, BOS/CHOCH state machine, full trendline engine (§4.3), chart overlays.
✅ Gate: 30-day replay par 50 random trendlines manually audit — ≥80% "main bhi yahi line kheenchta". Determinism test pass.

**P2 — Liquidity + SMC + Volume (Week 5–7)**
EQH/EQL, session/day levels, sweep detection, OB/FVG lifecycle, RVOL time-normalized, VWAP+bands, delta/absorption.
✅ Gate: replay overlay audit of 50 sweeps + 50 OBs; false-positive rate visually acceptable; sab objects lifecycle correctly transition karein.

**P3 — Scoring + Plan + Reasoning (Week 8–9)**
Hard gates, weighted score, S1/S2/S3 strategies, trade planner with fee-adjusted RR, rule-trace reasoning, quality panel UI.
✅ Gate: replay 90 days → signals table generated; manual review of top-20 & bottom-20 scored setups confirms score ordering makes sense.

**P4 — Recommendations Forward-Run + Journal + Analytics (Week 10–11)**
Recommendation lifecycle (invalidation/expiry), candle-based hypothetical outcome evaluator (SL-first worst case), auto-context journal (snapshot + rule-trace), manual quick-log UI (Taken/Skipped, Win/Loss, notes), analytics dashboard (manual + hypothetical expectancy, PF, MAE/MFE, per-strategy/per-session breakdown, system-vs-actual comparison).
✅ Gate: 2 weeks live forward-run, ≥60 recommendations generated, har ek ka evaluator outcome recorded + jitne taken hue sab ka manual log complete.

**P5 — Validation Campaign (Week 12–15)**
200+ forward recommendations. Data-backed decisions: SL formula tuning via MAE distribution (aapka ~25 July TradeOS scorecard pattern), strategy kill/keep, threshold calibration. Add strategies 4–6 only if S1–S3 positive expectancy dein.
✅ Gate: positive expectancy after fees (hypothetical evaluator basis) on ≥1 strategy over 200 recommendations → wo strategy **TRUSTED**. **Ye gate fail ho to us strategy par bharosa nahi — period.**

**P6 (OPTIONAL) — Delta Market Data (Week 16+)**
DeltaFeed: Delta Exchange **public** WS market data (no auth, no orders) — same FeedProvider interface. Binance↔Delta price-divergence display (manual execution helper jab user Delta par trade kare).
✅ Gate: DeltaFeed FeedProvider conformance suite pass.

**Explicitly OUT of v1 (queued, not deleted):** Gold feed, index options (SEBI algo compliance + Greeks), funding/OI context module, 7 additional strategies, LLM narrative polish, multi-symbol scanning (v1 = BTC + ETH only), mobile view. Future markets = feed provider swap + same engines. Naya architecture round kabhi nahi.
**Permanently OUT of scope (v1.2 decision):** automated/live order execution, broker integration, position management, order APIs — MarketScalper decision-support platform hai aur rahega. Agar kabhi execution add karna ho to wo alag, explicit project-level decision hoga.

---

## 12. Risk Register (Honest)

| Risk | Mitigation |
|------|-----------|
| 1m noise → engines spam false objects | ATR-scaled thresholds everywhere; min-size filters (FVG ≥0.3×ATR); confluence requirement before signal |
| Repaint creeps in via "small optimization" | Determinism CI test on every engine change; confirmed_ts audit column |
| Scope explosion (17 layers ka gravity) | v1 = 2 symbols, 3 strategies, crypto only. Har addition needs P5-style validation |
| Hypothetical vs actual gap (evaluator optimistic) | SL-first worst-case rule on same-candle ambiguity; journal me system-vs-actual comparison view |
| WS disconnect → stale recommendation | Heartbeat monitor; G1 gate blocks new signals; active recommendation auto-invalidate on feed gap |
| User discipline gap (recommendation follow na karna) | Manual journal + psychology guards + "system vs actual" analytics — data se dikhega |
| Overfitting thresholds to 90-day replay | Locked out-of-sample holdout month (TradeOS anti-overfit discipline) |
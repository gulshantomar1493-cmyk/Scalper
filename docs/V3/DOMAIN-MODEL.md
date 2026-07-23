# MarketScalper V3 — Core Domain Model (design-before-code)

Owner condition for Phase 1: objects first, implementation after. This file is
the object contract. Code must mirror it 1:1 (same names, states, transitions).

---

## Philosophy (engine-wide, non-negotiable)

```
MarketScalper never predicts.
It observes. It maps. It waits. It reacts. It recommends.

Observe → Understand → Wait → Confirm → Recommend → Manage → Learn
   (never:  Detect → Score → Recommend)
```

---

## 0. Base contract — every domain object

```
DomainObject:
  id            stable string  (e.g. "BTCUSDT:1h:zone:184")
  symbol, tf
  created_at    bar ts that created it (closed candles only)
  state         current lifecycle state (enum per type)
  history[]     append-only: (ts, event, reason)   ← the self-explanation
```

`history` answers, for any object at any time: why created · why still active ·
why state changed · why used in a setup · why ignored. Every state transition
MUST append a history entry with a human-readable reason. No silent mutation.

**Update model = state machine, not re-scan.** On each closed candle of a TF,
objects FOLD the new bar into their state (incremental, deterministic,
replay-safe). Nothing is recomputed from scratch; a full rebuild exists only as
a cold-start path and must produce byte-identical state (checked in tests).

---

## 1. Swing

```
Swing { kind: HIGH|LOW · price · ts · confirmed_ts · label: HH|HL|LH|LL|None }
state: CONFIRMED (immutable once labeled)
```
Parent of: structure events, trendline anchors, S/R clusters, equal-H/L pools.

## 2. Structure (one per symbol×tf)

```
Structure {
  trend: BULLISH|BEARISH|RANGE
  swings: chain refs
  last_bos:  { ts, direction, displaced: bool, broken_swing→Swing }
  last_choch:{ ts, direction, broken_swing→Swing }
}
```
Trend from swing labels only (HH+HL / LH+LL). Displacement = body > 1.2×ATR(tf).

## 3. Trendline

```
Trendline {
  side: SUPPORT|RESISTANCE · anchors: [→Swing] · slope(log) · touches: [(ts, price)]
  state: NEW → VALID → STRONG
                 ↓ touch violated (wick-through, close back)
               WEAK
                 ↓ decisive close through (+displacement)
               BROKEN → role-flip candidate (side inverted, once) → INVALID
  role_flipped: bool · broken_at
}
```
Only VALID/STRONG produce zones/setups. WEAK = warn only. History logs every
touch, violation and the break reason.

## 4. Zone

```
Zone {
  kind: SR | DEMAND | SUPPLY | ORDER_BLOCK | FVG | TRENDLINE
  band: [lo, hi] · origin: refs (swings / impulse leg / source candle / line)
  state: FRESH → TESTED → WEAK → BROKEN → RETIRED
                             ↓ (broken with displacement)
                          ROLE_FLIP (demand⇄supply, once) → new Zone FRESH
  touches: [(ts, reaction: HELD|PIERCED)] · flipped_from: →Zone|None
  invalidated_at · max_age(tf) enforced → RETIRED
}
```
Touch decay: 0 = FRESH (strongest) · 1st retest tradeable · 2nd caution ·
3rd+ → WEAK. RETIRED zones leave the live map (archive keeps them for memory).

## 5. LiquidityPool

```
LiquidityPool {
  kind: PWH|PWL|PDH|PDL|EQH|EQL|SESSION_H|SESSION_L|INTERNAL|MINOR
  price(band) · side: BUYSIDE(above)|SELLSIDE(below)
  priority: ★1..5   (PWH/PWL,PDH/PDL=5 · EQH/EQL=4 · SESSION=3 · INTERNAL=2 · MINOR=1)
  state: UNSWEPT → SWEPT
  swept_at · post_sweep: PENDING → REVERSED|CONTINUED   (resolved N bars later)
  members: [→Swing]
}
```
TP selection prefers highest-priority UNSWEPT in direction. SWEPT = devalued
target; a ★4+ sweep INTO a zone = premium reversal confluence. post_sweep
outcomes feed MarketMemory.

## 6. MapZone (L2 merge)

```
MapZone { components: [→Zone (any tf)] · tf_stack · band(merged) · weight }
```
Zones overlapping within 0.3×ATR(higher tf) merge. weight = stack depth +
component states (FRESH counts more) + trendline/liquidity confluence refs.

## 7. BiasLadder (L2)

```
BiasLadder { per_tf: {1d,4h,1h,15m,5m → trend} · overall: vote(1d:4,4h:3,1h:2,15m:1) }
```
Structure-only votes. Indicators never vote.

## 8. MarketMemory (L3)

```
MarketMemory {
  day_profile: yesterday {OHLC, range, direction, driving_session}
  sessions: {ASIA, LONDON, NY → {high, low, range, swept_by: session|None}}
  weekly: {PWH, PWL, week_open, month_open, position_in_week_range}
  zone_history: [(→Zone, outcome: STRONG_REACTION|WEAK|FAILED)]
  sweep_history: [(pool kind, outcome REVERSED|CONTINUED)]  (rolling N)
}
```
Memory adjusts weights/context only. It NEVER creates a setup.

## 9. Setup (full lifecycle — feeds journal + replay automatically)

```
Setup {
  archetype: REVERSAL|BREAKOUT|BREAKDOWN · direction: LONG|SHORT
  map_zone: →MapZone · entry · sl · tp1 · tp2 · rr_net(≥1.5, fees×2)
  confluence: [named factor + object ref]      ← the confluence graph
  grade: A+|A|B  (count: A+≥5 · A≥3 · B≥2; <2 never issued)
  avoid_reasons[] · invalidation · management[] · session_window
  state machine:
    WATCHING → ARMED → TRIGGERED → LIVE → TP1_HIT → TP2_HIT → CLOSED → ARCHIVED
       │         │         │         └─ SL → STOPPED ───────────────┐
       │         │         └─ entry not filled in N bars → EXPIRED ─┤
       │         └─ zone violated / session block → CANCELLED ──────┤
       └─ price left without arming → CANCELLED ────────────────────┴→ ARCHIVED
  transition timestamps (each state) · outcome {r_multiple, hold_time, mae, mfe}
}
```
Every transition appends history with the reason (e.g. "ARMED→CANCELLED: zone
broken with displacement 14:32"). ARCHIVED setups are persisted — journal,
replay reports and the engine-vs-trader benchmark all read the archive.

## 10. SessionClock (L5)

```
SessionClock { window(IST) · rating ⭐ · effect: NORMAL|BOOST|WARN_DOWNGRADE|STRONG_ONLY|BLOCK
               sunday_rule · session bounds ASIA/LONDON/NY (feeds pools + memory) }
```

---

## Relationships (ownership → reference)

```
Market(symbol)
 ├─ TimeframeRead ×{5m,15m,1h,4h,1d}
 │    ├─ Swing*  ── anchors ──▶ Trendline*
 │    ├─ Structure (BOS/CHOCH ref Swings)
 │    ├─ Zone*   (origin: Swings / impulse / candle / Trendline)
 │    └─ LiquidityPool* (members: Swings)
 ├─ MarketMap: MapZone* (components: Zones across TFs) + BiasLadder + targets
 ├─ MarketMemory (reads pools/zones outcomes; session bounds from SessionClock)
 └─ VirtualTrader: Setup* (map_zone: MapZone; confluence refs objects by id)
```
References are by id (loose coupling); ownership is strictly downward; no
object mutates another — each folds its own state from candles + referenced
ids.

---

## Update flow (per closed candle of a TF — the only trigger)

```
1m close:   Setup state machines step (confirmation, entry, SL/TP tracking)
tf close:   Swings fold → Structure fold → Trendlines update → Zones update
            → Pools update (swept?) → affected MapZones re-merge
day/session boundary: SessionClock + MarketMemory roll
```
No wall-clock, no randomness inside folds → deterministic, replay = live.

---

## P4 addition — Engine-vs-Trader benchmark (the top KPI)

A labeled set: owner marks the valid setups on N historical charts; the replay
engine runs the same charts. Report: `coverage = engine-found / trader-marked`,
plus extra setups the trader would reject (precision). Target: coverage ≥90%
with precision the owner accepts. This KPI ships with the P4 replay report.

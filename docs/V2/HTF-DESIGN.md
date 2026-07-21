# HTF Analysis — Professional Design (Phase 2.1)

**Status:** implemented + frozen for V2. Supersedes the HTF V1.1 scoring.
**Module:** `core/htf.py` (engine-isolated read-model; off the determinism stream).

## The principle (why this changed)

The V2 audit found the HTF **bias** was set by a weighted score in which **EMA
(±2) and momentum (±0.5)** could *move direction* — an indicator could flip a
timeframe bullish/bearish. That violates the project law: **price action
determines direction; indicators only confirm.** It also produced contradictions
("Daily uptrend, bearish bias") and a fabricated 0–100 score.

## The rule

> **Direction comes only from price action. Indicators may only raise or lower
> conviction. An indicator can never flip a bias.**

## Bias (direction) — price action only

Per timeframe, in priority order (`_pa_bias`):

1. **Market structure.** Clean **HH + HL → BULLISH**; **LH + LL → BEARISH**.
2. **Structural events break a mixed/forming structure.** The most recent
   **BOS** (then **CHOCH**) gives the lean.
3. Otherwise **NEUTRAL** — no clean read, no forced bias. (No EMA/momentum rescue,
   which is exactly what the old logic did.)

The displayed **trend** is now simply the bias (Uptrend / Downtrend / Range) — it
can never contradict it.

## Conviction (strength) — confirmations only

Once direction is fixed by price action, `_conviction_fraction` counts how many
**confirmations** agree (each once), then labels it **STRONG / MODERATE / WEAK**:

- price action: BOS in-direction · **no opposing CHOCH** · price reacting at the
  right supply/demand zone;
- indicators (confirmation only): EMA stack · momentum · the 200-EMA side.

A bullish structure with bearish EMA + down momentum stays **BULLISH** — just
**WEAK** conviction. Indicators are never in the direction decision.

## Overall roll-up — a timeframe-weighted vote

`aggregate_htf` takes a **weight-weighted vote** of the per-timeframe biases
(1d=4, 4h=3, 1h=2, 15m=1). The heavier side wins; a tie is NEUTRAL. **Confidence**
is the fraction of weight that agrees (a real emergent number, not a score);
**conviction** is the weighted confirmation strength of the agreeing timeframes.

## Market story — a narrative, not a summary

Consistent (trend == bias), and a CHOCH against the bias is surfaced as a
**caution** ("recent CHOCH down, moderate conviction"), never as a contradiction.
The deep who-controls / draw / trapped narrative lives in the Trade Engine
(`setup_engine`), which has the LTF liquidity the HTF read lacks.

## Output (updated contract)

Per timeframe: `bias`, `conviction`, `trend`, `structure`, `bos`, `choch`,
swings, liquidity, supply/demand, S/R, trendlines, `ema_alignment`, `momentum`.
**Removed:** `score`, `_signed`. Overall: `bias`, `conviction`, `confidence`,
`market_story`, `explanation`. Frontend (`htf.js`) renders the conviction level +
agreement % instead of the old `score/100`.

## Why simpler

No weighted magic numbers, no clamping, no fabricated percentage. Direction is a
structure read; conviction is a count; the roll-up is a vote. Fewer rules, and
every one is defensible to a trader.

## Guarantees

- **Indicators can never flip direction** (unit-tested: bullish structure +
  every indicator bearish → still BULLISH, weak conviction).
- Engine-isolated → **determinism V1–V4 byte-identical**; the frozen analysis
  engines are untouched (this is a read-model over them).
- Deterministic / pure (same candles → same result).

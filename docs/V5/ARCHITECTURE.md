# MarketScalper V5 — Price-Action Engine

**Status:** DESIGN + BUILD, 2026-07-29. Replaces the V4 *strategy* layer.
V4's plumbing (candles, ChartService, outcome accounting, persistence, alerts,
paper trading) is kept unchanged.

---

## 1. The failure being fixed

V4 resolved 11 live trades. **All 11 stopped out. Zero targets.** Average
−1.135R (worse than −1 because fees are charged on top). Ten of the eleven were
SHORT while price rallied.

The diagnosis is not "the stops were too tight" — it is in the code.
`v4/signals.py` decides direction like this:

```python
direction = 1 if lvl >= eval_bars["c"][i] else -1
```

Direction is **derived from where a level sits relative to price**, not from any
view of the market. Every bar computes a donchian high *and* a donchian low, so
the engine issues a long and a short on the same symbol at the same moment, from
the same strategy. `min_filters=1` on three of the six strategies then lets a
single weak agreement authorise the trade — and `structure_trend` flips to −1 on
any close below a swing low, which happens constantly inside an uptrend's
pullbacks. That is how ten shorts got issued into a rally.

The second half of the failure is the geometry. The max favourable excursion of
the eleven losers was 2.91R once, and 0.58–1.54R for the rest: **nine of eleven
went at least 0.5R into profit and then reversed**. A 10R target cannot be paid
by a market that hands you 0.8R. But lowering the target alone does not save it
— at 1:2 only two would have won (2×2 − 9×1.1 = −5.9R). **The entries have to
change too**, and that is what V5 is.

---

## 2. The market model

Computed on every closed candle, per symbol. This vocabulary is exactly what the
virtual trader narrates with — if the engine cannot name it, it cannot say it.

| Term | Timeframe | Definition |
|---|---|---|
| `swing_high` / `swing_low` | 4H, 1H, 15m | fractal: the bar's high is the strict maximum of the 2 bars either side (k=2). Confirmed only at bar i+k — never repaints. |
| `leg` | per TF | the labelled sequence of swings: HH, HL, LH, LL |
| `structure` | per TF | `BULLISH` (last high is HH **and** last low is HL), `BEARISH` (LH **and** LL), else `RANGE` |
| `bos` | per TF | close strictly beyond the last confirmed swing **in** the structure's direction |
| `choch` | per TF | close strictly beyond the last confirmed swing **against** it — the first warning of a change |
| `efficiency` | 4H | Kaufman ratio over 20 bars: `abs(c[n]−c[n−20]) / sum(abs(diff))`. ≥0.35 = TRENDING, ≤0.20 = RANGING, between = MIXED |
| `demand` / `supply` zone | 1H | the last opposite-colour candle before the impulse that produced a BOS; the zone is that candle's body-to-wick range |
| `htf_level` | 1D/1W | prior-day high/low, prior-week high/low, round numbers |
| `trendline` | 4H | the ray through the last two same-side swings, projected to the current bar |

Everything is causal: a value at bar *i* uses only bars ≤ *i*, and swings are
confirmed *k* bars late by construction.

---

## 3. The decision procedure

Run per symbol, in this exact order. Any step may stop with a **named reason** —
that reason is what the virtual trader reports when there is no trade.

1. **Warm-up.** Need ≥60 closed bars on 4H, 1H, 15m and 5m. Else
   `NOT_ENOUGH_DATA`.
2. **Daily bias.** Read 1D structure.
   - BULLISH → `bias = LONG`; BEARISH → `bias = SHORT`;
   - RANGE → fall through to 4H structure for the bias.
   - If 4H is also RANGE → `bias = NONE`, reason `NO_DIRECTIONAL_EDGE`.
3. **Conflict gate.** If 4H structure is the **opposite** of the daily bias →
   stop with `TIMEFRAME_CONFLICT`. This is the rule that makes ten shorts in a
   rally impossible: the daily was bullish the whole time.
4. **Regime.** Compute 4H `efficiency`.
   - TRENDING → the continuation playbooks are eligible.
   - RANGING → only the range playbook is eligible, and only if enabled.
   - MIXED → continuation playbooks only, and they need the stricter filter set.
5. **Playbook selection.** Evaluate the eligible strategies (§5) **in fixed
   priority order**. The first one that produces a valid setup wins; the rest are
   not evaluated on that bar.
6. **Direction lock.** A setup whose direction ≠ `bias` is discarded without
   being considered. There is therefore no arbitration to get wrong — a
   contrary-direction setup can never be constructed.
7. **Geometry.** Apply §4. If the structural target does not reach 2R, the setup
   is **rejected**, reason `RR_TOO_LOW` — not re-targeted to make the number fit.
8. **One live setup per symbol.** If a setup for this symbol is already OPEN or
   FILLED, no new one is issued. Reason `ALREADY_IN_A_TRADE`.

---

## 4. Trade geometry

**Stop is structural, then padded.** For a long: the low of the zone (or the
swing low that formed it) minus `0.25 × ATR(15m)`. For a short: the mirror. The
pad exists so a one-tick wick through the exact low does not stop the trade; it
is not a volatility stop.

**Target is structural.** The candidate targets, in order, are: the last opposing
swing on 1H, then the last opposing swing on 4H, then the nearest untested HTF
level beyond those. The chosen target is **the first candidate that is at least
2R away**. If none is, the setup is rejected.

**The R:R that results is therefore 2.0–4.0 by construction**, and is a real
price a real trader would aim at, not a multiple invented to look good. Setups
whose structure implies more than 4R are capped at the 4R candidate — beyond
that the target is in open space and the research's own tail argument has already
been shown not to pay in this era.

**Fees are charged into the R:R shown**, using the owner's real schedule (taker
0.05%, maker 0.02%, 18% GST, entry always taker). A setup must clear 2R *net*.

---

## 5. The strategy set — one tab each

Priority order is the evaluation order in step 5.

| # | id | When it is eligible | The setup |
|---|---|---|---|
| 1 | `trend_pullback` | regime TRENDING or MIXED, 4H structure = bias | price retraces into a 1H demand/supply zone inside the 0.382–0.786 band of the last impulse, then a **15m BOS** in the bias direction while price is in the zone |
| 2 | `bos_retest` | regime TRENDING, a 4H BOS in the bias direction within the last 10 bars | price returns to the broken swing level (±0.25×ATR(4h)) and a 15m BOS confirms |
| 3 | `level_reclaim` | any regime | price trades through a prior-day/week level, then **closes back through it** on 1H in the bias direction; stop beyond the sweep extreme |
| 4 | `range_fade` | regime RANGING only, **disabled by default** | price at a range boundary (the 20-bar 4H donchian edge) with a 15m BOS back into the range; target the opposite boundary |

`range_fade` contradicts the V4 research journal, which found mean reversion
gross-negative in every regime. It ships **off** and stays off until the
backtest in §9 says otherwise. It exists because the live failure was
range-driven, and because the journal never tested reversion *conditioned on a
measured range regime with lower-timeframe confirmation* — but that is a
hypothesis, not a finding.

---

## 6. What the virtual trader says

It speaks on every closed 15m candle, whether or not there is a trade. The
template is fixed so the reasoning is followable:

```
<SYMBOL> — <bias> ka mood
Daily: <structure>.  4H: <structure>, <regime>.  1H: <structure>.
Price <price> hai, <where it sits: zone / level / open space>.
<Either> Wait kar rahe hain: <what specifically must happen next>.
<Or>     Setup mila: <strategy>, entry <x>, stop <y>, target <z> (<rr>R).
<why in one sentence>
```

Example with no trade:

> ETHUSDT — LONG ka mood
> Daily: BULLISH. 4H: BULLISH, TRENDING. 1H: RANGE.
> Price 1,884.50 hai, kisi zone mein nahi — pichhle impulse ke upar khula hua.
> Wait kar rahe hain: 1,862–1,871 ke demand zone tak pullback, phir 15m par
> ek bullish BOS. Tab tak koi trade nahi.

Example with a trade:

> ETHUSDT — LONG ka mood
> Daily: BULLISH. 4H: BULLISH, TRENDING. 1H: BULLISH.
> Price 1,868.20 hai, demand zone (1,862–1,871) ke andar.
> Setup mila: Trend Pullback — entry 1,868.20, stop 1,858.40, target 1,897.60
> (3.0R net). 4H trend ke saath, zone se 15m BOS confirm ho gaya.

---

## 7. What is marked on the chart

| Element | Look | Meaning |
|---|---|---|
| swing labels | small HH / HL / LH / LL tags at the pivot | the structure the bias is read from |
| structure line | thin dashed ray through the last two same-side swings | the trendline the engine is actually using |
| zone | translucent box, green demand / red supply | where the engine wants price to come back to |
| HTF levels | pane-wide dotted lines, labelled | prior day/week high/low |
| BOS / CHOCH | a small arrow at the breaking candle | the confirmation event |
| entry / stop / target | solid accent / dashed red / dashed green price lines | the trade |
| shaded R zones | red between entry and stop, green between entry and target | the geometry at a glance |

All of it comes from the engine as coordinates. The frontend draws; it never
computes a level.

---

## 8. Module plan

```
backend/marketscalper/v5/
  structure.py   swings, labelling, structure state, BOS/CHOCH   (pure, numpy)
  zones.py       demand/supply zones from impulse legs           (pure)
  regime.py      Kaufman efficiency -> TRENDING/MIXED/RANGING    (pure)
  levels.py      HTF levels: PDH/PDL, PWH/PWL, round numbers     (pure)
  bias.py        the single directional view + conflict gate     (pure)
  strategies.py  the four playbooks -> Setup or a named reason   (pure)
  geometry.py    structural stop + first target ≥2R net of fees  (pure)
  narrate.py     the virtual trader's paragraph                  (pure)
  engine.py      per-symbol orchestration: candles -> Read       (pure)
  service.py     compute-on-read over ChartService + cache
  backtest.py    the 9-year proof harness
  api.py         /api/v5/*
```

**Reused unchanged:** `core/chart_service.py` (multi-TF candles),
`core/indicators.py`, `v4/outcome.py` (fill/stop/target accounting — it is
honest and already tested), `v4/store.py` + migration 008 (the recommendation
table fits V5 rows as-is), `v4/recorder.py` pattern, `alerts.py`, the paper
engine, the whole frontend chart stack.

Everything in `v5/` up to `engine.py` is **pure**: numpy arrays in, dataclasses
out. No I/O, no clock, no randomness — so the backtest and the live path run the
identical code, which is the only reason a backtest number means anything.

---

## 9. How it will be proven

Nothing about expectancy is shown to the owner until this has run.

`v5/backtest.py` replays the stored 1m candles (9 years, 4.7M bars per symbol,
99.82% complete), folds them to 15m/1H/4H/1D with the same folding code the live
path uses, and drives `engine.py` bar by bar. Outcomes go through the existing
`v4/outcome.py`, so fills, gaps, same-bar ambiguity and fees are accounted the
same way live trades are.

Reported **per strategy and per era** (2017–2020, 2021–2023, 2024–2026):
trades, win rate, average R, expectancy **net of the owner's real fee
schedule**, profit factor, max drawdown in R, and average hold.

**The honesty rule:** the UI shows the era breakdown, not a single blended
number. V4's headline was built on 2017–2020 and its own journal admits
significance was never established after 2021 (t = 0.7–1.7) — which is exactly
what losing 11/11 today looks like. A V5 strategy whose edge lives only in the
old era gets marked as such on its own tab, and stays off.

---

## 10. What we are not claiming

- **This is not validated.** It is a design derived from a failure analysis. The
  numbers in §9 do not exist yet.
- The V4 research journal explicitly found: mean reversion gross-negative in
  every regime; trendline *bounce* negative at zero fees; SMC sweep→CHOCH→OB not
  significant even at zero fees; fading a level worse than breaking it. V5's
  `level_reclaim` and `range_fade` are close relatives of rejected ideas. The
  difference V5 claims is **confirmation and regime-conditioning**, which the
  journal did not test. That claim is a hypothesis until §9 answers it.
- `trend_pullback` and `bos_retest` are continuation logic and are *not*
  contradicted by the journal — but they were not tested by it either.
- No strategy is TRUSTED. The gate remains 200 logged recommendations with
  positive net-of-fee expectancy, and no V5 strategy will be marked otherwise.

---

## 11. First backtest result — 2026-07-29

400 days of real BTC+ETH 1m data, both symbols, hourly decisions, fees charged
at the owner's real schedule. **This is the honest answer, and it is not good.**

| playbook set | trades | win rate | avg net R | total R | PF | max DD |
|---|---|---|---|---|---|---|
| all four (as specified) | 493 | 32.5% | −0.088 | −43.5 | 0.89 | 71.0 |
| drop `trend_pullback` | 398 | 33.4% | −0.026 | −10.5 | 0.96 | 51.4 |
| `bos_retest` alone | 70 | 32.9% | −0.193 | −13.5 | 0.73 | 24.0 |
| `level_reclaim` alone | 353 | 33.4% | **+0.004** | +1.4 | **1.01** | 37.7 |

**No playbook has a demonstrated edge.** The best is `level_reclaim` at +0.004R
per trade over 353 trades — statistically indistinguishable from zero.

Two things this run established that are worth more than the headline:

1. **`trend_pullback`, the playbook this design called the core, is the worst of
   the four** (22.8% win rate against a 24.4% breakeven at 3.09 R:R). It also ran
   first, so it was consuming setups the others would otherwise have taken.

2. **`bos_retest` appeared to be the winner (+0.019R) in the combined run and is
   the biggest loser (−0.193R) when run alone.** In the combined run it only saw
   the setups `trend_pullback` declined. Reading per-strategy numbers out of a
   combined run is selection bias, and it pointed the exact wrong way.

**Consequence for what ships.** The market reading — bias, regime, structure,
levels, and the narration — is sound and is a strict improvement on V4, which
could not say anything at all when it had no setup. That ships.

The **setups do not ship as recommendations**. Wiring a breakeven engine to the
alert system and calling its output a trade recommendation is precisely the V4
mistake, one layer further on. They ship visible-but-marked on their own tabs,
with these numbers next to them, and stay out of the alert path until a
parameter regime is found that the backtest supports.

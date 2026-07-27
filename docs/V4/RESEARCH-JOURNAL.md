# MarketScalper V3 — Quantitative Research Journal

Every experiment: objective, hypothesis, method, data, result, decision.
Rejected ideas stay documented. Accepted rules require replay evidence.

---

## E0 — Measurement environment (GATE)

**Objective.** Establish a trustworthy research environment before any strategy work.

**Status of the claimed production fix.** VERIFIED ABSENT. Local `main`, `origin/main`
and the production working tree are all still at `aec5b2d`, with clean trees and no
new commits. None of the audited measurement defects have been fixed in code.
Research therefore could NOT proceed on the production replay.

**Decision.** Build an independent research engine rather than patch production
(production changes are gated on owner approval; research must not wait on them).

**`research/engine.py` design guarantees:**

| # | Guarantee | How enforced |
|---|---|---|
| G1 | No lookahead | Signals use bars ≤ i; execution searches 1m bars with `ts >= decision_bar_close`. Test T3 places a fillable price *before* the decision and asserts NO_FILL. |
| G2 | Fees always charged | `fee_r = (fill_px*taker + exit_px*taker)/risk`, subtracted from every filled trade. Test T4 hand-computes 0.011. |
| G3 | Exits are real | Horizon exit is an actual market close paying fees, not an unrealized mark. `real_exit_frac` separates it. Test T8. |
| G4 | Conservative intrabar | Execution on **1m** bars (not 5m) so most same-bar ambiguity disappears; where it remains, SL wins. Test T5. |
| G5 | Honest gaps | Adverse gap through the stop is charged in full (loss can exceed −1R, test T6); favourable gap through the target is **not** credited (test T7c). |
| G6 | Determinism | Pure numpy, no clock/RNG. Test T9. |

**Self-test result: 24/24 PASS.** Two defects were found by these tests during
development and fixed (favourable-gap crediting; missing keys on NO_FILL).

**Differences vs the production replay (all defects it had, absent here):**
fees in outcomes · execution at 1m not 5m · no −1R loss floor · horizon exits not
counted as costless wins · no under-sampling (production stepped every 3rd 5m bar
while live polled every bar) · single entry model throughout.

**Data.** Full canonical 1m history pulled read-only from production:
BTCUSDT and ETHUSDT, **4,692,463 / 4,692,461 bars, 2017-08-17 → 2026-07-25**
(~9.0 years). Aggregation verified: mean minutes/bar 5.00 / 15.00 / 59.99 / 239.77
for 5m/15m/1h/4h — no material gaps.

**GATE: PASSED.** Research may proceed.

---

## E1 — Baseline sweep (no tuning)

**Objective.** Does ANY conventional strategy show positive NET-of-fees expectancy
over 9 years, on both symbols?

**Hypothesis H0 (null).** After 0.05%/side taker fees, no simple strategy is
net-positive on 15m–4h crypto.

**Method.** 7 strategies × 2 symbols × 3 timeframes, textbook parameters, full
history, market entry, 24h horizon, no filtering, no optimisation.
Strategies: donchian breakout, EMA pullback, Bollinger fade, compression breakout,
liquidity-sweep reversal, Asian-range break, time-series momentum.

**Result (first cell, donchian):** H0 **rejected** for trend-following at 4h.

| Symbol | TF | n | fee/R | gross R | **net R** | t | PF |
|---|---|---|---|---|---|---|---|
| BTC | 15m | 26,376 | 0.157 | −0.025 | −0.182 | −21.2 | 0.76 |
| BTC | 1h | 6,265 | 0.067 | +0.059 | −0.009 | −0.6 | 0.98 |
| **BTC** | **4h** | 1,769 | **0.032** | +0.114 | **+0.082** | **+3.73** | **1.25** |
| ETH | 15m | 26,769 | 0.112 | +0.006 | −0.106 | −12.3 | 0.86 |
| ETH | 1h | 6,395 | 0.049 | +0.065 | +0.016 | +1.0 | 1.03 |
| **ETH** | **4h** | 1,826 | **0.023** | +0.076 | **+0.052** | **+2.43** | **1.15** |

**Reading.** Gross expectancy is positive at almost every timeframe; **fees alone
decide the sign.** The fee burden per unit of risk falls ~5× from 15m to 4h because
ATR-scaled stops widen in percentage terms. This is the same mechanism that killed
the production V3 engine (0.23% stops ⇒ 0.60–0.73R of fees per trade).

**Central hypothesis H1 formed:** *net expectancy in this system is dominated by the
fee-to-risk ratio; any edge must be harvested where stops are wide enough in
percentage terms that the round-trip cost is a small fraction of 1R.*

**Status.** Full 7-strategy sweep running. Survivors go to the E-series validation
battery (per-year, OOS split, rolling windows, regime, parameter plateau, fee
sensitivity, bootstrap CI). No result is accepted on a single window.

**E1 FULL SWEEP RESULT — 7 of 40 cells net-positive.**

| Finding | Evidence |
|---|---|
| Timeframe decides everything | 0 positive cells at 15m · 2 at 1h · **5 at 4h** |
| Trend/continuation works | every positive cell is donchian / compression / momentum |
| Mean reversion has NO edge | `bb_fade` negative on all 6 cells and **gross-negative** (−0.085 to −0.117) — not a fee problem |
| **The V3 thesis is refuted** | `sweep_rev` (liquidity-sweep reversal = V3's core setup) negative on **all 6 cells**, gross-negative at 1h/4h |
| Session breakout fails | `asia_break` negative everywhere |

---

## E2 — H1 test: does widening the stop rescue low timeframes?
**Method.** donchian, rr fixed at 2.0, sl swept 1→16 ×ATR. fee/R must fall ~1/x.
**Result.** fee/R fell exactly inversely (0.321→0.020). Net rose monotonically at 15m
(−0.389→+0.000) and peaked at 1h with sl=4 (+0.030, t=+2.63).
**Decision.** H1 **CONFIRMED but REFINED**: low fee/R is *necessary, not sufficient*.
At 15m the gross edge is ~0 even with fees near zero — there is nothing to harvest.

## E3 — Timeframe ladder + horizon
Monotonic on **both** symbols: 15m −0.18/−0.11 · 1h −0.01/+0.03 · 4h +0.105/+0.095 ·
**1d +0.156/+0.188**. Horizon 3d beats 1d at 4h (24h truncated 68% of trades).

## E4 — Validation of 4h donchian
Significant (BTC t=+3.62 CI[+0.049,+0.163]; ETH t=+3.32 CI[+0.041,+0.155]).
365d windows positive 87.5%/100%. **BUT 2025 and 2026 negative on both symbols.**

## E5 — Falsification: is it just long beta?
BTC LONG +0.186 (t=4.82) / SHORT −0.011 (t=−0.25) — BTC 4h is effectively long-only.
ETH LONG +0.107 / SHORT **+0.079** (t=1.74) — ETH is genuinely two-sided.

## E6 — Decay test
BTC 2017-20 +0.250 → 21-22 −0.041 → 23-24 +0.162 → **25-26 −0.090 (t=−1.41)**.
ETH same shape, 25-26 −0.053 (t=−0.77). Negative but **not** significant: consistent
with normal trend-following drawdown cycles, not proof of decay.

## E7 — Practical value vs buy & hold (1% risk/trade)
| | CAGR | maxDD | return/DD |
|---|---|---|---|
| BTC donchian 4h | 21.2% | **33.8%** | 0.63 |
| BTC buy & hold | 35.5% | 84.0% | 0.42 |
| ETH donchian 4h | 19.8% | **35.3%** | 0.56 |
| ETH buy & hold | 22.7% | 94.3% | 0.24 |
Lower raw return, **~1/3 the drawdown** ⇒ decisively better risk-adjusted.

## E8–E11 — THE 1d SYSTEM (accepted candidate)
`donchian(lookback=20, sl=2×ATR, target=2R, hold≤10d)` on the **1d** timeframe.

* **E9 parameter PLATEAU** — every value tested is positive on both symbols:
  lookback 10/15/20/30/40/55/80 → BTC +0.14…+0.21, ETH +0.22…+0.39;
  stop 1–4×ATR all positive; target 1R–4R all positive (higher rr better = let trends run).
  This is a plateau, not a fitted spike.
* **E10 OUT-OF-SAMPLE (time-ordered, split 2022-01-01)** —
  BTC IN +0.259 (t=2.86) → **OUT +0.164 (t=1.99)**;
  ETH IN +0.371 (t=4.13) → **OUT +0.183 (t=2.10)**. Edge degrades but **survives**.
* **Fee robustness** — still positive at **0.40%/side (8× the assumed rate)**:
  BTC +0.122, ETH +0.217. (The V3 5m system died at 0.05%.)
* **E11 COMBINED PORTFOLIO** — n=680, net **+0.247R**, **t=+5.63**, PF 1.67,
  win 52.2%, 95% CI **[+0.162, +0.331]**. LONG +0.316 (t=5.45), SHORT +0.115 (t=1.83)
  ⇒ both directions positive, so not pure long beta.

**DECISION: ACCEPTED** as the first strategy meeting the project's bar.

## Rejected — kept on the record
| Idea | Why rejected |
|---|---|
| Liquidity-sweep reversal (V3 core) | negative on all 6 cells, gross-negative — no edge before fees |
| Mean reversion (Bollinger fade) | gross-negative everywhere; worst family tested |
| Asian-range session breakout | negative on every cell |
| EMA pullback | gross-positive but fee-killed at every TF except marginal 4h BTC |
| Any 15m strategy | 0 of 14 cells positive; no gross edge to harvest |
| 24h horizon on 4h trend trades | truncates 68% of trades; 3d strictly better |

---

# ADVERSARIAL SELF-AUDIT — attempting to falsify the above

| # | Attack | Result |
|---|---|---|
| A1 | Untested vectorisation of `efficiency_ratio` | **CLEAN** — matches naive to 4.7e-14 |
| A2 | Bar completeness never checked by strategies | 36 daily bars incomplete (1.1%), worst 29 min |
| A3 | Trades treated as IID | **FAILED** — 84%/80% overlap, lag-1 autocorr **+0.44** |
| A4/A5 | Re-run with one position at a time | ETH +0.282 → **+0.178**; combined t=5.63 → **NW_t 3.77** |
| A6/A13 | Incomplete-bar contamination | immaterial (+0.215→+0.204, +0.178→+0.169) |
| A7 | **Funding costs never modelled** | −0.019 to −0.027R typical; −0.09R stressed. Real but non-fatal |
| A8 | Multiple testing (40 cells + 17 param variants) | **BTC and ETH individually FAIL Bonferroni**; only pooled survives |
| A9 | "OOS" split chosen after seeing full-sample results | honest walk-forward: BTC t=+2.12, **ETH t=+1.70 (n.s.)** |
| A10 | Leak test (randomise direction) | BTC **+0.128R** — investigated in A14 |
| A11 | Slippage never modelled | immaterial (10bp/side costs 0.025R) — stops are ~5% wide |
| A12 | Era dependence | **significance evaporates post-2021**: BTC t=1.35, ETH t=0.96 |
| A14 | Leak or drift? | **DRIFT, not a leak.** Forced-LONG at donchian timings: BTC **+0.206** vs strategy +0.215 |

## The finding that changes the conclusion (A14)

Holding side constant at the strategy's own entry times:

| | strategy | forced LONG | forced SHORT | value added by direction call |
|---|---|---|---|---|
| BTC | +0.215 | **+0.206** | −0.142 | **+0.009R ≈ nothing** |
| ETH | +0.178 | +0.075 | −0.126 | **+0.103R — genuine** |

**BTC's "trend-following edge" is almost entirely long exposure in an asset that
rose 1,412%.** The donchian direction decision adds ~0.009R. ETH does show real
directional discrimination (+0.103R over always-long).

Correct benchmark is a drift-matched random-entry null, NOT zero. Measuring
expectancy against zero in a strongly drifting market overstates every result.

## Post-audit status
* **Measurement engine: VALIDATED** (A1, A10/A14 prove no leakage — the anomaly was drift).
* **V3 refutation: STANDS** — `sweep_rev` gross-negative is unaffected by any of these corrections.
* **1d donchian on BTC: DOWNGRADED to "beta timing", not an edge.**
* **1d donchian on ETH: WEAKENED but not killed** — real directional value, marginal significance.
* **NOT VERIFIED:** existence of the edge outside BTC/ETH (no other symbols in the DB —
  survivorship bias unaddressable with available data); whether 2025-26 weakness is
  decay or normal trend-follower drawdown (insufficient sample).

---

# CYCLE 2 — post-audit research (corrected methodology throughout)

All cycle-2 results use `harness.py`: non-overlapping trades, funding charged,
Newey-West HAC t, block bootstrap, and a **drift-matched or time-randomised null**.
Positive expectancy vs zero is never accepted as evidence.

## H2 — edge vs drift-matched null, by timeframe (BTC/ETH)
| TF | BTC edge (p) | ETH edge (p) | verdict |
|---|---|---|---|
| 15m | +0.002 (0.400) | +0.024 (0.040) but net −0.075 | **no usable edge — scalping dead** |
| 1h | **+0.075 (0.000)** | **+0.071 (0.000)** | real signal, fee-consumed |
| 4h | +0.111 (0.000) | +0.088 (0.022) | real |
| 1d | +0.142 (0.033) | +0.134 (0.033) | real, small n |

## H6 — maker limit-retest entry (ACCEPTED)
Enter with a **limit at the broken level** instead of chasing the close.
Two independent gains: better fill price (+0.012…+0.024R at identical fee) and
maker vs taker fee. BTC 1h NWt +0.86 → **+2.45**; ETH +1.72 → **+3.40**. 21% unfilled
(adverse selection, modelled). Validated against a **time-randomised null**
(identical limit mechanics): BTC EDGE +0.119 p=0.000, ETH +0.120 p=0.000.
**Caveat found by self-audit:** the direction-flip null is INVALID for limit entries
(flipping changes fill mechanics); only the time-randomised null is used.

## H7 — OUT-OF-UNIVERSE TEST (the decisive experiment)
22 symbols fetched from Binance public klines (1h, ~8.7y). BTC/ETH post-2021 were
the WEAKEST cells (+0.016/+0.043) — the "decay" was **specific to the two most
efficient pairs**, not the edge itself.

| Statistic (correctly handling cross-sectional dependence) | Result |
|---|---|
| Per-symbol mean, symbol = 1 observation (n=21) | +0.163R, t=**+13.84**, p=1.1e-11 |
| Sign test | **21/21 positive**, p=4.8e-07 |
| Equal-weight portfolio, monthly series | +0.174R, NWt **+9.47**, 86% positive months |
| **Modern era only (2021+)** | +0.141R, NWt **+6.85**, **84% positive months** |
| Paired vs time-randomised null (8 syms) | +0.166 vs −0.015, t=+17.42, p=5.1e-07 |

Parameter plateau across the universe — every value positive, NWt +8.4…+11.9:
lb 10/15/20/30/40 · sl 1.0–3.0×ATR · rr 1.0–3.0.
Fee robustness: still NWt **+4.98** at taker 0.10% (5× maker).
Frequency: **~2,965 trades/year across 22 symbols ≈ 57/week ≈ 8/day.**

**ECONOMIC STORY (why this is not curve-fitting):** the edge is inversely related to
market efficiency. BTC/ETH — the most liquid, most arbitraged pairs — retain almost
none. Less-liquid alts (SOL +0.278, FIL +0.220, AVAX +0.203 in the modern era)
retain a large edge. Breakout continuation persists where participation is thinner.

**DECISION: ACCEPTED** — survives every test applied, including the ones that
killed cycle 1.

## Unresolved limitation (recorded, not hidden)
**SURVIVORSHIP BIAS.** Binance's *currently listed* pairs exclude delisted coins.
Direction of bias is ambiguous for a long/short system (dead coins would have
produced strong short trends) but it CANNOT be quantified with available data.
This is the single largest remaining threat to the result. Marked **Not Verified**.
Secondary: maker fills assume queue priority when price touches the level.

---

# CYCLE 3 — FALSIFICATION OF THE ACCEPTED STRATEGY (H6/H7): **REJECTED**

Engine extended with three realism parameters, then re-validated: **30/30 self-tests
pass** (new: T12 fill-through, T13 entry delay, T14 slippage). One conservatism fix
made during this cycle: a limit now always fills at ITS OWN price — a favourable
entry gap is no longer credited, because crediting it shrinks |fill − sl| and
inflates every R multiple.

## S1 — Queue-priority realism (limit must trade THROUGH, not just touch)
| requirement | mean | NWt | positive months |
|---|---|---|---|
| touch fills (original assumption) | +0.1705 | +9.58 | 86% |
| 5% of risk through | +0.1218 | +5.91 | 78% |
| 10% through | +0.0736 | +3.57 | 66% |
| **25% through** | **−0.0587** | **−2.99** | 38% |
| 50% through | −0.2388 | −13.90 | 9% |

Fill rate barely moves (97%→95%), so this is **not** missed trades — it is
**adverse selection**: the fills obtained are the bad ones.

## S2/S7 — Execution latency: THE KILLER
4h donchian, market entry, decay of mean net R vs delay:

| delay | 0 min | 1 min | 5 min | 15 min | 60 min |
|---|---|---|---|---|---|
| mean | **+0.092** | +0.059 | +0.031 | **−0.072** | −0.108 |

**The edge loses 36% in the first MINUTE and inverts by 15 minutes.**

## S4 — Realistic combined stack (1h retest, maker 0.02%, 10% through, 5min, 2bp)
**−0.035R, NWt −1.06.** Pessimistic and brutal stacks: −0.33 and −1.19.

## S5/S6 — Do slower timeframes rescue it? NO
* 4h market entry: clean +0.092 (NWt 3.09) → 60-min delay **−0.108**.
* **1d on the 22-symbol universe is NOT significant even clean** (NWt +0.60…+1.15).
  The earlier 1d acceptance was BTC/ETH-only and does not replicate cross-sectionally.
* Patient-limit is *worse* under delay (−0.28 @60min, −0.96 @240min) — waiting for a
  pullback means being filled mainly when the breakout fails.

## VERDICT: **REJECTED for MarketScalper.**
The apparent edge is a **sub-15-minute post-breakout continuation effect**. It is real
(it beat time-randomised nulls decisively) but the market prices it in almost
immediately. Capturing it requires automated sub-minute execution and favourable queue
position. MarketScalper is, by frozen architecture (v1.2), a decision-support tool
where a human executes manually — the one workflow under which this edge is
provably negative.

**Consistency check:** this explains the earlier BTC/ETH-vs-alts split. The effect
decays fastest where participation is deepest (BTC/ETH ≈ 0 post-2021) and persists
slightly longer in thinner alt books — but not long enough for manual execution.

## What this rules IN for the next cycle
Any surviving strategy must have an edge that is **flat across execution delay**.
That points away from event-triggered entries and toward **slow, rebalancing,
cross-sectional** designs (hold days-to-weeks, entry timing largely irrelevant).
H3 cross-sectional momentum is the priority hypothesis and is latency-insensitive
by construction.

---

# CYCLE 4 — OWNER CONSTRAINTS: BTC/ETH ONLY, INTRADAY/SCALPING, PRICE-ACTION + SMC
Multi-symbol direction dropped at owner's instruction. Execution realism is now a
FIRST-CLASS gate applied from the first run, not a final audit.

## H8 — Time-of-day / session seasonality — **REJECTED**
*Rationale for testing it:* a fixed-clock entry is latency-insensitive by
construction, which is exactly the property the breakout family lacked.
*Method:* descriptive only. Mean forward 60-min return by UTC hour, two
independent eras (2017-21 vs 2022-26), both symbols. No rule built.
*Result:* **sign stability between eras = 42% (BTC) / 46% (ETH) — BELOW the 50%
coin-flip line.** Hour effects do not persist. The 1-2 hours that survive both eras
are what 24 simultaneous tests produce by chance, and their era-B magnitude
(~+0.04%/hr) is far below the 0.10% round-trip taker cost.
*Rejection reason:* no persistent structure + magnitude below transaction costs.

## H9 — SMC intraday (proper implementation) — component-wise
New module `smc.py` implements the real vocabulary causally (every object carries
the bar at which it becomes KNOWN): confirmed swings -> HH/HL/LH/LL -> BOS/CHOCH,
displacement, order blocks, FVGs, equal-high/low liquidity pools, prior-day and
Asian session levels, sweep detection (wick takes the level, body closes back),
premium/discount. This is a materially fairer test than cycle 1's one-line
`sweep_rev`.

Variants isolate each concept so we learn WHICH ingredient carries information:
`sweep` -> `sweep + CHOCH` -> `full (sweep -> CHOCH -> OB/FVG retest)` -> `+ HTF bias`.

**BTC 15m results (net R, taker 0.05%, funding charged, non-overlapping):**

| variant | signals | n | win | PF | delay 0 | delay 5m | delay 15m |
|---|---|---|---|---|---|---|---|
| sweep (naive) | 57,564 | 19,332 | 32% | 0.60 | **−0.352** (t−32.8) | −0.711 | −2.643 |
| sweep + CHOCH | 8,427 | 4,797 | 36% | 0.77 | **−0.169** (t−7.3) | −0.172 | −0.199 |

**Reading so far:** CHOCH confirmation is NOT noise — it halves the loss
(−0.352 → −0.169) and makes the setup far more delay-robust (wider stops).
So the SMC vocabulary does carry information. But the base is so deeply negative
at 15m that halving it still leaves it well short of zero — consistent with E2's
finding that 15m has no harvestable gross edge once fees are paid.

**BTC 15m full matrix (net R, taker 0.05%):**

| variant | n | win | PF | d0 | d5 | d15 |
|---|---|---|---|---|---|---|
| sweep | 19,332 | 32% | 0.60 | −0.352 | −0.711 | −2.643 |
| sweep + CHOCH | 4,797 | 36% | 0.77 | −0.169 | −0.172 | −0.199 |
| full (OB/FVG retest) | 2,566 | 41% | 0.51 | −0.202 | −0.213 | −0.259 |
| full + HTF bias | 1,240 | 42% | 0.52 | −0.197 | −0.208 | −0.250 |

**BTC 5m sweep:** −0.624 (t −81.8). Lower TF = higher fee/R = strictly worse.

## H9b — the DECISIVE test: SMC with limit-at-zone (maker), the entry traders use
| entry model | BTC net R | ETH net R |
|---|---|---|
| market, taker 0.05% | −0.202 | −0.178 |
| limit at zone, maker 0.02% | −0.161 | −0.124 |
| limit + 10% queue-through | −0.277 | −0.233 |
| limit + 5-min delay | −0.171 | −0.124 |
| **limit at ZERO fees (VIP rebate)** | **+0.035 (NWt +1.02)** | **+0.023 (NWt +0.66)** |

### H9 VERDICT: **REJECTED — and the rejection reason matters**
**Even with transaction costs set to ZERO, the SMC setup is not statistically
significant** (NWt +1.02 / +0.66). This is NOT a fee problem. There is no gross
edge to pay costs out of. fee/R at these settings is 0.15–0.20 because the SMC
entry sits at the zone while the stop sits beyond the sweep wick — tight risk in
percentage terms, which is precisely the geometry that makes fees fatal.

### What IS true about SMC (the honest positive finding)
The concepts are **not noise**. CHOCH confirmation halves the loss
(−0.352 → −0.169) and makes the setup materially delay-robust
(−0.169 → −0.199 at 15 min, vs breakouts which invert in 15 min). HTF bias adds a
little. So the vocabulary carries genuine information about *direction and
robustness* — it simply does not produce enough expectancy at intraday horizons
on BTC/ETH to clear the cost hurdle.

### The two-sided trap now fully mapped
| family | edge? | execution-robust? |
|---|---|---|
| Breakout / trend (4h) | YES (+0.09, t 3.1) | **NO** — decays 36% in 1 min, inverts by 15 min |
| SMC intraday (15m) | **NO** (≈0 even at zero fees) | YES — barely degrades with delay |
Neither family satisfies both conditions simultaneously.

**Full SMC matrix — ALL 16 CELLS NEGATIVE** (2 symbols × 2 TFs × 4 variants):
BTC 15m −0.169…−0.352 · BTC 5m −0.354…−0.624 · ETH 15m −0.106…−0.250 ·
ETH 5m −0.241…−0.464. Not one positive cell at any delay.

## H11 — BTC → ETH lead-lag — **REJECTED**
*Rationale:* uses only the owner's two symbols, is intraday, and if the lag were
minutes (not seconds) it would be execution-robust.
*Method:* 4,692,461 aligned minutes. BTC past return (5/15/30m) vs ETH forward
return (5/15/30m); correlation plus conditional means after >1sd BTC moves; two
independent eras. Descriptive only, no rule.
*Result:* correlations **−0.0005 to −0.025** (essentially zero and mostly the WRONG
sign). Conditional up-minus-down spreads **0.005%–0.045%** versus a **0.100%**
round-trip cost. Modern-era spreads are ~14× smaller than costs.
*Rejection reason:* no predictive power; magnitude an order of magnitude below
transaction costs. The relationship is arbitraged within seconds, not minutes.

## H10 — Trendline price action (bounce / break) — **REJECTED**
*Rationale:* explicitly requested by the owner; not previously tested.
*Method:* causal trendlines fitted through confirmed swings (line usable only once
BOTH anchors are confirmed), validated by >=3 touches within a bounded window and
<=1 prior close-through. Bounce = touch + rejection close; Break = displacement
close through. BTC/ETH 15m, delay applied from the start.
*Result (BTC 15m bounce):* 92,185 signals, n=13,253, win 33%, PF 0.70,
**fee/R = 0.201**. taker d0 **−0.246 (t −19.8)** · d5 −0.397 ·
**ZERO fees −0.045 (t −3.79)**.
*Rejection reason:* **significantly negative even at zero transaction cost.** Like
SMC, this is not a cost problem — the gross edge is negative. The 0.201 fee/R
confirms the same tight-stop geometry that dooms every intraday construction here.

---

# CYCLE 4 CONCLUSION — intraday BTC/ETH

Families tested and rejected, each for a DIFFERENT reason (which is what makes the
conclusion strong rather than a single failed idea):

| family | gross edge? | rejection reason |
|---|---|---|
| Breakout / trend (4h+) | YES | latency-fatal: −36% in 1 min, inverts by 15 min |
| SMC full vocabulary | NO (~0 at zero fees) | no edge to pay costs from |
| Trendline bounce | **NEGATIVE at zero fees** | negative gross edge |
| Mean reversion | NEGATIVE gross | no edge |
| Session / Asian-range break | NO | negative every cell |
| Time-of-day seasonality | NO | 42–46% era-to-era sign stability + sub-cost magnitude |
| BTC→ETH lead-lag | NO | corr ~0, spread 14× below cost |

**Structural cause (arithmetic, not opinion):** at 5m/15m the ATR-scaled stop is
tight in percentage terms, so **fee/R lands at 0.10–0.20**. Every intraday
construction must therefore clear a 10–20%-of-risk hurdle before it earns anything,
and the measured gross edges at those timeframes are approximately zero or negative.
This is the same mechanism that killed production V3 (0.23% stops → 0.60–0.73R fees).

**What survives as TRUE:** the SMC vocabulary carries real information about
direction and robustness (CHOCH halves the loss and confers delay-tolerance). It is
a valid *analysis* framework. It is not, on this evidence, a standalone intraday
signal generator on BTC/ETH.

**BLOCKED — owner scope decision required.** Further intraday hypotheses face the
same fee/R hurdle; continuing without a scope change is low expected value.

## H13 — Pre-placed orders at levels marked in advance — **DESIGN SUCCEEDED, EDGE ABSENT**
*Design rationale:* every prior failure was either latency-fatal or edgeless. If the
entry LEVEL is known hours ahead (PDH/PDL/Asian H/L computed at 00:00 / 08:00 UTC),
the order rests in the book: delay is structurally irrelevant and queue priority is
genuinely ours, so a touch-fill is fair.
*Engine work:* added **stop-order support** (`stop_entry`) — breakout entries need a
buy-stop above market, which the engine could not model. First attempt produced
win=0% / PF=0.00 over 11,909 trades, which was a TEST BUG, not a finding, and was
discarded. Also fixed a real engine bug found by this run: a negative `fill_i` from
the stop branch fell through as a negative numpy index. **Self-tests now 40/40.**

| | bounce (limit) | breakout (stop) |
|---|---|---|
| BTC taker | −0.275 | −0.314 |
| ETH taker | −0.252 | −0.217 |
| **BTC ZERO fee** | −0.161 (t −7.6) | **−0.028 (t −1.16)** |
| **ETH ZERO fee** | −0.167 (t −7.6) | **−0.005 (t −0.18)** |
| +60 min late | −0.342 | −0.318 |
| +4 h late | — | −0.321 |

**The latency-immunity goal was ACHIEVED** (4-hour delay moves the result by 0.007R).
That is a genuine methodological result: this design pattern works. But bounce is
significantly NEGATIVE gross, and breakout is EXACTLY ZERO gross. fee/R 0.21–0.29.

## H12 — REGIME → STRATEGY MAP (the owner's question, answered)
Regime is causal (efficiency-ratio trend axis, known at decision time).

**4h (net R, taker 0.05%, funding charged, non-overlapping):**

| strategy | TREND | MIXED | CHOP |
|---|---|---|---|
| **donchian** | **+0.185 (t +3.7)** | +0.068 (t +1.1) | +0.024 (t +0.4) |
| compression | +0.101 (t +1.2) | +0.032 | +0.023 |
| sweep_rev (SMC) | −0.188 (t −3.7) | +0.019 | −0.067 |
| ema_pullback | — | −0.145 | −0.061 |
| bb_fade (mean rev) | −0.211 (t −6.9) | −0.141 | −0.099 |

**15m: ALL 15 cells negative.** Best is donchian/TREND at −0.020 (t −1.3, n.s.).

### Conclusions from the map
1. **Exactly one statistically significant positive cell exists in the whole study:
   donchian breakout, TREND regime, 4h — +0.185R, t = +3.7.**
2. **Mean reversion loses in EVERY regime — including CHOP**, where theory says it
   should work. Striking and consistent across both symbols.
3. **SMC sweep-reversal loses hardest in TREND (−0.188)** — fading a trending market
   is where it does most damage; it is ~flat in MIXED.
4. Regime conditioning does NOT rescue intraday: the best 15m cell is still negative.

## H10 (final) — Trendline: bounce REJECTED, break shows a tiny gross edge
BTC break ZEROfee **+0.022 (t +2.29)**, ETH **+0.045 (t +4.87)** — a real but tiny
gross edge, buried by fee/R of 0.33/0.26. Bounce is negative even at zero fees.
Same structural verdict: intraday gross edges are an order of magnitude below costs.

## H14 — 4h DONCHIAN + PRE-PLACED STOP ORDER — **BEST CANDIDATE FOUND**
*Design:* the breakout LEVEL is known at bar close, so a stop order can REST there.
This is the original Turtle execution and it is latency-immune by construction —
combining two pieces validated separately (the 4h TREND edge, and H13's proof that
resting orders are delay-proof). Never tested together before.

**THE EXECUTION PROBLEM IS SOLVED:**
| | taker | +60 min | **+4 hours** |
|---|---|---|---|
| BTC | +0.128 (t+3.07) | +0.138 (t+3.40) | **+0.130 (t+3.24)** |
| ETH | +0.127 (t+3.08) | +0.136 (t+3.23) | **+0.112 (t+2.74)** |

(the market-entry version went +0.092 → **−0.108** at 60 min. This one does not move.)

**Validation:**
* per-year: **8/10 positive on BOTH symbols**
* parameter plateau: **every** value positive both symbols — lb 10/15/20/30/40 and
  rr 1.5/2.0/3.0, t from +1.6 to +4.0
* full-sample block-bootstrap CI excludes zero: BTC [+0.054,+0.208] ETH [+0.053,+0.207]
* fee/R only **0.026–0.035** (this is why it works — the 4h stop is wide in % terms)

**HONEST WEAKNESSES — why this is NOT yet accepted:**
1. **Modern era is NOT significant.** BTC 2021+ NWt **1.27**, CI [−0.031,+0.172];
   ETH 2021+ NWt **1.66**, CI [−0.006,+0.193]. Both CIs include zero.
2. **Slippage-sensitive.** Stop orders slip precisely when they trigger (fast
   breakout, thin book). BTC: 5bp → +0.093 (t 2.2) · 10bp → +0.058 (t 1.4) ·
   20bp → **−0.011**. ETH is more robust but degrades similarly.
3. Regime filter gave INCONSISTENT results (hurt BTC +0.128→+0.067, helped ETH
   +0.127→+0.176) ⇒ treated as noise and NOT used. Using it would be curve-fitting.
4. ~100 trades/year/symbol — this is swing trading, **not scalping**.

**STATUS: PROMISING, UNPROVEN.** It is the only construction in the entire study
that is simultaneously (a) statistically significant full-sample, (b) parameter-
robust, and (c) execution-realistic. But the modern era does not confirm it, so it
does not meet the project's bar for an accepted strategy.

## H15 — HTF LEVEL + 5m-SCALED TIGHT STOP + BIG TARGET (owner's requested structure)
*Design:* level from 4h donchian (HTF), stop sized on **5m** ATR (short-timeframe
precision, as the owner wants), entry = **resting stop order at the level**
(latency-immune per H13/H14), target = large R multiple.

**Rules:** at each 4h close, level = 20-bar high/low. Rest a stop order there.
SL = level ∓ 5×ATR(5m). TP = 10R. Horizon 5 days. One position at a time.

| config | BTC | ETH |
|---|---|---|
| sl 2×ATR5m, rr 3 (too tight, fee/R 0.27) | −0.374 | −0.199 |
| sl 3×ATR5m, rr 10 | +0.058 | +0.167 |
| **sl 5×ATR5m, rr 10** (fee/R 0.09–0.12) | **+0.191 (t 2.32)** | **+0.338 (t 3.92)** |
| ...+4 HOURS late | **+0.288 (t 3.24)** | **+0.290 (t 3.32)** |
| ...+5bp slippage | +0.071 | **+0.248 (t 2.86)** |

Win rate only 17–18%, carried by the 10R target. fee/R 0.09–0.12 is the reason it
works where every other intraday construction failed.

**Validation:** per-year **BTC 9/10, ETH 8/10 positive**. Parameter plateau: EVERY
value positive on both symbols (sl 3/4/5/7 · rr 5/8/10/15 · lb 10/20/30), t up to
+4.6. Full-sample CI excludes zero (BTC [+0.040,+0.348], ETH [+0.177,+0.512]).

**SAME FATAL WEAKNESS AS H14 — modern era not significant:**
BTC 2021+ NWt **0.68** CI[−0.106,+0.247] · ETH 2021+ NWt **1.34** CI[−0.037,+0.305].
BTC is also slippage-fragile (5bp → t 0.9; 10bp → negative). ETH is far more robust.

**IMPORTANT PATTERN:** every plateau sweep pushes MONOTONICALLY toward wider stops,
bigger targets and longer lookbacks (sl 7>5>4>3, rr 15>10>8>5, lb 30>20>10). The
data keeps asking for slower, wider trades — the opposite of scalping.

**CROSS-REFERENCE TO CYCLE 2:** the universe test found the modern era STRONG across
18 alts (portfolio NWt **+6.85**, 84% positive months) while **BTC/ETH were the two
WEAKEST symbols**. The owner's chosen pair is precisely where this edge has been
arbitraged away.

**STATUS: BEST CANDIDATE IN THE STUDY, STILL UNPROVEN** on the modern era.

## H16 — CONFLUENCE STACK: which price-action concepts actually ADD value?
Filters applied to the WORKING base (H15). Per-factor lift = mean net R with the
factor ON minus OFF. **Consistent across BOTH symbols:**

| factor | BTC lift | ETH lift | verdict |
|---|---|---|---|
| 1D trend alignment | **+0.189** | **+0.455** | HELPS |
| Structure / BOS alignment | **+0.273** | **+0.412** | HELPS |
| 4H trend alignment | +0.015 | **+0.683** | HELPS |
| volatility expansion | +0.027 | +0.042 | neutral |
| **premium / discount** | **−0.229** | **−0.454** | **HURTS** |
| **liquidity sweep** | **−0.227** | **−0.258** | **HURTS** |

**KEY INSIGHT: "combine everything" is WRONG.** Premium/discount and liquidity-sweep
are MEAN-REVERSION concepts (buy low / sell high). Bolting them onto a BREAKOUT
system makes it fight itself. Both symbols confirm this independently.

## H17 — CLEAN COMBINED STRATEGY (keep trend filters, drop mean-reversion filters)
Require N of 3 trend-alignment conditions: 4H close vs EMA50, 1D close vs EMA20,
4H structure trend — all in the breakout direction.

**ETHUSDT — monotonic improvement with confluence (real information):**
| need | ALL-era | **2021+ (modern)** |
|---|---|---|
| ≥1 | +0.372 (t 4.20) | +0.164 (t 1.61) |
| ≥2 | +0.409 (t 4.27) | +0.200 (t 1.82) |
| **≥3** | **+0.551 (t 4.61)** PF 1.62 | **+0.318 (t 2.34) CI[+0.052,+0.564]** |

**ETH need≥3 stress tests:** +4h late **+0.306 (t 2.11)** · +5bp slip +0.223 ·
+10bp slip +0.127 · double fees (0.10%) +0.223. **First configuration in the entire
study that is modern-era significant AND latency-immune AND slippage-tolerant.**

**BTCUSDT — the filters do NOT help.** need≥3: ALL +0.184 (t 1.81),
**2021+ −0.050 (t −0.45)**; dies at 5–10bp slippage. Flat/declining with confluence
(+0.199 → +0.177 → +0.184), i.e. no information.

**STATUS: ETH = strongest candidate of the whole study. BTC = REJECTED.**
Honest caveats: filters were chosen after seeing full-sample per-factor lifts (mild
selection); need≥3 chosen after inspection; the ETH/BTC divergence could be luck.
Mitigating: the kept filters are theoretically coherent with a breakout system, the
dropped ones are theoretically contradictory, and ETH's improvement is MONOTONIC.

## H18 — NON-SMC LEVEL TOOLS (VWAP · Volume-Profile POC · Opening Range · Round numbers)
All on the proven execution model (resting stop, 5×ATR(5m), 10R).

| level tool | BTC | ETH |
|---|---|---|
| **round numbers** (1000/100) | **+0.197 (t 2.56)** | **+0.156 (t 1.96)** |
| VWAP ±2σ band break | +0.026 | +0.165 (t 2.52) |
| Volume-Profile POC break | −0.122 | +0.120 |
| Opening-range break | −0.026 | +0.095 |
| **VWAP break** | **−0.147 (t −2.54)** | +0.071 |

Round numbers are the best non-SMC level and the only positive significant BTC cell
besides donchian — economically sensible (stop clusters at psychological prices).
**VWAP break is significantly NEGATIVE on BTC** — breaking VWAP should be faded, not
followed. POC and opening-range carry no usable edge.

## H19 — DOES COMBINING LEVEL TYPES HELP? **NO — IT DILUTES.**
| config | BTC ALL | ETH ALL | ETH 2021+ |
|---|---|---|---|
| round alone (no filters) | +0.197 | +0.156 | — |
| round + trend filters | +0.039 | +0.166 | +0.081 |
| **donchian + trend filters (need≥3)** | +0.184 | **+0.551 (t 4.61)** | **+0.318 (t 2.34)** |
| round + donchian ("both") + filters | +0.075 | +0.257 | +0.038 |

**LESSON: adding more level types makes it WORSE.** ETH donchian+filters +0.551 →
"both" +0.257. Signals that do not share the same edge mechanism dilute the good
ones. Round numbers work standalone but their edge is NOT trend-continuation, so
mixing them into a trend system destroys both.

### FINAL RANKING OF EVERYTHING TESTED
1. **ETH · donchian 4h level + 3 trend filters + resting stop + 5×ATR(5m) + 10R**
   ALL +0.551 (t 4.61) · **2021+ +0.318 (t 2.34), CI [+0.052,+0.564]** ·
   +4h late +0.520 · +5bp slip +0.465 · double fees +0.223. **BEST — the only
   configuration modern-era significant, latency-immune AND slippage-tolerant.**
2. BTC round-number breaks standalone +0.197 (t 2.56) — modern era not significant.
3. BTC donchian+filters +0.184 — modern era negative. NOT usable.

## H20 — CAN THE WINNER BE COMPRESSED INTO A 5-HOUR TRADE? **NO.**
Owner constraint: trades must close within 5 min – 5 hours.

**Holding-time reality of the winning system (5-day horizon):**
BTC median 6.0h, p75 27h, **p90 94h** — only 46% close inside 5h.
ETH median 9.2h, p75 34h, **p90 104h** — only 39% close inside 5h.

**Horizon × target sweep (all with the winning filters):**
| horizon | ETH ALL | ETH 2021+ |
|---|---|---|
| 2h (rr5) | +0.096 (t 3.54) | +0.036 (t 1.07) |
| **5h (rr5)** | **+0.132 (t 3.36)** | **+0.035 (t 0.74)** ✗ |
| 8h (rr5) | +0.197 (t 4.21) | +0.095 (t 1.68) |
| **5 days (rr10)** | **+0.551 (t 4.61)** | **+0.318 (t 2.34)** ✓ |

**BTC: all 16 horizon×target cells NEGATIVE.**

**Rescue attempts at the 5h cap — ALL FAILED to reach modern-era significance:**
| filter | ETH 2021+ |
|---|---|
| baseline | +0.035 (t 0.74) |
| volatility top 50% | −0.036 |
| volatility top 33% | −0.054 |
| expansion (ER≥.35) | +0.023 |
| vol50 + expansion | −0.016 |
| London+NY only | +0.008 |
| wider stop (8×ATR5m) | +0.055 (t 1.54) — best, still n.s. |

Notably **volatility filtering made it WORSE**, refuting the intuition that "trade
only in high vol so a big move can happen fast".

### MECHANISM (why the constraint and the edge are incompatible)
The system earns from a small number of winners running very far (10R). Those take
DAYS (p90 = 94–104h). Stops resolve in HOURS. A 5-hour cap therefore keeps
essentially all the losers and truncates essentially all the winners. Expectancy
falls monotonically with every reduction in horizon: 5d +0.551 → 8h +0.197 →
5h +0.132 → 2h +0.096, and modern-era significance is lost below ~8h.

**VERDICT: the owner's 5min–5h holding constraint is INCOMPATIBLE with the only
validated edge found in this study. Not a tuning problem — a structural one.**

## H21/H22 — LEVEL FAMILY on RECENT DATA + PORTFOLIO POOLING

### H21 — last 2 years, every level type, BREAK vs BOUNCE
**The cleanest behavioural result of the entire study:**

| level | BTC bounce | ETH bounce | BTC break+filter | ETH break+filter |
|---|---|---|---|---|
| donchian20 | **−0.293 (t −2.91)** | −0.123 | −0.073 | **+0.252** |
| donchian10 | **−0.329 (t −3.59)** | **−0.213 (t −2.41)** | −0.019 | **+0.248** |
| round | **−0.282 (t −3.13)** | **−0.195 (t −2.00)** | +0.045 | +0.106 |
| **pdh_pdl** | **−0.355 (t −3.95)** | −0.057 | **+0.194** | **+0.280** |
| swing | **−0.300 (t −2.76)** | −0.071 | +0.095 | +0.075 |

**FADING A LEVEL (bounce) LOSES ON EVERY LEVEL TYPE, BOTH SYMBOLS** — significantly
negative on BTC. **Trading the BREAK is positive**, especially with the trend filter
(ETH 5/5 level types positive). Best single level = **PDH/PDL**.
Hold times 18–25h, so the owner's 5-hour ideal is not met by any variant.

### H22 — portfolio of all 5 level types (monthly pooling handles correlation)
| era | months | mean | NWt | pos months | cells positive |
|---|---|---|---|---|---|
| **full 9y** | 107 | **+0.265R** | **+3.75** | 66% | **10/10** CI[+0.133,+0.409] |
| last 4y | 49 | +0.050R | +0.53 | 57% | 7/10 |
| last 2y | 25 | +0.071R | +0.91 | 60% | 8/10 |

**CONCLUSION (consistent with every other test in this study):** the level-breakout
edge is **strongly significant over 9 years (10/10 cells positive, t 3.75)** but is
**positive-but-not-significant in the last 2–4 years**. 2 years simply does not
contain enough independent trades to prove a t>2 result at this expectancy.

Directionally the recent data AGREES (8/10 cells positive, 60% positive months); it
cannot independently CONFIRM. This is the signature of either (a) a real edge in a
multi-year drawdown — normal for trend-following — or (b) genuine decay. The data
available cannot distinguish these two.

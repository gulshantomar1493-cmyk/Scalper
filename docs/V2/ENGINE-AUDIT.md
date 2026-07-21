# Trade Engine V2 — Forensic Trading Audit

**Reviewer stance:** discretionary trader / ICT+SMC+Wyckoff practitioner first,
quant + architect second. **Assumption: the first implementation is wrong until
proven otherwise.** Trading quality reviewed before code quality.

**Subject:** `core/setup_engine.py` (V2 engine) + the reused `core/htf.py`
(HTF bias + market story) it depends on.

---

## Executive verdict (read this first)

The engine has the right *skeleton* — top-down gate, reuse of no-repaint frozen
detections, a "no-trade" default, explainability — but as a **discretionary
trader it is not yet trustworthy**. Three defects are disqualifying:

1. **It manufactures false confidence.** It outputs `confidence: 95.5` — a
   fabricated percentage to one decimal, produced by an arbitrary weighted sum
   (35/15/20/15/10/5). No trader believes a trade is "95.5% confident," and the
   platform's own §0.3 rule bans fake confidence %. This is the exact thing the
   audit brief flagged.
2. **A setup can be graded "high-probability" while sitting in a bad location.**
   The location pillars (premium/discount, a real zone) are *optional points*, not
   requirements. `HTF-aligned (35) + sweep/shift (20) + HTF-strength (15) = 70` —
   the surface threshold — with **no discount and no order block**. That is a
   middle-of-nowhere entry sold as high-probability. A professional would pass.
3. **The market story is an indicator summary, not a narrative.** It concatenates
   per-timeframe trend+bias+structure and even prints contradictions ("Daily
   uptrend, bearish bias"). It never says who controls price, which liquidity was
   taken, where the draw is, or where price goes next and why — the questions the
   brief lists.

**Verdict: REFINE before Phase 3.** The fixes are more *simplification* than
addition — replace arbitrary scoring with hard necessary-conditions + an emergent
grade, and replace the summary with a real narrative.

---

## 1. Component-by-component challenge

| Component | Is this how a pro thinks? | Finding |
|---|---|---|
| **HTF analysis** | Partly | Right instinct (top-down). But the HTF *bias score* mixes **indicators into the directional read** — EMA alignment contributes ±2 and momentum ±0.5 of a ~±8 scale (`htf.py:294-309`). Price action must set direction; indicators only confirm. Here EMA can *swing the bias*. |
| **LTF analysis** | Weak | The engine consumes only the frozen `StrategyEngine` signals (S1/S2/S3) as triggers. It never *reads the LTF chart itself* — so it is really "gate + explain a mechanical signal," not discretionary LTF reasoning. If the mechanical strategy doesn't fire, a clean discretionary setup is invisible. |
| **Market story** | No | Indicator summary; contradictory; answers none of the narrative questions. See §3. |
| **Liquidity** | Partly | Uses `liquidity.pools` / `sweeps` / `shifts`, but only as a boolean "sweep happened." It does **not** identify the *draw on liquidity* (the nearest unswept pool price is heading to) — the single most important read in SMC. |
| **BOS / CHOCH** | OK (reused) | Detection is the frozen, no-repaint engine — sound. But the engine ignores an HTF **CHOCH against the bias** (a transition warning); it keeps trading the old bias. |
| **Swings / EQH / EQL** | Missing detail | Uses generic pools; no explicit equal-high/equal-low reasoning ("the double top's liquidity is the target"). |
| **Liquidity sweeps** | OK | The sweep→shift pairing (frozen) is the correct high-probability trigger. Good. |
| **Order blocks / FVG / S&D** | Coarse | Folded into a generic "confluence near entry" proximity test. A pro is specific: "entry at the unmitigated 1H bullish OB / into the FVG." The engine can't name *which* zone. |
| **Trend** | OK (reused) | Fine as context; not used as a trigger (correct). |
| **Premium / Discount** | Present but optional | Checked on the LTF range — good — but only *awards points*; it should be a **requirement**. And **HTF** premium/discount is ignored: the engine will long into HTF premium as long as the HTF trend is up. A pro won't. |
| **EMA** | Misused | Feeds the HTF *bias* (see above). Should be confirmation only. |
| **Volume** | Crude | `rvol ≥ 1.2 and cum_delta sign`. `cum_delta` is a *session* cumulative — its sign isn't "this move's" delta. Weak confirmation. |
| **Momentum** | Minor | Only an indirect input via the HTF score. Acceptable as confirmation. |
| **Risk planning** | Reused but gross | R:R is `|tp−entry|/|entry−sl|` — **gross of fees**. The frozen `risk.plan_trade` computes *net* RR; the engine should use it. |
| **Entry** | Trusted blindly | Taken from the signal; not re-validated against a *named* zone. |
| **Stop loss** | OK | Structural (beyond the swept extreme) — correct in spirit (it comes from the frozen strategy). |
| **Take profit** | Trusted blindly | TP1/TP2 from the signal; not re-verified to sit *at the next liquidity pool*. A pro targets liquidity, not an R multiple. |
| **Invalidation** | Thin | A single "close beyond SL" line. Missing the *soft* invalidations a trader watches (failure to displace, opposing CHOCH, time-based). |

---

## 2. Confluence logic — the core critique

**Current:** `confidence = 35·aligned + 15·htf_conf + 20·sweep_shift + 15·pd +
10·confluence + 5·volume`, surface if `≥ 70`.

**Why this is wrong for discretionary trading:**
- **Arbitrary weights.** Why is HTF-alignment 35 and confluence 10? There is no
  market basis for these ratios; they're invented. Two setups scoring 72 and 88
  imply a precision that does not exist.
- **Fungible pillars.** Because everything is additive, a *necessary* condition
  (being in discount) can be "bought" with *optional* ones (volume + HTF strength).
  That's how a bad-location trade reaches 70. A professional treats location as a
  **gate**, not a tie-breaker.
- **False precision → false confidence.** `95.5` is indefensible to a trader.

**Better approach (confidence emerges from agreement):**
Split into **necessary conditions** (all must hold or there is *no* setup) and
**supporting confluences** (each is an independent, real agreement). The **grade**
is then simply *how many independent confluences agree* on top of a valid base —
confidence *emerges*, it isn't dialed in.

- Necessary: fresh trigger · HTF-aligned (never counter a convinced HTF) · **entry
  in the correct half of the range** · sweep→shift present · net R:R ≥ 1.5 · data
  integrity.
- Supporting (each = one real agreement): a **named** zone (OB/FVG) at entry ·
  HTF also in discount/premium · strong HTF conviction · volume confirms ·
  no opposing HTF level in the path.
- Grade = A+ (≥4 agree) / A (2–3) / B (0–1). Displayed as a **grade + "N of M
  confluences"**, never a percentage.

This is *simpler* (no magic weights) and *more honest* (a B setup is openly "valid
but thin"). It directly fixes defects #1 and #2.

---

## 3. Market story — the second core critique

**Current output (prod):** *"Higher-timeframe bias is BULLISH (72.1/100, 60%
agreement). Daily uptrend, bearish bias (HH/HL, recent CHOCH DOWN). 4H uptrend,
bullish bias… 15M uptrend, bullish bias (…CHOCH DOWN)."*

**Problems:** it's a per-TF readout; "uptrend, bearish bias" is a self-contradiction
a human would never utter; it leans on fabricated numbers; and it answers **none**
of: who controls price · which liquidity was taken · where the draw is · where
trapped traders sit · trending/ranging/transitioning · where price goes next & why.

**Better:** build the narrative in the engine (it has the *LTF liquidity* data the
HTF service lacks):
> "Buyers control the higher timeframe (4H/1H uptrend). Price just **swept the
> sell-side liquidity** below the 96.4 low and **shifted up (CHOCH)** — the late
> shorts from that low are now **trapped**. The **draw is the buy-side liquidity
> at 104.5** (the unswept equal highs). Expect price to seek that pool; the 1m
> long into the discount order block is the execution of that story. Caution: the
> **daily just printed a CHOCH down** — this is a with-trend continuation, not a
> fresh trend, so it's a *reaction* trade, not a *position*."

That reads like a trader. It's built from data already present (`pools`, `sweeps`,
`shifts`, `bias`, `trend`, `choch`) — no new detection, just honest synthesis.

---

## 4. Trade-setup completeness

The brief requires fields the engine omits. A professional setup card needs:
**setup type · expected holding time · market context · primary confluence ·
secondary confluence · reasons to avoid · early-exit conditions · trade-management
notes** — on top of the six "why"s (which the engine already has). The most
important omission is **"reasons to avoid"**: a professional *always* states the
bear case for their own idea. An engine that only argues *for* a trade breeds
overconfidence.

---

## 5. Missing concepts

- **Draw on liquidity** (nearest unswept pool = where price is headed). *The* SMC read.
- **HTF premium/discount** (don't buy HTF premium in an uptrend).
- **HTF transition awareness** (an HTF CHOCH against bias = downgrade / "transitioning").
- **Trapped-trader identification** (the point of a sweep).
- **Named zones** (which OB/FVG, mitigated or not) rather than generic proximity.
- **Reasons-to-avoid / early-exit / management** (the professional's risk humility).
- **Net-of-fees R:R** (use the frozen `risk.plan_trade`).

## 6. Incorrect assumptions

- That a **weighted sum of pillars** models a trader's conviction. It doesn't;
  a trader gates on necessary conditions, then counts independent agreement.
- That **location is optional**. It is the trade.
- That **EMA/momentum may set bias**. Price action sets bias; indicators confirm.
- That a **5-bar (≈5 min) validity window** matches discretionary timing. HTF-level
  setups breathe longer; the window should scale with the trigger timeframe.
- That the **frozen signal's TP is at liquidity**. It should be verified, not assumed.

## 7. Overengineering / simplification

- The six-term weighted formula is *more* complex than the honest model and *worse*.
  Replace with necessary-gates + a confluence count. **Net simpler.**
- The one-decimal confidence is complexity that manufactures a false signal. Drop it.
- Keep reusing the frozen detections and `HtfService` bias — that reuse is correct.

## 8. Recommended improvements (priority order)

1. **Kill the fake %.** Emergent grade (A+/A/B) + "N of M confluences." *(critical)*
2. **Make location necessary.** Correct half of range + sweep→shift are gates, not
   points; no setup otherwise. *(critical — fixes false high-probability)*
3. **Real market narrative** built from HTF bias + LTF liquidity (control / taken /
   draw / trapped / phase / next & why). *(critical)*
4. **Add the missing card fields**, especially **reasons-to-avoid**, plus setup
   type, holding time, primary/secondary confluence, early-exit, management notes.
5. **HTF premium/discount + HTF-CHOCH downgrade** (don't buy HTF premium; flag
   transitions).
6. **Net-of-fees R:R** via `risk.plan_trade`; verify TP sits at a liquidity pool.
7. *(Later)* let the HTF bias be price-action-only (EMA/momentum → confirmation),
   inside `htf.py`. Deferred: it changes the HTF panel and needs its own pass.

---

## Final verdict

**Skeleton: sound. Trading behaviour: not yet trustworthy — REFINE now.** The
required changes make the engine *simpler and more honest*: hard gates on the
things that actually matter (bias, location, trigger, R:R), an emergent grade
instead of a manufactured percentage, and a story that sounds like a trader. Items
1–6 will be implemented before Phase 3; item 7 (reworking HTF bias to be
price-action-only) is logged for a dedicated HtfService pass so it doesn't
destabilise the existing HTF panel.

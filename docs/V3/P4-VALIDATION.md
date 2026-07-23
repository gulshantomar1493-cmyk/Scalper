# V3 P4 — Replay & Performance Validation: findings (honest)

Tool: `scripts/v3_replay.py` — replays any range through the FULL V3 stack
(no lookahead), simulates outcomes (limit fill, SL-first ambiguity, MAE/MFE,
24h horizon), reports win rate / expectancy / PF / max drawdown / per-grade /
per-session splits + FALSE trades + MISSED trades. Deterministic.

## Iteration log (BTCUSDT)

| run | issued (7d) | overall exp | A+ (7d) | note |
|---|---|---|---|---|
| v0 baseline | 346 | −0.23R | +0.42R (n=10) | over-issuance; 43% expired |
| v1 rules    | 152 | −0.28R | +0.67R (n=13) | floor 3 factors · edge entry · fuel guards |
| v2 rules    | 123 | −0.31R | +1.02R (n=11), PF 3.81 | counter-trend + trend-session guards |
| v3 rules    |  84 | −0.25R | +0.30R (n=14) | B issuance killed |
| **30-day**  | 517 | **−0.23R** | **−0.41R (n=78)** | **7d A+ edge did NOT generalize** |

## Verdict (no fake confidence — the numbers say it)

- The engine now BEHAVES like a trader: ~12 setups/day (was 50), honest
  watchlist, explainable confluence, honest avoid-reasons. Structure works.
- **Expectancy is NOT yet positive over a month.** Win rate 19% at ~2R targets
  (needs ~33%). The 5m candle-pattern confirmation at zones is a weak filter in
  a trending month; gates block winners and losers alike (841 missed ≥2R runs).
- Rules kept as PRINCIPLED (not curve-fit): ≥3-factor floor, zone-EDGE entry,
  counter-trend needs sweep/CHOCH, trend-session fades need structure, no B
  issuance. Session min-grade pins are CONFIG (sweepable), not truths.

## Root-cause hypotheses for the calibration campaign (owner-guided, the tool exists for this)

1. **Confirmation quality** — require displacement/CHOCH always (drop plain
   wick/engulfing), or confirm on 15m instead of 5m.
2. **Zone selection** — reversals only at stack ≥2 with an HTF (1h+) component;
   pure 5m zones are noise in trends.
3. **Bias-aligned only** — issue only with the ladder (counter-ladder = watch only).
4. **Archetype gap** — trend sessions need the BREAKOUT/BREAKDOWN archetype
   (designed, not yet built); fading them is structurally wrong.
5. **Target model** — TP1 = nearest pool is often noise-close; consider
   priority-≥4 pools only.

Each is one config/logic change + one replay run = objective before/after.

## Status

- Deployed engine = decision-support with honest grades + avoid-reasons +
  paper trading only; NOT validated for expectancy. The §0 "validate before
  trust" discipline stands: no strategy is TRUSTED until the replay (and then
  live logging) shows positive expectancy after fees.
- P4 tooling COMPLETE (replay engine + report + error scans). Calibration
  campaign + Engine-vs-Trader chart benchmark = owner-operated with this tool.

# D29 — Remove the G3 session-filter gate (keep G1)

**Status:** Accepted (owner decision, 2026-07-20)
**Supersedes:** the D24.1 part that made G3 a real gate (the rest of D24 stands)
**Engine:** `engines/qualification.py` — `ENGINE_VERSION` 2 → 3

## Context

§6 defines Stage-1 hard gates G1–G6 (any fail → `NO_SIGNAL`, score never shown).
**G3** was the *session filter*: a pure function of `candle.ts.hour` that failed the
whole bar during the **LATE** session (21:00–00:00 UTC = **02:30–05:30 AM IST**),
the thin-liquidity gap between the NY close and the Asian open. It was made a real
gate at D24.1 (which bumped `ENGINE_VERSION` 1 → 2).

The owner reviewed the gate roster and judged G3 unhelpful.

## Decision

**Remove G3 as a hard gate. Keep G1.**

The gate roster becomes **G1, G2, G4, G5, G6** (five). Labels G4/G5/G6 are **kept,
not renumbered**, so their §6 meanings (news / psychology / reward-to-risk) stay stable.

## Rationale

1. **Crypto is 24/7.** BTC/ETH trade continuously and globally; the LATE window is
   *lower-volume*, not *closed* (unlike stocks/forex where it would be a dead
   session). The "thin liquidity → suppress signals" premise is weak here.
2. **Decision-support, not auto-execution** (v1.2 scope). The owner executes manually
   and judges each setup's liquidity. A hardcoded time-of-day block is paternalistic
   for a tool that only *recommends*.
3. **Evidence over assumption** (§0 rule 4, "validate before trust"). The platform
   already breaks performance down **per session** (`analytics.session_of`,
   `GET /analytics`). Rather than hardcode "LATE is bad", generate LATE setups and let
   the per-session expectancy **prove** whether LATE underperforms — then, if it does,
   re-introduce a filter *with the evidence* (a new decision). This mirrors the
   forensic-investigation discipline: reproduce/measure, don't assume.

## Why G1 is kept

G1 is the **data-integrity** gate (30-candle continuity + clock sync) and the source
of the **"Data Integrity: PASS/DEGRADED"** badge (§0 rule 3, the two-tier display).
It is the live-feed health signal — how a silent Binance gap or clock drift becomes
visible. Removing it would gut a locked §0 discipline; it stays a hard gate.

## Consequences

- `ENGINE_VERSION` (qualification) **2 → 3** — the D1 stamp distinguishes
  pre/post-G3-removal signals (`<git>+…;qualification=3;…`). First stamp change since
  D24.1; all other engines stay at 1.
- `data_integrity` is still `G1 ∧ G2` (unchanged — G1/G2 are gate indices 0/1).
- The frontend quality panel shows **five** gates (the "Session" card is gone).
- Positional gate indices shifted for consumers that read G5 by position
  (`gates[4]` → `gates[3]`); `g1_ok = gates[0]` and `gates[0]∧gates[1]` are unaffected.
- **Determinism (V1–V4) stays green.** The §10 harness is a *self-consistency* check
  (replay twice, assert `stream_hash(first) == stream_hash(second)`) — it pins no
  absolute hash. The absolute payload bytes change (the `gates` array drops the G3
  entry), but the byte-identical property holds. V1–V4 span ASIA/LONDON/NY only, so
  G3 always *passed* for them; its removal does not change their `NO_SIGNAL` bars
  (all G1 warm-up), and the harness's only verdict guard is exactly that G1 warm-up
  `NO_SIGNAL` — which survives because G1 is kept.

## Unchanged / out of scope

- `session_of` (`engines/liquidity.py`) stays — it drives the liquidity engine's
  key-level rollovers **and** the analytics per-session breakdown (the very mechanism
  that now evaluates LATE performance). Only qualification's *use* of it for a gate is
  removed.
- G4 (news/events blackout) remains owner-operational pending the A11 `events.yaml`.
- No architecture change: no engine added/removed; §6's scoring, weights, verdicts,
  and G1/G2/G4/G5/G6 semantics are untouched.

## Reversibility

If the per-session analytics later show LATE-session recommendations materially
underperform (net of fees), a session filter can be re-introduced **with that
evidence** as a new decision, bumping `ENGINE_VERSION` again.

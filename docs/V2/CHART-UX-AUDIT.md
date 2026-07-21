# Phase 3 — Professional Chart UX Audit

**Stance:** reviewed as a futures trader who lives in Bloomberg / TradingView /
Sierra / Bookmap / ATAS — judging **workflow**, not feature lists. Grounded in the
real current DOM (not memory): **6 right-rail cards, 4 bottom tabs, 7 chart-area
widgets, 2 competing signal systems.**

## The measure that matters: the 3-second test

Can the eye answer each in ≤3s without scrolling or reading a paragraph?

| Question | Where it lives today | Pass? |
|---|---|---|
| **Best setup?** | "Trade Setup" card, top of rail | ✅ (M1 fixed this) |
| **Entry?** | inside the setup card | ✅ |
| **Risk?** | setup card `risk_level` | ✅ |
| **HTF bias?** | HTF panel — the **4th card down**, must scroll | ⚠️ slow |
| **Trend?** | appears in **3 places** (Market Context, HTF, Market Structure) | ❌ scattered |
| **Liquidity?** | only as chart overlays + the Market-Structure stream — no labeled readout | ❌ must decode the chart |
| **Expected move?** | buried inside the `market_context` sentence / the targets | ❌ not a distinct element |

**Score: 3 / 7 instant.** The setup is clear; the *market read that justifies it*
is scattered. A pro would not call this trade-ready at a glance.

---

## 1. UI Problems (what the eye sees)

1. **Rail overload — 6 stacked cards.** Trade Setup · Live Signal · Market
   Structure · HTF · Trade Plan/RR · Market Context. A tall column the eye must
   scan top-to-bottom every time. Too many surfaces compete for attention.
2. **Two competing "answers."** `Trade Setup` (V2) **and** `Live Signal` (V1) both
   show a direction + grade. A trader seeing two verdicts asks *"which do I
   trust?"* — the worst thing an interface can do at the moment of decision.
3. **Redundant cards.** `Trade Plan · Risk/Reward` duplicates the setup card's
   entry/stop/TP/R:R. `Market Context` duplicates the setup's `market_context` +
   trend. Three cards, one idea.
4. **The chart competes with price.** 7 chart-area widgets (legend, crosshair box,
   countdown, loading, paper-trade widget, order lines, chart) + structure boxes +
   VWAP bands + premium/discount shading + OB/FVG zones + pool lines, all on at
   once, can bury the candles. The principle "nothing competes with price" is
   violated whenever everything is enabled.
5. **Dev tabs in the trading view.** Bottom tabs are Signals · **Console** ·
   Trade Review · **Activity**. Console/Activity are operator tooling — they do not
   help a trade decision and add visual noise to the workspace.

## 2. UX Problems (the workflow)

1. **Cognitive load is high.** 6 cards + 2 toolbars + 4 bottom tabs + a tools
   drawer = far more decision surfaces than a futures trader keeps in view.
2. **Eye movement zig-zags.** To read the market the eye jumps chart → HTF (card 4)
   → Market Structure (card 3) → Market Context (card 6) → back to Setup (card 1).
   Professional screens flow **once**, top-down; this one bounces.
3. **Decision speed is gated by the 4 buried answers.** Trend / liquidity / HTF /
   expected-move must be *assembled* from multiple cards before a go/no-go.
4. **Redundancy forces reconciliation.** The trader must mentally reconcile V1 vs
   V2 and the duplicated plan/context — pure wasted effort.
5. **Trade readiness is incomplete.** The *trade* is legible; the *context that
   makes it a trade* is not co-located with it.

## 3. Professional Trading Workflow (how a futures trader actually reads a screen)

In seconds, in this fixed order:

1. **Price + structure** — "what is happening right now?" (the chart)
2. **Bias** — "which way am I leaning?" (HTF)
3. **Liquidity** — "what's been taken, where is the draw?" (where price is pulled)
4. **The setup** — "is there a trade, where's entry, what's my risk, what's the
   target?"
5. **Act + manage** — execute manually, then manage.

The screen must present these **in that order, each answerable at a glance**, so
the eye flows once and the hand acts. Everything else is reference, not workspace.

## 4. Redesigned Layout

```
┌───────────────────────────────────────────────────────────────────────────┐
│ SYMBOL  ▸ 1m 5m 15m 1h 4h 1d …   ● live 12ms          ☾  ⤢fullscreen  ⋮   │  top bar (thin)
├───────────────────────────────────────────────────────────────────────────┤
│ MARKET STATE  ▸ Trend: ▲Up  │ HTF: BULLISH · strong 100% │ Liquidity: buy-  │  ← the 3-second strip (NEW)
│  side draw 69,200 (sell-side swept) │ Expected: ↑ to 69,200 │ SETUP: LONG A+ │     all from /api/setups+/api/htf
│  │ Risk: LOW                                                                  │
├──────────────────────────────────────────────────────────────┬────────────┤
│                                                                │ ► TRADE    │
│                                                                │   SETUP    │  right rail (2 cards, not 6)
│                        C H A R T   (price = hero)              │  LONG  A+  │
│         structure + the ACTIVE SETUP drawn on price            │  entry/stop│
│         (entry/SL/TP lines + R:R zone). Extra overlays          │  /tp/rr    │
│         (VWAP, indicators) OFF by default, one toggle.          │  reasons ▾ │
│                                                                │────────────│
│                                                                │ ► HTF      │  compact HTF (bias·conv·
│                                                                │   1D 4H 1H │  agree + tiny tf cards)
│                                                                │   15M      │
└──────────────────────────────────────────────────────────────┴────────────┘
   (Signals · Trade Review as a slim collapsible; Console/Activity move to Settings/Ops)
```

**The key move — a "Market State" strip.** One horizontal band between the top bar
and the chart with 6 tiles: **Trend · HTF Bias (+conviction·agreement) · Liquidity
(draw / swept) · Expected Move · Best Setup (dir+grade) · Risk.** Every value comes
straight from the frozen contract (`/api/setups` + `/api/htf`) — no client logic.
This alone converts the 4 failing 3-second questions to instant.

**Rail: 6 → 2 cards.**
- **Trade Setup** (the decision) — absorbs `Trade Plan/RR` (already has entry/stop/
  TP/R:R) and `Market Context` (already has `market_context`).
- **HTF** — compact, visual (bias · conviction · agreement + four tiny tf tiles).
- **Remove `Live Signal` (V1)** from the trading view — one answer, not two. (The
  V1 recommendation system stays available elsewhere / Trade Review; it does not
  compete on the chart.)
- **Market Structure** stream → a thin ticker under the strip or on-chart markers,
  not a full card.

**Chart = hero.** Same size. Default overlays = structure + the active setup only.
VWAP / indicators / extra zones behind a single toggle so **price always wins**.
Fullscreen is one click.

**Bottom.** Console + Activity leave the trading view (they're ops — Settings/Ops
page). Signals + Trade Review become one slim, collapsed-by-default strip.

**Net:** eye flow becomes *chart → state strip → setup* (one pass); surfaces drop
from ~6 cards + 4 tabs to 2 cards + 1 strip + 1 slim tab-row.

## 5. Implementation Order

1. **Consolidate the rail (6 → 2).** Remove the V1 Live-Signal card; fold Trade
   Plan/RR + Market Context into the Setup card. *(biggest clutter win, lowest risk)*
2. **Build the Market State strip.** The six glance tiles from the frozen contract —
   the fix for the 3-second test. *(highest decision-speed win)*
3. **Draw the active setup on the chart** (entry/SL/TP lines + R:R zone + dir/grade
   tag) and set the **minimal-overlay default** (price wins).
4. **Market Story → concise sections** folded into the strip + compact HTF panel
   (no paragraph on the workspace).
5. **De-clutter the frame.** Move Console/Activity to Ops; slim the bottom; compact
   the drawing/indicator toolbars into popovers.
6. **Drawing persistence** (localStorage, per-symbol; survive refresh/symbol/TF) +
   the R:R + notes tools.
7. **Final pass — "would I trade from this every day?"** Re-run the 3-second test;
   if any answer is slow, iterate.

## Verdict

**Would I trade from the current screen every day? No** — it's dense and makes me
reconcile two verdicts and assemble the market read from four places. **Would I
trade from the redesign? Yes** — one glance strip answers *what/which-way/where/
is-there-a-trade*, the chart owns price, and the setup owns the decision. The goal
is not fewer pixels; it is a **single, fast read → act** path.

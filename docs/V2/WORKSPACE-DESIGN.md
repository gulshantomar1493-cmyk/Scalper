# Phase 3 — Trading Workspace Design (FROZEN)

**Status:** FROZEN before implementation. Backend + API frozen (contract v1.0);
this freezes the *information design* of the workspace so M2–M5 are assembly, not
rework. Feel target: **Bloomberg / Sierra / Bookmap / ATAS** — a dense, ordered
terminal, not a web dashboard of cards.

**The core idea:** the screen is a **conversation the eye reads in one pass**. The
seven questions are answered in order, each in a fixed place, price is the anchor,
and the *decision* (setup / entry / invalidation) lives **on price** so the eye
never leaves the anchor to act.

Every value is displayed straight from the frozen contract (`/api/htf`,
`/api/setups`). The frontend never derives trading logic.

---

## The seven questions → where the eye lands

| # | Question | Answered by | Where (zone) | Source (frozen contract) |
|---|---|---|---|---|
| 1 | What is the market doing? | **Trend** tile | Context strip, tile 1 | htf per-tf `trend` / setup `ltf_trend` |
| 2 | Who controls the market? | **Control** tile | Context strip, tile 2 | htf `overall.bias` + `conviction` (BULLISH→Buyers…) |
| 3 | Where is liquidity? | **Liquidity** tile | Context strip, tile 3 | htf `liquidity_sweep` (taken) + `liquidity` pools (resting) |
| 4 | Where is price likely going? | **Draw** tile | Context strip, tile 4 | setup `tp1`/target, else strongest HTF pool in the bias direction |
| 5 | Is there a setup? | **Setup** tile (dir + grade) | Context strip, tile 5 | setup `direction` + `grade`, else `message` |
| 6 | Where do I enter? | **Entry line** | **On the chart** (on price) | setup `entry` |
| 7 | When am I wrong? | **Stop line** ("invalidation") | **On the chart** (on price) | setup `sl` + `invalidation` |

Q1→Q5 read **left-to-right** across one thin strip (the market conversation).
Q6→Q7 are drawn **on price** (the action). The full case (grade_reason, why,
reasons-to-avoid, management) is one glance to the right — reference, not path.

---

## 1. Final Workspace Layout

```
┌──┬────────────────────────────────────────────────────────────────┬──────────┐
│  │ BTCUSDT ▾   1m 5m 15m 1H 4H 1D    ● live 12ms      ☾  ⤢  ⋮       │          │ top bar (44px)
│ n│────────────────────────────────────────────────────────────────│          │
│ a│ ①TREND      ②CONTROL      ③LIQUIDITY       ④DRAW      ⑤SETUP      │  SETUP   │ CONTEXT STRIP (48px)
│ v│ ▲ Uptrend   Buyers·strong  swept 67,380↓   → 69,200   LONG  A+   │  ──────  │  Q1→Q5, one sweep
│  │             (100% agree)   resting 69,200↑            (tap ▸)    │  LONG A+ │
│ i│────────────────────────────────────────────────────────────────│  Grade   │
│ c│                                                                  │  reason… │ RIGHT RAIL (300px)
│ o│                                                                  │  entry   │  1) Setup detail
│ n│                  C H A R T   —  price is the anchor              │  stop    │     (mirrors chart)
│ s│         muted structure overlays + the ACTIVE SETUP:            │  tp1/tp2 │  2) HTF compact
│  │           ┈┈┈ TP2 69,900 ┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈ (green)              │  R:R 2.1 │
│  │           ─── TP1 69,200 ───────────────── (green)              │  reasons │
│  │           ━━━ ENTRY 68,120 ━━━━━ [LONG A+] ━ (bright)  ⑥         │  avoid ▾ │
│  │           ─── STOP  67,380 ───── invalidation ─ (red)  ⑦         │──────────│
│  │                                                                  │  HTF     │
│  │                                                                  │  BULLISH │
│  │                                                                  │  1D 4H   │
│  │                                                                  │  1H 15M  │
└──┴────────────────────────────────────────────────────────────────┴──────────┘
```

**Zones**
- **Nav rail (52px, icons):** page switch only; collapsed by default.
- **Top bar (44px):** symbol · timeframe buttons · connection/latency · theme ·
  fullscreen · overflow (⋮ = replay, tools, drawings — off the workspace surface).
- **Context strip (48px, full width):** five tiles = Q1→Q5, left-to-right.
- **Chart (hero, fills):** price anchor; muted structure overlays; the active
  setup drawn on price (entry/SL/TP + a `LONG A+` tag at entry) → Q6, Q7.
- **Right rail (300px, TWO stacks):** ① Setup detail (the "if yes" — grade_reason,
  entry/stop/tp/R:R, reasons, expandable avoid/why/manage); ② HTF compact
  (bias · conviction · agreement + four tiny tf tiles).
- **No bottom tabs on the workspace.** Console/Activity → Ops. Signals/journal →
  their own pages (nav rail).

## 2. Eye-Movement Diagram

```
                      ┌── ① → ② → ③ → ④ → ⑤ ──┐   (one horizontal sweep = Q1..Q5)
                      │   the context strip     │
                      ▼                         │
        ┌─────────────────────────────┐        │
        │            (0)              │        │   (0) ANCHOR: eye lands on price
        │        P R I C E  ← anchor  │◀───────┘
        │        ⑥ entry line         │            ⑥ drop to entry (on price)
        │        ⑦ stop  line ────────┼──▶ ⑧        ⑦ stop just below entry
        └─────────────────────────────┘            ⑧ optional: glance right = full case
```

**Path:** `(0) land on price → sweep the strip ①..⑤ (Q1..Q5) → drop to the entry
& stop lines on price (Q6, Q7) → [optional] right to the setup card.`
- **Total travel:** one anchor + one horizontal sweep + one short vertical drop.
- **The decision never leaves the anchor** — entry/invalidation are *on price*
  (Bookmap/Sierra behaviour), not in a side panel.
- No diagonal zig-zag, no hunting. If there is **no setup**, the path stops at ⑤
  ("No setup") and the chart stays clean — the eye rests on price.

## 3. Information Hierarchy (emphasis, loudest → quietest)

- **L1 — Price + the setup lines on it.** Largest area; candles dominant; entry
  line the single brightest line; stop red; TPs dashed green. This is the anchor
  and the decision.
- **L2 — The context strip (Q1–Q5).** Medium: labelled tiles, tabular numerals,
  semantic color (up/down/neutral), but never louder than price. The Setup tile
  brightens only when a setup exists.
- **L3 — The Setup card + HTF card.** Reference detail; muted until looked at;
  smaller type; expand-on-demand.
- **L0 — Removed from the workspace** (answers none of the 7): the V1 "Live
  Signal" card, the "Trade Plan/RR" card, the "Market Context" card (all folded),
  the "Market Structure" stream card (→ on-chart markers), the Console + Activity
  bottom tabs, and any always-on VWAP/indicator overlay (→ one toggle).

**Feel:** flat tiles + hairlines (no rounded web cards), tabular/mono numerals,
tight spacing, dark-dominant, price-forward — a terminal, not a dashboard.

## 4. Interaction Flow (clicks, minimized)

| Action | Clicks | Notes |
|---|---|---|
| Read market + setup | **0** | the strip + on-chart lines answer Q1–Q7 |
| Full setup case | 1 | expand the Setup card (or tap the strip's Setup tile) |
| Switch symbol / timeframe | 1 | top bar (also keyboard hotkeys) |
| Fullscreen | 1 | top bar ⤢ |
| Draw / annotate | 1 | tool popover → draw → **auto-persists** (no save dialog) |
| Toggle extra overlays / replay / tools | 1 | behind the ⋮ overflow — off the primary surface |

No modal dialogs, no popups in the normal read→act loop. Everything the trader
needs to decide is visible without a click.

## 5. Implementation Plan (execution order; each maps to zones above)

1. **Consolidate the rail (6 → 2).** Remove the V1 Live-Signal card; fold Trade
   Plan/RR + Market Context into the Setup card; demote Market Structure to
   on-chart markers. *(L0 cleanup — the biggest clutter + redundancy win.)*
2. **Context strip (Q1–Q5).** Five tiles from the frozen contract; the fix for
   the 3-second read. *(the highest decision-speed win; also anchors the layout.)*
3. **Setup on price (Q6, Q7).** Draw entry/SL/TP lines + the `LONG A+` tag on the
   chart; set the **muted-overlay default** so price dominates.
4. **Setup card = reference + HTF compact.** grade_reason / why / avoid / manage,
   mirroring the chart; HTF as four tiny tf tiles + bias/conviction/agreement.
5. **De-clutter the frame.** Console/Activity → Ops; drawing + indicator toolbars
   → a single ⋮ popover; fullscreen.
6. **Drawing persistence** (localStorage, per-symbol; survive refresh/symbol/TF) +
   the R:R + notes tools.
7. **Final pass — the 7-question / 3-second test.** Every answer ≤3s or iterate.

## Freeze

This layout, eye path, hierarchy, and order are **frozen**. Implementation follows
this document exactly; any deviation is a design change, not an implementation
choice. Price is the anchor; the strip is the conversation (Q1–Q5); the decision
lives on price (Q6–Q7); everything that answers none of the seven questions is
gone.

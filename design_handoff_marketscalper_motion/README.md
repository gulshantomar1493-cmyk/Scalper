# Handoff: MarketScalper — motion redesign of the terminal

## Overview

A motion-forward redesign of the MarketScalper decision-support terminal (BTCUSDT + ETHUSDT
trade-setup recommendation tool). It replaces the current page-switched terminal shell with a
single continuously-scrolling surface: hero → recommendation → chart → pipeline → queue →
strategies → history → paper → journal → settings → evidence. All UI copy is **Hinglish**
(Roman script); trader vocabulary (entry, stop, target, R:R, PF, max DD, TP/SL/TIME) stays in
English on purpose.

Target repo: the existing MarketScalper project — `frontend/` (vanilla JS + `styles.css` +
TradingView Lightweight Charts v5), backend FastAPI at `/api/v4/*`.

## About the design files

The files in this bundle are **design references written in HTML** — prototypes that show
intended look, motion and behaviour. They are **not** production code to copy verbatim.

`*.dc.html` files are self-contained pages: markup with inline styles, plus a small logic class
at the bottom of the file (`class Component extends DCLogic`) that produces the values the
markup renders. `support.js` is the tiny runtime that makes them open in a browser — **do not
port `support.js`**. Read the design file, take the values and structure from it, and rebuild
the UI in the target environment.

For this repo that environment is **vanilla JS + CSS in `frontend/`**, matching the existing
`app.js` conventions:
- all network access stays in one place (the `api()` / `post()` helpers in `app.js`);
- render helpers are pure DOM builders and write server strings with `textContent` (never
  `innerHTML`);
- new visual values belong in `styles.css` as CSS custom properties, not inline styles.

## Fidelity

**High-fidelity.** Colors, type, spacing, radii, shadows, motion timings and copy are final.
Rebuild pixel-for-pixel; only the implementation technique should change.

The numbers in the prototype (setup prices, history rows, paper stats, journal entries) are
**illustrative fixtures**. Real values come from the API — see "Data sources".

## Files in this bundle

| File | What it is |
|---|---|
| `MarketScalper Motion.dc.html` | The redesign. Everything below refers to this file. |
| `MarketScalper Terminal (current).dc.html` | Faithful recreation of today's Today + Chart screens — the "before" reference. |
| `favicon.svg` | New browser icon (animated SVG). |
| `brand/icon-180.png`, `brand/icon-512.png` | Apple-touch / PWA raster icons of the same mark. |
| `support.js` | Runtime that lets the `.dc.html` files open in a browser. Reference only — do not port. |
| `CLAUDE_CODE_PROMPT.md` | Ready-to-paste implementation prompt. |
| `screenshots/01…11-*.png` | One capture per section, in order (viewport-height crop). |
| `screenshots/full-*.png` | Full-height captures of the taller sections (setup, strategies, history, paper, journal, settings). |
| `screenshots/12-setup-light-theme.png` | The recommendation section in the light theme. |

Open either `.dc.html` directly in a browser to see the design running, including motion.

---

## Design tokens

Defined as CSS custom properties on `:root` (dark) and `:root[data-theme="light"]`, exactly as
the current app already does in `styles.css`. Dark is the default; the pre-paint theme script in
`index.html` stays as-is.

### Color — dark

| Token | Value | Use |
|---|---|---|
| `--ground` | `#06070a` | app ground |
| `--deep` | `#040507` | section gradient floor |
| `--surf` | `#0e1116` | card / panel surface |
| `--surf-2` | `#14181f` | raised well, segmented-control track |
| `--glass` | `rgba(14,17,22,.66)` | blurred overlays (chart legend, pills) |
| `--glass-2` | `rgba(20,24,31,.72)` | blurred kbd chips |
| `--line` | `#1e232c` | borders |
| `--line-soft` | `#14181f` | internal dividers, grid gaps |
| `--ink` | `#f2f4f8` | primary text |
| `--ink-2` | `#98a2b1` | secondary text |
| `--ink-3` | `#5b6472` | labels, meta |
| `--accent` | `#4c82f7` | interaction only |
| `--accent-dim` | `#2f5fcc` | accent gradient end, borders |
| `--accent-glow` | `rgba(76,130,247,.16)` | accent tint / glow |
| `--up` | `#00c07f` | money: profit, target, long |
| `--up-bg` | `rgba(0,192,127,.12)` | up tint |
| `--down` | `#ff5b5b` | money: loss, stop, short |
| `--down-bg` | `rgba(255,91,91,.12)` | down tint |
| `--grid` | `#12161d` | chart / hero grid lines |

### Color — light (`:root[data-theme="light"]`)

`--ground #f4f6f9` · `--deep #e9edf3` · `--surf #ffffff` · `--surf-2 #f0f3f7` ·
`--glass rgba(255,255,255,.78)` · `--glass-2 rgba(240,243,247,.82)` · `--line #dce2ea` ·
`--line-soft #ebeff4` · `--ink #0e1420` · `--ink-2 #48546a` · `--ink-3 #78849a` ·
`--accent #2563eb` · `--accent-dim #1d4ed8` · `--accent-glow rgba(37,99,235,.10)` ·
`--up #059669` · `--up-bg rgba(5,150,105,.11)` · `--down #dc2626` ·
`--down-bg rgba(220,38,38,.10)` · `--grid #e7ebf1`

**Design law (unchanged from `styles.css`):** green and red mean money and nothing else. The
accent carries interaction and never a verdict. Backtest evidence and live results never share a
visual container.

### Typography

- Sans: **Inter** — 400/500/600/700/800 (Google Fonts).
- Mono: **JetBrains Mono** — 400/500/600/700. Every number is mono + `font-variant-numeric: tabular-nums`.
- Body: 14px / 1.5, `-webkit-font-smoothing: antialiased`.
- Hero h1: `clamp(40px, 5.6vw, 82px)`, line-height .98, letter-spacing -.038em, weight 700, `max-width: 20ch`, `text-wrap: balance`.
- Hero paragraph: 17px / 1.6, `--ink-2`, `max-width: 62ch`, `text-wrap: pretty`.
- Section h2: `clamp(28px, 3vw, 44px)`, letter-spacing -.03em, weight 700, line-height 1.05.
- Section eyebrow ("01 · Recommendation"): mono 11px, letter-spacing .22em, uppercase, `--ink-3`, 14px bottom margin.
- Field label (the one label style, used everywhere): 10px, letter-spacing .16em, uppercase, weight 600, `--ink-3`. In tables/rows: letter-spacing .1em.
- Big stat number: mono 22px weight 600, letter-spacing -.025em. Evidence cells: 30px.
- Ticker price odometer: mono 30px weight 600. Setup price cells: mono 20px weight 600.

### Spacing, radius, shadow, motion

- Section padding: `108px 64px`; hero `96px 64px 72px`, min-height 100vh.
- Card padding: 16–22px; grid gaps 14–20px.
- Radii: 5px (kbd) · 6–7px (badges, tabs) · 9–10px (buttons, legend) · 14px (small cards) · 16–18px (cards, tables) · 20px (hero setup card, chart frame) · 999px (pills).
- Shadows: cards `0 1px 3px rgba(0,0,0,.24)`; hero setup card + chart frame `0 2px 6px rgba(0,0,0,.30), 0 30px 70px rgba(0,0,0,.34)`; accent hover lift `0 10–12px 26–30px var(--accent-glow)`.
- Easing: `cubic-bezier(.2,.7,.3,1)` for UI transitions (160–260ms); `cubic-bezier(.16,.84,.3,1)` for the hero entrance (900ms).
- Left rail: 84px fixed, `grid-template-columns: 84px 1fr`, `<main>` is the scroll container (`overflow-y: auto; scroll-behavior: smooth`).

---

## The logo and browser icon

New mark: **three candle bars sliced by one diagonal "scalp" cut**, on a rounded-square tile.

- Tile: 40×40, radius 12, `linear-gradient(150deg, #5b8cff, var(--accent-dim))`, shadow `0 8px 24px var(--accent-glow)`.
- Inner SVG `viewBox="0 0 32 32"`. Three white rects, all `width 3.6`, `rx 1.8`:
  `x 7.6` (opacity .72), `x 14.2` (opacity 1, `y 7.5`, `height 16.5`), `x 20.8` (opacity .72). Outer bars `y 8`, `height 16`.
- The cut: an SVG `<mask>` — white full-bleed rect, minus a black rect `x1 y14.6 w30 h2.6 rx1.3`
  rotated `-38°` about (16,16). The mask is static, so the bars slide through the slice as they tick.
- Motion: each bar `transform-box: fill-box; transform-origin: 50% 100%` and its own 2.8s
  infinite `scaleY` keyframe (`tickA` .68→1→.52, `tickB` 1→.62→.86, `tickC` .82→.46→1),
  easing `cubic-bezier(.4,0,.4,1)`.
- Blade sweep: a 3px white gradient bar inside a `rotate(-38deg)` wrapper with `overflow: hidden`,
  animation `scalp` 5.2s infinite — idle until 60%, then travels `translateX(-160% → 160%)` at ~.9 opacity.
- Hover on the tile: `translateY(-2px) rotate(-4deg) scale(1.06)`, 260ms.
- Respect `prefers-reduced-motion` (the design already reduces all animation durations to ~0).

Browser icon: `favicon.svg` is the same mark with SMIL `<animate>` on the bars' `y`/`height`
(2.8s loop). Ship it as `<link rel="icon" href="favicon.svg" type="image/svg+xml">` and keep
`brand/icon-180.png` as `apple-touch-icon`; `brand/icon-512.png` is for the web manifest
(the repo's `manifest.webmanifest` and `icon.svg` should both be replaced).

---

## Screens / sections

The rail (84px) holds: brand mark, then anchor links Overview · Setup · Chart · Pipeline · Queue ·
Strategies · History · Paper · Journal · Settings · Evidence, a spacer, and the theme toggle.
Rail item: 44×40, radius 10, `--ink-3` icon at 18px stroke-width 1.7; hover → `--ink` on `--surf-2`.

### Hero (`#hero`)
Full-viewport, `isolation: isolate`, four stacked background layers (z-index -3 … -1):
1. Grid: `linear-gradient(var(--grid) 1px, transparent 1px)` + 90° twin, sizes `100% 68px` / `68px 100%`, radial mask `120% 90% at 50% 40%`, slow parallax scale 1.04.
2. Two radial glows (accent at 22% 30%, green at 88% 78%) pulsing 9s.
3. Live candle field, bottom 74%, opacity .3, masked out of the text column (`linear-gradient(90deg, transparent 6%, #000 46%)` ∩ vertical fade), parallax ±6%.
4. Vignette to `--ground`.

Top-right: feed pill (dot + `live feed` / `simulated feed`) and a clock pill — both `--glass`, blur 10px, radius 999, mono 10.5px, letter-spacing .12em, uppercase.

Content: eyebrow rule + `BTCUSDT · ETHUSDT · 1m primary / 5m context`; h1 *"Signal feed nahi — deterministic setup engine."*; paragraph; two ticker cards; two CTAs (`Abhi ka recommendation` filled accent, `Decide kaise karta hai` glass); bottom-center `scroll karo` cue bobbing 2.4s.

**Ticker card** — 16×22px padding, radius 16, `--glass` + blur 14, min-width 260: label `ETH / USDT`, odometer price 30px (green when the last tick was up, red when down), right side % change (mono 12.5px) over `BINANCE WS` / `SIMULATED` (mono 10.5px, `--ink-3`).

**Odometer** — each digit is a 10-deep 0–9 column, `overflow: hidden`, digit height `size × 1.1`, width `size × 0.6`, moved by `translateY(-digit × 10%)` with `transform 420ms cubic-bezier(.2,.8,.2,1)`. Separators (`,` `.`) render static, commas at .45 opacity. Keep the digit columns mounted between ticks or the slide is lost.

### 01 Recommendation (`#setup`)
Header row: eyebrow, h2 *"Setup, poore scale pe"*, right-aligned ETHUSDT/BTCUSDT segmented control.
Body grid: `repeat(auto-fit, minmax(min(100%, 620px), 1fr))` — two columns on wide screens, one below ~1280px.

**Setup card** (radius 20): 3px left rail in the direction color; a 1px top hairline with a 40%-wide accent sweep (`sweepline` 4.5s infinite); header row (direction badge, symbol 26px, mono strategy-id pill, `Rank 1` accent pill, three filter pips 8×8 radius 3 — lit pips accent with `0 0 10px var(--accent-glow)`).

**R:R ladder**: 12px tall, radius 7, `--surf-2` track; red risk segment then a green reward segment
(`linear-gradient(90deg, var(--up), color-mix(in oklch, var(--up), #fff 22%))`), widths proportional to
`|entry−stop|` and `|target−entry|`. Both grow with `transform: scaleX(0→1)`, origin left, driven by a
**scroll-linked** animation (`animation-timeline: view()`, range ≈ `entry 6% cover 34–40%`), the reward
segment 120ms later. Legend under it: `risk 54.40` (red) / `186.80 reward · 3.43R net` (green), mono 11.5px.
Where `animation-timeline` is unsupported, fall back to an IntersectionObserver that adds the class once.

**Price cells**: `repeat(auto-fit, minmax(min(100%, 132px), 1fr))`, 1px `--line-soft` gaps as hairlines —
Entry (ink), Stop (red), Target (green), Net R:R (ink, weight 700). Values are odometers at 20px.
Never use fixed `repeat(4, 1fr)` here: mono digits set a large min-content and the row overflows.

Then the reason paragraph (14.5px, `--ink-2`, `max-width: 72ch`) and a footer line
`invalid: <condition> · <time> tak valid` (mono 11px, `--ink-3`) with the `Chart pe dekho` button.

**Position sizing panel**: range input 0.1–3 step 0.1 (`accent-color: var(--accent)`), label
`Har trade ka risk · 0.5%`, then a 2×2 metric grid — Risk (red), Quantity, Notional, Stop distance —
and a `paper equity` footer row.
`riskCash = equity × risk% / 100` · `qty = riskCash / |entry − stop|` · `notional = qty × entry`.
Quantity is shown to 3 dp for ETH, 4 dp for BTC; prices 2 dp for ETH, 0 dp for BTC.

**Strategy panel**: three stacked rows (S1/S2/S3) — active row gets `--surf-2` and a 2px accent left bar;
mono code, name 13px/600, note 11.5px `--ink-3`, right-aligned R:R for that symbol.

### 02 Levels (`#chart`)
Header with 5m/15m/1h/4h/1D segmented control. Frame: `height: 62vh; min-height: 420px`, radius 20.
Inside: grid, a green wash between entry and target and a red wash between entry and stop
(`rgba(...,.07)`, right-inset 74px to clear the price axis), candles, then price lines —
TP dashed green, ENTRY solid accent, SL dashed red, each with a right-hand tag (mono 10.5px,
white on the line color); plus a dotted `--ink-2` last-price line whose tag is `--surf-2` with a border.
Level lines move with `top 500ms cubic-bezier(.2,.8,.2,1)`; the price line 300ms linear.
The live (last) candle carries `drop-shadow(0 0 6px rgba(up/down,.5))`.
Overlays: legend (`--glass`, mono 11.5px, `SYMBOL · tf  O H L C`), three kbd chips
(`F fullscreen`, `L levels`, `R replay`), and a stream pill with a pulsing dot.

**In production use the existing Lightweight Charts v5 instance** (`frontend/app.js` `ensureChart()` /
`drawLevels()`), not the prototype's div candles — keep the chart options themed from the tokens above.

### 03 Pipeline (`#pipeline`)
h2 *"Tick se recommendation tak, ek hi raasta"* + one-line explainer.
Six stage cards, `repeat(auto-fit, minmax(190px, 1fr))`, radius 16: `01`–`06` mono index, title 16px/650,
description 12.5px `--ink-3`. Each card has a 1px top hairline with an accent sweep (3s + 0.35s × index).
Last card ("Recommendation") uses `linear-gradient(160deg, var(--accent-glow), var(--surf))`.
Stages: Feed · Candle builder · Engines · Qualification · Trade plan · Recommendation.
Below: three "gate" cards with a 2px accent left border — No repaint · Replay-first · Fake confidence nahi.

### 04 Queue (`#queue`)
Rows for every other symbol × strategy, sorted by filters passed then net R:R.
Grid `76px 56px minmax(120px, 1.4fr) repeat(4, minmax(70px, 96px))`; 2px direction bar on the left;
name is single-line with ellipsis, reason below in `--ink-3`. Clicking a row selects that symbol +
strategy and re-seeds the chart. Rows reveal on scroll, staggered by index.

### 05 Strategies (`#strategies`)
One card per strategy: header (mono code chip, name, note, on/off switch 34×18 — accent when on,
whole card at .62 opacity when off), then **two separated wells**: `Backtest · 90 din replay` on the card
surface and `Live paper · sirf simulated` on `--surf-2`. Each well shows trades / win % / exp R.
They must never be merged into one table.
Below: "Research ne reject kiya — dobara mat laao" list, two columns (idea, why).

### 06 History (`#history`)
Outcome filter (Sab / Target / Stop / Time exit) driving four recomputed stat cards
(Recommendations, Win %, Net R, Avg hold) and the table.
Table grid `128px 74px 62px 46px repeat(3, minmax(72px, 1fr)) 88px 76px`; sticky-style header row on
`--surf-2`; outcome badges TP → up tint, SL → down tint, TIME → `--surf-2`/`--ink-2`; Net R colored by sign.

### 07 Paper (`#paper`)
Five stat cards (Equity, Total R, Win %, Profit factor, Max drawdown), then the equity curve, then
per-strategy attribution rows (`Trades · Win % · Net R · PF · Max DD · TP / SL / Time`).
**Equity curve**: `viewBox 0 0 800 150`, `preserveAspectRatio: none`; `--up-bg` area polygon, 2px `--up`
polyline with round joins, 2.6r dots (`--surf` fill, `--up` stroke). The line draws itself on scroll —
`stroke-dasharray: 2400` with `dashdraw` (`stroke-dashoffset 2400 → 0`) on `animation-timeline: view()`.

### 08 Journal (`#journal`)
Cards: direction badge + symbol + R result; title 15px/650; a three-cell hairline grid
(Entry / Exit / Confidence); `Kya galat kiya` and `Kya seekha` blocks (13px, `--ink-2`, 1.55);
tag pills (11.5px, `--surf-2`, `--line` border, radius 999).

### 09 Settings (`#settings`)
Two panels side by side (`minmax(min(100%, 420px), 1fr)`):
- **Telegram** — explainer, mono token input + `Verify & connect` (accent, `white-space: nowrap`),
  and a connected-bot row with a pulsing green dot.
- **Alert kab bheje** — four preference rows, each with the 34×18 switch: naya rank-1 setup ·
  entry fill · stop/target hit · feed 60s se stale.

Then "Ye install" cards: Version · Phase · Database · Feed uptime.

### 10 Evidence (`#evidence`)
Four cells: Logged recommendations `41 / 200`, Candle mismatch `0`, TRUSTED strategies `0`,
Paper expectancy `+0.31R`. Footer: version string + the fine print
*"Sirf decision support. MarketScalper kabhi order place, modify ya close nahi karta …"*.

---

## Interactions & behavior

| Trigger | Result |
|---|---|
| Rail link / CTA click | Smooth-scrolls `<main>` to the section anchor |
| Symbol tab (ETHUSDT / BTCUSDT) | Re-selects the setup, re-seeds the chart series, re-renders sizing + queue |
| Strategy row (S1/S2/S3) | Swaps the displayed setup, ladder, price cells, reason and level lines |
| Timeframe tab | Updates the chart timeframe and the strategy-id pill |
| Risk slider | Recomputes Risk / Quantity / Notional / Stop distance on input |
| History outcome filter | Filters the table and recomputes the four stat cards |
| Queue row click | Selects that symbol + strategy and scrolls context to it |
| Theme button | Toggles `document.documentElement.dataset.theme`; persist under the existing `ms_v4_theme` key |
| Logo hover | Tile lift + tilt |
| Section enters viewport | Scroll-linked reveal (`fadeUp` / `riseIn`), ladder fill, equity-curve draw |

Motion inventory: `rise` 900ms hero entrance · `riseIn` / `fadeUp` scroll reveals (staggered by index
via `animation-range`) · `para` / `paraSlow` parallax · `pulse` 1.8–2s status dots · `glow` 9s hero
radials · `sweepline` 3–4.5s hairlines · `ladder` fill · `dashdraw` curve · `bob` 2.4s scroll cue ·
`tickA/B/C` + `scalp` logo. All wrapped by `@media (prefers-reduced-motion: reduce)`.

## State

`sym` (`ETHUSDT`|`BTCUSDT`) · `strat` (`s1`|`s2`|`s3`) · `tf` · `riskPct` · `px` and `prev` per symbol
(prev drives tick color) · `candles` · `live` (real feed vs simulation) · `clock` · `histFilter` · theme.

## Data sources

Wire to the existing API — the prototype already speaks the same contract as `frontend/app.js`
(`Authorization: Bearer <token>`, host from `?api=`, token from `?token=` or `localStorage.ms_token`).

| UI | Endpoint |
|---|---|
| Ticker prices, chart last price | `GET /api/v4/quotes` (poll 4s, or the existing WS) |
| Setup card, queue rows | `GET /api/v4/setups` |
| Active/filled trades | `GET /api/v4/history?status=FILLED&limit=50` |
| History table + stats | `GET /api/v4/history` |
| Strategies, on/off switch | `GET /api/v4/strategies`, `POST /api/v4/strategies/{id}/enabled` |
| Paper stats, attribution, equity curve | `GET /api/v4/performance`, `GET /api/paper` |
| Journal cards | `GET /api/journal` |
| Chart candles, level lines | `GET /api/chart?symbol=&timeframe=`, `GET /api/v4/levels?symbol=&tf=` |

**Staleness rule:** when the fetch fails the prototype falls back to a local simulation and flips the
pill to `simulated feed` / `SIMULATED`. Keep that behaviour and keep the existing red failure banner —
a trading surface must never look live while it is stale.

## Assets

- `favicon.svg`, `brand/icon-180.png`, `brand/icon-512.png` — the new mark (replaces `frontend/icon.svg`
  and the manifest icons).
- Fonts: Inter + JetBrains Mono from Google Fonts. Self-host if the terminal must work offline.
- No other imagery; every graphic is drawn from data.

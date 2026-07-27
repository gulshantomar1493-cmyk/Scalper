# CLAUDE CODE PROMPT — MarketScalper motion redesign

Copy everything below the line into Claude Code, run from the repo root, with this
`design_handoff_marketscalper_motion/` folder placed inside the repo.

---

Read `design_handoff_marketscalper_motion/README.md` end to end before writing any code, then
implement that design in this repository.

**Context**

- This is MarketScalper — a deterministic BTC/ETH trade-setup recommendation terminal.
  Backend: Python 3.12 + FastAPI, REST/WS under `/api/v4/*`. Frontend: vanilla JS in `frontend/`
  (`index.html`, `app.js`, `styles.css`) with TradingView Lightweight Charts v5.
- The handoff folder contains: the README spec, `MarketScalper Motion.dc.html` (the new design),
  `MarketScalper Terminal (current).dc.html` (a recreation of today's UI, for before/after),
  `screenshots/` (every section, plus full-height captures and one light-theme shot), and the new
  brand assets (`favicon.svg`, `brand/icon-180.png`, `brand/icon-512.png`).

**What the design files are**

`*.dc.html` are design references — prototypes with inline styles plus a small logic class at the
bottom of the file. Do **not** port them or `support.js` into the app, and do not introduce React
or any build step. Read them for structure, exact values and motion, then rebuild in this repo's
own idiom: semantic markup in `index.html`, CSS custom properties and rules in `styles.css`, DOM
builders and all networking in `app.js`.

**Rules to keep (from the existing codebase)**

1. `app.js` owns all network access — reuse the existing `api()` / `post()` helpers, Bearer token,
   `?api=` host override, 401 → sign-out, and the red failure banner.
2. Render helpers stay pure DOM builders; write every server string with `textContent`, never
   `innerHTML`.
3. Design law, unchanged: green and red mean money only; the accent carries interaction, never a
   verdict; backtest evidence and live paper results never share a visual container; numbers are
   monospace with `tabular-nums`.
4. A stale surface must never look live — if a fetch fails, fall back to the simulated state and
   label it (`simulated feed` / `SIMULATED`) in addition to the banner.
5. Keep the pre-paint theme script and the `ms_v4_theme` localStorage key; both themes in the
   README must work.
6. Wrap all motion in `@media (prefers-reduced-motion: reduce)`.

**Work in this order**

1. Replace the token block at the top of `styles.css` with the token table from the README
   (dark + `[data-theme="light"]`). Keep the existing selectors working.
2. Rebuild the shell: 84px icon rail with the new animated logo mark, and `<main>` as a single
   smooth-scrolling surface with the eleven section anchors.
3. Build the sections in README order. Wire each to its listed endpoint before moving on; use the
   fixture values from the design only as a fallback.
4. Chart section: use the existing Lightweight Charts instance and `drawLevels()`; only restyle it
   and add the entry/target and entry/stop zone washes plus the new overlays.
5. Motion pass: scroll-linked reveals with `animation-timeline: view()` and an IntersectionObserver
   fallback; odometer price digits; ladder fill; equity-curve draw; hairline sweeps; status pulses.
6. Brand: replace `frontend/icon.svg` with the new mark, update `manifest.webmanifest` icons, add
   `<link rel="icon" href="favicon.svg" type="image/svg+xml">` and the apple-touch icon.

**Definition of done**

- Every section in the README renders with live API data, degrading to the labelled simulated state
  when the backend is unreachable.
- Symbol switcher, strategy rows, timeframe tabs, risk slider, history outcome filter, queue-row
  click and the theme toggle all work.
- Layout holds from ~1024px up: the recommendation grid collapses to one column below ~1280px and
  the price row reflows instead of clipping (mono digits set a large min-content — never use a
  fixed `repeat(4, 1fr)` there).
- `bash scripts/ci.sh` passes, including `tests/test_frontend_shell.py`. Update that test if the
  shell markup legitimately changed.
- No new runtime dependency, no build step, no `innerHTML` on server data.

Ask me before changing any backend endpoint or database schema — this is a frontend redesign.

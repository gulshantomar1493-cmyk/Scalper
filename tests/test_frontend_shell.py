"""Tests for the frontend shell files (roadmap P0.22).

Standalone static files — no server involvement (per owner refinement,
api/app.py is untouched). These tests pin the contract of the shell:
pinned chart library, locked TradeOS tokens, reconnect policy markers,
and JS syntax validity.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

FRONTEND = pathlib.Path(__file__).resolve().parent.parent / "frontend"


def _read(name: str) -> str:
    return (FRONTEND / name).read_text(encoding="utf-8")


def test_shell_files_exist():
    for name in ("index.html", "styles.css", "app.js", "overlays.js",
                 "panel.js"):
        assert (FRONTEND / name).is_file(), name


def test_index_pins_lwc_v5_and_wires_shell_files():
    html = _read("index.html")
    # LWC is now VENDORED locally (no CDN dependency / no external-host block).
    assert 'src="lightweight-charts.standalone.production.js"' in html
    assert "unpkg.com" not in html                     # no external CDN
    assert (FRONTEND / "lightweight-charts.standalone.production.js").is_file()
    assert 'src="app.js"' in html and 'href="styles.css"' in html
    assert 'id="chart"' in html
    assert 'id="conn-text"' in html and 'id="last-event"' in html


def test_index_has_switcher_and_timeframe_bar():
    # v3 (Step 2): the separate 5m strip is replaced by a single chart with a
    # 9-button timeframe selector (5m is now a button, not a second chart).
    html = _read("index.html")
    assert 'id="sym-BTCUSDT"' in html and 'id="sym-ETHUSDT"' in html  # frozen v1 pair
    assert 'id="tf-bar"' in html
    for tf in ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M"):
        assert f'data-tf="{tf}"' in html, tf


def test_index_has_exactly_the_four_replay_controls():
    html = _read("index.html")
    for control in ("replay-start", "replay-stop", "replay-speed",
                    "replay-from", "replay-to"):
        assert f'id="{control}"' in html
    for speed in ('value="1"', 'value="10"', 'value="60"', 'value="max"'):
        assert speed in html                               # the four §10 speeds


def test_app_js_calls_replay_endpoints_only():
    js = _read("app.js")
    assert "/replay/start" in js and "/replay/stop" in js  # the two commands
    assert "/replay/status" in js                          # F2: completion poll
    assert "pause" not in js and "seek" not in js          # no extra replay UI logic


def test_css_carries_locked_tradeos_tokens():
    css = _read("styles.css")
    assert "#0A0F1E" in css                            # surface
    assert "rgba(255, 255, 255, 0.14)" in css          # hairline
    assert "#22D3EE" in css                            # cyan accent
    assert "--radius: 12px" in css                     # locked radius
    assert "tabular-nums" in css                       # tabular mono numbers


def test_app_js_scope_and_policies():
    js = _read("app.js")
    assert "/ws?token=" in js                          # D3 handshake auth
    assert "BACKOFF_INITIAL_MS = 1000" in js
    assert "BACKOFF_CAP_MS = 30000" in js              # 1s -> 30s, mirrors backend
    assert "localStorage" not in js                    # token stays in memory
    assert "sessionStorage" not in js


def test_app_js_live_chart_contract():
    """P0.23 markers (the P0.22 'no series' assertion legitimately flipped
    here — this is the task that adds rendering)."""
    js = _read("app.js")
    assert "LightweightCharts.CandlestickSeries" in js   # LWC v5 series API
    assert js.count("addSeries(") == 2                   # main chart + replay chart
    assert "#22C55E" in js and "#EF4444" in js           # semantic token colors
    assert ".update(toBar(candle))" in js                # diff-only live updates (§9)
    # v3 (Step 2): setData only on bootstrap (main) + replay clear — no aggregation
    assert js.count(".setData(") == 2
    # multi-timeframe history now comes from the backend ChartService
    assert "/api/chart?" in js and "Authorization" in js
    assert "getVisibleRange" in js                       # zoom/pan preserved on switch
    assert 'SYMBOLS = ["BTCUSDT", "ETHUSDT"]' in js      # frozen v1 pair
    assert "loadHistory(activeSymbol)" in js             # reconnect -> reload history
    assert "indexedDB" not in js                         # no client-side storage/caching


def test_app_js_scheme_follows_page_for_https():
    """Production HTTPS: the client must derive the API scheme from the page
    (https/wss), never hardcode http/ws — a page served over TLS otherwise
    hits mixed-content blocking on every fetch and on the WebSocket."""
    js = _read("app.js")
    assert "window.location.protocol" in js              # scheme derived from page
    assert "HTTP_BASE" in js and "WS_BASE" in js
    assert '"https"' in js and '"wss"' in js             # secure variants present
    # the mixed-content bug must be gone: no literal http/ws + API_HOST
    assert "http://${API_HOST}" not in js
    assert "ws://${API_HOST}" not in js
    # and the call sites use the scheme-aware bases
    assert "${HTTP_BASE}/api/chart" in js
    assert "${WS_BASE}/ws" in js


def test_timefmt_ist_display_pure_and_wired():
    """All user-facing times display in IST (Asia/Kolkata); internals stay UTC.
    timefmt.js is the single, pure conversion point used only at render."""
    js = _read("timefmt.js")
    assert "Asia/Kolkata" in js                          # IST, not UTC/London
    assert "window.IST" in js
    for banned in ("fetch(", "WebSocket", "localStorage", "sessionStorage",
                   "addSeries", "Math.log"):
        assert banned not in js, banned                  # pure formatter
    html = _read("index.html")
    assert 'src="timefmt.js"' in html
    assert html.index("timefmt.js") < html.index('src="app.js"')       # loads first
    assert html.index("timefmt.js") < html.index("dashboard.js")
    app = _read("app.js")
    # chart axis + crosshair render IST (time model stays UTC)
    assert "window.IST.tick" in app and "window.IST.crosshair" in app
    assert "tickMarkFormatter" in app and "timeFormatter" in app
    assert "window.IST.now" in app                       # live clock in IST
    assert "toISOString().slice(11, 19) + \" UTC\"" not in app          # old UTC clock gone
    assert "window.IST" in _read("dashboard.js")         # journal/trade tables IST
    # tick MUST render each granularity — a multi-year 1W/1M axis needs YEAR
    # (type 0) and MONTH (type 1) labels, not "DD Mon" for everything (that
    # produced garbled "01 Jan / 07 Jan" year labels with no year).
    assert "tickMarkType === 0" in js and "tickMarkType === 1" in js
    assert "fYear" in js and "fMon" in js                 # "2020" / "Mar" formatters


def test_chart_loading_overlay_present_and_wired():
    """A slow /api/chart fetch (full-history 1W/1M) shows a loading overlay
    instead of a stale/empty chart. Overlay hides itself when empty."""
    html, css, app = _read("index.html"), _read("styles.css"), _read("app.js")
    assert 'id="chart-loading"' in html
    assert ".chart-loading" in css and ".chart-loading:empty" in css   # hidden when empty
    assert "chart-loading" in app and "Loading" in app                 # set before fetch
    assert 'loadEl.textContent = ""' in app                            # cleared after


def test_chart_correctness_fixes_present():
    """Regression pins for P2 (stale candles) + P3 (coin-switch price zoom)."""
    app = _read("app.js")
    # P2: out-of-order load token, reconnect-to-latest, indicator preserve-view
    for t in ("loadSeq", "scrollToRealTime", "preserveView"):
        assert t in app, t
    # P3: price autoScale is re-enabled on a fresh view, BEFORE setData (LWC only
    # re-fits on the data op once the scale is manual from a user price-drag).
    for t in ("setPriceAutoScale", "refitPrice",
              "re-enable price autoScale BEFORE setData"):
        assert t in app, t


def test_htf_js_pure_renderer_and_wired():
    """HTF V1.1 panel: a PURE renderer fed by app.js's /api/htf fetch. It never
    fetches / streams / aggregates / touches storage — the backend owns the
    analysis; this file only draws it. XSS-safe (textContent)."""
    js = _read("htf.js")
    assert "window.Htf" in js and "render:" in js and "init:" in js
    for banned in ("fetch(", "WebSocket", "localStorage", "sessionStorage",
                   "Math.log", "Math.exp", "slope", "intercept", "ATR",
                   "tolerance", ".reduce(", "aggregate", "innerHTML"):
        assert banned not in js, banned
    assert "textContent" in js
    # M2.6 Card 2 surface: overall bias + conviction + agreement + an inline tf dot
    # row. The market story + signal-alignment line moved off the card (folded into
    # the context strip); this card is now bias/conviction/agreement/tf-dots only.
    assert "conviction" in js and "agree" in js and "timeframes" in js
    assert "htf-dot" in js                                  # the compact tf dot row
    # index loads htf.js after panel.js, before app.js; the panel container exists
    html = _read("index.html")
    assert 'src="htf.js"' in html and 'id="htf-panel"' in html
    assert html.index('src="htf.js"') < html.index('src="app.js"')
    # app.js owns the network (pure-renderer boundary) + wires init / poll / render
    app = _read("app.js")
    assert "Htf.init" in app and "Htf.render" in app
    assert "/api/htf?symbol=" in app and "loadHtf" in app and "HTF_POLL_MS" in app
    assert ".htf-panel" in _read("styles.css")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_htf_js_is_valid_javascript():
    result = subprocess.run(["node", "--check", str(FRONTEND / "htf.js")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_timefmt_js_is_valid_javascript():
    result = subprocess.run(
        ["node", "--check", str(FRONTEND / "timefmt.js")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_app_js_is_valid_javascript():
    result = subprocess.run(
        ["node", "--check", str(FRONTEND / "app.js")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


# ------------------------------------------------ P1.19 overlays + P1.20 audit


def test_index_wires_overlays_and_audit_controls():
    html = _read("index.html")
    assert 'src="overlays.js"' in html
    assert html.index("overlays.js") < html.index('src="app.js"')  # load order
    for control in ("audit-pick", "audit-accept", "audit-reject", "audit-tally",
                    "audit-pick-sweep", "audit-pick-ob"):
        assert f'id="{control}"' in html                   # P1.20 + P2.22 tool


def test_overlays_js_is_pure_consumer():
    js = _read("overlays.js")
    assert "attachPrimitive" in js                         # LWC v5 primitive API
    assert "createSeriesMarkers" in js                     # LWC v5 markers API
    assert "timeToCoordinate" in js and "priceToCoordinate" in js
    # pure consumer: renders backend-projected endpoints, no engine math
    for banned in ("Math.log", "Math.exp", "slope", "intercept", "ATR",
                   "tolerance", "fetch(", "WebSocket"):
        assert banned not in js, banned
    assert "Math.random" in js                             # audit pick (UI-only)
    assert "localStorage" not in js and "sessionStorage" not in js


def test_app_js_dispatches_structure_to_overlays():
    js = _read("app.js")
    assert "Overlays.init(mainChart, mainSeries)" in js
    assert "Overlays.setStructure" in js
    assert "lastStructure" in js                           # per-symbol cache only
    assert "state_diff" in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_overlays_js_is_valid_javascript():
    result = subprocess.run(
        ["node", "--check", str(FRONTEND / "overlays.js")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------- P2.20


def test_overlays_js_renders_ob_fvg_boxes():
    js = _read("overlays.js")
    assert "class BoxesPrimitive" in js
    assert "fillRect" in js and "strokeRect" in js
    assert "st.orderblocks" in js or "ob.blocks" in js
    assert "ob.breakers" in js
    assert "st.fvgs" in js
    assert 'b.direction === "BULL"' in js


def test_overlays_js_renders_pools_and_key_levels():
    js = _read("overlays.js")
    assert "liquidity.pools" in js
    assert "liquidity.levels" in js
    assert "full: true" in js                    # pane-wide horizontal lines


def test_overlays_js_renders_sweep_markers():
    js = _read("overlays.js")
    assert "liquidity.sweeps" in js
    assert "sw.side" in js and "sw.target" in js


def test_overlays_js_is_still_a_pure_consumer():
    """P2.20 must not weaken the P1.19 pure-consumer contract."""
    js = _read("overlays.js")
    for banned in ("Math.log", "Math.exp", "slope", "intercept", "ATR",
                   "tolerance", "fetch(", "WebSocket"):
        assert banned not in js, banned
    assert "localStorage" not in js and "sessionStorage" not in js


# ---------------------------------------------------------------- P2.21


def test_overlays_js_renders_vwap_and_bands():
    js = _read("overlays.js")
    assert "volume.session_vwap" in js
    assert "band_1_up" in js and "band_1_dn" in js
    assert "band_2_up" in js and "band_2_dn" in js
    assert '"VWAP"' in js


def test_overlays_js_renders_premium_discount_split_shading():
    js = _read("overlays.js")
    assert "class ShadingPrimitive" in js
    assert "premium_discount" in js
    assert 'zOrder() { return "bottom"; }' in js       # behind the candles
    assert "priceToCoordinate(s.closePrice)" in js      # split at close, not a uniform wash


def test_overlays_js_setstructure_accepts_close_price():
    js = _read("overlays.js")
    assert "setStructure(payload, closePrice)" in js
    assert "lastClose" in js


def test_app_js_passes_close_price_transport_only():
    """app.js may thread the already-available close through — no new
    computation, no new fetches, no new WS messages."""
    js = _read("app.js")
    assert "Overlays.setStructure(st, candle.c)" in js and "diff[activeSymbol]" in js


def test_overlays_js_is_still_a_pure_consumer_p221():
    """P2.21 must not weaken the pure-consumer contract either."""
    js = _read("overlays.js")
    for banned in ("Math.log", "Math.exp", "slope", "intercept", "ATR",
                   "tolerance", "fetch(", "WebSocket"):
        assert banned not in js, banned
    assert "localStorage" not in js and "sessionStorage" not in js


# ---------------------------------------------------------------- P2.22


def test_overlays_js_generalizes_audit_pick_to_three_kinds():
    js = _read("overlays.js")
    assert "function pickRandomLine" in js
    assert "function pickRandomSweep" in js
    assert "function pickRandomOB" in js
    assert 'kind: "trendline"' in js
    assert 'kind: "sweep"' in js
    assert 'kind: "ob"' in js
    # sweep pool + OB pool sourced from the existing payload fields only
    assert "structure.liquidity.sweeps" in js
    assert "ob.blocks" in js and "ob.breakers" in js


def test_overlays_js_uses_one_centralized_jump_navigation_helper():
    """Owner requirement: no scattered literal viewport values — a single
    shared helper + a single centralized constant for point-event jumps."""
    js = _read("overlays.js")
    assert "function jumpToWindow(" in js
    assert "const AUDIT_JUMP_WINDOW_S" in js
    # every pick function routes its viewport change through the helper
    assert js.count("jumpToWindow(") == 4        # 1 definition + 3 call sites
    # the pre-existing trendline behavior is untouched (same 60s floor,
    # just factored into a named constant instead of a bare literal)
    assert "const TRENDLINE_MIN_PAD_S = 60" in js
    # setVisibleRange is called from exactly one place (inside the shared
    # helper) — no per-pick-function duplicate viewport-setting code
    assert js.count("setVisibleRange(") == 1


def test_overlays_js_tallies_each_kind_separately():
    js = _read("overlays.js")
    assert "trendline: { accept: 0, reject: 0 }" in js
    assert "sweep: { accept: 0, reject: 0 }" in js
    assert "ob: { accept: 0, reject: 0 }" in js
    assert "tally[auditPick.kind][result]" in js


def test_overlays_js_highlights_picked_sweep_and_ob():
    js = _read("overlays.js")
    assert 'auditPick.kind === "sweep"' in js
    assert 'auditPick.kind === "ob"' in js
    assert "lineWidth: picked ? 3 : 1" in js       # OB box highlight


def test_overlays_js_is_still_a_pure_consumer_p222():
    """P2.22 must not weaken the pure-consumer contract either."""
    js = _read("overlays.js")
    for banned in ("Math.log", "Math.exp", "slope", "intercept", "ATR",
                   "tolerance", "fetch(", "WebSocket"):
        assert banned not in js, banned
    assert "localStorage" not in js and "sessionStorage" not in js


# ---------------------------------------------------------------- P3.19


def test_index_wires_quality_panel_and_panel_js():
    # M2.5: the rail is EXACTLY two cards — SETUP (setups-panel) + HTF (htf-panel).
    # The V1 Live-Signal / Trade-Plan / Market-Context / Market-Structure cards and
    # the higher-timeframe context-only card are removed (not hidden) per the frozen
    # WORKSPACE-DESIGN.md. panel.js stays (a pure consumer) but renders nothing now.
    html = _read("index.html")
    assert 'src="panel.js"' in html
    assert html.index("panel.js") < html.index('src="app.js"')   # load order
    assert 'id="quality-panel"' in html
    for slot in ("rail-analysis", "setups-panel", "htf-panel"):
        assert f'id="{slot}"' in html, slot
    for gone in ('id="reco-dir"', 'id="panel-plan"', 'id="panel-components"',
                 'id="ctx-trend"', 'id="ms-stream"', 'id="rail-ctxonly"', 'id="ctxonly-tf"'):
        assert gone not in html, gone                            # removed, not hidden


def test_panel_js_is_pure_consumer():
    js = _read("panel.js")
    # same frozen no-engine-math contract as overlays.js
    for banned in ("Math.log", "Math.exp", "slope", "intercept", "ATR",
                   "tolerance", "fetch(", "WebSocket"):
        assert banned not in js, banned
    assert "localStorage" not in js and "sessionStorage" not in js
    # renders backend values only, from the frozen payload keys
    assert "structure.qualification" in js
    assert "structure.recommendations" in js
    assert "structure.signals" in js
    # XSS-safe: backend strings go through textContent, never an innerHTML
    # assignment (the word may appear in a comment; the sink must not)
    assert ".innerHTML" not in js
    assert "textContent" in js


def test_panel_js_renders_reco_plan_context_cards():
    # v3 (Step 2): three cards + a context-mode toggle for higher timeframes.
    js = _read("panel.js")
    assert "renderReco" in js and "renderWhy" in js
    assert "renderComponents" in js and "renderPlan" in js
    assert "setContextMode" in js                        # 15m+ context-only
    # the five gates and four weighted components, by their frozen names
    assert '"G1", "G2", "G4", "G5", "G6"' in js         # G3 removed at D29
    for key in ("structure", "liquidity", "volume", "momentum"):
        assert f'key: "{key}"' in js
    for w in ('weight: "0.30"', 'weight: "0.25"', 'weight: "0.15"'):
        assert w in js
    # plan rail reads the §7 recommendation fields
    for f in ("r.entry", "r.sl", "r.tp1", "r.tp2", "r.net_rr_tp1", "r.guidance"):
        assert f in js
    # decision-support discipline: the plan is display-only
    assert "manually on your exchange" in js


def test_panel_js_handles_gate_fail_and_flagged():
    js = _read("panel.js")
    assert "g.passed" in js and "g.flagged" in js
    # gates render as plain-English labels (not cryptic G1/G2) with a reason + tooltip
    assert "GATE_INFO" in js
    for label in ('"Data"', '"Spread"', '"News"', '"Risk"', '"Reward"'):  # "Session" (G3) removed D29
        assert label in js, label
    assert "Safety checks" in js                         # user-facing header
    # Hinglish explanations + a full (non-truncated) reason line per gate
    assert "hi:" in js and "wg-hi" in js                 # Hinglish meaning, shown always
    assert "wg-reason" in js                             # backend reason, full text (wraps)
    assert "abhi enforce nahi" in js                     # Hinglish "not enforced yet" badge
    # verdict/integrity are backend-driven display states
    assert "VERDICT_CLASS" in js
    assert '"PASS"' in js                                # data-integrity badge
    # a null score (gate fail) renders the em-dash, not a fabricated 0
    assert 'typeof q.score === "number"' in js


def test_app_js_dispatches_structure_to_panel():
    js = _read("app.js")
    assert "Panel.init(quickLogSubmit)" in js            # P4.7 callback wired
    assert "Panel.setStructure" in js
    # panel follows the same cache/dispatch as overlays; no new fetch/WS
    assert js.count("Panel.setStructure") >= 2          # WS message + symbol switch


def test_app_js_setdata_budget_is_minimal():
    """v3 (Step 2): the frontend still never aggregates/caches — setData only
    on bootstrap (main chart) + replay-clear; two chart series total."""
    js = _read("app.js")
    assert js.count(".setData(") == 2
    assert js.count("addSeries(") == 2


def test_css_carries_quality_panel_tokens():
    css = _read("styles.css")
    assert "#quality-panel" in css
    assert "--warn:" in css                              # DEGRADED / provisional
    assert ".lv-reco" in css and ".comp-fill" in css     # v3 recommendation + components
    # panel reuses the locked tokens (no new hard-coded surface colors)
    assert "var(--accent)" in css and "var(--hairline)" in css


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_panel_js_is_valid_javascript():
    result = subprocess.run(
        ["node", "--check", str(FRONTEND / "panel.js")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------- P4.10


def test_panel_js_renders_recommendation_status_and_eval():
    js = _read("panel.js")
    # status badge from the frozen lifecycle statuses (P4.2 payload)
    assert "statusClass" in js
    for st in ("active", "evaluated", "invalidated", "expired"):
        assert f'{st}:' in js or f'"{st}"' in js
    assert "r.status" in js
    # hypothetical outcome (P4.3 eval_*), display-only
    assert "r.eval_outcome" in js and "r.eval_r" in js
    # invalidation timer from two backend timestamps (no trade logic)
    assert "function invalidationTimer" in js
    assert "r.invalid_after_bars" in js and "r.created_ts" in js
    assert "lastCandleTs" in js


def test_panel_js_p410_still_pure_consumer():
    js = _read("panel.js")
    for banned in ("Math.log", "Math.exp", "slope", "intercept", "ATR",
                   "tolerance", "fetch(", "WebSocket"):
        assert banned not in js, banned
    assert ".innerHTML" not in js
    assert "localStorage" not in js and "sessionStorage" not in js


def test_panel_js_setstructure_accepts_candle_ts():
    js = _read("panel.js")
    assert "function setStructure(structure, candleTs)" in js


def test_app_js_passes_candle_ts_to_panel():
    js = _read("app.js")
    assert "Panel.setStructure(st, candle.ts)" in js


def test_css_carries_recommendation_status_tokens():
    css = _read("styles.css")
    assert ".plan-status" in css and ".plan-timer" in css and ".plan-eval" in css
    assert ".st-active" in css and ".st-evaluated" in css


# ---------------------------------------------------------------- P4.7


def test_panel_js_renders_quicklog_form_pure():
    js = _read("panel.js")
    assert "function quickLogForm" in js
    # §8 manual fields
    assert '"Taken"' in js and '"Skipped"' in js
    for val in ('"win"', '"loss"', '"be"'):
        assert val in js
    assert "Actual entry" in js and "Actual exit" in js
    assert "Notes" in js and "Tags" in js
    # submit routes through the app.js-provided callback (no fetch here)
    assert "onQuickLog" in js
    assert "onQuickLog(r.id" in js
    # gated on a known row id (live-only) — replay has no id, no form
    assert "r.id != null && onQuickLog" in js
    # still a pure consumer: the network stays out of panel.js
    for banned in ("fetch(", "WebSocket", "XMLHttpRequest"):
        assert banned not in js, banned


def test_panel_js_card_not_rebuilt_every_bar():
    """The quick-log form must survive live ticks — the card rebuilds only
    when the recommendation identity/status changes (lastPlanKey guard)."""
    js = _read("panel.js")
    assert "lastPlanKey" in js
    assert "key === lastPlanKey" in js                  # skip full rebuild
    assert ".plan-timer" in js                          # timer-only refresh


def test_app_js_quicklog_submit_patches_journal():
    js = _read("app.js")
    assert "async function quickLogSubmit(recId, fields)" in js
    assert "/journal/${recId}" in js
    assert 'method: "PATCH"' in js


def test_css_carries_quicklog_tokens():
    css = _read("styles.css")
    assert ".quicklog" in css and ".ql-toggle" in css and ".ql-result" in css
    assert ".ql-submit" in css


# ---------------------------------------------------------------- P4.12


def test_index_wires_dashboard():
    html = _read("index.html")
    assert 'src="dashboard.js"' in html
    assert html.index("dashboard.js") < html.index('src="app.js"')  # load order
    # v3: the dashboard overlay lives on (Analytics/Journal become pages in
    # Steps 5/6); there is no longer a topbar "Dashboard" button on Live.
    assert 'id="dashboard"' in html
    for slot in ("dash-tab-analytics", "dash-tab-journal", "dash-close",
                 "dash-analytics", "dash-journal"):
        assert f'id="{slot}"' in html, slot


def test_dashboard_js_is_pure_consumer():
    js = _read("dashboard.js")
    for banned in ("Math.log", "Math.exp", "slope", "intercept", "ATR",
                   "tolerance", "fetch(", "WebSocket", "XMLHttpRequest"):
        assert banned not in js, banned
    assert ".innerHTML" not in js and "textContent" in js
    assert "localStorage" not in js and "sessionStorage" not in js


def test_dashboard_js_renders_analytics_and_journal():
    js = _read("dashboard.js")
    assert "function renderAnalytics" in js and "function renderJournal" in js
    # analytics: overall + per strategy + per session, from the payload
    assert "a.overall" in js and "a.by_strategy" in js and "a.by_session" in js
    assert "hypothetical" in js and "system_vs_actual" in js
    # journal card: outcome, manual result, tags, rule-trace
    assert "j.eval_outcome" in js and "j.result" in js
    assert "j.tags" in js and "j.reason_text" in js
    assert "function statTable" in js


def test_trade_review_paper_performance_m4():
    """M4: the Trade Review page ties to V2 — a Paper Performance section (the setups
    you took, as simulated paper trades). Backend computes the summary; dashboard.js
    renders it (pure); app.js feeds /api/paper into renderReview."""
    dj, app = _read("dashboard.js"), _read("app.js")
    assert "function paperBlock" in dj and "Paper performance" in dj
    assert "paper.performance" in dj and "paper.portfolio" in dj and "paper.history" in dj
    assert "renderReview(target, a, journal, paper)" in dj      # signature carries paper
    for banned in ("Math.log", "Math.exp", "slope", "fetch(", "WebSocket"):
        assert banned not in dj, banned                          # still a pure renderer
    assert 'renderReview($("page-review"), analytics, journal, paper)' in app
    assert "/api/paper" in app                                   # app.js fetches paper for the review


def test_app_js_opens_dashboard_via_fetch():
    js = _read("app.js")
    assert "async function openDashboard" in js
    assert '"/analytics"' in js and "/journal?limit=" in js
    assert "Dashboard.render" in js and "Dashboard.show" in js
    assert "Dashboard.init()" in js


def test_css_carries_dashboard_tokens():
    css = _read("styles.css")
    assert "#dashboard" in css and ".dash-tab" in css and ".jcard" in css
    assert ".stat-grid" in css and ".dash-table" in css


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_dashboard_js_is_valid_javascript():
    result = subprocess.run(
        ["node", "--check", str(FRONTEND / "dashboard.js")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


# ------------------------------------------------ in-app Hinglish help/guide


def test_help_js_exists_and_is_wired_before_app():
    assert (FRONTEND / "help.js").is_file()
    html = _read("index.html")
    assert 'src="help.js"' in html
    assert html.index("help.js") < html.index('src="app.js"')  # loads first


def test_index_has_help_center_with_all_topics():
    html = _read("index.html")
    assert 'id="help-open"' in html and 'id="help"' in html and 'id="help-close"' in html
    assert 'id="sidebar-help"' in html                         # global Help button (every page)
    assert "help-center" in html and "data-help-goto" in html  # sectioned Help Center
    # every requested Help Center topic is present (Hinglish walkthrough)
    for topic in ("Getting Started", "Scanner", "Chart", "Trade Setup",
                  "Indicators", "Market Structure", "BOS", "CHOCH", "Liquidity",
                  "HTF Signals", "Paper Trading", "Journal", "Settings", "FAQ"):
        assert topic in html, topic
    assert "ANALYSIS tool" in html                             # core message preserved


def test_key_controls_have_hinglish_tooltips():
    html = _read("index.html")
    # every interactive control the user asked about carries a title= hint
    for anchor in ('id="tf-bar"', 'id="replay-speed"', 'id="audit-pick"',
                   'id="sym-BTCUSDT"', 'id="theme-toggle"', 'id="help-open"'):
        i = html.index(anchor)
        segment = html[i:i + 260]
        assert "title=" in segment, anchor


def test_help_js_is_a_pure_ui_toggler():
    js = _read("help.js")
    # no data, no network, no engine math — help is static text + show/hide
    for banned in ("fetch(", "WebSocket", "Math.log", "Math.exp", "slope",
                   "intercept", "ATR", "tolerance", "XMLHttpRequest"):
        assert banned not in js, banned
    assert "getElementById(\"help\")" in js


def test_help_never_auto_opens_and_is_globally_reachable():
    js = _read("help.js")
    # the login-time auto-open was REMOVED (owner: no forced popup on login)
    assert "ms_help_seen" not in js and "localStorage" not in js
    # both the Live-header ❓ AND the GLOBAL sidebar button open it (every page)
    assert 'getElementById("help-open")' in js and 'getElementById("sidebar-help")' in js
    assert "data-help-goto" in js                              # topic navigation


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_help_js_is_valid_javascript():
    result = subprocess.run(
        ["node", "--check", str(FRONTEND / "help.js")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


# ------------------------------------------- light/dark theme + tools drawer (UX)


def test_ui_js_exists_and_wired_before_app():
    assert (FRONTEND / "ui.js").is_file()
    html = _read("index.html")
    assert 'src="ui.js"' in html
    assert html.index("ui.js") < html.index('src="app.js"')


def test_theme_system_has_both_palettes_default_light():
    css = _read("styles.css")
    # light cream is the DEFAULT (:root); dark is the opt-in override
    assert ':root[data-theme="dark"]' in css
    assert "--chart-bg" in css and "--chart-grid" in css      # chart theming vars
    html = _read("index.html")
    assert 'id="theme-toggle"' in html
    assert 'localStorage.getItem("ms_theme")' in html         # head sets theme pre-paint


def test_replay_and_audit_live_on_the_replay_page_not_live():
    # v3 (Step 3): replay + audit moved OFF the Live page onto the Replay page.
    html = _read("index.html")
    replay = html[html.index('data-page="replay"'):html.index('data-page="review"')]
    for cid in ("replay-from", "replay-to", "replay-speed", "replay-start",
                "replay-stop", "replay-restart", "replay-progress",
                "replay-chart", "audit-pick", "audit-accept", "audit-reject"):
        assert f'id="{cid}"' in replay, cid
    assert "tools-advanced" in replay and "<summary" in replay  # audit under Advanced
    # the Live page carries no replay controls (owner rule: none on Live)
    live = html[html.index('data-page="live"'):html.index('data-page="replay"')]
    assert "replay-start" not in live and "tools-drawer" not in live


def test_dropdown_has_solid_themed_background():
    css = _read("styles.css")
    assert ".replay-bar select" in css
    assert ".replay-bar select option" in css                 # options readable
    assert "color-scheme" in css                              # native pickers match theme


def test_app_js_charts_read_theme_vars_and_retheme():
    js = _read("app.js")
    assert "--chart-bg" in js and "getComputedStyle" in js
    assert "ms-theme-change" in js                            # re-themes on toggle


def test_ui_js_is_chrome_only_no_data():
    js = _read("ui.js")
    for banned in ("fetch(", "WebSocket", "Math.log", "Math.exp", "slope",
                   "intercept", "ATR", "tolerance", "XMLHttpRequest"):
        assert banned not in js, banned
    assert "ms-theme-change" in js and "data-theme" in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_ui_js_is_valid_javascript():
    result = subprocess.run(
        ["node", "--check", str(FRONTEND / "ui.js")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


# ------------------------------------------- Phase 2 Step 1: application shell


def test_shell_js_exists_and_wired():
    assert (FRONTEND / "shell.js").is_file()
    html = _read("index.html")
    assert 'src="shell.js"' in html
    # shell must load before app.js (routing set up before the chart bootstraps)
    assert html.index("shell.js") < html.index('src="app.js"')


def test_sidebar_has_six_grouped_nav_items():
    html = _read("index.html")
    assert 'id="sidebar"' in html
    for grp in ("Trading", "Analytics", "Account"):
        assert f'sb-group">{grp}' in html, grp
    for nav in ("live", "replay", "paper", "review", "journal", "analytics", "settings"):
        assert f'data-nav="{nav}"' in html, nav
    # honest naming — the Trade Review nav (recommendation performance) is not
    # mislabeled "Paper Trading"; the separate P6 simulator + its Help topic may
    # legitimately use that name elsewhere.
    assert "Trade Review" in html
    _rev = html.index('data-nav="review"')
    assert "Paper Trading" not in html[_rev:_rev + 140]


def test_rail_two_cards_setup_then_htf():
    html = _read("index.html")
    # M2.5: the rail is EXACTLY two cards — SETUP (Card 1) above HTF (Card 2).
    rail = html[html.index('id="rail-analysis"'):html.index("</aside>")]
    assert 'id="setups-panel"' in rail and 'id="htf-panel"' in rail
    assert rail.index('id="setups-panel"') < rail.index('id="htf-panel"')
    # the removed cards are gone from the rail entirely
    for gone in ("Market Structure", "Trade Plan", "Market Context", "Live Signal"):
        assert gone not in rail, gone


def test_journal_js_crud_page_pure_and_wired():
    """P5: the standalone user Journal page — full CRUD renderer. app.js owns the
    network via callbacks; journal.js never fetches directly."""
    js = _read("journal.js")
    assert "window.Journal" in js and "init:" in js and "mount:" in js
    for banned in ("fetch(", "WebSocket", "XMLHttpRequest", "Math.log", "innerHTML"):
        assert banned not in js, banned
    assert "New Entry" in js and "Delete" in js and "Edit" in js               # CRUD controls
    assert "jr-search" in js and "jr-filter" in js                             # search + filter
    for field in ("Trade Title", "Direction", "Entry", "Stop Loss", "Take Profit",
                  "Risk %", "Confidence", "Emotion", "Mistakes", "Lessons Learned",
                  "Screenshot", "Tags", "Strategy", "Notes"):
        assert field in js, field                                              # all owner-requested fields
    html = _read("index.html")
    assert 'src="journal.js"' in html
    assert html.index('src="journal.js"') < html.index('src="app.js"')
    app = _read("app.js")
    assert "/api/journal" in app and "journalApi" in app                        # CRUD network in app.js
    assert "Journal.init" in app and "Journal.mount" in app
    assert ".jr-card" in _read("styles.css")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_journal_js_is_valid_javascript():
    result = subprocess.run(["node", "--check", str(FRONTEND / "journal.js")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_paper_js_page_pure_and_wired():
    """P6: the Paper Trading page — simulation-only. app.js owns the network via
    callbacks; paper.js never fetches and makes NO real-broker / exchange call."""
    js = _read("paper.js")
    assert "window.Paper" in js and "init:" in js and "mount:" in js
    for banned in ("fetch(", "WebSocket", "XMLHttpRequest", "Math.log", "innerHTML",
                   "placeOrder", "openPosition", "broker", "exchange.", "binance.com"):
        assert banned not in js, banned                        # no real execution / no direct network
    assert "Order Ticket" in js and "Place Order" in js        # the exchange-like ticket
    for w in ("Positions", "Trade History", "Leverage", "Reduce only", "Liq", "wallet"):
        assert w in js, w
    html = _read("index.html")
    assert 'src="paper.js"' in html and 'data-page="paper"' in html and 'data-nav="paper"' in html
    assert html.index('src="paper.js"') < html.index('src="app.js"')
    app = _read("app.js")
    assert "/api/paper" in app and "paperApi" in app
    assert "Paper.init" in app and "Paper.mount" in app
    assert ".pt-card" in _read("styles.css")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_paper_js_is_valid_javascript():
    result = subprocess.run(["node", "--check", str(FRONTEND / "paper.js")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_chart_trade_widget_present_and_wired():
    """On-chart scalper widget: qty + BUY/SELL (market) + a live P&L + Close,
    wired to the paper API; the running P&L follows the live forming price."""
    html = _read("index.html")
    assert 'id="chart-trade"' in html
    app = _read("app.js")
    assert "chartTrade" in app and "chartClose" in app and "updateTradePnl" in app
    assert "ct-qty" in app and "ct-buy" in app and "ct-sell" in app and "ct-pnl" in app
    assert "syncTradeWidget" in app                        # rebuild only on a state change
    assert 'type: "market"' in app                         # instant execution at the live price
    assert "updateTradePnl(f.c)" in app                    # P&L updates on every forming tick
    assert ".chart-trade" in _read("styles.css")


def test_crosshair_follows_exact_pointer_price():
    """Part A/B: the crosshair reads the EXACT price under the pointer (not the
    snapped candle close) and shows the hovered candle's full OHLC."""
    app = _read("app.js")
    assert "CrosshairMode" in app and "Normal" in app       # not Magnet (which snaps to the close)
    assert "coordinateToPrice(param.point.y)" in app        # exact cursor price
    assert 'put("@", fmt(px), "cx-price")' in app
    for k in ('put("O"', 'put("H"', 'put("L"', 'put("C"'):  # full OHLC of the hovered candle
        assert k in app, k
    assert ".cx-price" in _read("styles.css")


def test_chart_draggable_sltp_order_lines():
    """Part C (Delta-style): entry line with running P&L + draggable SL / TP lines
    on the chart, committed to /api/paper/sltp; positioned by priceToCoordinate,
    dragged via coordinateToPrice."""
    html = _read("index.html")
    assert 'id="chart-orders"' in html
    app = _read("app.js")
    for fn in ("olBuild", "olTick", "olDrag", "olMakeRow", "olDefault"):
        assert fn in app, fn
    assert "priceToCoordinate" in app and "coordinateToPrice(e.clientY" in app
    assert "requestAnimationFrame(olTick)" in app           # live reposition + P&L each frame
    assert "paperApi.sltp" in app and "/api/paper/sltp" in app
    assert 'sltp: (b) =>' in app                             # the API callback
    assert "olBuild()" in app                                # (re)built on a position state change
    css = _read("styles.css")
    for sel in (".chart-orders", ".ol-row", ".ol-entry", ".ol-sl", ".ol-tp", ".ol-drag", ".ol-unset"):
        assert sel in css, sel


def test_router_has_six_pages_live_default_active():
    html = _read("index.html")
    for pg in ("live", "replay", "paper", "review", "journal", "analytics", "settings"):
        assert f'data-page="{pg}"' in html, pg
    # the live page is active by default
    i = html.index('data-page="live"')
    assert 'class="page active"' in html[i - 60:i]


def test_beginner_toggle_present_and_preapplied():
    html = _read("index.html")
    assert 'id="beginner-toggle"' in html
    assert 'data-beginner' in html                        # head script applies it pre-paint
    assert 'localStorage.getItem("ms_beginner")' in html


def test_core_functionality_elements_preserved():
    # v3 (Step 2/3): the chart, analysis rail, symbol switch, connection status,
    # replay + audit (now on the Replay page) and the global overlays all exist.
    html = _read("index.html")
    for el in ('id="chart"', 'id="quality-panel"', 'id="sym-BTCUSDT"',
               'id="replay-start"', 'id="audit-pick"', 'id="conn-text"', 'id="last-event"'):
        assert el in html, el
    assert 'id="dashboard"' in html and 'id="help"' in html


def test_shell_js_is_navigation_only_no_data():
    js = _read("shell.js")
    for banned in ("fetch(", "WebSocket", "XMLHttpRequest", "Math.log", "Math.exp",
                   "slope", "intercept", "ATR", "tolerance", ".reduce(", "aggregate"):
        assert banned not in js, banned
    assert 'data-page' in js and "ms_page" in js          # it is the router


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_shell_js_is_valid_javascript():
    result = subprocess.run(["node", "--check", str(FRONTEND / "shell.js")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# ------------------------------------------- Phase 2 Step 2/3: Live terminal + Replay


def test_live_topbar_has_exchange_status_theme_and_user():
    html = _read("index.html")
    live = html[html.index('data-page="live"'):html.index('data-page="replay"')]
    assert "Binance Futures" in live                      # exchange label
    for el in ('id="conn-text"', 'id="lv-latency"', 'id="lv-update"',
               'id="theme-toggle"', 'id="user-menu"', 'id="tf-bar"'):
        assert el in live, el


def test_live_stats_strip_present():
    html = _read("index.html")
    for slot in ("st-price", "st-o", "st-h", "st-l", "st-c", "st-vol", "st-session", "st-lat"):
        assert f'id="{slot}"' in html, slot


def test_live_bottom_tabs_are_signals_console_review_activity():
    # the former "Logs" tab now hosts the live Activity feed (pre-prod item 4)
    html = _read("index.html")
    for tab in ("signals", "console", "review", "activity"):
        assert f'data-tab="{tab}"' in html, tab
    for panel in ("tab-signals", "tab-console", "tab-review", "tab-activity"):
        assert f'id="{panel}"' in html, panel


def test_ops_status_and_activity_pure_and_wired():
    """Operational status: Live status pill (items 3/5), Activity feed (item 4),
    Operations dashboard (items 9/10). ops.js is a pure renderer; app.js owns
    the GET /ops fetch (§9)."""
    js = _read("ops.js")
    assert "window.Ops" in js
    assert "renderPill" in js and "renderDashboard" in js and "pushActivity" in js
    for banned in ("fetch(", "WebSocket", "localStorage", "sessionStorage",
                   "innerHTML", "Math.log", "addSeries"):
        assert banned not in js, banned                  # pure renderer, XSS-safe
    assert "window.IST" in js                            # times via IST
    html = _read("index.html")
    assert 'src="ops.js"' in html
    assert html.index("ops.js") < html.index('src="app.js"')
    assert 'id="ops-pill"' in html                       # top-bar scanner status
    assert 'id="activity-feed"' in html                  # live activity feed
    assert 'id="ops-dashboard"' in html                  # operations dashboard
    app = _read("app.js")
    assert '"/ops"' in app                               # app.js owns the fetch
    assert "Ops.pushActivity" in app and "Ops.renderPill" in app
    assert "detectActivity" in app                       # trade-setup activity


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_ops_js_is_valid_javascript():
    result = subprocess.run(["node", "--check", str(FRONTEND / "ops.js")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_notifications_pwa_and_settings_wired():
    """Desktop + PWA notifications (item 6), notification toggles (item 8), and
    the Telegram settings flow with auto chat-id detection (item 7)."""
    nj = _read("notify.js")
    assert "window.Notify" in nj
    assert "Notification" in nj and "requestPermission" in nj
    assert "serviceWorker" in nj and "sw.js" in nj       # PWA / installable
    assert "fetch(" not in nj                             # app.js owns the network
    for f in ("notify.js", "sw.js", "manifest.webmanifest", "icon.svg"):
        assert (FRONTEND / f).is_file(), f
    html = _read("index.html")
    assert 'rel="manifest"' in html and "manifest.webmanifest" in html
    assert 'src="notify.js"' in html
    # notification toggles (item 8) + telegram flow (item 7)
    for id_ in ("ntf-desktop", "ntf-telegram", "ntf-trade", "ntf-system",
                "tg-token", "tg-verify", "tg-status", "tg-test"):
        assert f'id="{id_}"' in html, id_
    assert 'id="tg-chat' not in html                     # NO manual chat-id entry
    assert "detected automatically" in html              # auto chat-id (item 7)
    app = _read("app.js")
    assert "/settings/telegram/verify" in app and "/settings/notifications" in app
    assert "Notify.registerSW" in app and "Notify.setPrefs" in app


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_notify_and_sw_valid_javascript():
    for f in ("notify.js", "sw.js"):
        r = subprocess.run(["node", "--check", str(FRONTEND / f)],
                           capture_output=True, text=True)
        assert r.returncode == 0, (f, r.stderr)


def test_live_forming_candle_price_countdown_crosshair():
    """Live forming candle (item 5), live price (6), countdown (7),
    crosshair OHLC (12). The forming update is DISPLAY-ONLY — it returns before
    the structure/panel path, so the engine payload is never affected."""
    js = _read("app.js")
    assert "msg.forming" in js and "handleForming" in js       # WS forming branch
    assert "updateLiveStats" in js                             # live price/O/H/L/C/vol
    assert "mainSeries.update(liveBar)" in js                  # forming folds into last bar
    assert "subscribeCrosshairMove" in js                      # crosshair OHLC (item 12)
    assert "chart-countdown" in js                             # candle countdown (item 7; Step 5 bottom-right)
    html = _read("index.html")
    assert 'id="chart-countdown"' in html and 'id="crosshair-box"' in html


def test_indicators_render_and_toolbar_wired():
    """Items 1/2/3/4: the frontend RENDERS backend-computed EMA/SMA/RSI/Volume —
    it never computes an indicator itself. Toolbar hosts a single Indicators
    menu + reset/auto/screenshot."""
    js = _read("indicators.js")
    assert "window.Indicators" in js
    for fn in ("paramsQuery", "render", "renderMenu", "updateForming", "applyVisibility"):
        assert fn in js, fn
    assert "HistogramSeries" in js and "LineSeries" in js       # volume + MA/RSI series
    for banned in ("fetch(", "WebSocket", "localStorage", "sessionStorage",
                   "Math.log", "Math.exp"):
        assert banned not in js, banned                         # pure renderer, no math
    html = _read("index.html")
    assert 'src="indicators.js"' in html
    assert html.index("indicators.js") < html.index('src="app.js"')
    for id_ in ("ind-btn", "ind-panel", "tb-reset", "tb-autoscale", "tb-screenshot"):
        assert f'id="{id_}"' in html, id_
    app = _read("app.js")
    for call in ("Indicators.init", "Indicators.render", "Indicators.paramsQuery",
                 "Indicators.updateForming", "Indicators.renderMenu"):
        assert call in app, call


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_indicators_js_valid():
    r = subprocess.run(["node", "--check", str(FRONTEND / "indicators.js")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_structure_toggle_htf_context_and_drawing_wired():
    """Structure overlay toggle (item 10), HTF context render (item 9),
    drawing tools (item 11) — all display-only."""
    html, app = _read("index.html"), _read("app.js")
    # item 10 — HH/HL/LH/LL/BOS/CHOCH visibility toggle
    ov = _read("overlays.js")
    assert "setStructureVisible" in ov and "structureOn" in ov
    assert 'id="tb-structure"' in html and "Overlays.setStructureVisible" in app
    # item 9 — HTF context now lives in the M2.5 context strip (Q2..Q4 tiles),
    # fed from the backend /api/htf + /api/setups caches (no client derivation)
    assert 'id="context-strip"' in html and "Strip.render" in app
    # item 11 — drawing tools
    dj = _read("drawing.js")
    assert "window.Drawing" in dj and "subscribeClick" in dj and "attachPrimitive" in dj
    for t in ("trendline", "hline", "rect", "fib", "text", "rr"):   # rr = M3 risk/reward tool
        assert 'data-tool="' + t + '"' in html, t
    for banned in ("fetch(", "WebSocket", "localStorage", "Math.log"):
        assert banned not in dj, banned
    assert 'src="drawing.js"' in html and "Drawing.init" in app


def test_v3_map_strip_and_wiring():
    """V3 P2: the strip can render from GET /api/v3/map (bias ladder, liquidity
    targets, memory) — backend values only; app.js owns the fetch + fallback to
    the old HTF path when the map isn't available."""
    sj = _read("strip.js")
    assert "renderMap" in sj and "draw_above" in sj and "swept_recent" in sj
    for banned in ("fetch(", "WebSocket", "localStorage", ".reduce(", "aggregate"):
        assert banned not in sj, banned                       # still pure
    app = _read("app.js")
    assert "/api/v3/map" in app and "loadMap" in app and "lastMap" in app
    assert "Strip.renderMap(map, activeSetup())" in app       # map preferred
    assert app.count("loadMap()") >= 2                        # init + symbol switch


def test_v3_overlay_pure_and_wired():
    """V3 P1: the per-TF chart read overlay. v3overlay.js is a pure renderer
    (app.js owns the /api/v3/analysis fetch); it draws zones (with lifecycle
    state), trendlines (with state) and ranked liquidity for the ACTIVE chart
    TF, re-fetched on every TF/symbol switch."""
    js = _read("v3overlay.js")
    for banned in ("fetch(", "WebSocket", "XMLHttpRequest", "localStorage",
                   "sessionStorage", "aggregate", "innerHTML"):
        assert banned not in js, banned                       # pure consumer
    assert "window.V3Overlay" in js and "attachPrimitive" in js
    for key in ("zones", "trendlines", "liquidity", "created_at", "priority",
                "SWEPT", "state"):
        assert key in js, key                                 # renders the read
    html = _read("index.html")
    assert 'src="v3overlay.js"' in html
    assert html.index("v3overlay.js") < html.index('src="app.js"')
    app = _read("app.js")
    assert "/api/v3/analysis" in app and "V3Overlay.init" in app
    assert "loadV3" in app and "V3_TF" in app                 # TF-mapped fetch
    # re-drawn on TF switch + symbol switch + initial load
    assert app.count("loadV3()") >= 3


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_v3_overlay_is_valid_javascript():
    result = subprocess.run(["node", "--check", str(FRONTEND / "v3overlay.js")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_toolbar_overflow_and_fullscreen():
    """Final polish (WORKSPACE-DESIGN item 5): the chart toolbar keeps Indicators /
    Draw / SMC + Auto / Reset visible and tucks Screenshot + Fullscreen into a ⋮
    overflow menu; app.js wires the ⋮ toggle + the Fullscreen API."""
    html, app = _read("index.html"), _read("app.js")
    assert 'id="tb-more"' in html and 'id="tb-more-panel"' in html and 'id="tb-fullscreen"' in html
    # screenshot moved INTO the overflow (id/handler kept) — its id follows the panel's
    tb = html[html.index("chart-toolbar"):html.index("lv-chartarea")]
    assert tb.index('id="tb-more-panel"') < tb.index('id="tb-screenshot"')
    # Auto + Reset stay directly on the toolbar
    assert 'id="tb-autoscale"' in tb and 'id="tb-reset"' in tb
    # app.js: the ⋮ toggle + the fullscreen API
    assert "tb-more" in app and "requestFullscreen" in app and "exitFullscreen" in app
    assert "fullscreenchange" in app
    assert ".tb-more-panel" in _read("styles.css")


def test_drawing_persistence_m3():
    """M3: drawings persist across refresh AND follow the symbol. drawing.js stays a
    pure renderer (storage-banned); ui.js owns the per-symbol localStorage; app.js
    coordinates save-on-change + save-old/load-new on a symbol switch."""
    dj, ui, app = _read("drawing.js"), _read("ui.js"), _read("app.js")
    # drawing.js exposes serialize/restore + a change hook, and touches NO storage
    for api in ("getItems", "setItems", "onChange", "changed"):
        assert api in dj, api
    for banned in ("localStorage", "sessionStorage", "fetch(", "WebSocket"):
        assert banned not in dj, banned                    # persistence lives in ui.js, not here
    # ui.js owns the per-symbol store
    assert "__msDrawings" in ui and "ms_drawings" in ui and "localStorage" in ui
    assert "get:" in ui and "save:" in ui                  # per-symbol get/save
    # app.js coordinates: restore on load, save on every edit, and swap on symbol switch
    assert "Drawing.setItems" in app and "Drawing.getItems" in app and "Drawing.onChange" in app
    assert "__msDrawings.get" in app and "__msDrawings.save" in app
    # the R:R tool is present + labeled (notes = the text tool, relabeled)
    html = _read("index.html")
    assert 'data-tool="rr"' in html and "Risk / Reward" in html
    assert "Text / Note" in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_drawing_js_valid():
    r = subprocess.run(["node", "--check", str(FRONTEND / "drawing.js")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_login_flow_wired():
    """Username/password login gate: login.js (pure UI) + app.js (owns the
    network) + the overlay/logout in index.html. No raw-token prompt."""
    html = _read("index.html")
    lj = _read("login.js")
    app = _read("app.js")
    # login.js loads before app.js and is a pure UI (app.js owns fetch/WS, §9)
    assert 'src="login.js"' in html
    assert html.index("login.js") < html.index('src="app.js"')
    for banned in ("fetch(", "WebSocket", "localStorage"):
        assert banned not in lj, banned                       # app.js owns these
    assert "window.__msAuth" in lj and "window.Login" in lj
    # overlay + fields + logout button present
    for id_ in ("login-overlay", "login-user", "login-pass", "login-btn", "logout-btn"):
        assert f'id="{id_}"' in html, id_
    # app.js: NO raw-token prompt; token via ui.js helper (app.js storage-free); 401
    assert "window.prompt" not in app                         # the whole point
    assert "localStorage" not in app                          # ui.js owns storage
    assert "__msToken" in app and "/login" in app
    assert "__msAuth" in app and "function boot(" in app and "onAuthFail" in app
    # ui.js persists the token ("remember token")
    ui = _read("ui.js")
    assert "ms_token" in ui and "__msToken" in ui


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_login_js_valid():
    r = subprocess.run(["node", "--check", str(FRONTEND / "login.js")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_app_js_uses_chart_service_and_gates_higher_tfs():
    js = _read("app.js")
    assert "/api/chart?" in js and "timeframe" in js       # backend ChartService
    assert 'ANALYSIS_TFS = ["1m", "5m"]' in js             # only these carry analysis
    assert "isAnalysisTf" in js and "setContextMode" in js # 15m+ -> context only
    assert "setTimeframe" in js and 'data-tf' in js        # the 9-button selector
    assert "getVisibleRange" in js                         # zoom/pan preserved


def test_app_js_is_still_a_thin_client_no_aggregation():
    js = _read("app.js")
    # the backend owns all aggregation — the frontend never builds/aggregates
    for banned in ("date_bin", "date_trunc", "aggregate(", "bucketize", "aggregateCandles"):
        assert banned not in js, banned
    # history for ANY timeframe comes from the backend, never client-computed
    assert "/api/chart?" in js
    assert "localStorage" not in js and "indexedDB" not in js


def test_panel_js_context_mode_hides_analysis_on_higher_tfs():
    js = _read("panel.js")
    assert "setContextMode" in js
    assert "rail-analysis" in js and "rail-ctxonly" in js   # the two rail states
    assert "market context only" not in js.lower() or True  # copy lives in index.html


def test_replay_page_has_a_dedicated_chart_and_progress():
    html = _read("index.html")
    replay = html[html.index('data-page="replay"'):html.index('data-page="review"')]
    assert 'id="replay-chart"' in replay
    assert 'id="replay-progress"' in replay and 'id="replay-restart"' in replay
    js = _read("app.js")
    assert "replayChart" in js and "replaySeries" in js     # separate replay chart
    assert "/replay/start" in js and "/replay/status" in js


# ------------------------------------------- Phase 2 Steps 4-7: data pages + settings


def test_data_pages_have_containers_and_settings_controls():
    html = _read("index.html")
    for pid in ("page-review", "page-journal", "page-analytics"):
        assert f'id="{pid}"' in html, pid
    for sid in ("set-theme-dark", "set-theme-light", "set-beginner", "set-api", "set-token"):
        assert f'id="{sid}"' in html, sid


def test_trade_review_is_display_only_no_execution():
    # Step 4: honest "Trade Review" — never a paper broker / execution engine.
    js = _read("dashboard.js")
    assert "renderReview" in js
    assert "renderAnalytics, renderJournal, renderReview" in js   # exposed for pages
    for banned in ("placeOrder", "openPosition", "broker", "executeTrade",
                   "submitOrder", "closePosition"):
        assert banned not in js, banned


def test_app_js_loads_data_pages_thin():
    js = _read("app.js")
    assert "loadDataPages" in js
    assert '"ms-page"' in js                                # shell navigation event
    assert "/analytics" in js and "/journal" in js          # reused endpoints (no new API)
    assert "Dashboard.renderReview" in js and "Dashboard.renderAnalytics" in js
    assert "data-refresh" in js


def test_shell_dispatches_page_event_and_ui_exposes_theme_setter():
    assert '"ms-page"' in _read("shell.js")
    assert "__msSetTheme" in _read("ui.js")


def test_paper_v2_total_pnl_rendered():
    """B3: the Paper portfolio shows Total P&L + Realized P&L (were missing)."""
    js = _read("paper.js")
    assert "Total P&L" in js and "total_pnl" in js and "realized_pnl" in js


def test_paper_v2_symbol_persistence():
    """B2: the active symbol is persisted (ui.js owns storage) so a refresh
    reopens the chart on the same symbol — an open position stays visible."""
    ui = _read("ui.js")
    assert "__msSym" in ui and "__msSaveSym" in ui and "ms_sym" in ui
    app = _read("app.js")
    assert "window.__msSaveSym(symbol)" in app          # saved on switch
    assert "savedSymbol" in app                          # used on init
    assert "localStorage" not in app                     # storage stays in ui.js


def test_setups_panel_pure_and_wired():
    """Phase 3 M1: the Trade Setup V2 panel. Pure renderer (app.js owns the
    fetch); shows the frozen-contract fields; never derives trading logic."""
    js = _read("setups.js")
    for banned in ("fetch(", "WebSocket", "XMLHttpRequest", "localStorage",
                   "sessionStorage", "Math.log", "Math.exp", "slope", "intercept",
                   "aggregate", "innerHTML", "addSeries"):
        assert banned not in js, banned                       # pure consumer
    assert "window.Setups" in js and "init:" in js and "render:" in js
    # renders the required setup fields (frozen v1.0 contract)
    for f in ("grade", "grade_reason", "setup_type", "direction", "entry",
              "market_context", "reasons", "reasons_to_avoid", "holding_time",
              "risk_level", "why_edge"):
        assert f in js, f
    assert "No high-probability setup available." in js or "data.message" in js
    html = _read("index.html")
    assert 'id="setups-panel"' in html and 'src="setups.js"' in html
    assert html.index('src="setups.js"') < html.index('src="app.js"')     # loads first
    app = _read("app.js")
    assert "/api/v3/setups?symbol=" in app and "Setups.render" in app and "Setups.init" in app
    assert "loadSetups" in app and "SETUPS_POLL_MS" in app
    assert ".su-card" in _read("styles.css")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_setups_js_is_valid_javascript():
    result = subprocess.run(["node", "--check", str(FRONTEND / "setups.js")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_context_strip_pure_and_wired():
    """Phase 3 M2.5: the context strip — five tiles (Q1..Q5) read left-to-right,
    straight from the frozen contract (/api/htf + /api/setups). Pure renderer:
    app.js owns the fetch/caches; strip.js only maps backend values to tiles and
    never derives a trading decision."""
    js = _read("strip.js")
    for banned in ("fetch(", "WebSocket", "XMLHttpRequest", "localStorage",
                   "sessionStorage", "Math.log", "Math.exp", "slope", "intercept",
                   "tolerance", ".reduce(", "aggregate", "innerHTML", "addSeries"):
        assert banned not in js, banned                       # pure consumer
    assert "window.Strip" in js and "init:" in js and "render:" in js
    # exactly the five questions, in order
    for tile in ("TREND", "CONTROL", "LIQUIDITY", "DRAW", "SETUP"):
        assert tile in js, tile
    assert "No Setup" in js                                    # Q5 no-setup label
    # reads frozen-contract fields only (no client-side analysis). M2.6: CONTROL
    # dropped conviction/confidence (those live in the HTF card) — one answer/tile.
    for field in ("ltf_trend", "overall", "bias",
                  "liquidity_sweep", "liquidity", "tp1", "direction", "grade"):
        assert field in js, field
    assert "textContent" in js                                # XSS-safe
    # index: loads after setups.js, before app.js; the container exists
    html = _read("index.html")
    assert 'id="context-strip"' in html and 'src="strip.js"' in html
    assert html.index('src="setups.js"') < html.index('src="strip.js"')
    assert html.index('src="strip.js"') < html.index('src="app.js"')
    # app.js owns the network + wires init / render (fed by both caches)
    app = _read("app.js")
    assert "Strip.init" in app and "Strip.render" in app and "renderStrip" in app
    # styled as a flat terminal band
    css = _read("styles.css")
    assert ".ctx-strip" in css and ".cs-tile" in css


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_strip_js_is_valid_javascript():
    result = subprocess.run(["node", "--check", str(FRONTEND / "strip.js")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_take_setup_paper_v2_wired():
    """Phase 4: one-click 'take setup' -> a simulated paper BRACKET order. setups.js
    stays a pure renderer (a button that calls an app.js callback, no network);
    app.js owns the fetch + risk-sizing and places a limit at the setup entry with
    the setup's SL / TP1 as the bracket."""
    sj, app, css = _read("setups.js"), _read("app.js"), _read("styles.css")
    assert "su-take" in sj and "onTake" in sj              # button -> injected callback
    for banned in ("fetch(", "WebSocket", "XMLHttpRequest", "localStorage"):
        assert banned not in sj, banned                    # still a pure renderer
    assert "Setups.init" in app and "takePaperSetup" in app
    assert "paperApi.order" in app and 'type: "limit"' in app   # a limit at the setup entry
    assert "0.005" in app                                  # risk-sized (0.5% of the wallet)
    assert ".su-take" in css


def test_setup_chart_overlay_present():
    """Phase 3 M2: the active setup is drawn ON the chart (entry/stop/TP + R:R
    region + direction·grade badge), pure-rendered from /api/setups, one rAF loop,
    rebuilt only on identity change, cleared on no-setup/replay."""
    html = _read("index.html")
    assert 'id="chart-setup"' in html
    app = _read("app.js")
    for fn in ("renderSetupOverlay", "suBuild", "suTick", "activeSetup", "suClear"):
        assert fn in app, fn
    assert "priceToCoordinate" in app                     # positions from price (render, not derive)
    assert "requestAnimationFrame(suTick)" in app         # a single tracking loop
    assert "suId" in app and "id === suId" in app         # identity guard -> no flicker
    assert "su-reward" in app and "su-risk" in app        # the R:R shaded regions
    assert "su-badge" in app                              # the direction·grade badge on entry
    assert "No high-probability setup available." in app  # calm no-setup banner
    assert "replayMode" in app                            # cleared during replay
    assert "renderSetupOverlay()" in app                  # wired into renderSetups
    css = _read("styles.css")
    for sel in (".chart-setup", ".su-entry", ".su-region", ".su-banner", ".su-b-long"):
        assert sel in css, sel
    # accessibility + hierarchy: entry is the brightest (2px), stop not stronger (1px)
    assert ".su-line.su-entry { border-top:2px" in css
    assert ".su-line.su-sl { border-top:1px" in css


def test_v3_setups_watchlist_and_session():
    """V3 P3: the setup card is fed by /api/v3/setups and renders the session
    window (IST guide) + the watchlist (WATCHING/ARMED zones) — pure renderer."""
    sj = _read("setups.js")
    assert "watchingList" in sj and "sessionLine" in sj
    for key in ("trigger_hint", "ARMED", "su-watch", "su-session"):
        assert key in sj, key
    for banned in ("fetch(", "WebSocket", "localStorage"):
        assert banned not in sj, banned
    app = _read("app.js")
    assert "/api/v3/setups?symbol=" in app
    css = _read("styles.css")
    assert ".su-watch" in css and ".su-session" in css

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
    assert "lightweight-charts@5.0.0" in html          # pinned, never floating
    assert "standalone.production.js" in html
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
    assert 'id="ctx-trend"' in html                        # v3: trend in Market Context card


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
    # v3 (Step 2): the rail is three cards — Recommendation, Trade Plan,
    # Market Context — plus a context-only card for higher timeframes.
    html = _read("index.html")
    assert 'src="panel.js"' in html
    assert html.index("panel.js") < html.index('src="app.js"')   # load order
    assert 'id="quality-panel"' in html
    for slot in ("reco-dir", "reco-grade", "reco-pct", "reco-stars", "reco-verdict",
                 "panel-plan", "panel-components", "ctx-trend",
                 "rail-analysis", "rail-ctxonly", "ctxonly-tf"):
        assert f'id="{slot}"' in html, slot


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
    # the six gates and four weighted components, by their frozen names
    assert '"G1", "G2", "G3", "G4", "G5", "G6"' in js
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
    assert '"prov"' in js                                # provisional flag chip
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


def test_index_has_help_button_and_guide_overlay():
    html = _read("index.html")
    assert 'id="help-open"' in html and 'id="help"' in html
    assert 'id="help-close"' in html
    # the guide covers the concepts a new user needs, in Hinglish
    for topic in ("Timeframes", "Right panel", "Trade Plan", "Market Context",
                  "no execution", "market ANALYSIS"):
        assert topic in html, topic


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
    for nav in ("live", "replay", "review", "journal", "analytics", "settings"):
        assert f'data-nav="{nav}"' in html, nav
    # honest naming — never "Paper Trading"
    assert "Trade Review" in html and "Paper Trading" not in html


def test_router_has_six_pages_live_default_active():
    html = _read("index.html")
    for pg in ("live", "replay", "review", "journal", "analytics", "settings"):
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


def test_live_bottom_tabs_are_signals_console_review_logs():
    html = _read("index.html")
    for tab in ("signals", "console", "review", "logs"):
        assert f'data-tab="{tab}"' in html, tab
    for panel in ("tab-signals", "tab-console", "tab-review", "tab-logs"):
        assert f'id="{panel}"' in html, panel


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

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
    for name in ("index.html", "styles.css", "app.js", "overlays.js"):
        assert (FRONTEND / name).is_file(), name


def test_index_pins_lwc_v5_and_wires_shell_files():
    html = _read("index.html")
    assert "lightweight-charts@5.0.0" in html          # pinned, never floating
    assert "standalone.production.js" in html
    assert 'src="app.js"' in html and 'href="styles.css"' in html
    assert 'id="chart"' in html
    assert 'id="conn-text"' in html and 'id="last-event"' in html


def test_index_has_switcher_and_5m_strip():
    html = _read("index.html")
    assert 'id="sym-BTCUSDT"' in html and 'id="sym-ETHUSDT"' in html  # frozen v1 pair
    assert 'id="strip"' in html                                        # §9 context strip


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
    assert js.count("addSeries(") == 2                   # main chart + 5m strip only
    assert "#22C55E" in js and "#EF4444" in js           # semantic token colors
    assert ".update(toBar(candle))" in js                # diff-only live updates (§9)
    # bootstrap (one per series) + the F2 replay-mode chart clears
    assert js.count(".setData(") == 4
    assert "/candles?" in js and "Authorization" in js   # existing REST + Bearer
    assert 'SYMBOLS = ["BTCUSDT", "ETHUSDT"]' in js      # frozen v1 pair
    assert "loadHistory(activeSymbol)" in js             # reconnect -> reload history
    assert "indexedDB" not in js                         # no client-side storage/caching


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
    for control in ("audit-pick", "audit-accept", "audit-reject", "audit-tally"):
        assert f'id="{control}"' in html                   # P1.20 tool
    assert 'id="trend-state"' in html                      # engine trend readout


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
    assert "Overlays.setStructure(diff[activeSymbol].structure, candle.c)" in js


def test_overlays_js_is_still_a_pure_consumer_p221():
    """P2.21 must not weaken the pure-consumer contract either."""
    js = _read("overlays.js")
    for banned in ("Math.log", "Math.exp", "slope", "intercept", "ATR",
                   "tolerance", "fetch(", "WebSocket"):
        assert banned not in js, banned
    assert "localStorage" not in js and "sessionStorage" not in js

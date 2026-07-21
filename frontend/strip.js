/* MarketScalper — Context Strip (Phase 3 M2.5, per the frozen WORKSPACE-DESIGN.md).
 *
 * PURE RENDERER. The "market conversation" the eye reads in one horizontal sweep:
 * five tiles answering Q1..Q5 left-to-right, straight from the frozen contract
 * (/api/htf + /api/setups). app.js owns the network + the per-symbol caches and
 * calls Strip.render(htf, setup); this file only MAPS backend enums/values to a
 * label + glyph + semantic color and draws them. It never fetches, streams,
 * computes an indicator, rolls up candles, derives a trading decision, or touches
 * storage. XSS-safe (textContent only).
 *
 *   1 TREND      what is the market doing?     setup.ltf_trend / htf per-tf trend
 *   2 CONTROL    who controls the market?      htf overall.bias + conviction (+agree%)
 *   3 LIQUIDITY  where is liquidity?           htf liquidity_sweep (taken) / pools (resting)
 *   4 DRAW       where is price likely going?  setup.tp1 / strongest bias-aligned pool
 *   5 SETUP      is there a setup?             setup.direction + grade / "No Setup"
 */
(function () {
  "use strict";
  var root = null;
  var TF_LOW = ["15m", "1h", "4h", "1d"];        // lowest (timeliest) first

  function el(tag, cls, txt) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (txt != null) e.textContent = txt;
    return e;
  }
  function fmt(v) {                                // whole numbers — a glance strip, not the card
    return v == null ? "—" : Number(v).toLocaleString("en-US", { maximumFractionDigits: 0 });
  }
  function init(container) { root = container; }

  function readyTf(htf) {                          // lowest ready tf's analysis, or null
    var tfs = (htf && htf.timeframes) || {};
    for (var i = 0; i < TF_LOW.length; i++) { var a = tfs[TF_LOW[i]]; if (a && a.ready) return a; }
    return null;
  }

  function tile(q, label, ico, val, cls, sub) {
    var t = el("div", "cs-tile cs-t" + q);
    t.appendChild(el("div", "cs-lab", label));
    var v = el("div", "cs-val cs-" + (cls || "neu"));
    v.appendChild(el("span", "cs-ico", ico));
    v.appendChild(el("span", "cs-txt", val));
    t.appendChild(v);
    if (sub != null) t.appendChild(el("div", "cs-sub", sub));
    return t;
  }

  /* Q1 — TREND. setup.ltf_trend when a setup exists (1m, most immediate), else the
     lowest ready HTF trend. Backend enums mapped to a glyph + color; no derivation. */
  function trendTile(setup, htf) {
    var raw = setup ? setup.ltf_trend : null;
    if (raw == null) { var a = readyTf(htf); raw = a ? a.trend : null; }
    var m = { BULLISH: ["▲", "Uptrend", "up"], Uptrend: ["▲", "Uptrend", "up"],
              BEARISH: ["▼", "Downtrend", "down"], Downtrend: ["▼", "Downtrend", "down"],
              RANGE: ["◆", "Range", "neu"], Range: ["◆", "Range", "neu"] };
    var v = m[raw] || ["·", "—", "dim"];
    return tile("1", "TREND", v[0], v[1], v[2]);
  }

  /* Q2 — CONTROL. htf overall bias -> Buyers / Sellers / Balanced; conviction + agree% below. */
  function controlTile(htf) {
    var o = (htf && htf.overall) || null;
    if (!o) return tile("2", "CONTROL", "·", "—", "dim");
    var who = { BULLISH: "Buyers", BEARISH: "Sellers" }[o.bias] || "Balanced";
    var cls = { BULLISH: "up", BEARISH: "down" }[o.bias] || "neu";
    var conv = (o.conviction || "").toLowerCase();
    var sub = conv + (o.confidence != null ? " · " + o.confidence + "%" : "");
    return tile("2", "CONTROL", "●", who, cls, sub.trim() || null);
  }

  /* Q3 — LIQUIDITY. The most timely sweep (taken) else the strongest resting pool;
     the level sits on the sub-line so the headline stays a clean glance. */
  function liquidityTile(htf) {
    var tfs = (htf && htf.timeframes) || {};
    for (var i = 0; i < TF_LOW.length; i++) {
      var a = tfs[TF_LOW[i]]; if (!a || !a.ready) continue;
      var s = a.liquidity_sweep;
      if (s) {
        var lbl = s.side === "HIGH" ? "BSL" : "SSL";     // buy-side high / sell-side low taken
        var arr = s.side === "HIGH" ? "↓" : "↑";
        return tile("3", "LIQUIDITY", "◎", lbl + " swept",
                    s.side === "HIGH" ? "down" : "up", fmt(s.price) + " " + arr);
      }
    }
    for (var j = 0; j < TF_LOW.length; j++) {
      var b = tfs[TF_LOW[j]]; if (!b || !b.ready || !b.liquidity || !b.liquidity.length) continue;
      var p = b.liquidity[0];                             // backend pre-sorted by strength
      var k = p.kind === "EQH" ? "BSL" : "SSL";
      return tile("3", "LIQUIDITY", "◇", k + " resting", "neu", fmt(p.price));
    }
    return tile("3", "LIQUIDITY", "·", "—", "dim");
  }

  /* Q4 — DRAW. setup.tp1 (the backend's stated target). With no setup we only know the
     bias direction, not a specific unpassed level, so show the direction (a pool below
     an up-trending price would mislead) — honest over falsely precise. */
  function drawTile(setup, htf) {
    if (setup && setup.tp1 != null) {
      var up = setup.direction === "LONG";
      return tile("4", "DRAW", up ? "↑" : "↓", fmt(setup.tp1), up ? "up" : "down");
    }
    var bias = ((htf && htf.overall) || {}).bias;
    if (bias === "BULLISH") return tile("4", "DRAW", "↑", "Higher", "up");
    if (bias === "BEARISH") return tile("4", "DRAW", "↓", "Lower", "down");
    return tile("4", "DRAW", "·", "—", "dim");
  }

  /* Q5 — SETUP. direction + grade (brightened tile) else a calm "No Setup". */
  function setupTile(setup) {
    if (setup) {
      var up = setup.direction === "LONG";
      var t = tile("5", "SETUP", "●", setup.direction + " " + setup.grade, up ? "up" : "down");
      t.classList.add("cs-live");
      return t;
    }
    return tile("5", "SETUP", "○", "No Setup", "dim");
  }

  function render(htf, setup) {
    if (!root) return;
    root.textContent = "";
    root.appendChild(trendTile(setup, htf));
    root.appendChild(controlTile(htf));
    root.appendChild(liquidityTile(htf));
    root.appendChild(drawTile(setup, htf));
    root.appendChild(setupTile(setup));
  }

  window.Strip = { init: init, render: render };
})();

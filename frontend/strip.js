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

  // One tile = a colored glyph + a category LABEL (line 1) and a single bold VALUE
  // (line 2). The tile's semantic class colors both glyph and value. One question,
  // one answer — no sub-lines (M2.6: conviction/level live in the HTF card + chart).
  function tile(q, label, ico, val, cls) {
    var t = el("div", "cs-tile cs-t" + q + " cs-" + (cls || "neu"));
    var hd = el("div", "cs-hd");
    hd.appendChild(el("span", "cs-ico", ico));
    hd.appendChild(el("span", "cs-lab", label));
    t.appendChild(hd);
    t.appendChild(el("div", "cs-val", val));
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

  /* Q2 — CONTROL. htf overall bias -> Buyers / Sellers / Balanced. (Conviction +
     agreement live in the HTF card — the strip answers only "who controls?".) */
  function controlTile(htf) {
    var o = (htf && htf.overall) || null;
    if (!o) return tile("2", "CONTROL", "·", "—", "dim");
    var who = { BULLISH: "Buyers", BEARISH: "Sellers" }[o.bias] || "Balanced";
    var cls = { BULLISH: "up", BEARISH: "down" }[o.bias] || "neu";
    return tile("2", "CONTROL", "●", who, cls);
  }

  /* Q3 — LIQUIDITY. The most timely sweep (taken) else the strongest resting pool.
     Just what's happened to liquidity — the exact level is on the chart. */
  function liquidityTile(htf) {
    var tfs = (htf && htf.timeframes) || {};
    for (var i = 0; i < TF_LOW.length; i++) {
      var a = tfs[TF_LOW[i]]; if (!a || !a.ready) continue;
      var s = a.liquidity_sweep;
      if (s) {                                             // buy-side high / sell-side low taken
        var lbl = s.side === "HIGH" ? "BSL Swept" : "SSL Swept";
        return tile("3", "LIQUIDITY", "◎", lbl, s.side === "HIGH" ? "down" : "up");
      }
    }
    for (var j = 0; j < TF_LOW.length; j++) {
      var b = tfs[TF_LOW[j]]; if (!b || !b.ready || !b.liquidity || !b.liquidity.length) continue;
      var k = b.liquidity[0].kind === "EQH" ? "BSL Resting" : "SSL Resting";
      return tile("3", "LIQUIDITY", "◇", k, "neu");
    }
    return tile("3", "LIQUIDITY", "·", "—", "dim");
  }

  /* Q4 — DRAW. setup.tp1 (the backend's stated target — the one price in the strip).
     With no setup we only know the bias direction, not a specific unpassed level, so
     show the direction (a pool behind an up-trending price would mislead). */
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

  /* Q5 — SETUP. direction + grade (brightened, cyan-threaded to the chart + card)
     else a calm "No Setup". */
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

  /* ---- V3 Market Map mode (P2): the same five questions, answered from
     GET /api/v3/map (bias ladder · liquidity targets · memory). Backend values
     only — no derivation here. ---- */
  function v3TrendTile(setup, map) {
    var raw = setup ? setup.ltf_trend : null;
    if (raw == null) {
      var per = (map.bias || {}).per_tf || {};
      raw = per["5m"] || per["15m"] || per["1h"] || null;
    }
    var m = { BULLISH: ["▲", "Uptrend", "up"], BEARISH: ["▼", "Downtrend", "down"],
              RANGE: ["◆", "Range", "neu"] };
    var v = m[raw] || ["·", "—", "dim"];
    return tile("1", "TREND", v[0], v[1], v[2]);
  }
  function v3ControlTile(map) {
    var o = (map.bias || {}).overall;
    var who = { BULLISH: "Buyers", BEARISH: "Sellers" }[o] || "Balanced";
    var cls = { BULLISH: "up", BEARISH: "down" }[o] || "neu";
    return tile("2", "CONTROL", "●", who, cls);
  }
  function v3LiquidityTile(map) {
    var liq = map.liquidity || {};
    var sw = (liq.swept_recent || [])[0];
    if (sw) {
      var isHigh = sw.side === "BUYSIDE";
      return tile("3", "LIQUIDITY", "◎", (isHigh ? "BSL" : "SSL") + " Swept",
                  isHigh ? "down" : "up");
    }
    var d = liq.draw_above || liq.draw_below;
    if (d) return tile("3", "LIQUIDITY", "◇", d.kind + " Resting", "neu");
    return tile("3", "LIQUIDITY", "·", "—", "dim");
  }
  function v3DrawTile(setup, map) {
    if (setup && setup.tp1 != null) {
      var up = setup.direction === "LONG";
      return tile("4", "DRAW", up ? "↑" : "↓", fmt(setup.tp1), up ? "up" : "down");
    }
    var o = (map.bias || {}).overall, liq = map.liquidity || {};
    if (o === "BULLISH" && liq.draw_above)
      return tile("4", "DRAW", "↑", fmt(liq.draw_above.price), "up");
    if (o === "BEARISH" && liq.draw_below)
      return tile("4", "DRAW", "↓", fmt(liq.draw_below.price), "down");
    return tile("4", "DRAW", "·", "—", "dim");
  }
  function renderMap(map, setup) {
    if (!root) return;
    if (!map || !map.ready) { render(null, setup); return; }
    root.textContent = "";
    root.appendChild(v3TrendTile(setup, map));
    root.appendChild(v3ControlTile(map));
    root.appendChild(v3LiquidityTile(map));
    root.appendChild(v3DrawTile(setup, map));
    root.appendChild(setupTile(setup));
  }

  window.Strip = { init: init, render: render, renderMap: renderMap };
})();

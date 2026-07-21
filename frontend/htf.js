/* MarketScalper — HTF V1.1 panel (Higher-Timeframe Intelligence).
 *
 * PURE RENDERER. app.js fetches GET /api/htf (the backend HtfService, isolated
 * from the decision engine and the determinism stream) and passes the result
 * here; this file only DRAWS the overall bias, market story, per-timeframe cards,
 * and the alignment with the current 1m/5m signal. It never fetches, streams,
 * computes an indicator, rolls up candles, or touches storage. XSS-safe (textContent).
 *
 * HTF is CONTEXT ONLY — execution stays 1m/5m; it improves context and confidence.
 */
(function () {
  "use strict";
  var root = null;
  var BIAS_CLASS = { BULLISH: "htf-bull", BEARISH: "htf-bear", NEUTRAL: "htf-neu" };
  var TF_ORDER = ["1d", "4h", "1h", "15m"];
  var TF_LABEL = { "15m": "15M", "1h": "1H", "4h": "4H", "1d": "1D" };

  function el(tag, cls, txt) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (txt != null) e.textContent = txt;
    return e;
  }

  function init(container) { root = container; }

  // One per-timeframe chip: a bias-colored dot + the tf label, inline. The full read
  // (bias · trend) is on hover (title) — never color alone. No engine math here.
  function tfDot(tf, a) {
    var ready = a && a.ready;
    var d = el("div", "htf-dot-tile" + (ready ? "" : " htf-dim"));
    d.appendChild(el("span", "htf-dot " + (ready ? (BIAS_CLASS[a.bias] || "htf-neu") : "htf-warm"), "●"));
    d.appendChild(el("span", "htf-dot-tf", TF_LABEL[tf]));
    d.title = TF_LABEL[tf] + ": " + (ready ? (a.bias + " · " + (a.trend || "")) : "warming up");
    return d;
  }

  // Card 2 (M2.6): bias · conviction · agreement + an inline tf dot row. Nothing
  // more — no paragraphs; the market story folds into the context strip and the
  // setup card owns the rest. Only glanceable information.
  function render(data, direction) {                           // direction: unused (freeze)
    if (!root) return;
    root.textContent = "";
    if (!data || !data.overall) return;                        // nothing yet
    var o = data.overall;

    var head = el("div", "htf-head");
    head.appendChild(el("span", "htf-title", "HTF"));
    head.appendChild(el("span", "htf-badge " + (BIAS_CLASS[o.bias] || "htf-neu"), o.bias));
    root.appendChild(head);

    var meta = el("div", "htf-meta");
    meta.appendChild(el("span", "htf-score", (o.conviction || "—").toLowerCase() + " conviction"));
    meta.appendChild(el("span", "htf-conf", (o.confidence != null ? o.confidence : 0) + "% agree"));
    root.appendChild(meta);

    var grid = el("div", "htf-grid");
    TF_ORDER.forEach(function (tf) { grid.appendChild(tfDot(tf, (data.timeframes || {})[tf])); });
    root.appendChild(grid);
  }

  window.Htf = { init: init, render: render };
})();

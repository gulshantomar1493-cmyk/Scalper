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
  var TF_LABEL = { "15m": "15M", "1h": "1H", "4h": "4H", "1d": "Daily" };

  function el(tag, cls, txt) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (txt != null) e.textContent = txt;
    return e;
  }

  function fmt(v) {
    return v == null ? "—" : Number(v).toLocaleString("en-US", { maximumFractionDigits: 2 });
  }

  function init(container) { root = container; }

  // Alignment of the HTF bias with the current 1m/5m signal direction — the
  // display-layer "integration": a plain string comparison, no engine math.
  function alignment(bias, direction) {
    if (!direction || bias === "NEUTRAL") return null;
    var want = direction === "LONG" ? "BULLISH" : "BEARISH";
    return bias === want ? "aligned" : "conflicting";
  }

  function tfCard(tf, a) {
    var card = el("div", "htf-card");
    var top = el("div", "htf-card-top");
    top.appendChild(el("span", "htf-tf", TF_LABEL[tf]));
    if (a && a.ready) {
      top.appendChild(el("span", "htf-cbadge " + (BIAS_CLASS[a.bias] || "htf-neu"), a.bias));
      card.appendChild(top);
      var l1 = el("div", "htf-card-line");
      l1.appendChild(el("span", "htf-k", a.trend));
      l1.appendChild(el("span", "htf-v", "score " + a.score));
      card.appendChild(l1);
      var l2 = el("div", "htf-card-line");
      l2.appendChild(el("span", "htf-k", a.structure));
      l2.appendChild(el("span", "htf-v", a.ema_alignment));
      card.appendChild(l2);
      var l3 = el("div", "htf-card-line");
      l3.appendChild(el("span", "htf-k", "S/R"));
      l3.appendChild(el("span", "htf-v", fmt(a.support) + " / " + fmt(a.resistance)));
      card.appendChild(l3);
      var l4 = el("div", "htf-card-line");
      l4.appendChild(el("span", "htf-k", "Momentum"));
      var mom = (a.momentum && a.momentum.direction) || "flat";
      var ev = a.choch ? "CHOCH " + a.choch.direction : (a.bos ? "BOS " + a.bos.direction : "");
      l4.appendChild(el("span", "htf-v", mom + (ev ? " · " + ev : "")));
      card.appendChild(l4);
    } else {
      top.appendChild(el("span", "htf-cbadge htf-neu", "—"));
      card.appendChild(top);
      card.appendChild(el("div", "htf-card-line htf-dim", "warming up"));
    }
    return card;
  }

  function render(data, direction) {
    if (!root) return;
    root.textContent = "";
    if (!data || !data.overall) return;                        // nothing yet
    var o = data.overall;

    var head = el("div", "htf-head");
    head.appendChild(el("span", "htf-title", "Higher-Timeframe Bias"));
    head.appendChild(el("span", "htf-badge " + (BIAS_CLASS[o.bias] || "htf-neu"), o.bias));
    root.appendChild(head);

    var meta = el("div", "htf-meta");
    meta.appendChild(el("span", "htf-score", (o.score != null ? o.score : "—") + "/100"));
    meta.appendChild(el("span", "htf-conf", (o.confidence != null ? o.confidence : 0) + "% agree"));
    root.appendChild(meta);

    var al = alignment(o.bias, direction);
    if (al) {
      var sym = al === "aligned" ? "✓" : "✗";
      root.appendChild(el("div", "htf-align " + (al === "aligned" ? "ok" : "bad"),
        sym + " HTF " + al + " with your " + direction + " signal"));
    }

    if (o.market_story) root.appendChild(el("div", "htf-story", o.market_story));

    var grid = el("div", "htf-grid");
    TF_ORDER.forEach(function (tf) { grid.appendChild(tfCard(tf, (data.timeframes || {})[tf])); });
    root.appendChild(grid);
  }

  window.Htf = { init: init, render: render };
})();

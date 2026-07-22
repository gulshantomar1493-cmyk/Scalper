/* MarketScalper — Trade Setups panel (Trade Engine V2, frozen contract v1.0).
 *
 * PURE RENDERER. app.js fetches GET /api/setups and passes the result here; this
 * file only DRAWS the setup card(s). It never fetches, streams, computes, or
 * touches storage, and never derives trading logic — every value shown comes
 * straight from the backend (the backend owns all analysis). XSS-safe (textContent).
 *
 * Visual hierarchy: this is the top "Trade Setup" card. Direction + grade read in
 * a glance; entry/stop/targets + R:R are the action; the rest expands on demand.
 */
(function () {
  "use strict";
  var root = null;
  var DIR_CLASS = { LONG: "su-long", SHORT: "su-short" };
  var GRADE_CLASS = { "A+": "su-aplus", "A": "su-a", "B": "su-b" };
  var RISK_CLASS = { LOW: "su-risk-low", MEDIUM: "su-risk-med", HIGH: "su-risk-high" };

  function el(tag, cls, txt) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (txt != null) e.textContent = txt;
    return e;
  }
  function fmt(v) {
    return v == null ? "—" : Number(v).toLocaleString("en-US", { maximumFractionDigits: 2 });
  }

  var onTake = null;
  function init(container, take) { root = container; onTake = take || null; }

  // one label/value line (full width)
  function levelRow(label, value, cls) {
    var r = el("div", "su-lv");
    r.appendChild(el("span", "su-lv-k", label));
    r.appendChild(el("span", "su-lv-v " + (cls || ""), value));
    return r;
  }
  var divider = function () { return el("div", "su-div"); };

  function list(title, items, cls) {
    var wrap = el("div", "su-list");
    wrap.appendChild(el("div", "su-list-h", title));
    (items || []).forEach(function (t) { wrap.appendChild(el("div", "su-li " + (cls || ""), t)); });
    return wrap;
  }

  function card(s, top) {
    var c = el("div", "su-card" + (top ? " su-top" : ""));

    // header: direction · setup type · grade
    var hd = el("div", "su-hd");
    hd.appendChild(el("span", "su-dir " + (DIR_CLASS[s.direction] || ""), s.direction));
    hd.appendChild(el("span", "su-type", s.setup_type));
    hd.appendChild(el("span", "su-grade " + (GRADE_CLASS[s.grade] || ""), s.grade));
    c.appendChild(hd);

    // grade reason — WHY this grade (backend-provided; never derived here)
    if (s.grade_reason) c.appendChild(el("div", "su-greason", s.grade_reason));

    // the action, divider-separated: entry (cyan-threaded) / stop / targets / R:R · risk
    var lv = el("div", "su-levels");
    lv.appendChild(levelRow("Entry", fmt(s.entry), "su-entry"));
    lv.appendChild(divider());
    lv.appendChild(levelRow("Stop", fmt(s.sl), "su-stop"));
    lv.appendChild(divider());
    var tps = el("div", "su-lv-pair");
    tps.appendChild(levelRow("TP1", fmt(s.tp1), "su-tp"));
    if (s.tp2 != null) tps.appendChild(levelRow("TP2", fmt(s.tp2), "su-tp"));
    lv.appendChild(tps);
    lv.appendChild(divider());
    var meta = el("div", "su-lv-pair");
    meta.appendChild(levelRow("R:R", (s.rr != null ? s.rr + " : 1" : "—")));
    meta.appendChild(levelRow("Risk", s.risk_level, RISK_CLASS[s.risk_level] || ""));
    lv.appendChild(meta);
    c.appendChild(lv);

    // one-click "take this setup" -> a simulated paper position (Phase 4). Only the
    // top setup is actionable; app.js owns the network (this file stays pure).
    if (top && onTake) {
      var take = el("button", "su-take " + (DIR_CLASS[s.direction] || ""), "▸ Take setup — paper");
      take.addEventListener("click", function () { onTake(s); });
      c.appendChild(take);
    }

    // everything else expands on demand — the visible card stays the decision only
    var det = el("details", "su-more");
    det.appendChild(el("summary", null, "Details"));
    if (s.holding_time) det.appendChild(levelRow("Holding time", s.holding_time));
    if (s.market_context) det.appendChild(el("div", "su-ctx", s.market_context));
    if (s.reasons && s.reasons.length) det.appendChild(list("Confluences", s.reasons, "su-good"));
    if (s.reasons_to_avoid && s.reasons_to_avoid.length)
      det.appendChild(list("Reasons to avoid", s.reasons_to_avoid, "su-warn"));
    if (s.invalidation) det.appendChild(list("Invalidation", [s.invalidation], "su-warn"));
    if (s.early_exit && s.early_exit.length) det.appendChild(list("Early exit", s.early_exit, "su-warn"));
    if (s.management_notes && s.management_notes.length)
      det.appendChild(list("Management", s.management_notes, ""));
    if (s.why) {
      var w = el("div", "su-why");
      w.appendChild(el("div", "su-list-h", "Why"));
      [["Now", s.why.why_now], ["Entry", s.why.why_entry], ["Stop", s.why.why_sl],
       ["Targets", s.why.why_targets], ["Edge", s.why.why_edge]].forEach(function (p) {
        if (!p[1]) return;
        var row = el("div", "su-why-row");
        row.appendChild(el("span", "su-why-k", p[0]));
        row.appendChild(el("span", "su-why-v", p[1]));
        w.appendChild(row);
      });
      det.appendChild(w);
    }
    c.appendChild(det);
    return c;
  }

  // data = GET /api/setups response (frozen v1.0). Renders the setup(s) or the
  // calm "no high-probability setup" state — never fabricates one.
  function render(data) {
    if (!root) return;
    root.textContent = "";
    root.appendChild(el("div", "su-title", "Trade Setup"));
    var setups = (data && data.setups) || [];
    if (!setups.length) {
      var msg = (data && data.message) || "No high-probability setup available.";
      root.appendChild(el("div", "su-none", msg));
      root.appendChild(el("div", "su-none-sub", "The engine is watching. Patience — no forced trades."));
      return;
    }
    setups.forEach(function (s, i) { root.appendChild(card(s, i === 0)); });
  }

  window.Setups = { init: init, render: render };
})();

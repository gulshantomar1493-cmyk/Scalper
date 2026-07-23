/* MarketScalper — Trade Recommendation History page (V3).
 *
 * PURE RENDERER. app.js owns the network (GET /api/v3/history via the injected
 * api callbacks) and calls History.render(list) / History.renderDetail(rec).
 * This file only builds DOM: the filter bar, the sortable table, pagination,
 * the CSV button and the full-reasoning detail view. No fetch / WS / storage /
 * engine math. XSS-safe (textContent only).
 */
(function () {
  "use strict";
  var els = null, cb = null;                 // cb: {load(params), open(id), csv(params)}
  var state = { offset: 0, limit: 50, sort: "ts", order: "desc" };

  var STATUS_CLS = { TP1_HIT: "h-win", TP2_HIT: "h-win", STOP_LOSS: "h-loss",
                     ACTIVE: "h-active", CANCELLED: "h-dim", EXPIRED: "h-dim",
                     TIMEOUT: "h-dim" };
  var FILTERS = [
    ["symbol", ["", "BTCUSDT", "ETHUSDT"]],
    ["grade", ["", "A+", "A"]],
    ["status", ["", "ACTIVE", "TP1_HIT", "TP2_HIT", "STOP_LOSS", "CANCELLED",
                "EXPIRED", "TIMEOUT"]],
    ["setup_type", ["", "Zone Reversal", "Breakout", "Breakdown"]],
    ["direction", ["", "LONG", "SHORT"]],
  ];
  var COLS = [["ts", "Time (IST)"], ["symbol", "Sym"], ["setup_type", "Type"],
              ["grade", "Gr"], ["status", "Result"], ["rr", "R:R"],
              ["result_r", "R"], ["session_label", "Session"]];

  function el(tag, cls, txt) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (txt != null) e.textContent = txt;
    return e;
  }
  function fmt(v, d) {
    return v == null ? "—" : Number(v).toLocaleString("en-US",
      { maximumFractionDigits: d == null ? 2 : d });
  }

  function init(container, callbacks) { els = container; cb = callbacks || null; }

  function params() {
    var p = { limit: state.limit, offset: state.offset,
              sort: state.sort, order: state.order };
    FILTERS.forEach(function (f) {
      var s = document.getElementById("hf-" + f[0]);
      if (s && s.value) p[f[0]] = s.value;
    });
    var df = document.getElementById("hf-from"), dt = document.getElementById("hf-to");
    if (df && df.value) p.date_from = df.value;
    if (dt && dt.value) p.date_to = dt.value;
    var q = document.getElementById("hf-q");
    if (q && q.value.trim()) p.q = q.value.trim();
    return p;
  }

  function filters() {
    var host = els.filters;
    host.textContent = "";
    FILTERS.forEach(function (f) {
      var sel = el("select", "hf-sel");
      sel.id = "hf-" + f[0];
      f[1].forEach(function (v) {
        var o = el("option", null, v || ("all " + f[0].replace("_", " ")));
        o.value = v;
        sel.appendChild(o);
      });
      sel.addEventListener("change", function () { state.offset = 0; reload(); });
      host.appendChild(sel);
    });
    ["from", "to"].forEach(function (k) {
      var d = el("input", "hf-date");
      d.type = "date"; d.id = "hf-" + k; d.title = k + " date";
      d.addEventListener("change", function () { state.offset = 0; reload(); });
      host.appendChild(d);
    });
    var q = el("input", "hf-q");
    q.type = "search"; q.id = "hf-q"; q.placeholder = "search reasoning…";
    q.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { state.offset = 0; reload(); }
    });
    host.appendChild(q);
    var csv = el("button", "hf-btn", "⬇ CSV");
    csv.type = "button";
    csv.addEventListener("click", function () { if (cb) cb.csv(params()); });
    host.appendChild(csv);
  }

  function reload() { if (cb) cb.load(params()); }

  function render(data) {
    if (!els) return;
    if (!els.filters.firstChild) filters();
    var host = els.table;
    host.textContent = "";
    els.detail.hidden = true;
    var items = (data && data.items) || [];
    if (!items.length) {
      host.appendChild(el("div", "hist-empty",
        "No recommendations recorded yet — the engine records every issued setup automatically."));
      els.pager.textContent = "";
      return;
    }
    var table = el("table", "h-table");
    var hr = el("tr");
    COLS.forEach(function (c) {
      var th = el("th", null, c[1] + (state.sort === c[0] ? (state.order === "desc" ? " ↓" : " ↑") : ""));
      th.addEventListener("click", function () {
        if (state.sort === c[0]) state.order = state.order === "desc" ? "asc" : "desc";
        else { state.sort = c[0]; state.order = "desc"; }
        state.offset = 0; reload();
      });
      hr.appendChild(th);
    });
    table.appendChild(hr);
    items.forEach(function (it) {
      var tr = el("tr", "h-row " + (STATUS_CLS[it.status] || ""));
      tr.appendChild(el("td", "mono", it.ts ? window.IST.dateTime(it.ts) : "—"));
      tr.appendChild(el("td", "mono", it.symbol));
      tr.appendChild(el("td", null, it.setup_type));
      tr.appendChild(el("td", "mono h-g", it.grade));
      var st = el("td", "mono h-st", it.status.replace("_", " "));
      tr.appendChild(st);
      tr.appendChild(el("td", "mono", fmt(it.rr)));
      tr.appendChild(el("td", "mono " + ((it.result_r || 0) >= 0 ? "h-win" : "h-loss"),
        it.result_r == null ? "—" : (it.result_r >= 0 ? "+" : "") + fmt(it.result_r) + "R"));
      tr.appendChild(el("td", "h-sess", it.session_label || "—"));
      tr.addEventListener("click", function () { if (cb) cb.open(it.id); });
      table.appendChild(tr);
    });
    host.appendChild(table);
    var pager = els.pager;
    pager.textContent = "";
    var page = Math.floor(state.offset / state.limit) + 1;
    var pages = Math.max(1, Math.ceil((data.total || 0) / state.limit));
    var prev = el("button", "hf-btn", "‹ prev");
    prev.type = "button"; prev.disabled = state.offset <= 0;
    prev.addEventListener("click", function () {
      state.offset = Math.max(0, state.offset - state.limit); reload();
    });
    var next = el("button", "hf-btn", "next ›");
    next.type = "button"; next.disabled = page >= pages;
    next.addEventListener("click", function () { state.offset += state.limit; reload(); });
    pager.appendChild(prev);
    pager.appendChild(el("span", "hf-page", page + " / " + pages + " · " + (data.total || 0) + " recs"));
    pager.appendChild(next);
  }

  /* ---- detail view: full engine reasoning + timeline + outcome ---- */
  function kv(host, k, v, cls) {
    var r = el("div", "hd-kv");
    r.appendChild(el("span", "hd-k", k));
    r.appendChild(el("span", "hd-v " + (cls || ""), v == null ? "—" : String(v)));
    host.appendChild(r);
  }
  function listBlock(host, title, items, cls) {
    if (!items || !items.length) return;
    host.appendChild(el("div", "hd-h", title));
    items.forEach(function (t) { host.appendChild(el("div", "hd-li " + (cls || ""), t)); });
  }
  function renderDetail(rec) {
    if (!els || !rec) return;
    var d = els.detail;
    d.textContent = "";
    d.hidden = false;
    var back = el("button", "hf-btn", "‹ back to list");
    back.type = "button";
    back.addEventListener("click", function () { d.hidden = true; });
    d.appendChild(back);

    var hd = el("div", "hd-head");
    hd.appendChild(el("span", "h-g mono", rec.grade));
    hd.appendChild(el("span", "hd-dir " + (rec.direction === "LONG" ? "h-win" : "h-loss"), rec.direction));
    hd.appendChild(el("span", null, rec.setup_type + " · " + rec.symbol));
    hd.appendChild(el("span", "h-st mono " + (STATUS_CLS[rec.status] || ""), rec.status.replace("_", " ")));
    d.appendChild(hd);

    var g = el("div", "hd-grid");
    var left = el("div", "hd-col");
    kv(left, "Issued", rec.ts ? window.IST.dateTime(rec.ts) : "—");
    kv(left, "Session", rec.session_label);
    kv(left, "Entry", fmt(rec.entry), "hd-entry");
    kv(left, "Stop", fmt(rec.sl), "h-loss");
    kv(left, "TP1", fmt(rec.tp1), "h-win");
    kv(left, "TP2", fmt(rec.tp2), "h-win");
    kv(left, "Planned R:R", fmt(rec.rr));
    var right = el("div", "hd-col");
    kv(right, "Result", rec.result_r == null ? "—"
      : (rec.result_r >= 0 ? "+" : "") + fmt(rec.result_r) + "R",
      (rec.result_r || 0) >= 0 ? "h-win" : "h-loss");
    kv(right, "Points captured", fmt(rec.points_captured));
    kv(right, "Points lost", fmt(rec.points_lost));
    kv(right, "MAE / MFE", fmt(rec.mae_r) + "R / " + fmt(rec.mfe_r) + "R");
    kv(right, "Holding", rec.holding_minutes == null ? "—" : rec.holding_minutes + " min");
    kv(right, "Filled", rec.filled_ts ? window.IST.dateTime(rec.filled_ts) : "—");
    kv(right, "Closed", rec.closed_ts ? window.IST.dateTime(rec.closed_ts) : "—");
    g.appendChild(left); g.appendChild(right);
    d.appendChild(g);

    var a = rec.analysis || {};
    if (a.market_context) d.appendChild(el("div", "hd-ctx", a.market_context));
    if (a.grade_reason) d.appendChild(el("div", "hd-greason", a.grade_reason));
    listBlock(d, "Confluences (" + (a.confluences || 0) + "/" + (a.confluences_total || 7) + ")",
      a.reasons, "hd-good");
    listBlock(d, "Reasons to avoid", a.reasons_to_avoid, "hd-warn");
    if (a.invalidation) listBlock(d, "Invalidation", [a.invalidation], "hd-warn");
    listBlock(d, "Early exit", a.early_exit, "hd-warn");
    listBlock(d, "Management", a.management_notes, "");
    if (a.zone) {
      listBlock(d, "Zone", [
        "band " + fmt(a.zone.lo) + " – " + fmt(a.zone.hi) +
        (a.zone.stack ? " · " + a.zone.stack + "-TF stack" : ""),
        a.zone.explain || ""], "");
    }
    if (a.why) {
      d.appendChild(el("div", "hd-h", "Why"));
      Object.keys(a.why).forEach(function (k) {
        if (!a.why[k]) return;
        var r = el("div", "hd-kv");
        r.appendChild(el("span", "hd-k", k.replace("why_", "")));
        r.appendChild(el("span", "hd-v", a.why[k]));
        d.appendChild(r);
      });
    }
    d.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  window.History = { init: init, render: render, renderDetail: renderDetail,
                     reload: reload };
})();

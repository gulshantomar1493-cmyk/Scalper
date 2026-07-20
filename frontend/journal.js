/* MarketScalper — user Journal page (P5): full CRUD over /api/journal.
 *
 * app.js OWNS the network: Journal.init receives {list, create, update, remove}
 * callbacks that do the fetching. This module renders the toolbar (New Entry +
 * search + filters), the entries list (each with Edit / Delete), and the
 * create/edit form, and manages local editing state. No direct fetch, no engine
 * math. XSS-safe — textContent / input.value only (no HTML injection). */
(function () {
  "use strict";
  var root = null, api = null, entries = [], editing = null, filters = {};

  // [key, label, input-type]
  var FIELDS = [
    ["title", "Trade Title", "text"], ["symbol", "Coin / Symbol", "text"],
    ["direction", "Direction", "dir"], ["strategy", "Strategy", "text"],
    ["entry", "Entry", "num"], ["exit_px", "Exit", "num"],
    ["sl", "Stop Loss", "num"], ["tp", "Take Profit", "num"],
    ["risk_pct", "Risk %", "num"], ["confidence", "Confidence (1-10)", "int"],
    ["emotion", "Emotion", "text"], ["mistakes", "Mistakes", "area"],
    ["lessons", "Lessons Learned", "area"], ["notes", "Notes", "area"],
    ["screenshot", "Screenshot (URL)", "text"], ["tags", "Tags (comma-separated)", "tags"],
  ];

  function el(tag, cls, txt) { var e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; }
  function num(v) { return v == null ? "—" : Number(v).toLocaleString("en-US", { maximumFractionDigits: 8 }); }
  function when(iso) { return window.IST ? window.IST.dateTime(iso) : new Date(iso).toLocaleString(); }

  function init(callbacks) { api = callbacks; }
  function mount(container) { root = container; load(); }

  function load() {
    if (!root || !api) return;
    api.list(filters).then(function (rows) { entries = rows || []; render(); })
      .catch(function () { entries = []; render(); });
  }

  function render() {
    if (!root) return;
    root.textContent = "";
    root.appendChild(toolbar());
    if (editing) root.appendChild(form());
    root.appendChild(listEl());
  }

  function toolbar() {
    var bar = el("div", "jr-toolbar");
    var add = el("button", "jr-btn jr-add", "＋ New Entry");
    add.addEventListener("click", function () { editing = {}; render(); });
    var search = el("input", "jr-search"); search.type = "search";
    search.placeholder = "Search title / notes / lessons…"; search.value = filters.search || "";
    search.addEventListener("input", function () { filters.search = search.value || undefined; });
    search.addEventListener("keydown", function (e) { if (e.key === "Enter") load(); });
    var go = el("button", "jr-btn", "Search"); go.addEventListener("click", load);
    var sym = el("input", "jr-filter"); sym.placeholder = "Coin"; sym.value = filters.symbol || "";
    sym.addEventListener("change", function () { filters.symbol = sym.value || undefined; load(); });
    var dir = el("select", "jr-filter");
    ["", "LONG", "SHORT"].forEach(function (d) { var o = el("option", null, d || "All directions"); o.value = d; dir.appendChild(o); });
    dir.value = filters.direction || "";
    dir.addEventListener("change", function () { filters.direction = dir.value || undefined; load(); });
    [add, search, go, sym, dir].forEach(function (n) { bar.appendChild(n); });
    return bar;
  }

  function field(f, val) {
    var wrap = el("label", "jr-field");
    wrap.appendChild(el("span", "jr-flabel", f[1]));
    var input;
    if (f[2] === "area") { input = el("textarea", "jr-input jr-area"); input.value = val == null ? "" : val; }
    else if (f[2] === "dir") {
      input = el("select", "jr-input");
      ["", "LONG", "SHORT"].forEach(function (d) { var o = el("option", null, d || "—"); o.value = d; input.appendChild(o); });
      input.value = val || "";
    } else if (f[2] === "tags") { input = el("input", "jr-input"); input.value = (val && val.length) ? val.join(", ") : ""; }
    else { input = el("input", "jr-input"); if (f[2] === "num" || f[2] === "int") input.type = "number"; input.value = val == null ? "" : val; }
    input.setAttribute("data-k", f[0]); input.setAttribute("data-t", f[2]);
    wrap.appendChild(input);
    return wrap;
  }

  function collectForm(formEl) {
    var body = {};
    formEl.querySelectorAll("[data-k]").forEach(function (inp) {
      var k = inp.getAttribute("data-k"), t = inp.getAttribute("data-t"), v = inp.value.trim();
      if (t === "num") body[k] = v === "" ? null : parseFloat(v);
      else if (t === "int") body[k] = v === "" ? null : parseInt(v, 10);
      else if (t === "tags") body[k] = v === "" ? [] : v.split(",").map(function (x) { return x.trim(); }).filter(Boolean);
      else body[k] = v === "" ? null : v;
    });
    return body;
  }

  function form() {
    var f = el("div", "jr-form");
    f.appendChild(el("div", "jr-form-title", editing.id ? "Edit entry" : "New journal entry"));
    var grid = el("div", "jr-form-grid");
    FIELDS.forEach(function (fd) { grid.appendChild(field(fd, editing[fd[0]])); });
    f.appendChild(grid);
    var actions = el("div", "jr-form-actions");
    var save = el("button", "jr-btn jr-save", editing.id ? "Save changes" : "Create");
    var cancel = el("button", "jr-btn", "Cancel");
    save.addEventListener("click", function () {
      var body = collectForm(f);
      var p = editing.id ? api.update(editing.id, body) : api.create(body);
      p.then(function () { editing = null; load(); })
        .catch(function (e) { window.alert("Save failed: " + ((e && e.message) || e)); });
    });
    cancel.addEventListener("click", function () { editing = null; render(); });
    actions.appendChild(save); actions.appendChild(cancel);
    f.appendChild(actions);
    return f;
  }

  function entryCard(e) {
    var card = el("div", "jr-card");
    var head = el("div", "jr-card-head");
    head.appendChild(el("span", "jr-title", e.title || "(untitled)"));
    if (e.direction) head.appendChild(el("span", "jr-dir " + (e.direction === "LONG" ? "up" : "down"), e.direction));
    if (e.symbol) head.appendChild(el("span", "jr-sym", e.symbol));
    head.appendChild(el("span", "jr-spacer"));
    var edit = el("button", "jr-mini", "Edit");
    edit.addEventListener("click", function () { editing = Object.assign({}, e); render(); if (root) root.scrollTop = 0; });
    var del = el("button", "jr-mini jr-del", "Delete");
    del.addEventListener("click", function () { if (window.confirm("Delete this journal entry?")) api.remove(e.id).then(load); });
    head.appendChild(edit); head.appendChild(del);
    card.appendChild(head);
    var rail = el("div", "jr-rail");
    [["Entry", e.entry], ["Exit", e.exit_px], ["SL", e.sl], ["TP", e.tp], ["Risk%", e.risk_pct], ["Conf", e.confidence]]
      .forEach(function (p) {
        if (p[1] != null) { var it = el("span", "jr-kv"); it.appendChild(el("b", null, p[0])); it.appendChild(document.createTextNode(" " + num(p[1]))); rail.appendChild(it); }
      });
    if (rail.children.length) card.appendChild(rail);
    [["Strategy", e.strategy], ["Emotion", e.emotion], ["Notes", e.notes], ["Mistakes", e.mistakes], ["Lessons", e.lessons]]
      .forEach(function (r) {
        if (r[1]) { var line = el("div", "jr-line"); line.appendChild(el("b", null, r[0] + ": ")); line.appendChild(document.createTextNode(r[1])); card.appendChild(line); }
      });
    if (e.tags && e.tags.length) { var t = el("div", "jr-tags"); e.tags.forEach(function (tag) { t.appendChild(el("span", "jr-tag", tag)); }); card.appendChild(t); }
    if (e.screenshot) { var a = el("a", "jr-shot", "📷 screenshot"); a.href = e.screenshot; a.target = "_blank"; a.rel = "noopener"; card.appendChild(a); }
    if (e.created_at) card.appendChild(el("div", "jr-date", when(e.created_at)));
    return card;
  }

  function listEl() {
    var wrap = el("div", "jr-list");
    if (!entries.length) { wrap.appendChild(el("div", "jr-empty", "No journal entries yet. Click “New Entry” to log your first trade.")); return wrap; }
    entries.forEach(function (e) { wrap.appendChild(entryCard(e)); });
    return wrap;
  }

  window.Journal = { init: init, mount: mount, reload: load };
})();

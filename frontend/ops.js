/* MarketScalper — operational status: Live status pill, Operations dashboard,
 * and the live Activity feed (pre-prod items 3/4/5/9/10).
 *
 * Pure renderer (the dashboard.js / panel.js pattern): app.js owns the network
 * (it polls GET /ops and feeds the data here) and the runtime clock; this file
 * only renders. No network calls, no browser storage, no engine math. Times via
 * window.IST. XSS-safe: it writes textContent, never raw markup.
 */
(function () {
  "use strict";

  // The real per-candle pipeline stages — cycled in the status pill so the app
  // visibly "never appears idle" while it scans (item 3/4). Honest: the engine
  // genuinely runs structure -> liquidity -> volume -> confluence each candle.
  var ACTIVITIES = [
    "Reading market structure", "Checking liquidity", "Analysing volume",
    "Scoring confluence", "Searching trade setups",
  ];

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function fmtUptime(s) {
    if (s == null) return "—";
    var d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600),
        m = Math.floor((s % 3600) / 60);
    if (d) return d + "d " + h + "h";
    if (h) return h + "h " + m + "m";
    return m + "m";
  }
  function dot(ok) { return el("span", "dot" + (ok ? " live" : ok === false ? " down" : "")); }

  // -------- Live top-bar status pill (items 3/5) --------
  function renderPill(target, data, activityText) {
    if (!target) return;
    target.textContent = "";
    var running = data && data.scanner && data.scanner.running;
    target.appendChild(dot(running ? true : (data ? false : null)));
    var label = el("span", "ops-pill-label",
      running ? "Scanner running" : (data ? "Scanner idle" : "Connecting…"));
    target.appendChild(label);
    if (running && activityText) {
      target.appendChild(el("span", "ops-pill-act", "· " + activityText));
    }
    var last = data && data.scanner && data.scanner.last_scan;
    target.appendChild(el("span", "ops-pill-scan",
      last ? "· last scan " + window.IST.hm(last) : ""));
  }

  // -------- Operations dashboard (items 9/10) --------
  function row(grid, label, valueNode) {
    grid.appendChild(el("div", "ops-k", label));
    if (typeof valueNode === "string" || typeof valueNode === "number") {
      grid.appendChild(el("div", "ops-v", String(valueNode)));
    } else {
      var v = el("div", "ops-v"); v.appendChild(valueNode); grid.appendChild(v);
    }
  }
  function badge(ok, textOk, textBad) {
    var b = el("span", "ops-badge " + (ok ? "ok" : ok === false ? "bad" : "warn"),
      ok ? textOk : ok === false ? textBad : "unknown");
    return b;
  }
  function renderDashboard(target, data) {
    if (!target) return;
    target.textContent = "";
    if (!data) { target.appendChild(el("div", "ops-empty", "Loading operations…")); return; }
    var grid = el("div", "ops-grid");
    row(grid, "Feed", badge(data.feed && data.feed.connected, "Connected", "Disconnected"));
    row(grid, "Scanner", badge(data.scanner && data.scanner.running, "Running", "Idle"));
    row(grid, "Database", badge(data.database && data.database.ok, "OK", "Unavailable"));
    row(grid, "Backfill", badge(!(data.backfill && data.backfill.active), "Idle (up to date)", "Catching up"));
    row(grid, "Uptime", fmtUptime(data.uptime_s));
    row(grid, "Last scan", data.scanner && data.scanner.last_scan ? window.IST.full(data.scanner.last_scan) : "—");
    target.appendChild(grid);

    // per-symbol last candle + data coverage (item 10)
    var syms = (data.feed && data.feed.symbols) || [];
    for (var i = 0; i < syms.length; i++) {
      var s = syms[i];
      var cov = (data.data_coverage || {})[s];
      var lc = (data.last_candle || {})[s];
      var card = el("div", "ops-symcard");
      card.appendChild(el("div", "ops-sym", s));
      var line = el("div", "ops-symline");
      line.appendChild(el("span", "ops-k2", "Last candle"));
      line.appendChild(el("span", "ops-v2", lc ? window.IST.dateTime(lc) : "—"));
      card.appendChild(line);
      var line2 = el("div", "ops-symline");
      line2.appendChild(el("span", "ops-k2", "Coverage"));
      line2.appendChild(el("span", "ops-v2 mono",
        cov ? ((cov.count || 0) + " candles · " +
               (cov.earliest ? window.IST.dateTime(cov.earliest) : "?") + " → " +
               (cov.latest ? window.IST.dateTime(cov.latest) : "?")) : "—"));
      card.appendChild(line2);
      target.appendChild(card);
    }
  }

  // -------- live Activity feed (item 4) --------
  var feedEl = null, MAX = 40;
  function initActivity(target) { feedEl = target; if (feedEl) feedEl.textContent = ""; }
  function pushActivity(text, kind) {
    if (!feedEl) return;
    var line = el("div", "act-line" + (kind ? " act-" + kind : ""));
    line.appendChild(el("span", "act-time mono", window.IST.time(new Date())));
    line.appendChild(el("span", "act-msg", text));
    feedEl.insertBefore(line, feedEl.firstChild);
    while (feedEl.childNodes.length > MAX) feedEl.removeChild(feedEl.lastChild);
  }

  window.Ops = {
    ACTIVITIES: ACTIVITIES,
    renderPill: renderPill,
    renderDashboard: renderDashboard,
    initActivity: initActivity,
    pushActivity: pushActivity,
    fmtUptime: fmtUptime,
  };
})();

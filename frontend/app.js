/* ============================================================================
   MarketScalper V4 — application logic
   ----------------------------------------------------------------------------
   This file owns ALL network access. Rendering helpers below are pure DOM
   builders and always write server strings with textContent.
   ========================================================================== */
(function () {
  "use strict";

  // ------------------------------------------------------------------ setup --
  var qs = new URLSearchParams(location.search);
  var HOST = qs.get("api") || (location.port === "9000" ? "127.0.0.1:8000" : location.host);
  var HTTP = location.protocol + "//" + HOST;
  var TOKEN = qs.get("token") || localStorage.getItem("ms_token") || "";
  if (qs.get("token")) localStorage.setItem("ms_token", qs.get("token"));

  var state = {
    page: "today", symFilter: "", chartSym: "ETHUSDT", chartTf: "4h",
    showLevels: true, setups: [], catalogue: null, chart: null, series: null,
    lines: [], perf: null
  };

  var failures = 0;
  function banner(msg) {
    var b = $("banner"); if (!b) return;
    if (msg) { $("banner-msg").textContent = msg; b.classList.add("on"); }
    else b.classList.remove("on");
  }
  function api(path, opts) {
    var init = opts || {};
    init.headers = TOKEN ? { Authorization: "Bearer " + TOKEN } : {};
    return fetch(HTTP + path, init)
      .then(function (r) {
        if (r.status === 401) { signOut(); throw new Error("signed out"); }
        if (!r.ok) throw new Error(r.status + " " + path);
        if (failures) { failures = 0; banner(null); }
        return r.json();
      })
      .catch(function (e) {
        // A trading tool must never look live while it is actually stale.
        failures++;
        banner("Backend unreachable — figures on screen may be stale. (" +
               String(e.message || e) + ")");
        throw e;
      });
  }
  /* The single write path. Surfaces the server's own error text, which for
     the paper endpoints is the actionable part ("insufficient margin"). */
  function post(path, body, method) {
    var h = { "Content-Type": "application/json" };
    if (TOKEN) h.Authorization = "Bearer " + TOKEN;
    return fetch(HTTP + path, { method: method || "POST", headers: h,
                                body: JSON.stringify(body) })
      .then(function (r) {
        if (r.status === 401) { signOut(); throw new Error("signed out"); }
        if (r.ok) return r.json();
        return r.text().then(function (t) { throw new Error(t || r.status); });
      })
      .catch(function (e) {
        banner("Could not save — " + String(e.message || e));
        throw e;
      });
  }
  var $ = function (id) { return document.getElementById(id); };
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function fmt(n, d) {
    if (n === null || n === undefined || isNaN(n)) return "—";
    return Number(n).toLocaleString(undefined, { minimumFractionDigits: d === undefined ? 2 : d,
                                                 maximumFractionDigits: d === undefined ? 2 : d });
  }
  function sign(n, d) { return (n > 0 ? "+" : "") + fmt(n, d); }
  function when(ts) {
    if (!ts) return "—";
    var dt = new Date(ts * 1000);
    return dt.toLocaleString(undefined, { month: "short", day: "2-digit",
                                          hour: "2-digit", minute: "2-digit" });
  }

  // ------------------------------------------------------------------ login --
  /* The API gate is the Bearer token (D3). This screen only exchanges the
     owner's credentials for it via POST /login; no session, no cookie. If the
     token is ever rejected we come straight back here rather than leaving a
     dead terminal on screen. */
  function showGate(message) {
    var g = $("gate"); if (!g) return;
    g.hidden = false;
    var err = $("gate-err");
    if (message) { err.textContent = message; err.hidden = false; }
    else err.hidden = true;
    $("gate-user").focus();
  }

  function signOut() {
    TOKEN = "";
    try { localStorage.removeItem("ms_token"); } catch (e) {}
    banner(null);
    showGate("Your session is no longer valid. Please sign in again.");
  }

  $("gate-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var btn = $("gate-go");
    btn.disabled = true; btn.textContent = "Signing in…";
    fetch(HTTP + "/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: $("gate-user").value,
                             password: $("gate-pass").value })
    }).then(function (r) {
      if (r.status === 401) throw new Error("Wrong username or password.");
      if (r.status === 503) throw new Error("Login is not configured on this server.");
      if (!r.ok) throw new Error("Sign-in failed (" + r.status + ").");
      return r.json();
    }).then(function (d) {
      TOKEN = d.token;
      try { localStorage.setItem("ms_token", TOKEN); } catch (e) {}
      $("gate-pass").value = "";
      $("gate").hidden = true;
      boot();
    }).catch(function (e) {
      showGate(String(e.message || e));
    }).then(function () {
      btn.disabled = false; btn.textContent = "Sign in";
    });
  });

  // ----------------------------------------------------------------- router --
  function go(page) {
    state.page = page;
    document.querySelectorAll(".page").forEach(function (p) {
      p.classList.toggle("active", p.dataset.page === page);
    });
    document.querySelectorAll(".nav-btn[data-go]").forEach(function (b) {
      b.classList.toggle("active", b.dataset.go === page);
    });
    if (page === "chart") { ensureChart(); loadChart(); loadPaperBook(); }
    if (page === "history") loadHistory();
    if (page === "paper" || page === "strategies") loadPerformance();
    if (page === "paper" && !(state.histRows || []).length) loadHistory();
    if (page === "journal") loadJournal();
    if (page === "settings") loadSettings();
  }
  document.querySelectorAll(".nav-btn[data-go]").forEach(function (b) {
    b.addEventListener("click", function () { go(b.dataset.go); });
  });
  $("refresh").addEventListener("click", function () { boot(); });

  // ------------------------------------------------------------------ theme --
  function setTheme(t) {
    document.documentElement.dataset.theme = t;
    try { localStorage.setItem("ms_v4_theme", t); } catch (e) {}
    if (state.chart) {
      state.chart.applyOptions(chartTheme());
      state.series.applyOptions(seriesTheme());
      drawLevels();                       // price lines carry their own colours
    }
    drawEquityCurve();
  }
  $("theme").addEventListener("click", function () {
    setTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
  });
  var bx = $("banner-x");
  if (bx) bx.addEventListener("click", function () { banner(null); });

  // ------------------------------------------------------------------ TODAY --
  function viewOnChart(s) {
    state.chartSym = s.symbol; state.chartTf = s.level_tf; state.ticket = s;
    syncSeg("chart-sym", "sym", s.symbol); syncSeg("chart-tf", "tf", s.level_tf);
    go("chart");
  }

  /* The geometry, drawn to scale. "R:R 8.96" is a number you have to trust;
     a bar where the green dwarfs the red is a fact you can see in one glance. */
  function ladder(s) {
    var risk = Math.abs(s.entry - s.stop), reward = Math.abs(s.target - s.entry);
    var total = risk + reward || 1;
    var wrap = el("div", "ladder");
    var bar = el("div", "ladder-bar");
    var r = el("i", "risk"); r.style.width = (risk / total * 100).toFixed(2) + "%";
    var w = el("i", "reward"); w.style.width = (reward / total * 100).toFixed(2) + "%";
    bar.appendChild(r); bar.appendChild(w);
    wrap.appendChild(bar);

    var legend = el("div", "ladder-legend");
    var left = el("span", "risk-l");
    left.appendChild(document.createTextNode("Risk "));
    left.appendChild(el("b", null, fmt(risk, 2)));
    var right = el("span", "reward-l");
    right.appendChild(el("b", null, fmt(reward, 2)));
    right.appendChild(document.createTextNode(" reward · " + fmt(s.rr, 2) + "R net"));
    legend.appendChild(left); legend.appendChild(right);
    wrap.appendChild(legend);
    return wrap;
  }

  function priceCells(s) {
    var pr = el("div", "rail-prices");
    [["Entry", s.entry, ""], ["Stop", s.stop, "stop"],
     ["Target", s.target, "target"], ["Net R:R", s.rr, "rr"]].forEach(function (row) {
      var c = el("div", "pcell " + row[2]);
      c.appendChild(el("div", "k", row[0]));
      c.appendChild(el("div", "v", fmt(row[1], 2)));
      pr.appendChild(c);
    });
    return pr;
  }

  /* Rank 1 only. Everything the trader needs to decide, without a click. */
  function setupCard(s, rank) {
    var card = el("div", "setup rank-" + rank + " " + (s.direction > 0 ? "long" : "short"));

    var h = el("div", "setup-h");
    h.appendChild(el("span", "dir " + (s.direction > 0 ? "long" : "short"), s.direction_label));
    h.appendChild(el("span", "sym", s.symbol.replace("USDT", "")));
    h.appendChild(el("span", "strat-tag", s.strategy_id));
    var sp = el("div"); sp.style.flex = "1"; h.appendChild(sp);
    if (rank === 1) h.appendChild(el("span", "rank-badge", "Best setup"));
    var pips = el("div", "filters");
    pips.title = s.filters_passed + " of 3 trend filters agree";
    for (var i = 0; i < 3; i++) pips.appendChild(el("span", "pip" + (i < s.filters_passed ? " on" : "")));
    h.appendChild(pips);
    card.appendChild(h);

    card.appendChild(ladder(s));
    card.appendChild(priceCells(s));
    card.appendChild(el("div", "why", s.reason));

    var f = el("div", "setup-f");
    f.appendChild(el("span", "sub", "risk " + fmt(s.risk_pct, 2) + "% of price · valid till " +
                                    when(s.valid_until_ts)));
    var gap = el("div"); gap.style.flex = "1"; f.appendChild(gap);
    var b = el("button", "btn primary", "View on chart");
    b.addEventListener("click", function () { viewOnChart(s); });
    f.appendChild(b);
    card.appendChild(f);
    return card;
  }

  /* Setups 2..n. Same information, a tenth of the ink — a dozen equally-sized
     cards makes the reader re-rank what the engine already ranked. */
  function setupRow(s, i) {
    var row = el("div", "srow " + (s.direction > 0 ? "long" : "short"));
    row.style.animationDelay = Math.min(i * 22, 220) + "ms";
    row.appendChild(el("span", "dir " + (s.direction > 0 ? "long" : "short"), s.direction_label));
    row.appendChild(el("span", "sym", s.symbol.replace("USDT", "")));

    var mid = el("div");
    mid.appendChild(el("div", "strat-tag", s.strategy_id));
    mid.appendChild(el("div", "meta", s.reason));
    row.appendChild(mid);

    [["Entry", s.entry, ""], ["Stop", s.stop, "stop"],
     ["Target", s.target, "target hide-md"], ["R:R", s.rr, ""]].forEach(function (col) {
      var cell = el("div", "cell " + col[2]);
      cell.appendChild(el("span", "k", col[0]));
      cell.appendChild(el("span", "v", fmt(col[1], 2)));
      row.appendChild(cell);
    });

    var b = el("button", "btn", "Chart");
    b.addEventListener("click", function (ev) { ev.stopPropagation(); viewOnChart(s); });
    row.appendChild(b);
    row.addEventListener("click", function () { viewOnChart(s); });
    return row;
  }

  /* A filled setup is a LIVE position — it needs managing, not deciding, so it
     gets its own section above the ones still waiting for a level to break. */
  function activeRow(r, i) {
    var lng = Number(r.direction) > 0;
    var row = el("div", "srow " + (lng ? "long" : "short"));
    row.style.animationDelay = Math.min(i * 22, 200) + "ms";
    row.appendChild(el("span", "dir " + (lng ? "long" : "short"), lng ? "LONG" : "SHORT"));
    row.appendChild(el("span", "sym", String(r.symbol).replace("USDT", "")));

    var mid = el("div");
    mid.appendChild(el("div", "strat-tag", r.strategy_id));
    var fill = r.fill_price != null ? r.fill_price : r.entry;
    mid.appendChild(el("div", "meta", "filled " + fmt(fill, 2) + " · " + when(r.filled_ts || r.decision_ts)));
    row.appendChild(mid);

    var px = state.lastQuote[r.symbol];
    var risk = Math.abs(fill - r.stop);
    var openR = (px != null && risk) ? (px - fill) / risk * (lng ? 1 : -1) : null;

    [["Stop", r.stop, "stop"], ["Target", r.target, "target hide-md"],
     ["Now", px, ""]].forEach(function (col) {
      var cell = el("div", "cell " + col[2]);
      cell.appendChild(el("span", "k", col[0]));
      cell.appendChild(el("span", "v", col[1] == null ? "—" : fmt(col[1], 2)));
      row.appendChild(cell);
    });
    var pnl = el("div", "cell");
    pnl.appendChild(el("span", "k", "Open R"));
    var v = el("span", "v " + (openR == null ? "" : openR >= 0 ? "up" : "down"),
               openR == null ? "—" : sign(openR, 2) + "R");
    pnl.appendChild(v);
    row.appendChild(pnl);

    var b = el("button", "btn", "Chart");
    b.addEventListener("click", function (ev) {
      ev.stopPropagation();
      viewOnChart({ symbol: r.symbol, level_tf: r.level_tf || "4h" });
    });
    row.appendChild(b);
    return row;
  }

  function renderActive() {
    var box = $("active"); if (!box) return;
    var rows = (state.active || []).filter(function (r) {
      return !state.symFilter || r.symbol === state.symFilter;
    });
    box.textContent = "";
    if (!rows.length) return;                    // no section at all when empty
    var t = el("div", "sec-t");
    t.appendChild(document.createTextNode("Active trades · " + rows.length));
    t.appendChild(el("span", "sub", "entry filled, running against the stop"));
    box.appendChild(t);
    var list = el("div", "setup-list");
    rows.forEach(function (r, i) { list.appendChild(activeRow(r, i)); });
    box.appendChild(list);
  }

  function loadActive() {
    return api("/api/v4/history?status=FILLED&limit=50").then(function (d) {
      state.active = d.rows || [];
      renderActive(); renderToday();
    }).catch(function () { state.active = []; });
  }

  function renderToday() {
    var rows = state.setups.filter(function (s) {
      return !state.symFilter || s.symbol === state.symFilter;
    });
    var box = $("setups"); box.textContent = "";

    if (!rows.length) {
      var e = el("div", "empty");
      e.appendChild(el("div", "big", "No setup right now"));
      e.appendChild(el("div", "sub",
        "That is a normal and common state — these strategies wait for a level to break " +
        "with the trend behind it, which happens a few times a week. Levels are being watched."));
      box.appendChild(e);
    } else {
      var head = el("div", "sec-t");
      head.appendChild(document.createTextNode(
        rows.length === 1 ? "Trade setup" : "Trade setups · " + rows.length));
      head.appendChild(el("span", "sub", "waiting for the level to break"));
      box.appendChild(head);
      box.appendChild(setupCard(rows[0], 1));
      if (rows.length > 1) {
        box.appendChild(el("div", "sec-t", "Also live · " + (rows.length - 1)));
        var list = el("div", "setup-list");
        rows.slice(1).forEach(function (s, i) { list.appendChild(setupRow(s, i)); });
        box.appendChild(list);
      }
    }

    var longs = rows.filter(function (r) { return r.direction > 0; }).length;
    var best = rows.length ? fmt(rows[0].rr, 2) + "R" : "—";
    var st = $("today-stats"); st.textContent = "";
    [["Trade setups", rows.length, rows.length ? "ranked by filters, then R:R" : "watching levels"],
     ["Active trades", (state.active || []).length, "entry filled, running"],
     ["Long / Short", longs + " / " + (rows.length - longs), "direction split"],
     ["Best net R:R", best, "after fees and funding"],
     ["Strategies live", state.catalogue ? state.catalogue.strategies.length : "—", "BTC · ETH"]
    ].forEach(function (row) {
      var c = el("div", "stat");
      c.appendChild(el("div", "stat-k", row[0]));
      c.appendChild(el("div", "stat-v", String(row[1])));
      c.appendChild(el("div", "stat-s", row[2]));
      st.appendChild(c);
    });
    $("today-sub").textContent = rows.length + " actionable · updated " +
      new Date().toLocaleTimeString();
  }

  function syncSeg(id, key, val) {
    var g = $(id); if (!g) return;
    g.querySelectorAll("button").forEach(function (b) {
      b.classList.toggle("on", b.dataset[key] === val);
    });
  }
  $("sym-filter").addEventListener("click", function (e) {
    var b = e.target.closest("button"); if (!b) return;
    state.symFilter = b.dataset.sym;
    syncSeg("sym-filter", "sym", state.symFilter);
    renderToday();
  });

  // ---------------------------------------------------------------- QUOTES --
  state.lastQuote = {};
  function loadQuotes() {
    api("/api/v4/quotes").then(function (d) {
      var box = $("quotes"); if (!box) return;
      box.textContent = "";
      box.appendChild(el("span", "q-lbl", "Last price"));
      Object.keys(d.quotes || {}).forEach(function (sym) {
        var q = d.quotes[sym], prev = state.lastQuote[sym];
        var dir = prev === undefined ? 0 : (q.price > prev ? 1 : q.price < prev ? -1 : 0);
        state.lastQuote[sym] = q.price;
        var c = el("div", "q " + (dir > 0 ? "up" : dir < 0 ? "down" : ""));
        c.appendChild(el("span", "s", sym.replace("USDT", "")));
        c.appendChild(el("span", "p", fmt(q.price, 2)));
        box.appendChild(c);
      });
    }).catch(function () {});
  }

  // ------------------------------------------------------------- STRATEGIES --
  /* Owner switch. Disabling only stops NEW setups being issued — rows already
     recorded keep resolving, so live stats never get silently truncated. */
  function strategyToggle(s) {
    var lab = el("label", "toggle");
    lab.title = s.can_toggle
      ? "Turn this strategy off — it stops issuing new setups"
      : "Runtime switching needs the settings store (live server only)";
    var box = document.createElement("input");
    box.type = "checkbox"; box.checked = !!s.enabled;
    box.disabled = !s.can_toggle;
    box.setAttribute("aria-label", (s.enabled ? "Disable " : "Enable ") + s.label);
    box.addEventListener("change", function () {
      var want = box.checked;
      box.disabled = true;
      post("/api/v4/strategies/" + s.id + "/enabled", { enabled: want })
        .then(function () { loadCatalogue(); loadSetups(); })
        .catch(function () { box.checked = !want; box.disabled = false; });
    });
    lab.appendChild(box);
    lab.appendChild(el("span", "track"));
    return lab;
  }

  function renderStrategies() {
    var d = state.catalogue; if (!d) return;
    var g = d.geometry;
    $("geometry").textContent = "Geometry (fixed by research, not tunable): entry = " +
      g.entry + " · stop = " + g.stop + " · target = " + g.target +
      " · max hold " + g.max_hold_days + "d · taker " + (g.taker_fee * 100).toFixed(3) +
      "% · funding " + (g.funding_per_day * 100).toFixed(3) + "%/day";

    var box = $("strategy-cards"); box.textContent = "";
    d.strategies.forEach(function (s) {
      var live = state.perf && state.perf.by_strategy ? state.perf.by_strategy[s.id] : null;
      var c = el("div", "card" + (s.enabled ? "" : " off"));
      var h = el("div", "card-h");
      h.appendChild(el("span", "dot " + (s.enabled ? "live" : "off")));
      h.appendChild(el("span", "card-t", s.label));
      var sp = el("div"); sp.style.flex = "1"; h.appendChild(sp);
      h.appendChild(el("span", "strat-tag", s.level + " · " + s.min_filters + "/3 filters"));
      h.appendChild(strategyToggle(s));
      c.appendChild(h);

      var sp2 = el("div", "split");
      var a = el("div");
      a.appendChild(el("div", "lbl", "Backtest · " + s.backtest.period));
      a.appendChild(el("div", "num", sign(s.backtest.net_r, 3) + " R / trade"));
      a.appendChild(el("div", "sub", "t = " + fmt(s.backtest.t_stat, 2) + " · PF " +
        fmt(s.backtest.profit_factor, 2) + " · " + fmt(s.backtest.trades_per_year, 0) + " trades/yr"));
      sp2.appendChild(a);
      var bdiv = el("div");
      bdiv.appendChild(el("div", "lbl", "Live paper"));
      if (live && live.n) {
        bdiv.appendChild(el("div", "num", sign(live.avg_net_r, 3) + " R / trade"));
        bdiv.appendChild(el("div", "sub", live.n + " trades · win " +
          fmt(live.win_rate * 100, 0) + "% · PF " + fmt(live.profit_factor, 2)));
      } else {
        bdiv.appendChild(el("div", "num", "—"));
        bdiv.appendChild(el("div", "sub", "no live trades yet"));
      }
      sp2.appendChild(bdiv);
      c.appendChild(sp2);
      if (s.note) c.appendChild(el("div", "why", s.note));
      box.appendChild(c);
    });

    var rej = $("rejected"); rej.textContent = "";
    Object.keys(d.rejected_ideas || {}).forEach(function (k) {
      var tr = el("tr");
      tr.appendChild(el("td", null, k.replace(/_/g, " ")));
      tr.appendChild(el("td", null, d.rejected_ideas[k]));
      rej.appendChild(tr);
    });
  }

  // ---------------------------------------------------------------- HISTORY --
  state.histRows = []; state.sortKey = "decision_ts"; state.sortDir = -1;

  function renderHistory() {
    var d = { rows: state.histRows.slice() };
    var k = state.sortKey, dir = state.sortDir;
    d.rows.sort(function (a, b) {
      var x = a[k], y = b[k];
      if (x === null || x === undefined) return 1;
      if (y === null || y === undefined) return -1;
      return x === y ? 0 : (x > y ? dir : -dir);
    });
    document.querySelectorAll("#history-table th.sortable, th.sortable").forEach(function (th) {
      th.classList.remove("asc", "desc");
      if (th.dataset.sort === k) th.classList.add(dir > 0 ? "asc" : "desc");
    });
    drawHistory(d);
  }

  function loadHistory() {
    var s = $("hist-strategy").value, st = $("hist-status").value;
    var q = "/api/v4/history?limit=300" + (s ? "&strategy=" + s : "") + (st ? "&status=" + st : "");
    api(q).then(function (d) {
      state.histRows = d.rows || [];
      state.histNote = d.note;
      renderHistory();
    }).catch(function () {});
  }

  function drawHistory(d) {
    (function () {
      var body = $("hist-rows"); body.textContent = "";
      if (!d.rows || !d.rows.length) {
        var tr = el("tr"), td = el("td", null, state.histNote || "No recommendations recorded yet.");
        td.colSpan = 12; td.style.textAlign = "center"; td.style.padding = "34px";
        td.style.color = "var(--ink-3)"; tr.appendChild(td); body.appendChild(tr);
        return;
      }
      d.rows.forEach(function (r) {
        var tr = el("tr");
        tr.appendChild(el("td", null, when(r.decision_ts)));
        tr.appendChild(el("td", null, (r.symbol || "").replace("USDT", "")));
        var dcell = el("td");
        dcell.appendChild(el("span", "dir " + (r.direction > 0 ? "long" : "short"),
                             r.direction > 0 ? "LONG" : "SHORT"));
        tr.appendChild(dcell);
        tr.appendChild(el("td", null, r.strategy_id || "—"));
        tr.appendChild(el("td", "num", fmt(r.entry)));
        tr.appendChild(el("td", "num", fmt(r.stop)));
        tr.appendChild(el("td", "num", fmt(r.target)));
        var oc = el("td");
        var cls = r.status === "TP" ? "tp" : r.status === "SL" ? "sl" :
                  r.status === "TIME" ? "time" : "open";
        oc.appendChild(el("span", "badge " + cls, r.status || "OPEN"));
        tr.appendChild(oc);
        var nr = el("td", "num " + (r.net_r > 0 ? "up" : r.net_r < 0 ? "down" : ""),
                    r.net_r === null || r.net_r === undefined ? "—" : sign(r.net_r, 3));
        tr.appendChild(nr);
        tr.appendChild(el("td", "num", r.mae_r === null || r.mae_r === undefined ? "—" : fmt(r.mae_r, 2)));
        tr.appendChild(el("td", "num", r.mfe_r === null || r.mfe_r === undefined ? "—" : fmt(r.mfe_r, 2)));
        tr.appendChild(el("td", "num", r.hold_minutes ? Math.round(r.hold_minutes / 60) + "h" : "—"));
        body.appendChild(tr);
      });
    })();
  }

  document.querySelectorAll("th.sortable").forEach(function (th) {
    th.addEventListener("click", function () {
      var k = th.dataset.sort;
      if (state.sortKey === k) state.sortDir = -state.sortDir;
      else { state.sortKey = k; state.sortDir = -1; }
      renderHistory();
    });
  });

  var csvBtn = $("hist-csv");
  if (csvBtn) csvBtn.addEventListener("click", function () {
    var rows = state.histRows || [];
    if (!rows.length) return;
    var cols = ["decision_ts", "symbol", "direction", "strategy_id", "entry", "stop",
                "target", "status", "gross_r", "fee_r", "funding_r", "net_r",
                "mae_r", "mfe_r", "hold_minutes"];
    var out = [cols.join(",")];
    rows.forEach(function (r) {
      out.push(cols.map(function (c) {
        var v = r[c];
        if (c === "decision_ts" && v) v = new Date(v * 1000).toISOString();
        return v === null || v === undefined ? "" : String(v);
      }).join(","));
    });
    var blob = new Blob([out.join("\n")], { type: "text/csv" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "marketscalper-v4-history.csv";
    a.click(); URL.revokeObjectURL(a.href);
  });

  $("hist-strategy").addEventListener("change", loadHistory);
  $("hist-status").addEventListener("change", loadHistory);

  // ------------------------------------------------------------ PERFORMANCE --
  function statBlock(target, items) {
    var box = $(target); box.textContent = "";
    items.forEach(function (s) {
      var c = el("div", "stat");
      c.appendChild(el("div", "stat-k", s[0]));
      var v = el("div", "stat-v" + (s[2] ? " " + s[2] : ""), s[1]);
      c.appendChild(v);
      if (s[3]) c.appendChild(el("div", "stat-s", s[3]));
      box.appendChild(c);
    });
  }
  function loadPerformance() {
    api("/api/v4/performance").then(function (d) {
      state.perf = d;
      var o = d.overall || {};
      var items = [
        ["Closed trades", o.n || 0, ""],
        ["Net R / trade", o.avg_net_r === undefined ? "—" : sign(o.avg_net_r, 3),
          o.avg_net_r > 0 ? "up" : o.avg_net_r < 0 ? "down" : ""],
        ["Total R", o.total_r === undefined ? "—" : sign(o.total_r, 1),
          o.total_r > 0 ? "up" : o.total_r < 0 ? "down" : ""],
        ["Win rate", o.win_rate === undefined ? "—" : fmt(o.win_rate * 100, 1) + "%", ""],
        ["Profit factor", o.profit_factor === undefined || o.profit_factor === null ? "—" : fmt(o.profit_factor, 2), ""],
        ["Max drawdown", o.max_drawdown_r === undefined ? "—" : fmt(o.max_drawdown_r, 1) + " R", ""],
        ["Open", o.n_open || 0, ""],
        ["Avg hold", o.avg_hold_minutes ? Math.round(o.avg_hold_minutes / 60) + "h" : "—", ""]
      ];
      statBlock("paper-stats", items);
      statBlock("hist-stats", items.slice(0, 6));

      var body = $("paper-rows"); body.textContent = "";
      var by = d.by_strategy || {};
      var keys = Object.keys(by);
      if (!keys.length) {
        var tr = el("tr"), td = el("td", null, "No paper trades yet.");
        td.colSpan = 8; td.style.textAlign = "center"; td.style.padding = "34px";
        td.style.color = "var(--ink-3)"; tr.appendChild(td); body.appendChild(tr);
      } else {
        keys.forEach(function (k) {
          var s = by[k], tr = el("tr");
          tr.appendChild(el("td", null, k));
          tr.appendChild(el("td", "num", s.n || 0));
          tr.appendChild(el("td", "num", s.win_rate === undefined ? "—" : fmt(s.win_rate * 100, 0) + "%"));
          tr.appendChild(el("td", "num " + (s.avg_net_r > 0 ? "up" : "down"), sign(s.avg_net_r, 3)));
          tr.appendChild(el("td", "num " + (s.total_r > 0 ? "up" : "down"), sign(s.total_r, 1)));
          tr.appendChild(el("td", "num", s.profit_factor ? fmt(s.profit_factor, 2) : "—"));
          tr.appendChild(el("td", "num", fmt(s.max_drawdown_r, 1)));
          tr.appendChild(el("td", null, (s.tp || 0) + " / " + (s.sl || 0) + " / " + (s.time_exit || 0)));
          body.appendChild(tr);
        });
      }
      if (state.page === "strategies") renderStrategies();
      drawEquityCurve();
    }).catch(function () {});
  }

  /* cumulative R over closed trades — drawn from history, not from summary
     stats, so the curve and the table can never disagree. */
  function drawEquityCurve() {
    var svg = $("equity-curve"); if (!svg) return;
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    var rows = (state.histRows || []).filter(function (r) { return r.net_r !== null && r.net_r !== undefined; });
    rows.sort(function (a, b) { return a.decision_ts - b.decision_ts; });
    if (rows.length < 2) {
      var t = document.createElementNS("http://www.w3.org/2000/svg", "text");
      t.setAttribute("x", 400); t.setAttribute("y", 62);
      t.setAttribute("text-anchor", "middle"); t.setAttribute("fill", tok("--ink-3"));
      t.setAttribute("font-size", "13");
      t.textContent = "Not enough closed trades yet";
      svg.appendChild(t); return;
    }
    var cum = 0, pts = rows.map(function (r, i) { cum += r.net_r; return [i, cum]; });
    var ys = pts.map(function (p) { return p[1]; });
    var lo = Math.min(0, Math.min.apply(null, ys)), hi = Math.max(0, Math.max.apply(null, ys));
    var pad = (hi - lo) * 0.1 || 1; lo -= pad; hi += pad;
    var X = function (i) { return (i / (pts.length - 1)) * 800; };
    var Y = function (v) { return 120 - ((v - lo) / (hi - lo)) * 120; };
    var NS = "http://www.w3.org/2000/svg";
    var zero = document.createElementNS(NS, "line");
    zero.setAttribute("x1", 0); zero.setAttribute("x2", 800);
    zero.setAttribute("y1", Y(0)); zero.setAttribute("y2", Y(0));
    zero.setAttribute("stroke", tok("--chart-border")); zero.setAttribute("stroke-dasharray", "4 4");
    svg.appendChild(zero);
    var d = pts.map(function (p, i) { return (i ? "L" : "M") + X(p[0]).toFixed(1) + " " + Y(p[1]).toFixed(1); }).join(" ");
    var area = document.createElementNS(NS, "path");
    area.setAttribute("d", d + " L800 " + Y(0).toFixed(1) + " L0 " + Y(0).toFixed(1) + " Z");
    area.setAttribute("fill", cum >= 0 ? tok("--up-bg") : tok("--down-bg"));
    svg.appendChild(area);
    var line = document.createElementNS(NS, "path");
    line.setAttribute("d", d); line.setAttribute("fill", "none");
    line.setAttribute("stroke", cum >= 0 ? tok("--up") : tok("--down"));
    line.setAttribute("stroke-width", "2"); line.setAttribute("vector-effect", "non-scaling-stroke");
    svg.appendChild(line);
  }

  // ------------------------------------------------------------------ CHART --
  /* The chart library cannot read CSS variables, so the theme is handed to it
     explicitly — one source of truth (styles.css), two consumers. */
  function tok(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  function chartTheme() {
    return {
      layout: { background: { color: tok("--chart-bg") }, textColor: tok("--chart-text"),
                fontFamily: "ui-monospace, monospace" },
      grid: { vertLines: { color: tok("--chart-grid") }, horzLines: { color: tok("--chart-grid") } },
      rightPriceScale: { borderColor: tok("--chart-border") },
      timeScale: { borderColor: tok("--chart-border") }
    };
  }
  function seriesTheme() {
    var up = tok("--up"), down = tok("--down");
    return { upColor: up, downColor: down, borderVisible: false,
             wickUpColor: up, wickDownColor: down };
  }

  function ensureChart() {
    if (state.chart || !window.LightweightCharts) return;
    var host = $("chart");
    var t = chartTheme();
    t.timeScale.timeVisible = true; t.timeScale.secondsVisible = false;
    t.crosshair = { mode: 0 };
    t.autoSize = true;
    state.chart = LightweightCharts.createChart(host, t);
    state.series = state.chart.addSeries
      ? state.chart.addSeries(LightweightCharts.CandlestickSeries, seriesTheme())
      : state.chart.addCandlestickSeries(seriesTheme());
    state.chart.subscribeCrosshairMove(function (p) {
      if (!p || !p.seriesData) return;
      var d = p.seriesData.get(state.series);
      if (!d) return;
      var lg = $("legend"); lg.textContent = "";
      lg.appendChild(el("b", null, state.chartSym + " " + state.chartTf + "  "));
      lg.appendChild(document.createTextNode(
        "O " + fmt(d.open) + "  H " + fmt(d.high) + "  L " + fmt(d.low) + "  C " + fmt(d.close)));
    });
  }

  function clearLines() {
    state.lines.forEach(function (l) { try { state.series.removePriceLine(l); } catch (e) {} });
    state.lines = [];
  }

  function loadChart() {
    if (!state.series) return;
    $("chart-status").textContent = "loading…";
    var end = new Date(), span = { "5m": 3, "15m": 7, "1h": 30, "4h": 120, "1d": 500 }[state.chartTf] || 60;
    var start = new Date(end.getTime() - span * 864e5);
    api("/api/chart?symbol=" + state.chartSym + "&timeframe=" + state.chartTf +
        "&from=" + start.toISOString() + "&to=" + end.toISOString())
      .then(function (d) {
        var bars = (d.candles || []).map(function (c) {
          return { time: Math.floor(new Date(c.ts).getTime() / 1000),
                   open: c.o, high: c.h, low: c.l, close: c.c };
        });
        state.series.setData(bars);
        $("chart-status").textContent = bars.length + " bars";
        drawLevels();
      })
      .catch(function (e) { $("chart-status").textContent = "chart unavailable"; });
  }

  function drawLevels() {
    clearLines();
    if (!state.showLevels) return;
    api("/api/v4/levels?symbol=" + state.chartSym + "&tf=" + state.chartTf)
      .then(function (d) {
        (d.levels || []).forEach(function (lv) {
          state.lines.push(state.series.createPriceLine({
            price: lv.price, color: tok("--accent"), lineWidth: 1,
            lineStyle: 2, axisLabelVisible: true, title: lv.label }));
        });
      }).catch(function () {});
    /* Several strategies commonly break the SAME level, producing identical
       entry/stop/target prices. Draw each price once and name every strategy
       behind it, otherwise the axis stacks three labels on one line. */
    var seen = {};
    state.setups.filter(function (s) { return s.symbol === state.chartSym; })
      .slice(0, 3).forEach(function (s) {
        [["Entry", s.entry, "--ink"], ["Stop", s.stop, "--down"],
         ["Target", s.target, "--up"]].forEach(function (p) {
          var key = p[0] + "@" + p[1];
          if (seen[key]) { seen[key].push(s.strategy_id); return; }
          seen[key] = [s.strategy_id];
          seen[key].line = state.series.createPriceLine({
            price: p[1], color: tok(p[2]), lineWidth: 2, lineStyle: 0,
            axisLabelVisible: true, title: p[0] });
          state.lines.push(seen[key].line);
        });
      });
    Object.keys(seen).forEach(function (key) {
      var entry = seen[key];
      if (key.indexOf("Entry@") === 0)
        entry.line.applyOptions({ title: "Entry · " + entry.join(", ") });
    });
  }

  $("chart-sym").addEventListener("click", function (e) {
    var b = e.target.closest("button"); if (!b) return;
    state.chartSym = b.dataset.sym; syncSeg("chart-sym", "sym", state.chartSym);
    loadChart(); loadPaperBook();
  });
  $("chart-tf").addEventListener("click", function (e) {
    var b = e.target.closest("button"); if (!b) return;
    state.chartTf = b.dataset.tf; syncSeg("chart-tf", "tf", state.chartTf); loadChart();
  });
  $("toggle-levels").addEventListener("click", function () {
    state.showLevels = !state.showLevels;
    this.classList.toggle("primary", state.showLevels);
    drawLevels();
  });
  $("fs").addEventListener("click", function () {
    document.body.classList.toggle("fullscreen");
    var on = document.body.classList.contains("fullscreen");
    this.textContent = on ? "⛶ Exit" : "⛶ Fullscreen";
    if (state.chart) setTimeout(function () { state.chart.timeScale().fitContent(); }, 60);
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && document.body.classList.contains("fullscreen")) $("fs").click();
    if (state.page === "chart" && !/input|select/i.test(e.target.tagName)) {
      if (e.key === "f") $("fs").click();
      if (e.key === "l") $("toggle-levels").click();
      if (e.key === "p") $("pp-toggle").click();
    }
  });


  // =========================================================================
  //  ON-CHART PAPER TRADING - live book, qty sizing, running P&L, R progress
  // =========================================================================
  state.paper = null; state.ticket = null; state.posLines = [];

  function equity() {
    var p = state.paper;
    if (!p) return 0;
    if (p.portfolio && p.portfolio.equity != null) return p.portfolio.equity;
    if (p.account && p.account.balance != null) return p.account.balance;
    return 0;
  }

  // qty from risk: risking `pct`% of equity across the entry->stop distance
  function sizeFor(entry, stop, pct) {
    var eq = equity(), dist = Math.abs(entry - stop);
    if (!eq || !dist) return 0;
    return (eq * (pct / 100)) / dist;
  }

  function clearPosLines() {
    state.posLines.forEach(function (l) { try { state.series.removePriceLine(l); } catch (e) {} });
    state.posLines = [];
  }

  function isLong(x) { return String(x.side).toUpperCase() === "BUY" || x.side === "long"; }

  function renderPaperBook() {
    var p = state.paper, body = $("pp-body");
    if (!body) return;
    body.textContent = "";
    $("pp-equity").textContent = p ? "$" + fmt(equity(), 2) : "-";
    var allPos = (p && p.positions) ? p.positions : [];
    var tot = allPos.reduce(function (a, x) { return a + (x.unrealized_pnl || 0); }, 0);
    var tEl = $("pp-upnl");
    if (tEl) {
      tEl.textContent = (tot >= 0 ? "+" : "-") + "$" + fmt(Math.abs(tot), 2);
      tEl.className = "v " + (tot > 0 ? "up" : tot < 0 ? "down" : "");
    }
    $("pp-dot").className = "dot " + (p ? "live" : "off");

    var pos = (p && p.positions ? p.positions : []).filter(function (x) { return x.symbol === state.chartSym; });
    var orders = (p && p.orders ? p.orders : []).filter(function (x) { return x.symbol === state.chartSym; });

    /* An empty book should not sit on top of the price axis just to say it is
       empty. Shrink to the equity line; it re-expands the moment there is
       something to show. */
    var panel = $("pos-panel");
    if (panel) panel.classList.toggle("idle", !pos.length && !orders.length);
    if (!pos.length && !orders.length) {
      var e = el("div", "pos");
      e.appendChild(el("div", "sub", "No open position or resting order on " + state.chartSym.replace("USDT", "")));
      body.appendChild(e);
    }

    pos.forEach(function (x) {
      var lng = isLong(x), d = lng ? 1 : -1;
      var mark = x.mark != null ? x.mark : x.avg_entry;
      var upnl = x.unrealized_pnl != null ? x.unrealized_pnl : 0;
      var stop = x.sl, tgt = x.tp;
      var risk = stop ? Math.abs(x.avg_entry - stop) : null;
      var rNow = risk ? ((mark - x.avg_entry) * d) / risk : null;

      var box = el("div", "pos " + (lng ? "long" : "short"));
      var top = el("div", "pos-top");
      top.appendChild(el("span", "dir " + (lng ? "long" : "short"), lng ? "LONG" : "SHORT"));
      top.appendChild(el("span", "pos-sym", x.symbol.replace("USDT", "")));
      var gap = el("div"); gap.style.flex = "1"; top.appendChild(gap);
      top.appendChild(el("span", "pos-pnl " + (upnl >= 0 ? "up" : "down"),
                        (upnl >= 0 ? "+" : "-") + "$" + fmt(Math.abs(upnl), 2)));
      box.appendChild(top);

      var g = el("div", "pos-grid");
      [["Qty", fmt(x.qty, 4)], ["Entry", fmt(x.avg_entry, 2)], ["Mark", fmt(mark, 2)],
       ["Stop", stop ? fmt(stop, 2) : "-"], ["Target", tgt ? fmt(tgt, 2) : "-"],
       ["R now", rNow === null ? "-" : sign(rNow, 2)],
       ["Notional", "$" + fmt(x.qty * mark, 2)],
       ["Leverage", x.leverage ? fmt(x.leverage, 0) + "x" : "-"],
       ["Liq.", x.liq_price ? fmt(x.liq_price, 2) : "-"]].forEach(function (kv) {
        var c = el("div");
        c.appendChild(el("span", "k", kv[0]));
        c.appendChild(el("span", "v", kv[1]));
        g.appendChild(c);
      });
      box.appendChild(g);

      if (rNow !== null && tgt) {
        var maxR = Math.abs(tgt - x.avg_entry) / risk;
        var bar = el("div", "rbar");
        bar.appendChild(el("i"));
        var fill = el("b");
        var w = Math.min(Math.abs(rNow) / (rNow >= 0 ? maxR : 1), 1) * 50;
        if (rNow >= 0) { fill.style.left = "50%"; fill.style.width = w + "%"; fill.style.background = "var(--up)"; }
        else { fill.style.right = "50%"; fill.style.width = w + "%"; fill.style.background = "var(--down)"; }
        bar.appendChild(fill);
        box.appendChild(bar);
      }

      var f = el("div", "pp-f");
      f.style.borderTop = "0"; f.style.padding = "9px 0 0";
      var closeBtn = el("button", "btn", "Close position");
      closeBtn.addEventListener("click", function () {
        closeBtn.disabled = true;
        post("/api/paper/close", { symbol: x.symbol }).then(loadPaperBook)
          .catch(function () { closeBtn.disabled = false; closeBtn.textContent = "failed"; });
      });
      f.appendChild(closeBtn);
      box.appendChild(f);
      body.appendChild(box);
    });

    orders.forEach(function (o) {
      var box = el("div", "pos");
      var top = el("div", "pos-top");
      top.appendChild(el("span", "badge open", "RESTING " + String(o.type).toUpperCase()));
      top.appendChild(el("span", "pos-sym", o.symbol.replace("USDT", "")));
      box.appendChild(top);
      var g = el("div", "pos-grid");
      [["Side", o.side], ["Qty", fmt(o.qty, 4)], ["Trigger", fmt(o.stop_price || o.price, 2)],
       ["Stop", o.sl ? fmt(o.sl, 2) : "-"], ["Target", o.tp ? fmt(o.tp, 2) : "-"], ["", ""]]
        .forEach(function (kv) {
          var c = el("div");
          c.appendChild(el("span", "k", kv[0]));
          c.appendChild(el("span", "v", kv[1]));
          g.appendChild(c);
        });
      box.appendChild(g);
      var f = el("div", "pp-f"); f.style.borderTop = "0"; f.style.padding = "9px 0 0";
      var can = el("button", "btn", "Cancel order");
      can.addEventListener("click", function () {
        can.disabled = true;
        post("/api/paper/order/cancel", { id: o.id }).then(loadPaperBook)
          .catch(function () { can.disabled = false; });
      });
      f.appendChild(can); box.appendChild(f);
      body.appendChild(box);
    });

    drawPositionLines(pos, orders);
  }

  function drawPositionLines(pos, orders) {
    if (!state.series) return;
    clearPosLines();
    pos.forEach(function (x) {
      var lng = isLong(x);
      state.posLines.push(state.series.createPriceLine({
        price: x.avg_entry, color: lng ? tok("--up") : tok("--down"), lineWidth: 2,
        lineStyle: 0, axisLabelVisible: true, title: "POS " + fmt(x.qty, 4) }));
      if (x.sl) state.posLines.push(state.series.createPriceLine({
        price: x.sl, color: tok("--down"), lineWidth: 1, lineStyle: 2,
        axisLabelVisible: true, title: "SL" }));
      if (x.tp) state.posLines.push(state.series.createPriceLine({
        price: x.tp, color: tok("--up"), lineWidth: 1, lineStyle: 2,
        axisLabelVisible: true, title: "TP" }));
    });
    orders.forEach(function (o) {
      state.posLines.push(state.series.createPriceLine({
        price: o.stop_price || o.price, color: tok("--accent"), lineWidth: 1,
        lineStyle: 3, axisLabelVisible: true, title: "ORDER " + o.side }));
    });
  }

  function loadPaperBook() {
    return api("/api/paper").then(function (d) {
      state.paper = d; renderPaperBook(); renderTicket();
    }).catch(function () { state.paper = null; renderPaperBook(); });
  }

  function renderTicket() {
    var t = state.ticket, box = $("ticket");
    if (!box) return;
    if (!t || t.symbol !== state.chartSym) { box.hidden = true; return; }
    box.hidden = false;
    $("tk-dir").textContent = t.direction > 0 ? "LONG" : "SHORT";
    $("tk-dir").className = "dir " + (t.direction > 0 ? "long" : "short");
    $("tk-sym").textContent = t.symbol.replace("USDT", "");
    $("tk-strat").textContent = t.strategy_id;
    $("tk-entry").textContent = fmt(t.entry, 2);
    $("tk-stop").textContent = fmt(t.stop, 2);
    $("tk-target").textContent = fmt(t.target, 2);
    $("tk-risk").textContent = fmt(t.risk_pct, 2) + "%";
    var pct = parseFloat($("tk-riskpct").value) || 0.5;
    var q = sizeFor(t.entry, t.stop, pct);
    $("tk-qty").textContent = q ? fmt(q, 4) : "-";
    $("tk-notional").textContent = q ? "$" + fmt(q * t.entry, 2) : "-";
    $("tk-place").disabled = !q;
  }

  var tkClose = $("tk-close");
  if (tkClose) tkClose.addEventListener("click", function () {
    state.ticket = null; renderTicket();
  });

  var riskInp = $("tk-riskpct");
  if (riskInp) riskInp.addEventListener("input", renderTicket);

  var placeBtn = $("tk-place");
  if (placeBtn) placeBtn.addEventListener("click", function () {
    var t = state.ticket; if (!t) return;
    var pct = parseFloat($("tk-riskpct").value) || 0.5;
    var q = sizeFor(t.entry, t.stop, pct);
    if (!q) return;
    placeBtn.disabled = true; placeBtn.textContent = "placing...";
    post("/api/paper/order", {
      symbol: t.symbol, side: t.direction > 0 ? "BUY" : "SELL", type: "stop",
      qty: Number(q.toFixed(6)), stop_price: t.entry, sl: t.stop, tp: t.target
    }).then(function () {
      placeBtn.textContent = "placed";
      setTimeout(function () { placeBtn.textContent = "Place paper order"; placeBtn.disabled = false; }, 1600);
      loadPaperBook();
    }).catch(function () {
      placeBtn.textContent = "failed";
      setTimeout(function () { placeBtn.textContent = "Place paper order"; placeBtn.disabled = false; }, 2200);
    });
  });

  var ppToggle = $("pp-toggle");
  if (ppToggle) ppToggle.addEventListener("click", function () {
    var pnl = $("pos-panel");
    pnl.classList.toggle("collapsed");
    this.textContent = pnl.classList.contains("collapsed") ? "Show" : "Hide";
  });

  setInterval(function () { if (state.page === "chart") loadPaperBook(); }, 8000);

  // ---------------------------------------------------------------- JOURNAL --
  /* The owner's OWN record (migration 003) — deliberately independent of what
     the system recommended, so the two can be compared honestly. */
  var JR_NUM = ["entry", "exit_px", "sl", "tp", "risk_pct"];
  var JR_TEXT = ["title", "symbol", "direction", "strategy", "emotion",
                 "mistakes", "lessons", "notes"];
  state.editingId = null;

  function jrForm() { return $("jr-form"); }

  function jrShow(entry) {
    var f = jrForm();
    f.reset();
    state.editingId = entry ? entry.id : null;
    if (entry) {
      JR_TEXT.concat(JR_NUM).forEach(function (k) {
        if (f.elements[k] && entry[k] !== null && entry[k] !== undefined)
          f.elements[k].value = entry[k];
      });
      if (entry.confidence != null) f.elements.confidence.value = entry.confidence;
      f.elements.tags.value = (entry.tags || []).join(", ");
    }
    $("jr-save").textContent = entry ? "Update entry" : "Save entry";
    $("jr-status").textContent = "";
    f.hidden = false;
    f.elements.title.focus();
  }

  function jrPayload() {
    var f = jrForm(), out = {};
    JR_TEXT.forEach(function (k) {
      var v = f.elements[k].value.trim();
      out[k] = v || null;
    });
    JR_NUM.forEach(function (k) {
      var v = f.elements[k].value.trim();
      out[k] = v === "" ? null : Number(v);
    });
    var c = f.elements.confidence.value.trim();
    out.confidence = c === "" ? null : parseInt(c, 10);
    var tags = f.elements.tags.value.split(",").map(function (t) { return t.trim(); })
      .filter(Boolean);
    out.tags = tags.length ? tags : null;
    return out;
  }

  function loadJournal() {
    var q = [];
    if ($("jr-search").value.trim()) q.push("search=" + encodeURIComponent($("jr-search").value.trim()));
    if ($("jr-symbol").value) q.push("symbol=" + $("jr-symbol").value);
    if ($("jr-direction").value) q.push("direction=" + $("jr-direction").value);
    api("/api/journal" + (q.length ? "?" + q.join("&") : ""))
      .then(renderJournal).catch(function () {});
  }

  function renderJournal(rows) {
    var box = $("jr-list"); box.textContent = "";
    if (!rows || !rows.length) {
      var e = el("div", "empty");
      e.appendChild(el("div", "big", "No entries yet"));
      e.appendChild(el("div", null,
        "Log a trade after you take it — what you did, and what you learned."));
      box.appendChild(e); return;
    }
    rows.forEach(function (r) { box.appendChild(journalCard(r)); });
  }

  function journalCard(r) {
    var c = el("div", "card jr-card");
    var h = el("div", "card-h");
    if (r.direction) h.appendChild(el("span", "dir " + (r.direction === "LONG" ? "long" : "short"), r.direction));
    if (r.symbol) h.appendChild(el("span", "sym", r.symbol.replace("USDT", "")));
    h.appendChild(el("span", "card-t", r.title || "Untitled"));
    var sp = el("div"); sp.style.flex = "1"; h.appendChild(sp);
    if (r.strategy) h.appendChild(el("span", "strat-tag", r.strategy));
    c.appendChild(h);

    var prices = [["Entry", r.entry], ["Exit", r.exit_px], ["Stop", r.sl], ["Target", r.tp]]
      .filter(function (p) { return p[1] !== null && p[1] !== undefined; });
    if (prices.length) {
      var pr = el("div", "rail-prices");
      prices.forEach(function (p) {
        var cell = el("div", "pcell");
        cell.appendChild(el("div", "k", p[0]));
        cell.appendChild(el("div", "v", fmt(p[1], 2)));
        pr.appendChild(cell);
      });
      c.appendChild(pr);
    }
    [["What I did wrong", r.mistakes], ["What I learned", r.lessons], ["Notes", r.notes]]
      .forEach(function (row) {
        if (!row[1]) return;
        var d = el("div", "jr-note");
        d.appendChild(el("span", "k", row[0]));
        d.appendChild(el("span", null, row[1]));
        c.appendChild(d);
      });
    if (r.tags && r.tags.length) {
      var tg = el("div", "jr-tags");
      r.tags.forEach(function (t) { tg.appendChild(el("span", "tag", t)); });
      c.appendChild(tg);
    }
    var f = el("div", "setup-f");
    var meta = [when(r.created_at ? Date.parse(r.created_at) / 1000 : 0)];
    if (r.emotion) meta.push("felt " + r.emotion);
    if (r.confidence != null) meta.push("confidence " + r.confidence + "/10");
    f.appendChild(el("span", "sub", meta.join(" · ")));
    var gap = el("div"); gap.style.flex = "1"; f.appendChild(gap);
    var ed = el("button", "btn", "Edit");
    ed.addEventListener("click", function () { jrShow(r); });
    var del = el("button", "btn", "Delete");
    del.addEventListener("click", function () {
      if (!window.confirm("Delete this entry? This cannot be undone.")) return;
      api("/api/journal/" + r.id, { method: "DELETE" })
        .then(loadJournal).catch(function () {});
    });
    f.appendChild(ed); f.appendChild(del);
    c.appendChild(f);
    return c;
  }

  $("jr-new").addEventListener("click", function () { jrShow(null); });
  $("jr-cancel").addEventListener("click", function () { jrForm().hidden = true; });
  ["jr-search", "jr-symbol", "jr-direction"].forEach(function (id) {
    $(id).addEventListener("change", loadJournal);
  });
  $("jr-search").addEventListener("input", function () {
    clearTimeout(state.jrTimer);
    state.jrTimer = setTimeout(loadJournal, 300);
  });
  jrForm().addEventListener("submit", function (ev) {
    ev.preventDefault();
    var id = state.editingId;
    $("jr-status").textContent = "saving…";
    post("/api/journal" + (id ? "/" + id : ""), jrPayload(), id ? "PATCH" : "POST")
      .then(function () {
        $("jr-status").textContent = "saved ✓";
        jrForm().hidden = true;
        loadJournal();
      })
      .catch(function () { $("jr-status").textContent = "not saved"; });
  });

  // ---------------------------------------------------------------- SETTINGS --
  /* Telegram is the channel that works when the app is CLOSED, which for a tool
     built on resting orders is the only channel that matters. Everything here
     is a thin editor over GET/PUT /settings. */
  function toggleRow(label, hint, checked, onChange) {
    var r = el("label", "pref");
    var box = document.createElement("input");
    box.type = "checkbox"; box.checked = !!checked;
    box.addEventListener("change", function () { onChange(box.checked, box); });
    var lab = el("div", "pref-t");
    lab.appendChild(el("div", null, label));
    if (hint) lab.appendChild(el("div", "sub", hint));
    var sw = el("span", "toggle");
    sw.appendChild(box); sw.appendChild(el("span", "track"));
    r.appendChild(lab); r.appendChild(sw);
    return r;
  }

  function saveAlerts(patch) {
    return post("/settings/alerts", patch, "PUT").then(function (d) {
      state.settings.alerts = d.alerts; return d.alerts;
    });
  }

  function renderSettings() {
    var d = state.settings; if (!d) return;
    var a = d.alerts || {}, n = d.notifications || {};

    var ab = $("alert-prefs"); ab.textContent = "";
    ab.appendChild(toggleRow("Price approaching an entry",
      "The one that matters — a resting order sits for hours, so “it is coming” " +
      "is actionable and “it filled” is just news.",
      a.on_approach, function (v) { saveAlerts({ on_approach: v }); }));

    var prox = el("label", "pref");
    var pt = el("div", "pref-t");
    pt.appendChild(el("div", null, "How close is “approaching”"));
    pt.appendChild(el("div", "sub", "percent of price away from the entry level"));
    var inp = document.createElement("input");
    inp.className = "inp"; inp.type = "number"; inp.step = "0.05";
    inp.min = "0.01"; inp.max = "10"; inp.style.width = "88px";
    inp.value = a.proximity_pct;
    inp.addEventListener("change", function () {
      saveAlerts({ proximity_pct: Number(inp.value) })
        .then(function (out) { inp.value = out.proximity_pct; })
        .catch(function () { inp.value = a.proximity_pct; });
    });
    prox.appendChild(pt); prox.appendChild(inp);
    ab.appendChild(prox);

    ab.appendChild(toggleRow("A new setup is found", "when a strategy first issues it",
      a.on_new_setup, function (v) { saveAlerts({ on_new_setup: v }); }));
    ab.appendChild(toggleRow("Entry triggered", "the resting order filled",
      a.on_trigger, function (v) { saveAlerts({ on_trigger: v }); }));
    ab.appendChild(toggleRow("Trade closed", "target, stop or time exit, with the net R",
      a.on_close, function (v) { saveAlerts({ on_close: v }); }));

    var cb = $("channel-prefs"); cb.textContent = "";
    [["telegram", "Telegram", "works with the app closed"],
     ["trade_alerts", "Trade alerts", "setups, entries and exits"],
     ["system_alerts", "System alerts", "feed disconnects and errors"],
     ["desktop", "Desktop notifications", "only while a tab is open"]
    ].forEach(function (row) {
      cb.appendChild(toggleRow(row[1], row[2], n[row[0]], function (v) {
        var patch = {}; patch[row[0]] = v;
        post("/settings/notifications", patch, "PUT").then(function (out) {
          state.settings.notifications = out.notifications;
        });
      }));
    });

    var bots = $("tg-bots"); bots.textContent = "";
    var list = d.telegram_bots || [];
    if (!list.length) {
      bots.appendChild(el("div", "sub", "No bot connected — alerts will not leave the browser."));
    }
    list.forEach(function (b) {
      var r = el("div", "bot");
      var left = el("div");
      left.appendChild(el("div", "bot-n", "@" + (b.bot_username || "bot")));
      left.appendChild(el("div", "sub", "chat " + b.chat_id +
        (b.verified ? " · verified" : " · not verified")));
      r.appendChild(left);
      var sp = el("div"); sp.style.flex = "1"; r.appendChild(sp);
      var t = el("button", "btn", "Send test");
      t.addEventListener("click", function () {
        t.disabled = true; t.textContent = "Sending…";
        post("/settings/telegram/test", {})
          .then(function () { t.textContent = "Sent ✓"; })
          .catch(function () { t.textContent = "Failed"; })
          .then(function () { setTimeout(function () {
            t.disabled = false; t.textContent = "Send test"; }, 2000); });
      });
      var x = el("button", "btn", "Remove");
      x.addEventListener("click", function () {
        if (!window.confirm("Remove this bot? Alerts stop going to that chat.")) return;
        api("/settings/telegram/" + b.id, { method: "DELETE" }).then(loadSettings);
      });
      r.appendChild(t); r.appendChild(x);
      bots.appendChild(r);
    });

    var about = $("about-box"); about.textContent = "";
    [["API host", HTTP.replace(/^https?:\/\//, "")],
     ["Symbols", "BTCUSDT · ETHUSDT"],
     ["Strategies", state.catalogue ? state.catalogue.strategies.length + " in the catalogue" : "—"],
     ["Execution", "none — this tool never places an order"]
    ].forEach(function (row) {
      var line = el("div", "pref");
      var t2 = el("div", "pref-t"); t2.appendChild(el("div", null, row[0]));
      line.appendChild(t2); line.appendChild(el("div", "sub", row[1]));
      about.appendChild(line);
    });
  }

  function loadSettings() {
    return api("/settings").then(function (d) {
      state.settings = d; renderSettings();
    }).catch(function () {});
  }

  $("tg-verify").addEventListener("click", function () {
    var btn = $("tg-verify"), err = $("tg-err"), tok = $("tg-token");
    if (!tok.value.trim()) return;
    btn.disabled = true; btn.textContent = "Verifying…"; err.hidden = true;
    post("/settings/telegram/verify", { token: tok.value.trim() })
      .then(function (d) {
        if (!d.ok) throw new Error(d.error || "Telegram rejected that token.");
        tok.value = "";
        return loadSettings();
      })
      .catch(function (e) { err.textContent = String(e.message || e); err.hidden = false; })
      .then(function () { btn.disabled = false; btn.textContent = "Verify & connect"; });
  });

  // ------------------------------------------------------------------- BOOT --
  function loadCatalogue() {
    return api("/api/v4/strategies").then(function (d) {
      state.catalogue = d;
      $("confidence").textContent = d.confidence_note || "";
      var sel = $("hist-strategy");
      if (sel.options.length <= 1) {
        d.strategies.forEach(function (s) {
          var o = document.createElement("option");
          o.value = s.id; o.textContent = s.label; sel.appendChild(o);
        });
      }
      renderStrategies();
    }).catch(function () {
      $("confidence").textContent = "Backend not reachable — start the API and refresh.";
    });
  }

  function loadSetups() {
    return api("/api/v4/setups").then(function (d) {
      state.setups = d.setups || [];
      renderToday();
      if (state.page === "chart") drawLevels();
    }).catch(function () { state.setups = []; renderToday(); });
  }

  function boot() {
    loadCatalogue();
    loadSetups();
    loadPerformance();
    loadQuotes();
    loadActive();
  }

  if (TOKEN) boot(); else showGate();
  setInterval(function () { if (TOKEN) loadQuotes(); }, 30000);
  setInterval(function () { if (TOKEN) { loadSetups(); loadActive(); } }, 60000);
})();

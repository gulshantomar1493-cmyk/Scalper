/* ============================================================================
   MarketScalper — application logic
   ----------------------------------------------------------------------------
   This file owns ALL network access. Render helpers below are pure DOM builders
   and write every server string with textContent (never innerHTML).

   Staleness rule: when a fetch fails we keep the last good values, flip the feed
   pill to "simulated feed" / SIMULATED and raise the red banner. A trading
   surface must never look live while it is stale.
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
    sym: "ETHUSDT", pick: null, tf: "4h", riskPct: 0.5,
    setups: [], catalogue: null, perf: null, history: [], paper: null,
    settings: null, quotes: {}, prev: {}, dayOpen: {}, live: false,
    histFilter: "", chart: null, series: null, lines: [], active: [],
    equity: Number(localStorage.getItem("ms_equity")) || 10000,
    heroCandles: []
  };

  /* Failing paths are tracked individually: a 4-second quotes poll succeeding
     must not erase the warning raised by a history endpoint that is still
     down. The banner clears only when nothing is failing. */
  var failing = Object.create(null);
  function banner(msg) {
    var b = $("banner"); if (!b) return;
    if (msg) { $("banner-msg").textContent = msg; b.classList.add("on"); }
    else b.classList.remove("on");
  }
  function noteFailure(path, err) {
    failing[path] = String(err && err.message || err);
    var paths = Object.keys(failing);
    banner("Backend se " + paths.length + " request fail ho rahi hai — screen pe " +
           "jo hai wo purana ho sakta hai. (" + failing[paths[0]] + ")");
  }
  function noteSuccess(path) {
    if (!(path in failing)) return;
    delete failing[path];
    if (!Object.keys(failing).length) banner(null);
  }
  function setLive(on) {
    state.live = on;
    var dot = $("feed-dot"), lab = $("feed-label");
    if (lab) lab.textContent = on ? "live feed" : "simulated feed";
    if (dot) dot.className = "dot" + (on ? " live" : "");
  }

  function key(path) { return String(path).split("?")[0]; }

  function api(path, opts) {
    var init = opts || {};
    init.headers = TOKEN ? { Authorization: "Bearer " + TOKEN } : {};
    return fetch(HTTP + path, init)
      .then(function (r) {
        if (r.status === 401) { signOut(); throw new Error("signed out"); }
        if (!r.ok) throw new Error(r.status + " " + path);
        noteSuccess(key(path));
        return r.json();
      })
      .catch(function (e) {
        setLive(false); renderTickers();
        noteFailure(key(path), e);
        throw e;
      });
  }

  /* The single write path. Surfaces the server's own error text, which is the
     actionable part ("invalid bot token"). */
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
      .catch(function (e) { banner("Save nahi hua — " + String(e.message || e)); throw e; });
  }

  var $ = function (id) { return document.getElementById(id); };
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }
  function tok(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  function fmt(n, d) {
    if (n === null || n === undefined || isNaN(n)) return "—";
    var dd = d === undefined ? 2 : d;
    return Number(n).toLocaleString("en-US", { minimumFractionDigits: dd, maximumFractionDigits: dd });
  }
  function sign(n, d) { return (n > 0 ? "+" : "") + fmt(n, d); }
  function dec(sym) { return sym === "BTCUSDT" ? 0 : 2; }
  function when(ts) {
    if (!ts) return "—";
    var dt = typeof ts === "number" ? new Date(ts * 1000) : new Date(ts);
    if (isNaN(dt.getTime())) return "—";
    return dt.toLocaleString(undefined, { month: "short", day: "2-digit",
                                          hour: "2-digit", minute: "2-digit" });
  }
  function labelled(text) { return el("span", "lbl", text); }

  /* ---- the numbers a trader actually acts on ---------------------------- */

  function livePrice(sym) {
    var q = state.quotes[sym];
    return q && q.price != null ? q.price : null;
  }

  /* How far price still has to travel to trigger the resting order. For a
     LONG the entry sits ABOVE price, for a SHORT below — so a positive
     distance always means "not there yet". */
  function distanceToEntry(s) {
    var px = livePrice(s.symbol);
    if (px == null || !s.entry) return null;
    var away = (s.direction > 0 ? s.entry - px : px - s.entry);
    return { abs: away, pct: away / px * 100, through: away <= 0 };
  }

  /* A resting order is only actionable inside its window. Past it the setup
     is history, and showing it as live is how a trader ends up placing an
     order the engine already abandoned. */
  function validity(s) {
    if (!s.valid_until_ts) return { expired: false, text: "" };
    var left = s.valid_until_ts * 1000 - Date.now();
    if (left <= 0) return { expired: true, text: "expire ho chuka" };
    var m = Math.floor(left / 60000), h = Math.floor(m / 60);
    return { expired: false,
             text: (h ? h + "h " + (m % 60) + "m" : m + "m") + " bacha hai" };
  }

  function geometry() { return (state.catalogue && state.catalogue.geometry) || {}; }

  /* Round-trip taker fee plus funding for the max hold. The whole research
     programme turned on fee/R, so it belongs on screen, not in a footnote. */
  function costOf(s, qty) {
    var g = geometry();
    var taker = g.taker_fee == null ? 0.0005 : g.taker_fee;
    var fund = g.funding_per_day == null ? 0.0003 : g.funding_per_day;
    var days = g.max_hold_days == null ? 3 : g.max_hold_days;
    var notional = qty * s.entry;
    return { fee: notional * taker * 2, funding: notional * fund * days };
  }

  function sizeFor(s) {
    var riskCash = state.equity * (state.riskPct / 100);
    var perUnit = Math.abs(s.entry - s.stop) || 1;
    var qty = riskCash / perUnit;
    return { riskCash: riskCash, perUnit: perUnit, qty: qty, notional: qty * s.entry };
  }

  /* Odometer: each digit is a 0-9 column moved by transform. The columns stay
     mounted between ticks or the slide is lost. */
  function odo(text, size, color) {
    var wrap = el("span", "odo");
    wrap.style.fontSize = size + "px";
    if (color) wrap.style.color = color;
    /* Every digit is a 0-9 column, so the columns would read as
       "0123456789012..." to a screen reader and to copy-paste. Carry the true
       value in an accessible span and hide the mechanism. */
    wrap.setAttribute("role", "text");
    wrap.setAttribute("aria-label", String(text));
    wrap.appendChild(el("span", "odo-true", String(text)));
    String(text).split("").forEach(function (ch) {
      if (!/[0-9]/.test(ch)) {
        var sep = el("span", "sep" + (ch === "," ? " comma" : ""), ch);
        sep.setAttribute("aria-hidden", "true");
        wrap.appendChild(sep);
        return;
      }
      var win = el("span", "win");
      win.setAttribute("aria-hidden", "true");
      win.style.height = (size * 1.1) + "px";
      win.style.width = (size * 0.6) + "px";
      var col = el("span", "col");
      col.style.transform = "translateY(" + (-Number(ch) * 10) + "%)";
      "0123456789".split("").forEach(function (d) {
        var s = el("span", null, d);
        s.style.height = (size * 1.1) + "px";
        col.appendChild(s);
      });
      win.appendChild(col);
      wrap.appendChild(win);
    });
    return wrap;
  }

  // ------------------------------------------------------------------ login --
  function showGate(message) {
    var g = $("gate"); if (!g) return;
    g.hidden = false;
    var err = $("gate-err");
    if (message) { err.textContent = message; err.hidden = false; } else err.hidden = true;
    $("gate-user").focus();
  }
  function signOut() {
    TOKEN = "";
    try { localStorage.removeItem("ms_token"); } catch (e) {}
    banner(null);
    showGate("Session ab valid nahi hai. Dobara sign in karo.");
  }
  $("gate-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var btn = $("gate-go");
    btn.disabled = true; btn.textContent = "Signing in…";
    fetch(HTTP + "/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: $("gate-user").value, password: $("gate-pass").value })
    }).then(function (r) {
      if (r.status === 401) throw new Error("Username ya password galat hai.");
      if (r.status === 503) throw new Error("Is server pe login configured nahi hai.");
      if (!r.ok) throw new Error("Sign-in fail (" + r.status + ").");
      return r.json();
    }).then(function (d) {
      TOKEN = d.token;
      try { localStorage.setItem("ms_token", TOKEN); } catch (e) {}
      $("gate-pass").value = "";
      $("gate").hidden = true;
      boot();
    }).catch(function (e) { showGate(String(e.message || e)); })
      .then(function () { btn.disabled = false; btn.textContent = "Sign in"; });
  });

  // ------------------------------------------------------------------ theme --
  function setTheme(t) {
    document.documentElement.dataset.theme = t;
    try { localStorage.setItem("ms_v4_theme", t); } catch (e) {}
    if (state.chart) {
      state.chart.applyOptions(chartTheme());
      state.series.applyOptions(seriesTheme());
      drawLevels();
    }
    renderHeroCandles();
    renderEquityCurve();
  }
  $("theme").addEventListener("click", function () {
    setTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
  });
  $("banner-x").addEventListener("click", function () { banner(null); });

  /* Scroll-linked reveals where the browser supports them; otherwise an
     observer adds .in once. Same resting state either way. */
  if (!(window.CSS && CSS.supports && CSS.supports("animation-timeline: view()"))) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
      });
    }, { rootMargin: "0px 0px -12% 0px" });
    document.querySelectorAll(".reveal, .rise").forEach(function (n) { io.observe(n); });
    window.__msObserve = function (n) { io.observe(n); };
  }

  // ------------------------------------------------------------------- hero --
  function renderTickers() {
    var box = $("tickers"); if (!box) return;
    clear(box);
    ["ETHUSDT", "BTCUSDT"].forEach(function (k) {
      var q = state.quotes[k];
      var px = q ? q.price : null;
      var prev = state.prev[k];
      var up = (prev === undefined) ? true : px >= prev;
      var card = el("div", "ticker");
      var left = el("div");
      left.appendChild(labelled(k.replace("USDT", "") + " / USDT"));
      /* DESIGN LAW: green means money. A missing price is not an up-tick, so
         the placeholder stays neutral. */
      var o = odo(px === null ? "—" : fmt(px, 2), 30,
                  px === null ? "var(--ink-3)" : (up ? "var(--up)" : "var(--down)"));
      o.style.marginTop = "8px";
      left.appendChild(o);
      card.appendChild(left);
      card.appendChild(el("div", "sp"));
      var right = el("div");
      /* Change vs TODAY's open — the number a trader expects next to a price.
         Diffing against the previous poll would just print +0.00% forever. */
      var open = state.dayOpen[k];
      var chg = (px !== null && open) ? (px / open - 1) * 100 : null;
      var c = el("div", "chg", chg === null ? "—" : (chg >= 0 ? "+" : "") + chg.toFixed(2) + "%");
      c.style.color = chg === null ? "var(--ink-3)" : (chg >= 0 ? "var(--up)" : "var(--down)");
      right.appendChild(c);
      right.appendChild(el("div", "src", state.live ? "BINANCE WS" : "SIMULATED"));
      card.appendChild(right);
      box.appendChild(card);
    });
  }

  function renderHeroCandles() {
    var host = $("hero-candles"); if (!host) return;
    clear(host);
    var c = state.heroCandles;
    if (!c.length) return;
    var hi = Math.max.apply(null, c.map(function (x) { return x.h; }));
    var lo = Math.min.apply(null, c.map(function (x) { return x.l; }));
    var rng = (hi - lo) || 1;
    var up = tok("--up"), down = tok("--down");
    c.forEach(function (k, i) {
      var isUp = k.c >= k.o;
      var top = (1 - (k.h - lo) / rng) * 100, bot = (1 - (k.l - lo) / rng) * 100;
      var bt = (1 - (Math.max(k.o, k.c) - lo) / rng) * 100;
      var bb = (1 - (Math.min(k.o, k.c) - lo) / rng) * 100;
      var n = el("div", "hcandle");
      n.style.left = (i / c.length * 100) + "%";
      n.style.width = (100 / c.length) + "%";
      n.style.top = top + "%";
      n.style.bottom = (100 - bot) + "%";
      var wick = el("div", "wick"); wick.style.background = isUp ? up : down;
      var body = el("div", "body");
      body.style.background = isUp ? up : down;
      body.style.top = ((bt - top) / (bot - top || 1) * 100) + "%";
      body.style.height = (Math.abs(bb - bt) / (bot - top || 1) * 100) + "%";
      n.appendChild(wick); n.appendChild(body);
      host.appendChild(n);
    });
  }

  var lastTtl = "";
  function tickClock() {
    var c = $("clock");
    if (c) c.textContent = new Date().toLocaleTimeString();
    /* re-render the card only when the displayed countdown actually changes,
       so a half-typed equity field is never wiped by the 1-second tick */
    var s = current();
    if (!s) return;
    var v = validity(s);
    if (v.text !== lastTtl) { lastTtl = v.text; renderSetup(); }
  }

  // ----------------------------------------------------------------- setups --
  function symSetups(sym) {
    return state.setups.filter(function (s) { return s.symbol === sym; });
  }
  function setupKey(s) { return s.strategy_id + "@" + s.entry; }
  function current() {
    var rows = symSetups(state.sym);
    if (!rows.length) return null;
    var pick = rows.filter(function (s) { return setupKey(s) === state.pick; })[0];
    if (pick) return pick;                       // an explicit choice always wins
    /* otherwise lead with a setup that is still armed — an expired one is
       history, and heading the page with it invites acting on it */
    var live = rows.filter(function (s) { return !validity(s).expired; });
    return live[0] || rows[0];
  }
  function stratCode(id) {
    var list = (state.catalogue && state.catalogue.strategies) || [];
    for (var i = 0; i < list.length; i++) if (list[i].id === id) return "S" + (i + 1);
    return "—";
  }
  function strategyMeta(id) {
    var list = (state.catalogue && state.catalogue.strategies) || [];
    for (var i = 0; i < list.length; i++) if (list[i].id === id) return list[i];
    return {};
  }

  function renderSymbolTabs() {
    var box = $("sym-tabs"); clear(box);
    ["ETHUSDT", "BTCUSDT"].forEach(function (k) {
      var b = el("button", state.sym === k ? "on" : null, k);
      b.addEventListener("click", function () {
        state.sym = k; state.pick = null;
        renderSymbolTabs(); renderSetup(); renderQueue(); loadChart();
      });
      box.appendChild(b);
    });
  }

  function renderTfTabs() {
    var box = $("tf-tabs"); clear(box);
    ["5m", "15m", "1h", "4h", "1d"].forEach(function (t) {
      var b = el("button", state.tf === t ? "on" : null, t === "1d" ? "1D" : t);
      b.addEventListener("click", function () {
        state.tf = t; renderTfTabs(); loadChart();
      });
      box.appendChild(b);
    });
  }

  function ladderNode(s) {
    var risk = Math.abs(s.entry - s.stop), reward = Math.abs(s.target - s.entry);
    var total = risk + reward || 1, d = dec(s.symbol);
    var wrap = el("div", "ladder-wrap");
    var bar = el("div", "ladder");
    var r = el("i", "risk"); r.style.width = (risk / total * 100).toFixed(2) + "%";
    var w = el("i", "reward"); w.style.width = (reward / total * 100).toFixed(2) + "%";
    bar.appendChild(r); bar.appendChild(w);
    /* where price sits on the stop→target span, so the geometry is not just
       proportions but a position you can read at a glance */
    var px = livePrice(s.symbol);
    if (px != null) {
      var lo = Math.min(s.stop, s.target), hi = Math.max(s.stop, s.target);
      /* Before a setup arms, price sits OUTSIDE the stop→target band — which
         is the useful fact, not a reason to hide the marker. Clamp it to the
         edge it is beyond and mark it as outside. */
      var frac = (px - lo) / (hi - lo);
      var outside = frac < 0 || frac > 1;
      var mk = el("i", "now" + (outside ? " outside" : ""));
      mk.style.left = (Math.max(0, Math.min(1, frac)) * 100).toFixed(2) + "%";
      mk.title = outside
        ? "abhi price " + fmt(px, d) + " — is band ke bahar"
        : "abhi price " + fmt(px, d);
      bar.appendChild(mk);
    }
    wrap.appendChild(bar);
    var lg = el("div", "ladder-legend");
    lg.appendChild(el("span", "r", "risk " + fmt(risk, d)));
    lg.appendChild(el("span", "w", fmt(reward, d) + " reward · " + fmt(s.rr, 2) + "R net"));
    wrap.appendChild(lg);
    return wrap;
  }

  function renderSetup() {
    var card = $("setup-card"); clear(card);
    card.className = "setup-card";
    var s = current();
    if (!s) {
      card.appendChild(el("div", "empty",
        "Is symbol pe abhi koi setup nahi. Ye normal hai — ye strategies level break " +
        "hone ka wait karti hain, jo hafte mein kuch hi baar hota hai."));
      renderSizing(null); renderStratList();
      return;
    }
    var lng = s.direction > 0, d = dec(s.symbol);
    if (validity(s).expired) card.className = "setup-card stale";

    var rail = el("span", "dir-rail");
    rail.style.background = lng ? "var(--up)" : "var(--down)";
    card.appendChild(rail);
    var hair = el("span", "hair"); hair.appendChild(el("i"));
    card.appendChild(hair);

    var head = el("div", "setup-head");
    head.appendChild(el("span", "dir " + (lng ? "long" : "short"), lng ? "LONG" : "SHORT"));
    head.appendChild(el("span", "sym-big", s.symbol.replace("USDT", "")));
    head.appendChild(el("span", "chip", s.strategy_id));
    head.appendChild(el("div", "sp"));
    if (validity(s).expired) head.appendChild(el("span", "rank expired", "Expired"));
    else if (state.setups.indexOf(s) === 0) head.appendChild(el("span", "rank", "Rank 1"));
    var pips = el("div", "pips");
    pips.title = s.filters_passed + " of 3 trend filters agreeing";
    for (var i = 0; i < 3; i++) pips.appendChild(el("i", i < s.filters_passed ? "on" : null));
    head.appendChild(pips);
    card.appendChild(head);

    card.appendChild(ladderNode(s));

    /* THE operational number: how far price still has to travel, and how long
       the order stays armed. Without it the card cannot tell you whether this
       triggers in ten minutes or never. */
    var rowsForSym = symSetups(s.symbol);
    var anyLive = rowsForSym.some(function (x) { return !validity(x).expired; });
    if (!anyLive) {
      var warn = el("div", "stale-note");
      warn.appendChild(el("b", null, "Koi armed setup nahi."));
      warn.appendChild(document.createTextNode(
        " Neeche jo dikh raha hai uski validity window nikal chuki hai — ye " +
        "record ke liye hai, lene ke liye nahi. Agla setup tab banega jab " +
        "engine ko naya level break milega."));
      card.appendChild(warn);
    }
    var dist = distanceToEntry(s), val = validity(s);
    var strip = el("div", "dist-strip" + (val.expired ? " expired" : ""));
    var px = livePrice(s.symbol);
    strip.appendChild(el("span", "k", "Abhi"));
    strip.appendChild(el("span", "v", px == null ? "—" : fmt(px, d)));
    strip.appendChild(el("span", "arrow", s.direction > 0 ? "↑" : "↓"));
    strip.appendChild(el("span", "k", "entry se"));
    strip.appendChild(el("span", "v " + (dist && dist.through ? "through" : ""),
      dist == null ? "—"
        : dist.through ? "level cross ho chuka"
        : fmt(dist.abs, d) + " (" + dist.pct.toFixed(2) + "%)"));
    strip.appendChild(el("div", "sp"));
    strip.appendChild(el("span", "ttl" + (val.expired ? " bad" : ""), val.text));
    card.appendChild(strip);

    var cells = el("div", "price-cells");
    [["Entry", s.entry, "var(--ink)"], ["Stop", s.stop, "var(--down)"],
     ["Target", s.target, "var(--up)"], ["Net R:R", s.rr, "var(--ink)"]
    ].forEach(function (row) {
      var c = el("div", "pcell");
      c.appendChild(labelled(row[0]));
      c.appendChild(odo(row[0] === "Net R:R" ? fmt(row[1], 2) : fmt(row[1], d), 20, row[2]));
      cells.appendChild(c);
    });
    card.appendChild(cells);

    card.appendChild(el("p", "reason", s.reason));

    var foot = el("div", "setup-foot");
    foot.appendChild(el("span", "meta", "invalid: " + fmt(s.stop, d) +
      (lng ? " ke neeche close" : " ke upar close") +
      " · " + when(s.valid_until_ts) + " tak valid"));
    foot.appendChild(el("div", "sp"));
    foot.appendChild(el("div", "sp"));
    var copy = el("button", "btn", "Order copy karo");
    copy.addEventListener("click", function () {
      var z = sizeFor(s);
      var text = [
        s.symbol + " " + (lng ? "LONG" : "SHORT") + "  (" + s.strategy_id + ")",
        "entry  " + fmt(s.entry, d) + "   (resting STOP order)",
        "stop   " + fmt(s.stop, d),
        "target " + fmt(s.target, d),
        "qty    " + fmt(z.qty, s.symbol === "BTCUSDT" ? 4 : 3) +
          "   (risk $" + fmt(z.riskCash, 2) + " = " + state.riskPct.toFixed(1) + "%)",
        "net R:R " + fmt(s.rr, 2)
      ].join("\n");
      navigator.clipboard.writeText(text).then(function () {
        copy.textContent = "Copy ho gaya ✓";
        setTimeout(function () { copy.textContent = "Order copy karo"; }, 1800);
      }).catch(function () { copy.textContent = "Copy nahi hua"; });
    });
    foot.appendChild(copy);
    var go = el("a", "btn primary", "Chart pe dekho");
    go.href = "#chart";
    foot.appendChild(go);
    card.appendChild(foot);

    renderSizing(s);
    renderStratList();
  }

  function renderSizing(s) {
    var box = $("sizing"); clear(box);
    var eqInput = $("equity-input");
    if (eqInput && document.activeElement !== eqInput) eqInput.value = Math.round(state.equity);
    $("risk-label").textContent = state.riskPct.toFixed(1) + "%";
    var costBox = $("cost-row"); clear(costBox);
    if (!s) return;

    var z = sizeFor(s);
    [["Risk", "$" + fmt(z.riskCash, 2), "var(--down)"],
     ["Quantity", fmt(z.qty, s.symbol === "BTCUSDT" ? 4 : 3), "var(--ink)"],
     ["Notional", "$" + fmt(z.notional, 2), "var(--ink)"],
     ["Stop distance", fmt(z.perUnit, dec(s.symbol)), "var(--ink-2)"]
    ].forEach(function (row) {
      var c = el("div");
      c.appendChild(labelled(row[0]));
      var v = el("div", "v", row[1]); v.style.color = row[2];
      c.appendChild(v);
      box.appendChild(c);
    });

    /* Fee as a share of risk is THE number this whole strategy set turns on:
       the same geometry at 2xATR gives fee/R 0.27 and a negative edge, at
       5xATR it gives 0.09 and a positive one. Show it, do not bury it. */
    var c2 = costOf(s, z.qty);
    var total = c2.fee + c2.funding;
    var share = z.riskCash ? total / z.riskCash * 100 : null;
    var row = el("div", "cost");
    row.appendChild(el("span", "k", "Round-trip cost"));
    row.appendChild(el("span", "v", "$" + fmt(total, 2)));
    row.appendChild(el("span", "sep2", "·"));
    row.appendChild(el("span", "k", "risk ka"));
    var pct = el("span", "v " + (share > 20 ? "warn" : ""),
                 share == null ? "—" : share.toFixed(1) + "%");
    pct.title = "fee $" + fmt(c2.fee, 2) + " + funding $" + fmt(c2.funding, 2) +
                " (max " + (geometry().max_hold_days || 3) + " din hold)";
    row.appendChild(pct);
    costBox.appendChild(row);
  }

  /* Taking every live setup is one correlated bet, not N independent ones.
     A trader sizing each at 0.5% needs to see the total before they do it. */
  function renderExposure() {
    var box = $("exposure"); if (!box) return;
    clear(box);
    var rows = state.setups;
    if (!rows.length) return;
    var longs = rows.filter(function (x) { return x.direction > 0; }).length;
    var totalRisk = rows.length * state.riskPct;
    var byS = {};
    rows.forEach(function (x) { byS[x.symbol] = (byS[x.symbol] || 0) + 1; });
    var top = Object.keys(byS).sort(function (a, b) { return byS[b] - byS[a]; })[0];
    var concentrated = byS[top] / rows.length;

    var wrap = el("div", "expo");
    [["Live setups", String(rows.length), ""],
     ["Long / Short", longs + " / " + (rows.length - longs), ""],
     ["Sab liye to risk", totalRisk.toFixed(1) + "%", totalRisk > 3 ? "warn" : ""],
     [top.replace("USDT", "") + " mein", Math.round(concentrated * 100) + "%",
      concentrated > 0.7 ? "warn" : ""]
    ].forEach(function (c) {
      var cell = el("div");
      cell.appendChild(labelled(c[0]));
      cell.appendChild(el("div", "v " + c[2], c[1]));
      wrap.appendChild(cell);
    });
    box.appendChild(wrap);
    box.appendChild(el("div", "expo-note",
      "Ye setups ek dusre se independent nahi hain — ek hi level, ek hi symbol, " +
      "ek hi direction. Sab ek saath lene ka matlab ek bada correlated trade hai, " +
      (rows.length) + " chhote trades nahi."));
  }

  /* One row per strategy that has a live setup on this symbol — clicking swaps
     which setup the card, ladder and level lines show. */
  function renderStratList() {
    var box = $("strat-list"); clear(box);
    var rows = symSetups(state.sym);
    if (!rows.length) { box.appendChild(el("div", "empty", "koi live setup nahi")); return; }
    var cur = current();
    rows.forEach(function (s) {
      var meta = strategyMeta(s.strategy_id);
      var b = el("button", "strat-row" +
        (cur && setupKey(s) === setupKey(cur) ? " on" : ""));
      b.appendChild(el("span", "code", stratCode(s.strategy_id)));
      var mid = el("div");
      mid.appendChild(el("div", "nm", meta.label || s.strategy_id));
      /* the level distinguishes two setups from the SAME strategy */
      mid.appendChild(el("div", "note",
        s.level_source + " " + s.level_tf + " @ " + fmt(s.entry, dec(s.symbol))));
      b.appendChild(mid);
      b.appendChild(el("span", "rr", fmt(s.rr, 2) + "R"));
      b.addEventListener("click", function () {
        state.pick = setupKey(s);
        renderSetup(); renderQueue(); drawLevels();
      });
      box.appendChild(b);
    });
  }

  // ------------------------------------------------------------------ queue --
  function renderQueue() {
    var box = $("queue-rows"); clear(box);
    var cur = current();
    var rows = state.setups.filter(function (s) {
      return !cur || setupKey(s) !== setupKey(cur) || s.symbol !== cur.symbol;
    });
    if (!rows.length) {
      box.appendChild(el("div", "empty", "Baaki koi live setup nahi."));
      return;
    }
    /* Ranked by filters then R:R by the engine; within that, the trader wants
       to see what is closest to triggering. Sort by distance when we know it. */
    rows = rows.slice().sort(function (a, b) {
      var da = distanceToEntry(a), db = distanceToEntry(b);
      if (!da || !db) return 0;
      return Math.abs(da.pct) - Math.abs(db.pct);
    });
    rows.forEach(function (s) {
      var lng = s.direction > 0, d = dec(s.symbol);
      var meta = strategyMeta(s.strategy_id);
      var row = el("button", "qrow" + (validity(s).expired ? " stale" : ""));
      var bar = el("span", "bar"); bar.style.background = lng ? "var(--up)" : "var(--down)";
      row.appendChild(bar);
      row.appendChild(el("span", "dir " + (lng ? "long" : "short"), lng ? "LONG" : "SHORT"));
      row.appendChild(el("span", "sym", s.symbol.replace("USDT", "")));
      var mid = el("div");
      mid.appendChild(el("div", "nm", meta.label || s.strategy_id));
      mid.appendChild(el("div", "why", s.reason));
      row.appendChild(mid);
      var dist = distanceToEntry(s);
      [["Entry", s.entry, ""], ["Stop", s.stop, "down"],
       ["Target", s.target, "up"], ["R:R", s.rr, ""]].forEach(function (c) {
        var cell = el("div", "cell");
        cell.appendChild(labelled(c[0]));
        cell.appendChild(el("span", "v " + c[2],
          c[0] === "R:R" ? fmt(c[1], 2) : fmt(c[1], d)));
        row.appendChild(cell);
      });
      var away = el("div", "cell");
      away.appendChild(labelled("Door"));
      away.appendChild(el("span", "v " + (dist && dist.through ? "up" : ""),
        dist == null ? "—" : dist.through ? "cross" : dist.pct.toFixed(2) + "%"));
      row.appendChild(away);
      row.addEventListener("click", function () {
        state.sym = s.symbol; state.pick = setupKey(s);
        renderSymbolTabs(); renderSetup(); renderQueue(); loadChart();
        $("setup").scrollIntoView({ behavior: "smooth" });
      });
      box.appendChild(row);
    });
  }

  /* A filled setup is a LIVE position — it needs managing, not deciding, so it
     sits above the setups still waiting for their level to break. */
  function renderLiveTrades() {
    var box = $("live-trades"); if (!box) return;
    clear(box);
    var rows = state.active || [];
    if (!rows.length) return;
    var head = el("div", "sub-head");
    head.style.marginTop = "0";
    head.textContent = "Abhi live trades · " + rows.length + " — entry fill ho chuka hai";
    box.appendChild(head);
    var list = el("div", "list");
    list.style.marginBottom = "20px";
    rows.forEach(function (r) {
      var lng = r.direction > 0, d = dec(r.symbol);
      var fill = r.fill_price != null ? r.fill_price : r.entry;
      var px = state.quotes[r.symbol] ? state.quotes[r.symbol].price : null;
      var risk = Math.abs(fill - r.stop);
      var openR = (px != null && risk) ? (px - fill) / risk * (lng ? 1 : -1) : null;
      var row = el("div", "qrow");
      var bar = el("span", "bar"); bar.style.background = lng ? "var(--up)" : "var(--down)";
      row.appendChild(bar);
      row.appendChild(el("span", "dir " + (lng ? "long" : "short"), lng ? "LONG" : "SHORT"));
      row.appendChild(el("span", "sym", String(r.symbol).replace("USDT", "")));
      var mid = el("div");
      mid.appendChild(el("div", "nm", strategyMeta(r.strategy_id).label || r.strategy_id));
      mid.appendChild(el("div", "why", "fill " + fmt(fill, d) + " · " + when(r.filled_ts || r.decision_ts)));
      row.appendChild(mid);
      [["Stop", r.stop, "down"], ["Target", r.target, "up"],
       ["Now", px, ""]].forEach(function (c) {
        var cell = el("div", "cell");
        cell.appendChild(labelled(c[0]));
        cell.appendChild(el("span", "v " + c[2], c[1] == null ? "—" : fmt(c[1], d)));
        row.appendChild(cell);
      });
      var pnl = el("div", "cell");
      pnl.appendChild(labelled("Open R"));
      pnl.appendChild(el("span", "v " + (openR == null ? "" : openR >= 0 ? "up" : "down"),
        openR == null ? "—" : sign(openR, 2) + "R"));
      row.appendChild(pnl);
      list.appendChild(row);
    });
    box.appendChild(list);
  }

  function loadActive() {
    return api("/api/v4/history?status=FILLED&limit=50").then(function (d) {
      state.active = d.rows || [];
      renderLiveTrades();
    }).catch(function () { state.active = []; });
  }

  // --------------------------------------------------------------- pipeline --
  var STAGES = [
    ["Feed", "Binance WS — aggTrade · kline_1m · bookTicker"],
    ["Candle builder", "1m primary, 5m context — official klines se zero mismatch"],
    ["Levels", "donchian · swing · prior-day · round numbers"],
    ["Filters", "trend anchor, daily bias aur structure"],
    ["Trade plan", "resting stop order, 5×ATR(5m) stop, 10R target"],
    ["Recommendation", "manual execution, manual outcome log"]
  ];
  var GATES = [
    ["No repaint", "Closed candle kabhi revise nahi hoti. 14:01 pe jo aapne padha, log mein hamesha wahi rahega."],
    ["Replay-first", "Live jaane se pehle har strategy 9 saal ke recorded tape pe bilkul same code path chalati hai."],
    ["Fake confidence nahi", "Filters passed aur net R:R fact hain. “78% confidence” fact nahi hai, isliye wo badge yahan nahi hai."]
  ];
  function renderPipeline() {
    var box = $("stages"); clear(box);
    STAGES.forEach(function (st, i) {
      var c = el("div", "stage rise" + (i === STAGES.length - 1 ? " final" : ""));
      var hair = el("div", "hair");
      var sweep = el("i");
      sweep.style.animation = "sweepline " + (3 + i * 0.35) + "s linear infinite";
      hair.appendChild(sweep); c.appendChild(hair);
      c.appendChild(el("div", "idx", String(i + 1).padStart(2, "0")));
      c.appendChild(el("div", "t", st[0]));
      c.appendChild(el("div", "d", st[1]));
      box.appendChild(c);
      if (window.__msObserve) window.__msObserve(c);
    });
    var g = $("rules"); clear(g);
    GATES.forEach(function (row) {
      var c = el("div", "rule-card reveal");
      c.appendChild(el("div", "t", row[0]));
      c.appendChild(el("div", "d", row[1]));
      g.appendChild(c);
      if (window.__msObserve) window.__msObserve(c);
    });
  }

  // ------------------------------------------------------------- strategies --
  function well(title, sub, metrics, isLive) {
    var w = el("div", "well" + (isLive ? " live" : ""));
    var h = el("div", "well-h");
    h.appendChild(el("span", "t", title));
    h.appendChild(el("span", "s", "· " + sub));
    w.appendChild(h);
    var g = el("div", "well-grid");
    metrics.forEach(function (m) {
      var c = el("div", "metric");
      c.appendChild(labelled(m[0]));
      c.appendChild(el("div", "v", m[1]));
      g.appendChild(c);
    });
    w.appendChild(g);
    return w;
  }

  function renderStrategies() {
    var box = $("strat-cards"); clear(box);
    var d = state.catalogue; if (!d) return;
    var perf = (state.perf && state.perf.by_strategy) || {};

    d.strategies.forEach(function (st, idx) {
      var live = perf[st.id] || {};
      var card = el("div", "scard rise" + (st.enabled ? "" : " off"));
      var h = el("div", "scard-h");
      /* S1..Sn in catalogue order — a stable short handle. Slicing the level
         source gave unreadable stubs like "DONC" and "PDH_". */
      h.appendChild(el("span", "code", "S" + (idx + 1)));
      var mid = el("div"); mid.style.flex = "1"; mid.style.minWidth = "0";
      mid.appendChild(el("div", "nm", st.label));
      mid.appendChild(el("div", "note", st.note));
      h.appendChild(mid);
      var sw = el("button", "sw" + (st.enabled ? " on" : ""));
      sw.appendChild(el("i"));
      sw.title = st.enabled ? "on — naye setups issue kar raha hai" : "off — naye setups band";
      sw.disabled = !st.can_toggle;
      sw.setAttribute("aria-label", (st.enabled ? "Disable " : "Enable ") + st.label);
      sw.addEventListener("click", function () {
        sw.disabled = true;
        post("/api/v4/strategies/" + st.id + "/enabled", { enabled: !st.enabled })
          .then(function () { loadCatalogue(); loadSetups(); })
          .catch(function () { sw.disabled = false; });
      });
      h.appendChild(sw);
      card.appendChild(h);

      /* Two separated wells — backtest evidence and live paper never merge. */
      card.appendChild(well("Backtest", "9 saal replay", [
        ["trades", fmt(st.backtest.trades_per_year, 0) + "/yr"],
        ["net R", sign(st.backtest.net_r, 3)],
        ["PF", fmt(st.backtest.profit_factor, 2)]
      ], false));
      card.appendChild(well("Live paper", "sirf simulated", [
        ["trades", live.n === undefined ? "—" : String(live.n)],
        ["win %", live.win_rate == null ? "—" : fmt(live.win_rate * 100, 1)],
        ["net R", live.avg_net_r == null ? "—" : sign(live.avg_net_r, 3)]
      ], true));
      box.appendChild(card);
      if (window.__msObserve) window.__msObserve(card);
    });

    var rej = $("rejected"); clear(rej);
    var ideas = d.rejected_ideas || {};
    Object.keys(ideas).forEach(function (k) {
      var r = el("div", "rej");
      r.appendChild(el("span", "idea", k.replace(/_/g, " ")));
      r.appendChild(el("span", "why", ideas[k]));
      rej.appendChild(r);
    });
  }

  // ---------------------------------------------------------------- history --
  var OUTCOMES = [["", "Sab"], ["TP", "Target"], ["SL", "Stop"], ["TIME", "Time exit"]];
  function renderHistTabs() {
    var box = $("hist-tabs"); clear(box);
    OUTCOMES.forEach(function (o) {
      var b = el("button", state.histFilter === o[0] ? "on" : null, o[1]);
      b.addEventListener("click", function () {
        state.histFilter = o[0]; renderHistTabs(); renderHistory();
      });
      box.appendChild(b);
    });
  }

  function renderHistory() {
    var f = state.histFilter;
    var rows = state.history.filter(function (r) { return !f || r.status === f; });

    var stats = $("hist-stats"); clear(stats);
    var closed = rows.filter(function (r) { return r.net_r !== null && r.net_r !== undefined; });
    var netR = closed.reduce(function (a, r) { return a + r.net_r; }, 0);
    var wins = closed.filter(function (r) { return r.net_r > 0; }).length;
    var hold = closed.length
      ? closed.reduce(function (a, r) { return a + (r.hold_minutes || 0); }, 0) / closed.length : 0;
    [["Recommendations", String(rows.length), "log mein total", ""],
     ["Win %", closed.length ? fmt(wins / closed.length * 100, 1) : "—", "net R positive", ""],
     ["Net R", closed.length ? sign(netR, 2) : "—", "fees ke baad",
      closed.length ? (netR >= 0 ? "up" : "down") : ""],
     ["Avg hold", closed.length ? Math.round(hold) + "m" : "—", "entry se exit tak", ""]
    ].forEach(function (s) {
      var c = el("div", "stat reveal");
      c.appendChild(labelled(s[0]));
      c.appendChild(el("div", "v " + s[3], s[1]));
      c.appendChild(el("div", "sub", s[2]));
      stats.appendChild(c);
      if (window.__msObserve) window.__msObserve(c);
    });

    var box = $("hist-rows"); clear(box);
    var head = el("div", "trow head");
    ["Issued", "Symbol", "Dir", "Strat", "Entry", "Stop", "Target", "Outcome", "Net R"]
      .forEach(function (h, i) { head.appendChild(el("span", "lbl" + (i >= 4 ? " r" : ""), h)); });
    box.appendChild(head);

    if (!rows.length) {
      box.appendChild(el("div", "empty", "Is filter pe koi recommendation nahi."));
      return;
    }
    rows.forEach(function (r) {
      var d = dec(r.symbol), lng = r.direction > 0;
      var badge = { TP: ["tp", "Target"], SL: ["sl", "Stop"], TIME: ["time", "Time"],
                    OPEN: ["open", "Open"], FILLED: ["open", "Live"],
                    CANCELLED: ["time", "Cancelled"] }[r.status] || ["time", r.status];
      var row = el("div", "trow");
      row.appendChild(el("span", "ts", when(r.decision_ts)));
      row.appendChild(el("span", "sy", String(r.symbol).replace("USDT", "")));
      row.appendChild(el("span", "dir " + (lng ? "long" : "short"), lng ? "LONG" : "SHORT"));
      row.appendChild(el("span", "st", stratCode(r.strategy_id)));
      row.appendChild(el("span", "n", fmt(r.entry, d)));
      row.appendChild(el("span", "n down", fmt(r.stop, d)));
      row.appendChild(el("span", "n up", fmt(r.target, d)));
      row.appendChild(el("span", "badge " + badge[0], badge[1]));
      row.appendChild(el("span", "netr " + (r.net_r == null ? "" : (r.net_r >= 0 ? "up" : "down")),
        r.net_r == null ? "—" : sign(r.net_r, 2) + "R"));
      box.appendChild(row);
    });
  }

  // ------------------------------------------------------------------ paper --
  function renderPaper() {
    var o = (state.perf && state.perf.overall) || {};
    var acct = state.paper || {};
    var eq = (acct.portfolio && acct.portfolio.equity) != null
      ? acct.portfolio.equity : (acct.account && acct.account.balance);
    var box = $("paper-stats"); clear(box);
    [["Equity", eq == null ? "—" : "$" + fmt(eq, 2), "start $10,000", ""],
     ["Total R", o.total_r == null ? "—" : sign(o.total_r, 2), "closed trades",
      o.total_r == null ? "" : (o.total_r >= 0 ? "up" : "down")],
     ["Win %", o.win_rate == null ? "—" : fmt(o.win_rate * 100, 1), (o.n || 0) + " trades", ""],
     ["Profit factor", o.profit_factor == null ? "—" : fmt(o.profit_factor, 2), "gross win / gross loss", ""],
     ["Max drawdown", o.max_drawdown_r == null ? "—" : "-" + fmt(o.max_drawdown_r, 2) + "R",
      "peak se trough", o.max_drawdown_r ? "down" : ""]
    ].forEach(function (s) {
      var c = el("div", "stat rise");
      c.appendChild(labelled(s[0]));
      c.appendChild(el("div", "v " + s[3], s[1]));
      c.appendChild(el("div", "sub", s[2]));
      box.appendChild(c);
      if (window.__msObserve) window.__msObserve(c);
    });

    renderEquityCurve();

    var att = $("attribution"); clear(att);
    var head = el("div", "arow head");
    ["Strategy", "Trades", "Win %", "Net R", "PF", "Max DD", "TP / SL / Time"]
      .forEach(function (h, i) { head.appendChild(el("span", "lbl" + (i ? " r" : ""), h)); });
    att.appendChild(head);
    var by = (state.perf && state.perf.by_strategy) || {};
    var keys = Object.keys(by);
    if (!keys.length) {
      att.appendChild(el("div", "empty", "Abhi koi closed paper trade nahi."));
      return;
    }
    keys.forEach(function (k) {
      var v = by[k];
      var row = el("div", "arow");
      row.appendChild(el("span", "nm", strategyMeta(k).label || k));
      row.appendChild(el("span", "n", String(v.n || 0)));
      row.appendChild(el("span", "n", v.win_rate == null ? "—" : fmt(v.win_rate * 100, 1)));
      row.appendChild(el("span", "n " + (v.total_r == null ? "" : (v.total_r >= 0 ? "up" : "down")),
        v.total_r == null ? "—" : sign(v.total_r, 2)));
      row.appendChild(el("span", "n", v.profit_factor == null ? "—" : fmt(v.profit_factor, 2)));
      /* a zero drawdown is not a loss — only a real one wears red */
      row.appendChild(el("span", "n " + (v.max_drawdown_r ? "down" : ""),
        v.max_drawdown_r == null ? "—" : fmt(v.max_drawdown_r, 2)));
      row.appendChild(el("span", "mix", (v.tp || 0) + " / " + (v.sl || 0) + " / " + (v.time_exit || 0)));
      att.appendChild(row);
    });
  }

  function renderEquityCurve() {
    var svg = $("equity-curve"); if (!svg) return;
    clear(svg);
    var NS = "http://www.w3.org/2000/svg";
    var closed = state.history.filter(function (r) { return r.net_r != null; })
      .slice().sort(function (a, b) { return a.decision_ts - b.decision_ts; });
    if (closed.length < 2) {
      var t = document.createElementNS(NS, "text");
      t.setAttribute("x", 400); t.setAttribute("y", 78);
      t.setAttribute("text-anchor", "middle"); t.setAttribute("fill", tok("--ink-3"));
      t.setAttribute("font-size", "13");
      t.textContent = "Abhi itne closed trades nahi hain";
      svg.appendChild(t);
      return;
    }
    var cum = 0, steps = [0];
    closed.forEach(function (r) { cum += r.net_r; steps.push(cum); });
    var W = 800, H = 150;
    var max = Math.max.apply(null, steps), min = Math.min.apply(null, steps);
    var y = function (v) { return H - 12 - ((v - min) / ((max - min) || 1)) * (H - 30); };
    var pts = steps.map(function (v, i) { return [(i / (steps.length - 1)) * W, y(v)]; });
    var line = pts.map(function (p) { return p[0].toFixed(1) + "," + p[1].toFixed(1); }).join(" ");

    var area = document.createElementNS(NS, "polygon");
    area.setAttribute("points", "0," + H + " " + line + " " + W + "," + H);
    area.setAttribute("fill", tok("--up-bg"));
    svg.appendChild(area);
    var poly = document.createElementNS(NS, "polyline");
    poly.setAttribute("points", line);
    poly.setAttribute("fill", "none");
    poly.setAttribute("stroke", tok("--up"));
    poly.setAttribute("stroke-width", "2");
    poly.setAttribute("stroke-linejoin", "round");
    poly.setAttribute("stroke-linecap", "round");
    svg.appendChild(poly);
    pts.forEach(function (p) {
      var c = document.createElementNS(NS, "circle");
      c.setAttribute("cx", p[0]); c.setAttribute("cy", p[1]); c.setAttribute("r", 2.6);
      c.setAttribute("fill", tok("--surf")); c.setAttribute("stroke", tok("--up"));
      c.setAttribute("stroke-width", "1.6");
      svg.appendChild(c);
    });
  }

  // ---------------------------------------------------------------- journal --
  function renderJournal(rows) {
    var box = $("journal-cards"); clear(box);
    if (!rows || !rows.length) {
      box.appendChild(el("div", "empty",
        "Abhi koi entry nahi. Trade lene ke baad likho — kya kiya aur kya seekha."));
      return;
    }
    rows.forEach(function (r) {
      var card = el("div", "jcard rise");
      var h = el("div", "jcard-h");
      if (r.direction) h.appendChild(el("span", "dir " + (r.direction === "LONG" ? "long" : "short"), r.direction));
      if (r.symbol) h.appendChild(el("span", "sy", String(r.symbol).replace("USDT", "")));
      if (r.entry != null && r.exit_px != null && r.sl != null) {
        var risk = Math.abs(r.entry - r.sl);
        if (risk) {
          var rr = (r.exit_px - r.entry) / risk * (r.direction === "SHORT" ? -1 : 1);
          h.appendChild(el("span", "r " + (rr >= 0 ? "up" : "down"), sign(rr, 2) + "R"));
        }
      }
      card.appendChild(h);
      card.appendChild(el("div", "t", r.title || "Untitled"));

      var g = el("div", "jgrid");
      [["Entry", r.entry], ["Exit", r.exit_px], ["Confidence", r.confidence]].forEach(function (c) {
        var cell = el("div");
        cell.appendChild(labelled(c[0]));
        cell.appendChild(el("div", "v", c[1] == null ? "—"
          : (c[0] === "Confidence" ? c[1] + "/10" : fmt(c[1], 2))));
        g.appendChild(cell);
      });
      card.appendChild(g);

      [["Kya galat kiya", r.mistakes], ["Kya seekha", r.lessons]].forEach(function (b) {
        if (!b[1]) return;
        var blk = el("div", "jblock");
        blk.appendChild(labelled(b[0]));
        blk.appendChild(el("p", null, b[1]));
        card.appendChild(blk);
      });
      if (r.tags && r.tags.length) {
        var tg = el("div", "tags");
        r.tags.forEach(function (t) { tg.appendChild(el("span", null, t)); });
        card.appendChild(tg);
      }
      box.appendChild(card);
      if (window.__msObserve) window.__msObserve(card);
    });
  }

  // --------------------------------------------------------------- settings --
  function prefRow(title, sub, checked, onChange) {
    var r = el("label", "pref");
    var g = el("span", "grow");
    g.appendChild(el("span", "t", title));
    g.appendChild(el("span", "s", sub));
    r.appendChild(g);
    var sw = el("button", "sw" + (checked ? " on" : ""));
    sw.appendChild(el("i"));
    sw.setAttribute("aria-label", title);
    sw.addEventListener("click", function (ev) {
      ev.preventDefault();
      var next = !sw.classList.contains("on");
      sw.classList.toggle("on", next);
      onChange(next, sw);
    });
    r.appendChild(sw);
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

    var box = $("alert-prefs"); clear(box);
    box.appendChild(prefRow("Price entry ke paas aa raha hai",
      "resting order ghanton baith sakta hai — kaam ki baat ye hai ki wo aa raha hai",
      a.on_approach, function (v) { saveAlerts({ on_approach: v }); }));

    var prox = el("label", "pref");
    var pg = el("span", "grow");
    pg.appendChild(el("span", "t", "Kitna paas matlab “paas”"));
    pg.appendChild(el("span", "s", "entry level se price ka % faasla"));
    prox.appendChild(pg);
    var inp = el("input", "inp mono");
    inp.type = "number"; inp.step = "0.05"; inp.min = "0.01"; inp.max = "10";
    inp.value = a.proximity_pct;
    inp.addEventListener("change", function () {
      saveAlerts({ proximity_pct: Number(inp.value) })
        .then(function (out) { inp.value = out.proximity_pct; })
        .catch(function () { inp.value = a.proximity_pct; });
    });
    prox.appendChild(inp);
    box.appendChild(prox);

    box.appendChild(prefRow("Naya setup mila", "jab koi strategy pehli baar issue kare",
      a.on_new_setup, function (v) { saveAlerts({ on_new_setup: v }); }));
    box.appendChild(prefRow("Entry fill ho gaya", "resting order bhar gaya, trade live hai",
      a.on_trigger, function (v) { saveAlerts({ on_trigger: v }); }));
    box.appendChild(prefRow("Trade band ho gaya", "target, stop ya time exit — net R ke saath",
      a.on_close, function (v) { saveAlerts({ on_close: v }); }));
    box.appendChild(prefRow("Telegram channel", "app band ho tab bhi ye kaam karta hai",
      n.telegram, function (v) {
        post("/settings/notifications", { telegram: v }, "PUT").then(function (out) {
          state.settings.notifications = out.notifications;
        });
      }));

    var bots = $("tg-bots"); clear(bots);
    var list = d.telegram_bots || [];
    if (!list.length) {
      bots.appendChild(el("div", "bot",
        "Koi bot connect nahi — alerts browser se bahar nahi jaayenge."));
    }
    list.forEach(function (b) {
      var r = el("div", "bot");
      r.appendChild(el("span", "dot on"));
      r.appendChild(el("span", "nm", "@" + (b.bot_username || "bot")));
      r.appendChild(el("div", "sp"));
      r.appendChild(el("span", "meta", b.verified ? "connected · chat detected" : "verify pending"));
      var t = el("button", "btn", "Test");
      t.addEventListener("click", function () {
        t.disabled = true; t.textContent = "…";
        post("/settings/telegram/test", {})
          .then(function () { t.textContent = "Sent ✓"; })
          .catch(function () { t.textContent = "Fail"; })
          .then(function () { setTimeout(function () {
            t.disabled = false; t.textContent = "Test"; }, 2000); });
      });
      var x = el("button", "btn", "Remove");
      x.addEventListener("click", function () {
        if (!window.confirm("Ye bot hata dein? Us chat pe alerts band ho jaayenge.")) return;
        api("/settings/telegram/" + b.id, { method: "DELETE" }).then(loadSettings);
      });
      r.appendChild(t); r.appendChild(x);
      bots.appendChild(r);
    });

    var about = $("about-cells"); clear(about);
    [["Version", "v1.0.0-foundation", "V4 level-breakout layer"],
     ["Strategies", String((state.catalogue && state.catalogue.strategies.length) || 0), "catalogue mein"],
     ["Symbols", "BTC · ETH", "1m primary / 5m context"],
     ["Execution", "koi nahi", "ye tool order place nahi karta"]
    ].forEach(function (c) {
      var cell = el("div", "stat reveal");
      cell.appendChild(labelled(c[0]));
      cell.appendChild(el("div", "v", c[1]));
      cell.appendChild(el("div", "sub", c[2]));
      about.appendChild(cell);
      if (window.__msObserve) window.__msObserve(cell);
    });
  }

  $("tg-verify").addEventListener("click", function () {
    var btn = $("tg-verify"), err = $("tg-err"), t = $("tg-token");
    if (!t.value.trim()) return;
    btn.disabled = true; btn.textContent = "Verifying…"; err.hidden = true;
    post("/settings/telegram/verify", { token: t.value.trim() })
      .then(function (d) {
        if (!d.ok) throw new Error(d.error || "Telegram ne ye token reject kar diya.");
        t.value = "";
        return loadSettings();
      })
      .catch(function (e) { err.textContent = String(e.message || e); err.hidden = false; })
      .then(function () { btn.disabled = false; btn.textContent = "Verify & connect"; });
  });

  // --------------------------------------------------------------- evidence --
  function renderEvidence() {
    var box = $("evidence-cells"); clear(box);
    var o = (state.perf && state.perf.overall) || {};
    [["Logged recommendations", state.history.length + " / 200", "P5 validation gate", ""],
     ["Closed paper trades", String(o.n || 0), "accounted, fees ke baad", ""],
     ["TRUSTED strategies", "0", "abhi tak koi gate pass nahi kiya", "dim"],
     ["Paper expectancy", o.avg_net_r == null ? "—" : sign(o.avg_net_r, 2) + "R",
      "provisional — sirf " + (o.n || 0) + " trades",
      o.avg_net_r == null ? "" : (o.avg_net_r >= 0 ? "up" : "down")]
    ].forEach(function (c) {
      var cell = el("div", "stat reveal");
      cell.appendChild(labelled(c[0]));
      cell.appendChild(el("div", "v " + c[3], c[1]));
      cell.appendChild(el("div", "sub", c[2]));
      box.appendChild(cell);
      if (window.__msObserve) window.__msObserve(cell);
    });
  }

  // ------------------------------------------------------------------ chart --
  function chartTheme() {
    return {
      layout: { background: { color: "transparent" }, textColor: tok("--chart-text"),
                fontFamily: "'JetBrains Mono', ui-monospace, monospace" },
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
    var t = chartTheme();
    t.timeScale.timeVisible = true; t.timeScale.secondsVisible = false;
    t.crosshair = { mode: 0 };
    t.autoSize = true;
    state.chart = LightweightCharts.createChart($("chart-host"), t);
    state.series = state.chart.addSeries
      ? state.chart.addSeries(LightweightCharts.CandlestickSeries, seriesTheme())
      : state.chart.addCandlestickSeries(seriesTheme());
    state.chart.timeScale().subscribeVisibleLogicalRangeChange(paintZones);
    state.chart.subscribeCrosshairMove(function (p) {
      if (!p || !p.seriesData) return;
      var d = p.seriesData.get(state.series);
      if (!d) return;
      var dd = dec(state.sym);
      $("legend").textContent = state.sym + " · " + state.tf +
        "   O " + fmt(d.open, dd) + "  H " + fmt(d.high, dd) +
        "  L " + fmt(d.low, dd) + "  C " + fmt(d.close, dd);
    });
  }

  /* The geometry as an area, not just lines: green between entry and target,
     red between entry and stop. Redrawn whenever the price scale moves. */
  function paintZones() {
    var up = $("zone-up"), down = $("zone-down");
    if (!up || !down) return;
    var s = current();
    if (!state.series || !s || s.symbol !== state.sym) {
      up.style.display = down.style.display = "none";
      return;
    }
    function band(node, a, b) {
      var ya = state.series.priceToCoordinate(a);
      var yb = state.series.priceToCoordinate(b);
      if (ya == null || yb == null) { node.style.display = "none"; return; }
      node.style.display = "block";
      node.style.top = Math.min(ya, yb) + "px";
      node.style.height = Math.abs(ya - yb) + "px";
    }
    band(up, s.entry, s.target);
    band(down, s.entry, s.stop);
  }

  function clearLines() {
    state.lines.forEach(function (l) { try { state.series.removePriceLine(l); } catch (e) {} });
    state.lines = [];
  }

  /* Catalogue levels plus the selected setup's geometry: TP dashed green,
     ENTRY solid accent, SL dashed red. */
  function drawLevels() {
    if (!state.series) return;
    clearLines();
    api("/api/v4/levels?symbol=" + state.sym + "&tf=" + state.tf)
      .then(function (d) {
        (d.levels || []).forEach(function (lv) {
          state.lines.push(state.series.createPriceLine({
            price: lv.price, color: tok("--ink-3"), lineWidth: 1,
            lineStyle: 2, axisLabelVisible: true, title: lv.label }));
        });
      }).catch(function () {});

    var s = current();
    if (!s || s.symbol !== state.sym) return;
    [["TP", s.target, tok("--up"), 2], ["ENTRY", s.entry, tok("--accent"), 0],
     ["SL", s.stop, tok("--down"), 2]].forEach(function (p) {
      state.lines.push(state.series.createPriceLine({
        price: p[1], color: p[2], lineWidth: 2, lineStyle: p[3],
        axisLabelVisible: true, title: p[0] }));
    });
    paintZones();
  }

  function loadChart() {
    ensureChart();
    if (!state.series) return;
    $("stream-label").textContent = (state.live ? "live" : "sim") + " · " + state.tf + " · " + state.sym;
    var bars = { "5m": 300, "15m": 300, "1h": 400, "4h": 400, "1d": 300 }[state.tf] || 300;
    var secs = { "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400 }[state.tf] || 300;
    var end = new Date();
    var start = new Date(end.getTime() - bars * secs * 1000);
    var q = "/api/chart?symbol=" + state.sym + "&timeframe=" + state.tf +
            "&from=" + start.toISOString() + "&to=" + end.toISOString();
    api(q).then(function (d) {
      var rows = (d.candles || []).map(function (k) {
        return { time: Math.floor(new Date(k.ts).getTime() / 1000),
                 open: k.o, high: k.h, low: k.l, close: k.c };
      });
      state.series.setData(rows);
      var dd = dec(state.sym);
      var last = rows[rows.length - 1];
      if (last) {
        $("legend").textContent = state.sym + " · " + state.tf +
          "   O " + fmt(last.open, dd) + "  H " + fmt(last.high, dd) +
          "  L " + fmt(last.low, dd) + "  C " + fmt(last.close, dd);
        state.heroCandles = rows.slice(-90).map(function (r) {
          return { o: r.open, h: r.high, l: r.low, c: r.close };
        });
        renderHeroCandles();
      }
      drawLevels();
      requestAnimationFrame(paintZones);
    }).catch(function () {
      $("legend").textContent = state.sym + " · " + state.tf + "   data unavailable";
    });
  }

  // ------------------------------------------------------------------ loads --
  function loadCatalogue() {
    return api("/api/v4/strategies").then(function (d) {
      state.catalogue = d;
      renderStrategies(); renderStratList(); renderSettings();
    }).catch(function () {});
  }
  function loadSetups() {
    return api("/api/v4/setups").then(function (d) {
      state.setups = d.setups || [];
      renderSetup(); renderQueue(); renderExposure(); drawLevels();
    }).catch(function () {});
  }
  function loadQuotes() {
    return api("/api/v4/quotes").then(function (d) {
      var q = d.quotes || {};
      Object.keys(q).forEach(function (k) {
        if (state.quotes[k]) state.prev[k] = state.quotes[k].price;
        state.quotes[k] = q[k];
      });
      setLive(true);
      renderTickers();
    }).catch(function () {});
  }
  /* Today's opening price per symbol, so the ticker's % is a real day change. */
  function loadDayOpens() {
    var end = new Date();
    var start = new Date(end.getTime() - 5 * 86400 * 1000);
    return Promise.all(["ETHUSDT", "BTCUSDT"].map(function (sym) {
      return api("/api/chart?symbol=" + sym + "&timeframe=1d&from=" +
                 start.toISOString() + "&to=" + end.toISOString())
        .then(function (d) {
          var rows = d.candles || [];
          if (rows.length) state.dayOpen[sym] = rows[rows.length - 1].o;
        }).catch(function () {});
    })).then(renderTickers);
  }

  function loadHistory() {
    return api("/api/v4/history?limit=300").then(function (d) {
      state.history = d.rows || [];
      renderHistory(); renderPaper(); renderEvidence();
    }).catch(function () {});
  }
  function loadPerformance() {
    return api("/api/v4/performance").then(function (d) {
      state.perf = d;
      renderPaper(); renderStrategies(); renderEvidence();
    }).catch(function () {});
  }
  function loadPaper() {
    return api("/api/paper").then(function (d) {
      state.paper = d;
      /* the paper book seeds the sizing panel only until the trader types
         their own account size — after that their number wins */
      var eq = (d.portfolio && d.portfolio.equity) != null ? d.portfolio.equity
             : (d.account && d.account.balance);
      if (eq != null && !localStorage.getItem("ms_equity")) {
        state.equity = eq; renderSizing(current());
      }
      renderPaper();
    }).catch(function () {});
  }
  function loadJournal() {
    return api("/api/journal?limit=12").then(renderJournal).catch(function () {});
  }
  function loadSettings() {
    return api("/settings").then(function (d) {
      state.settings = d; renderSettings();
    }).catch(function () {});
  }

  $("risk").addEventListener("input", function () {
    state.riskPct = Number(this.value);
    renderSizing(current()); renderExposure();
  });
  $("equity-input").addEventListener("input", function () {
    var v = Number(this.value);
    if (!(v > 0)) return;
    state.equity = v;
    try { localStorage.setItem("ms_equity", String(v)); } catch (e) {}
    renderSizing(current());
  });

  // ------------------------------------------------------------------- boot --
  function boot() {
    renderSymbolTabs(); renderTfTabs(); renderHistTabs(); renderPipeline(); renderTickers();
    /* Paint every empty state before the first request. If the backend is
       unreachable the user sees "kuch nahi mila" rather than a blank slab —
       the failure is then explained by the banner, not by absence. */
    renderSetup(); renderQueue(); renderExposure(); renderLiveTrades(); renderHistory();
    renderPaper(); renderEvidence(); renderJournal([]);
    loadCatalogue().then(loadSetups).then(function () { loadChart(); });
    loadQuotes();
    loadDayOpens();
    loadHistory();
    loadPerformance();
    loadPaper();
    loadActive();
    loadJournal();
    loadSettings();
    tickClock();
  }

  if (TOKEN) boot(); else showGate();
  setInterval(tickClock, 1000);
  setInterval(function () { if (TOKEN) loadQuotes(); }, 4000);
  setInterval(function () { if (TOKEN) { loadSetups(); loadHistory(); loadActive(); } }, 60000);
})();

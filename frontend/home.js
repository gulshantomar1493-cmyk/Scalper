/* MarketScalper — Dashboard (V3): Home / Learn / Trade Recommendations.
 *
 * PURE RENDERER. app.js owns the network (GET /api/v3/map, /api/v3/setups,
 * /api/v3/history) and passes results in; this file only maps backend enums to
 * simple Hinglish and builds DOM. No fetch / WS / storage / engine math — the
 * Hinglish lines are presentation of backend values, never new analysis.
 * XSS-safe (textContent only).
 */
(function () {
  "use strict";
  var els = null, cb = null;      // cb: {fullChart(), loadRecs(filters)}

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

  /* ---------------- tabs ---------------- */
  function selectTab(which) {
    var map = { home: "dash-home", learn: "dash-learn", recs: "dash-recs" };
    Object.keys(map).forEach(function (k) {
      var pane = document.getElementById(map[k]);
      var tab = document.getElementById("dash-tab-" + k);
      if (pane) pane.hidden = k !== which;
      if (tab) tab.classList.toggle("on", k === which);
    });
  }

  function init(elements, callbacks) {
    els = elements; cb = callbacks || null;
    ["home", "learn", "recs"].forEach(function (k) {
      var t = document.getElementById("dash-tab-" + k);
      if (t) t.addEventListener("click", function () { selectTab(k); });
    });
    var fc = document.getElementById("home-fullchart");
    if (fc && cb && cb.fullChart) fc.addEventListener("click", cb.fullChart);
  }

  /* ---------------- TAB 1 · HOME — Hinglish market summary ---------------- */

  // Backend enum → simple Hinglish (presentation mapping only)
  function biasLine(b) {
    if (b === "BULLISH") return ["Market abhi bullish hai — buyers control mein hain.", "up"];
    if (b === "BEARISH") return ["Market abhi bearish hai — sellers control mein hain.", "down"];
    return ["Market abhi decided nahi hai — buyers/sellers ki ladai barabar hai.", "neu"];
  }
  function trendWord(t) {
    return t === "BULLISH" ? "upar (bullish)" : t === "BEARISH" ? "niche (bearish)" : "sideways (range)";
  }
  function pdLine(pd) {
    if (pd === "PREMIUM") return "Price abhi PREMIUM area mein hai (mehngi) — LONG ke liye ideal nahi, SHORT zones dekho.";
    if (pd === "DISCOUNT") return "Price abhi DISCOUNT area mein hai (sasti) — LONG zones se reaction ka chance.";
    return "Premium/Discount abhi clear nahi.";
  }

  function helpRow(label, value, cls, helpText) {
    var row = el("div", "hc-row");
    var d = el("details", "hc-help");
    var sm = el("summary", "hc-k");
    sm.appendChild(el("span", null, label));
    sm.appendChild(el("span", "hc-q", "?"));
    d.appendChild(sm);
    d.appendChild(el("div", "hc-exp", helpText));
    row.appendChild(d);
    row.appendChild(el("span", "hc-v " + (cls || ""), value));
    return row;
  }

  function symbolCard(sym, map, setups) {
    var card = el("div", "home-card");
    var head = el("div", "hc-head");
    head.appendChild(el("span", "hc-sym mono", sym.replace("USDT", "")));
    head.appendChild(el("span", "hc-px mono", map && map.price != null ? fmt(map.price) : "—"));
    card.appendChild(head);
    if (!map || !map.ready) {
      card.appendChild(el("div", "dash-empty", "Data load ho raha hai…"));
      return card;
    }
    var bias = (map.bias || {}).overall || "NEUTRAL";
    var per = (map.bias || {}).per_tf || {};
    var bl = biasLine(bias);

    // the one-line story
    card.appendChild(el("div", "hc-story " + bl[1], bl[0]));

    // indicator rows — each with a "?" Hinglish explainer
    card.appendChild(helpRow("Overall Trend",
      bias === "NEUTRAL" ? "Range / Mixed" : (bias === "BULLISH" ? "Bullish ▲" : "Bearish ▼"),
      bias === "BULLISH" ? "up" : bias === "BEARISH" ? "down" : "neu",
      "Ye batata hai market overall upar chal raha hai ya niche. Sab timeframes ka vote milakar banta hai (1D ka vote sabse bada)."));
    card.appendChild(helpRow("HTF Direction (1D / 4H / 1H)",
      trendWord(per["1d"]) + " · " + trendWord(per["4h"]) + " · " + trendWord(per["1h"]),
      "", "Bade timeframes ki direction — market ka असली rukh yahi batata hai. Inke against trade karna risky hota hai."));
    card.appendChild(helpRow("LTF Direction (15m / 5m)",
      trendWord(per["15m"]) + " · " + trendWord(per["5m"]),
      "", "Chhote timeframes ki direction — entry ka timing yahin se milta hai."));
    card.appendChild(helpRow("Market Structure",
      per["1h"] === "BULLISH" ? "Higher Highs ban rahe (upar)" :
      per["1h"] === "BEARISH" ? "Lower Lows ban rahe (niche)" : "Range — koi clear structure nahi",
      "", "Ye batata hai price highs/lows kaise bana rahi hai. HH+HL = uptrend, LH+LL = downtrend."));
    card.appendChild(helpRow("Momentum",
      per["5m"] === per["15m"] && per["5m"] !== "RANGE"
        ? ("Abhi " + (per["5m"] === "BULLISH" ? "upar" : "niche") + " ki taraf momentum hai")
        : "Momentum mixed hai — koi clear push nahi",
      "", "Chhote timeframes ek hi direction mein hon to momentum strong maana jata hai."));
    card.appendChild(helpRow("Premium / Discount", pdLine((map.premium_discount || {})["1h"]),
      "", "Range ke upar wala half premium (mehnga), niche wala discount (sasta). Sasta kharido, mehnga becho."));

    // liquidity + zones + levels
    var liq = map.liquidity || {};
    if (liq.draw_above) {
      card.appendChild(helpRow("Upar ki Liquidity",
        liq.draw_above.kind + " @ " + fmt(liq.draw_above.price),
        "up", "Upar ka sabse bada stops-ka-pool — price aksar isi taraf khichti hai. Wahan profit booking hoti hai."));
    }
    if (liq.draw_below) {
      card.appendChild(helpRow("Niche ki Liquidity",
        liq.draw_below.kind + " @ " + fmt(liq.draw_below.price),
        "down", "Niche ka sabse bada stops-ka-pool — girne par price yahan tak ja sakti hai."));
    }
    var zones = (map.decision_points || []).slice(0, 3);
    if (zones.length) {
      var zwrap = el("div", "hc-zones");
      var zd = el("details", "hc-help");
      var zs = el("summary", "hc-k");
      zs.appendChild(el("span", null, "Important Zones (watch karo)"));
      zs.appendChild(el("span", "hc-q", "?"));
      zd.appendChild(zs);
      zd.appendChild(el("div", "hc-exp",
        "Ye wo price areas hain jahan engine reaction expect karta hai — demand/supply, order blocks, S/R. Price yahan aaye to engine confirmation ka wait karta hai."));
      zwrap.appendChild(zd);
      zones.forEach(function (z) {
        var r = el("div", "hc-zone mono");
        r.appendChild(el("span", "hc-zside " + (z.side === "ABOVE" ? "down" : z.side === "BELOW" ? "up" : "neu"),
          z.side === "ABOVE" ? "UPAR" : z.side === "BELOW" ? "NICHE" : "YAHIN"));
        r.appendChild(el("span", null, fmt(z.lo) + " – " + fmt(z.hi)));
        r.appendChild(el("span", "hc-zw", z.stack + "-TF"));
        zwrap.appendChild(r);
      });
      card.appendChild(zwrap);
    }

    // session + setup line
    var sess = setups && setups.session;
    if (sess) {
      card.appendChild(helpRow("Session (IST)", sess.label || "—",
        sess.effect === "BOOST" ? "up" : sess.effect === "BLOCK" ? "down" : "",
        "Din ke time ke hisaab se market ka mood — London+NY overlap sabse best, raat ka dead zone avoid."));
    }
    var act = (setups && setups.setups) || [];
    if (act.length) {
      var s0 = act[0];
      card.appendChild(el("div", "hc-setup up",
        "⚡ Setup mila: " + s0.direction + " " + s0.grade + " @ " + fmt(s0.entry) +
        " (SL " + fmt(s0.sl) + ", TP1 " + fmt(s0.tp1) + ")"));
    } else {
      card.appendChild(el("div", "hc-setup dim",
        "Abhi koi setup nahi — engine " + ((setups && setups.watching) || []).length +
        " zones par nazar rakhe hue hai. Patience."));
    }
    return card;
  }

  function renderMarket(data) {
    var host = document.getElementById("home-cards");
    if (!host) return;
    host.textContent = "";
    ["BTCUSDT", "ETHUSDT"].forEach(function (sym) {
      var d = (data || {})[sym] || {};
      host.appendChild(symbolCard(sym, d.map, d.setups));
    });
  }

  /* -------- TAB 3 · recommendations (active + closed, IST, colors) -------- */

  function recRow(it, closed) {
    var r = el("div", "dr-row " + (closed ? (it.result_r > 0 ? "dr-win" : it.result_r < 0 ? "dr-loss" : "dr-flat") : "dr-active"));
    var l1 = el("div", "dr-l1");
    l1.appendChild(el("span", "dr-sym mono", it.symbol.replace("USDT", "")));
    l1.appendChild(el("span", "dr-dir " + (it.direction === "LONG" ? "up" : "down"), it.direction));
    l1.appendChild(el("span", "dr-grade mono", it.grade));
    l1.appendChild(el("span", "dr-type", it.setup_type));
    var badge = null;
    if (closed) {
      if (it.status === "TP1_HIT" || it.status === "TP2_HIT") {
        badge = el("span", "dr-badge dr-b-win", "🎯 Target Hit" + (it.status === "TP2_HIT" ? " (TP2)" : ""));
      } else if (it.status === "STOP_LOSS") {
        badge = el("span", "dr-badge dr-b-loss", "🛑 Stop Loss Hit");
      } else {
        badge = el("span", "dr-badge dr-b-dim", it.status.replace("_", " "));
      }
    } else {
      badge = el("span", "dr-badge dr-b-active", "ACTIVE");
    }
    l1.appendChild(badge);
    r.appendChild(l1);
    var l2 = el("div", "dr-l2 mono");
    l2.appendChild(el("span", null, "Entry " + fmt(it.entry)));
    l2.appendChild(el("span", "down", "SL " + fmt(it.sl)));
    l2.appendChild(el("span", "up", "TP1 " + fmt(it.tp1)));
    if (it.tp2 != null) l2.appendChild(el("span", "up", "TP2 " + fmt(it.tp2)));
    l2.appendChild(el("span", null, "R:R " + fmt(it.rr)));
    if (closed && it.result_r != null) {
      l2.appendChild(el("span", it.result_r >= 0 ? "up" : "down",
        (it.result_r >= 0 ? "+" : "") + fmt(it.result_r) + "R"));
    }
    r.appendChild(l2);
    var l3 = el("div", "dr-l3");
    l3.appendChild(el("span", null, "Issued: " + (it.ts ? window.IST.dateTime(it.ts) : "—") + " IST"));
    if (closed && it.closed_ts) {
      l3.appendChild(el("span", null, "Closed: " + window.IST.dateTime(it.closed_ts) + " IST"));
    }
    r.appendChild(l3);
    return r;
  }

  function renderRecs(active, closed, meta) {
    var ha = document.getElementById("dashrec-active");
    var hc = document.getElementById("dashrec-closed");
    if (ha) {
      ha.textContent = "";
      if (!active || !active.length) {
        ha.appendChild(el("div", "dash-empty", "Abhi koi active recommendation nahi — engine market map par nazar rakhe hue hai."));
      } else {
        active.forEach(function (it) { ha.appendChild(recRow(it, false)); });
      }
    }
    if (hc) {
      hc.textContent = "";
      if (meta && meta.note) hc.appendChild(el("div", "dr-note", meta.note));
      if (!closed || !closed.length) {
        hc.appendChild(el("div", "dash-empty", "Koi closed trade nahi mila is filter mein."));
      } else {
        closed.forEach(function (it) { hc.appendChild(recRow(it, true)); });
      }
    }
  }

  // filter bar for closed trades (values only; app.js fetches)
  function renderRecFilters(current) {
    var host = document.getElementById("dashrec-filters");
    if (!host || host.firstChild) return;                 // build once
    function sel(id, opts, label) {
      var s = el("select", "hf-sel");
      s.id = id;
      opts.forEach(function (o) {
        var op = el("option", null, o || label);
        op.value = o;
        s.appendChild(op);
      });
      s.addEventListener("change", refresh);
      return s;
    }
    function refresh() {
      if (!cb || !cb.loadRecs) return;
      cb.loadRecs({
        symbol: document.getElementById("drf-symbol").value,
        direction: document.getElementById("drf-direction").value,
        grade: document.getElementById("drf-grade").value,
        status: document.getElementById("drf-status").value,
        date_from: document.getElementById("drf-from").value,
        date_to: document.getElementById("drf-to").value,
      });
    }
    host.appendChild(sel("drf-symbol", ["", "BTCUSDT", "ETHUSDT"], "sab symbols"));
    host.appendChild(sel("drf-direction", ["", "LONG", "SHORT"], "dono directions"));
    host.appendChild(sel("drf-grade", ["", "A+", "A"], "sab grades"));
    host.appendChild(sel("drf-status", ["", "TP1_HIT", "TP2_HIT", "STOP_LOSS", "CANCELLED", "EXPIRED", "TIMEOUT"], "sab results"));
    ["from", "to"].forEach(function (k) {
      var d = el("input", "hf-date");
      d.type = "date"; d.id = "drf-" + k; d.title = k + " date";
      d.addEventListener("change", refresh);
      host.appendChild(d);
    });
  }

  window.Home = { init: init, selectTab: selectTab, renderMarket: renderMarket,
                  renderRecs: renderRecs, renderRecFilters: renderRecFilters };
})();

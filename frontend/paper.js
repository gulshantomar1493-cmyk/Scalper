/* MarketScalper — Paper Trading page (P6, decision D31): simulation-only.
 *
 * app.js OWNS the network: Paper.init receives {state, order, close, cancel,
 * wallet} callbacks. This module renders the portfolio, the order ticket, open
 * positions, open orders, and trade history, and manages local form state. It
 * NEVER fetches directly, does no engine math, and places NO real order —
 * everything is 100% simulated server-side. XSS-safe (textContent only). */
(function () {
  "use strict";
  var root = null, api = null, state = null, timer = null;
  var SYMBOLS = ["BTCUSDT", "ETHUSDT"];

  function el(t, c, x) { var e = document.createElement(t); if (c) e.className = c; if (x != null) e.textContent = x; return e; }
  function money(v) { return v == null ? "—" : "$" + Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function num(v, d) { return v == null ? "—" : Number(v).toLocaleString("en-US", { maximumFractionDigits: d == null ? 4 : d }); }
  function cls(v) { return v > 0 ? "up" : v < 0 ? "down" : ""; }

  function init(callbacks) { api = callbacks; }

  function mount(container) {
    root = container; load();
    if (timer) clearInterval(timer);
    timer = setInterval(function () {
      if (root && root.isConnected) load();
      else if (timer) { clearInterval(timer); timer = null; }
    }, 4000);                                            // live PnL refresh
  }

  function load() {
    if (!root || !api) return;
    api.state().then(function (s) { state = s; render(); }).catch(function () {});
  }

  function readTicket() {
    var g = function (id) { var e = root.querySelector("#pt-" + id); return e ? e.value : ""; };
    return { symbol: g("sym"), side: g("side"), type: g("type"), qty: g("qty"),
             price: g("price"), stop: g("stop"), lev: g("lev"), ro: !!(root.querySelector("#pt-ro") || {}).checked };
  }

  function render() {
    if (!root || !state) return;
    var keep = root.querySelector(".pt-form") ? readTicket() : null;
    root.textContent = "";
    root.appendChild(portfolioEl());
    var cols = el("div", "pt-cols");
    cols.appendChild(ticketEl(keep));
    cols.appendChild(bookEl());
    root.appendChild(cols);
    root.appendChild(historyEl());
  }

  function portfolioEl() {
    var p = state.portfolio || {};
    var wrap = el("div", "pt-portfolio");
    [["Equity", money(p.equity)], ["Balance", money(p.balance)],
     ["Total P&L", money(p.total_pnl), cls(p.total_pnl)],           // B3: realized + open
     ["Realized PnL", money(p.realized_pnl), cls(p.realized_pnl)],
     ["Unrealized PnL", money(p.unrealized_pnl), cls(p.unrealized_pnl)],
     ["ROI", (p.roi_pct == null ? "—" : (p.roi_pct >= 0 ? "+" : "") + num(p.roi_pct, 2) + "%"), cls(p.roi_pct)],
     ["Used Margin", money(p.used_margin)], ["Available", money(p.available_margin)],
     ["Open", num(p.open_positions, 0)]].forEach(function (c) {
      var b = el("div", "pt-stat");
      b.appendChild(el("span", "pt-stat-k", c[0]));
      b.appendChild(el("span", "pt-stat-v " + (c[2] || ""), c[1]));
      wrap.appendChild(b);
    });
    return wrap;
  }

  function opt(sel, val, txt, cur) { var o = el("option", null, txt); o.value = val; if (val === cur) o.selected = true; sel.appendChild(o); }

  function ticketEl(keep) {
    var card = el("div", "pt-card pt-form");
    card.appendChild(el("div", "pt-ch", "Order Ticket"));
    var symSel = el("select", "pt-input"); symSel.id = "pt-sym";
    SYMBOLS.forEach(function (s) { opt(symSel, s, s, keep ? keep.symbol : "BTCUSDT"); });
    var typeSel = el("select", "pt-input"); typeSel.id = "pt-type";
    ["market", "limit", "stop"].forEach(function (t) { opt(typeSel, t, t.toUpperCase(), keep ? keep.type : "market"); });
    var buy = el("button", "pt-side pt-buy", "BUY / Long"), sell = el("button", "pt-side pt-sell", "SELL / Short");
    var sideInput = el("input"); sideInput.type = "hidden"; sideInput.id = "pt-side"; sideInput.value = keep ? keep.side : "BUY";
    function paintSide() { buy.classList.toggle("on", sideInput.value === "BUY"); sell.classList.toggle("on", sideInput.value === "SELL"); }
    buy.addEventListener("click", function () { sideInput.value = "BUY"; paintSide(); });
    sell.addEventListener("click", function () { sideInput.value = "SELL"; paintSide(); });
    paintSide();
    var sideRow = el("div", "pt-side-row"); sideRow.appendChild(buy); sideRow.appendChild(sell);

    function fld(id, label, type, val, ph) {
      var w = el("label", "pt-field"); w.appendChild(el("span", "pt-flabel", label));
      var i = el("input", "pt-input"); i.id = "pt-" + id; if (type) i.type = type; if (val != null) i.value = val; if (ph) i.placeholder = ph;
      w.appendChild(i); return w;
    }
    card.appendChild(wrapField("Symbol", symSel));
    card.appendChild(sideRow);
    card.appendChild(sideInput);
    card.appendChild(wrapField("Type", typeSel));
    card.appendChild(fld("qty", "Quantity", "number", keep ? keep.qty : "", "0.00"));
    card.appendChild(fld("price", "Limit Price", "number", keep ? keep.price : "", "for LIMIT"));
    card.appendChild(fld("stop", "Stop Price", "number", keep ? keep.stop : "", "for STOP"));
    card.appendChild(fld("lev", "Leverage", "number", keep ? keep.lev : "10", "1..125"));
    var roW = el("label", "pt-check"); var ro = el("input"); ro.type = "checkbox"; ro.id = "pt-ro"; if (keep && keep.ro) ro.checked = true;
    roW.appendChild(ro); roW.appendChild(el("span", null, "Reduce only")); card.appendChild(roW);

    var place = el("button", "pt-btn pt-place", "Place Order");
    place.addEventListener("click", submitOrder);
    card.appendChild(place);

    // wallet
    var w = el("div", "pt-wallet");
    w.appendChild(el("span", "pt-flabel", "Virtual wallet (USD)"));
    var bal = el("input", "pt-input"); bal.id = "pt-bal"; bal.type = "number"; bal.placeholder = "10000";
    bal.value = state.account ? state.account.balance : 10000;
    var reset = el("button", "pt-btn pt-reset", "Set / Reset");
    reset.addEventListener("click", function () {
      var v = parseFloat(bal.value);
      if (v > 0 && window.confirm("Reset the paper wallet to " + money(v) + "? This closes all positions.")) api.wallet({ balance: v }).then(load).catch(err);
    });
    w.appendChild(bal); w.appendChild(reset);
    card.appendChild(w);
    return card;
  }

  function wrapField(label, control) { var w = el("label", "pt-field"); w.appendChild(el("span", "pt-flabel", label)); w.appendChild(control); return w; }

  function submitOrder() {
    var t = readTicket();
    var body = { symbol: t.symbol, side: t.side, type: t.type,
                 qty: parseFloat(t.qty), leverage: parseFloat(t.lev) || undefined, reduce_only: t.ro };
    if (!(body.qty > 0)) { window.alert("Enter a quantity."); return; }
    if (t.type === "limit") body.price = parseFloat(t.price);
    if (t.type === "stop") body.stop_price = parseFloat(t.stop);
    api.order(body).then(load).catch(err);
  }
  function err(e) { window.alert("Order failed: " + ((e && e.message) || e)); }

  function bookEl() {
    var card = el("div", "pt-card");
    card.appendChild(el("div", "pt-ch", "Positions"));
    var pos = state.positions || [];
    if (!pos.length) card.appendChild(el("div", "pt-empty", "No open positions."));
    pos.forEach(function (p) {
      var row = el("div", "pt-pos");
      var top = el("div", "pt-pos-top");
      top.appendChild(el("span", "pt-pdir " + (p.side === "LONG" ? "up" : "down"), p.side));
      top.appendChild(el("span", "pt-psym", p.symbol));
      top.appendChild(el("span", "pt-pqty", num(p.qty) + " @ " + num(p.avg_entry, 2)));
      top.appendChild(el("span", "pt-ppnl " + cls(p.unrealized_pnl), money(p.unrealized_pnl)));
      row.appendChild(top);
      var meta = el("div", "pt-pmeta");
      meta.appendChild(el("span", null, "Mark " + num(p.mark, 2)));
      meta.appendChild(el("span", null, "Liq " + num(p.liq_price, 2)));
      meta.appendChild(el("span", null, "Margin " + money(p.margin)));
      meta.appendChild(el("span", null, p.leverage + "x"));
      var close = el("button", "pt-mini pt-del", "Close");
      close.addEventListener("click", function () { api.close({ position_id: p.id }).then(load).catch(err); });
      meta.appendChild(close);
      row.appendChild(meta);
      card.appendChild(row);
    });
    var orders = state.orders || [];
    if (orders.length) {
      card.appendChild(el("div", "pt-ch pt-ch2", "Open Orders"));
      orders.forEach(function (o) {
        var row = el("div", "pt-order");
        row.appendChild(el("span", "pt-odir " + (o.side === "BUY" ? "up" : "down"), o.side));
        row.appendChild(el("span", null, o.type.toUpperCase() + " " + num(o.qty) + (o.price ? " @ " + num(o.price, 2) : (o.stop_price ? " stop " + num(o.stop_price, 2) : ""))));
        var cancel = el("button", "pt-mini", "Cancel");
        cancel.addEventListener("click", function () { api.cancel({ order_id: o.id }).then(load).catch(err); });
        row.appendChild(cancel);
        card.appendChild(row);
      });
    }
    return card;
  }

  function historyEl() {
    var card = el("div", "pt-card");
    card.appendChild(el("div", "pt-ch", "Trade History"));
    var h = state.history || [];
    if (!h.length) { card.appendChild(el("div", "pt-empty", "No trades yet.")); return card; }
    h.forEach(function (t) {
      var row = el("div", "pt-hist");
      row.appendChild(el("span", "pt-hdir " + (t.side === "BUY" ? "up" : "down"), t.side));
      row.appendChild(el("span", "pt-hsym", t.symbol));
      row.appendChild(el("span", null, num(t.qty) + " @ " + num(t.price, 2)));
      row.appendChild(el("span", "pt-hpnl " + cls(t.realized_pnl), (t.realized_pnl ? money(t.realized_pnl) : "")));
      row.appendChild(el("span", "pt-htime", t.ts && window.IST ? window.IST.dateTime(t.ts) : ""));
      card.appendChild(row);
    });
    return card;
  }

  window.Paper = { init: init, mount: mount, reload: load, positions: function () { return (state && state.positions) || []; } };
})();

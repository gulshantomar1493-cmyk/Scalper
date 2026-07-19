/* MarketScalper — indicator rendering + Indicators menu (chart UX items 2/3/4).
 *
 * PURE RENDERER. The BACKEND computes EMA/SMA/RSI (owner rule): /api/chart
 * returns {time,value} point series and the live forming stream carries the
 * interim values. This file only DRAWS them as Lightweight Charts series and
 * builds the Indicators menu — it never computes an indicator, never fetches,
 * never streams, never stores (app.js/ui.js own those). Volume is the candle's
 * own v rendered as a histogram; RSI lives in its own pane.
 */
(function () {
  "use strict";
  const LC = () => window.LightweightCharts;
  const EMA_COLOR = { 20: "#F5C518", 50: "#FF9800", 200: "#9C7BFF" };
  const DEFAULTS = {
    ema: { 20: { on: true, color: EMA_COLOR[20] }, 50: { on: true, color: EMA_COLOR[50] }, 200: { on: true, color: EMA_COLOR[200] } },
    sma: { on: false, len: 100, color: "#B8C0D0" },
    rsi: { on: false, len: 14, ob: 70, os: 30, color: "#22D3EE" },
    volume: { on: true },
  };
  const VUP = "rgba(34,197,94,0.5)", VDN = "rgba(239,68,68,0.5)";

  let chart = null, cfg = clone(DEFAULTS);
  const S = {};                       // series by key
  let obLine = null, osLine = null;

  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  function merge(saved) {
    const c = clone(DEFAULTS);
    if (!saved) return c;
    try {
      for (const p of [20, 50, 200]) if (saved.ema && saved.ema[p]) Object.assign(c.ema[p], saved.ema[p]);
      if (saved.sma) Object.assign(c.sma, saved.sma);
      if (saved.rsi) Object.assign(c.rsi, saved.rsi);
      if (saved.volume) Object.assign(c.volume, saved.volume);
    } catch (e) { /* fall back to defaults */ }
    return c;
  }
  function lineOpts(color) {
    return { color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
  }
  function tsec(iso) { return Math.floor(Date.parse(iso) / 1000); }

  function ensure() {
    for (const p of [20, 50, 200]) if (!S["ema" + p]) S["ema" + p] = chart.addSeries(LC().LineSeries, lineOpts(cfg.ema[p].color));
    if (!S.sma) S.sma = chart.addSeries(LC().LineSeries, lineOpts(cfg.sma.color));
    if (!S.volume) {
      S.volume = chart.addSeries(LC().HistogramSeries, { priceFormat: { type: "volume" }, priceScaleId: "vol", lastValueVisible: false, priceLineVisible: false });
      S.volume.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    }
    if (!S.rsi) {
      S.rsi = chart.addSeries(LC().LineSeries, Object.assign(lineOpts(cfg.rsi.color), { priceScaleId: "rsi" }), 1);  // pane 1
      obLine = S.rsi.createPriceLine({ price: cfg.rsi.ob, color: "rgba(239,68,68,0.55)", lineStyle: 2, lineWidth: 1, axisLabelVisible: true, title: "OB" });
      osLine = S.rsi.createPriceLine({ price: cfg.rsi.os, color: "rgba(34,197,94,0.55)", lineStyle: 2, lineWidth: 1, axisLabelVisible: true, title: "OS" });
    }
    applyVisibility();
  }
  function applyVisibility() {
    for (const p of [20, 50, 200]) S["ema" + p].applyOptions({ visible: cfg.ema[p].on, color: cfg.ema[p].color });
    S.sma.applyOptions({ visible: cfg.sma.on, color: cfg.sma.color });
    S.rsi.applyOptions({ visible: cfg.rsi.on, color: cfg.rsi.color });
    S.volume.applyOptions({ visible: cfg.volume.on });
  }

  function init(c, saved) { chart = c; cfg = merge(saved); ensure(); }

  // Query string for /api/chart based on the enabled indicators (the backend
  // computes exactly these — the browser sends the config, never the formula).
  function paramsQuery() {
    const on = [20, 50, 200].filter(p => cfg.ema[p].on), parts = [];
    if (on.length) parts.push("ema=" + on.join(","));
    if (cfg.sma.on) parts.push("sma=" + cfg.sma.len);
    if (cfg.rsi.on) parts.push("rsi=" + cfg.rsi.len);
    return parts.join("&");
  }

  function render(body) {
    if (!chart) return;
    const candles = (body && body.candles) || [], ind = (body && body.indicators) || {};
    S.volume.setData(cfg.volume.on ? candles.map(c => ({ time: tsec(c.ts), value: c.v, color: c.c >= c.o ? VUP : VDN })) : []);
    for (const p of [20, 50, 200]) S["ema" + p].setData(cfg.ema[p].on && ind.ema ? (ind.ema[String(p)] || []) : []);
    S.sma.setData(cfg.sma.on && ind.sma ? (ind.sma[String(cfg.sma.len)] || []) : []);
    S.rsi.setData(cfg.rsi.on && ind.rsi ? (ind.rsi[String(cfg.rsi.len)] || []) : []);
  }

  // Live: the forming stream's interim indicators are 1m; extend the last point
  // (higher TFs refresh on reload). Backend-supplied — no browser math.
  function updateForming(f, tf) {
    if (!chart || tf !== "1m" || !f) return;
    const t = tsec(f.ts), fi = f.indicators || {};
    for (const p of [20, 50, 200]) if (cfg.ema[p].on && fi["ema" + p] != null) S["ema" + p].update({ time: t, value: fi["ema" + p] });
    if (cfg.rsi.on && fi.rsi != null) S.rsi.update({ time: t, value: fi.rsi });
    if (cfg.volume.on) S.volume.update({ time: t, value: f.v, color: f.c >= f.o ? VUP : VDN });
  }

  // ---- Indicators menu (item 3) — onChange(needData): true = reload /api/chart
  //      (enable / length changed), false = just restyle. ----
  function renderMenu(panel, onChange) {
    panel.textContent = "";
    const mk = (tag, cls, txt) => { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };
    const row = (label, controls) => {
      const r = mk("div", "ind-row"); r.appendChild(mk("span", "ind-label", label));
      controls.forEach(c => r.appendChild(c)); panel.appendChild(r);
    };
    const toggle = (get, set) => {
      const b = mk("button", "ind-tog"); b.type = "button";
      const paint = () => b.classList.toggle("on", get()); paint();
      b.addEventListener("click", () => { set(!get()); paint(); onChange(true); });
      return b;
    };
    const num = (get, set, needData) => {
      const i = mk("input", "ind-num"); i.type = "number"; i.value = get();
      i.addEventListener("change", () => { const v = parseInt(i.value, 10); if (v >= 1 && v <= 1000) { set(v); onChange(needData); } });
      return i;
    };
    const color = (get, set) => {
      const i = mk("input", "ind-color"); i.type = "color"; i.value = get();
      i.addEventListener("input", () => { set(i.value); onChange(false); });
      return i;
    };
    const sub = (t, el) => { const s = mk("span", "ind-sub", t); s.appendChild(el); return s; };
    for (const p of [20, 50, 200]) row("EMA " + p, [toggle(() => cfg.ema[p].on, v => cfg.ema[p].on = v), color(() => cfg.ema[p].color, v => cfg.ema[p].color = v)]);
    row("SMA", [toggle(() => cfg.sma.on, v => cfg.sma.on = v), num(() => cfg.sma.len, v => cfg.sma.len = v, true), color(() => cfg.sma.color, v => cfg.sma.color = v)]);
    row("RSI", [toggle(() => cfg.rsi.on, v => cfg.rsi.on = v), num(() => cfg.rsi.len, v => cfg.rsi.len = v, true),
      sub("OB", num(() => cfg.rsi.ob, v => { cfg.rsi.ob = v; if (obLine) obLine.applyOptions({ price: v }); }, false)),
      sub("OS", num(() => cfg.rsi.os, v => { cfg.rsi.os = v; if (osLine) osLine.applyOptions({ price: v }); }, false))]);
    row("Volume", [toggle(() => cfg.volume.on, v => cfg.volume.on = v)]);
  }

  window.Indicators = { init, ensure, paramsQuery, render, updateForming, renderMenu, applyVisibility, config: () => cfg };
})();

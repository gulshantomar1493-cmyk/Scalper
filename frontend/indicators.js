/* MarketScalper — indicator rendering + Indicators menu (chart UX items 2/3/4).
 *
 * PURE RENDERER. The BACKEND computes EMA/SMA/RSI (owner rule): /api/chart
 * returns {time,value} point series and the live forming stream carries the
 * interim values. This file only DRAWS them as Lightweight Charts series, builds
 * the Indicators menu, and shows a legend. It never computes an indicator,
 * fetches, streams, or stores (app.js/ui.js own those).
 *
 * The three moving-average slots have EDITABLE periods (e.g. 9 / 21 / 200) —
 * changing a period refetches /api/chart with the new period (backend computes).
 */
(function () {
  "use strict";
  const LC = () => window.LightweightCharts;
  const DEFAULTS = {
    ema: [
      { len: 20, on: true, color: "#F5C518" },
      { len: 50, on: true, color: "#FF9800" },
      { len: 200, on: true, color: "#9C7BFF" },
    ],
    sma: { on: false, len: 100, color: "#B8C0D0" },
    rsi: { on: false, len: 14, ob: 70, os: 30, color: "#22D3EE" },
    volume: { on: true },
  };
  const VUP = "rgba(34,197,94,0.5)", VDN = "rgba(239,68,68,0.5)";
  const SLOTS = [0, 1, 2];

  let chart = null, cfg = clone(DEFAULTS), legendEl = null;
  const S = {};                       // series by key: ema0/ema1/ema2/sma/rsi/volume
  const lastV = {};                   // latest value per key (for the legend)
  let obLine = null, osLine = null;

  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  function merge(saved) {
    const c = clone(DEFAULTS);
    if (!saved) return c;
    try {
      if (Array.isArray(saved.ema)) SLOTS.forEach(i => { if (saved.ema[i]) Object.assign(c.ema[i], saved.ema[i]); });
      if (saved.sma) Object.assign(c.sma, saved.sma);
      if (saved.rsi) Object.assign(c.rsi, saved.rsi);
      if (saved.volume) Object.assign(c.volume, saved.volume);
    } catch (e) { /* defaults */ }
    return c;
  }
  function lineOpts(color) { return { color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }; }
  function tsec(iso) { return Math.floor(Date.parse(iso) / 1000); }
  function fmtN(v) { return v == null ? "" : Number(v).toLocaleString("en-US", { maximumFractionDigits: 2 }); }
  function lastPoint(pts) { return (pts && pts.length) ? pts[pts.length - 1].value : null; }

  function ensure() {
    SLOTS.forEach(i => { if (!S["ema" + i]) S["ema" + i] = chart.addSeries(LC().LineSeries, lineOpts(cfg.ema[i].color)); });
    if (!S.sma) S.sma = chart.addSeries(LC().LineSeries, lineOpts(cfg.sma.color));
    if (!S.volume) {
      S.volume = chart.addSeries(LC().HistogramSeries, { priceFormat: { type: "volume" }, priceScaleId: "vol", lastValueVisible: false, priceLineVisible: false });
      S.volume.priceScale().applyOptions({ scaleMargins: { top: 0.86, bottom: 0 } });
    }
    applyVisibility();
  }
  // RSI lives in its OWN bottom pane (paneIndex 1). Create it LAZILY, only when
  // enabled — an always-present empty RSI pane otherwise steals ~30% of the
  // chart height (that was the dead band under the candles). removeSeries()
  // drops the now-empty pane (LWC v5), giving the space back to the candles.
  function ensureRsi() {
    if (S.rsi) return;
    S.rsi = chart.addSeries(LC().LineSeries, Object.assign(lineOpts(cfg.rsi.color), { priceScaleId: "rsi" }), 1);
    obLine = S.rsi.createPriceLine({ price: cfg.rsi.ob, color: "rgba(239,68,68,0.55)", lineStyle: 2, lineWidth: 1, axisLabelVisible: true, title: "OB" });
    osLine = S.rsi.createPriceLine({ price: cfg.rsi.os, color: "rgba(34,197,94,0.55)", lineStyle: 2, lineWidth: 1, axisLabelVisible: true, title: "OS" });
  }
  function removeRsi() {
    if (!S.rsi) return;
    try { chart.removeSeries(S.rsi); } catch (e) { /* already gone */ }
    S.rsi = null; obLine = null; osLine = null; lastV.rsi = null;
  }
  function syncRsi() { if (cfg.rsi.on) ensureRsi(); else removeRsi(); }
  function applyVisibility() {
    SLOTS.forEach(i => S["ema" + i].applyOptions({ visible: cfg.ema[i].on, color: cfg.ema[i].color }));
    S.sma.applyOptions({ visible: cfg.sma.on, color: cfg.sma.color });
    syncRsi();
    if (S.rsi) S.rsi.applyOptions({ visible: true, color: cfg.rsi.color });
    S.volume.applyOptions({ visible: cfg.volume.on });
    renderLegend();
  }

  // TradingView-style legend (top-left) — colour dot + name + latest value.
  function renderLegend() {
    if (!legendEl) return;
    legendEl.textContent = "";
    const item = (label, color, val) => {
      const s = document.createElement("span"); s.className = "leg-item";
      const d = document.createElement("span"); d.className = "leg-dot"; d.style.background = color;
      const t = document.createElement("span"); t.className = "leg-txt";
      t.textContent = label + (val != null ? " " + fmtN(val) : "");
      s.appendChild(d); s.appendChild(t); legendEl.appendChild(s);
    };
    SLOTS.forEach(i => { if (cfg.ema[i].on) item("EMA" + cfg.ema[i].len, cfg.ema[i].color, lastV["ema" + i]); });
    if (cfg.sma.on) item("SMA" + cfg.sma.len, cfg.sma.color, lastV.sma);
    if (cfg.rsi.on) item("RSI" + cfg.rsi.len, cfg.rsi.color, lastV.rsi == null ? null : Math.round(lastV.rsi * 10) / 10);
    if (cfg.volume.on) item("Vol", "#94A3B8", null);
  }

  function init(c, saved) { chart = c; cfg = merge(saved); legendEl = document.getElementById("chart-legend"); ensure(); renderLegend(); }

  function paramsQuery() {
    const lens = [...new Set(cfg.ema.filter(s => s.on).map(s => s.len))], parts = [];
    if (lens.length) parts.push("ema=" + lens.join(","));
    if (cfg.sma.on) parts.push("sma=" + cfg.sma.len);
    if (cfg.rsi.on) parts.push("rsi=" + cfg.rsi.len);
    return parts.join("&");
  }

  function render(body) {
    if (!chart) return;
    const candles = (body && body.candles) || [], ind = (body && body.indicators) || {};
    S.volume.setData(cfg.volume.on ? candles.map(c => ({ time: tsec(c.ts), value: c.v, color: c.c >= c.o ? VUP : VDN })) : []);
    SLOTS.forEach(i => {
      const slot = cfg.ema[i];
      const pts = slot.on && ind.ema ? (ind.ema[String(slot.len)] || []) : [];
      S["ema" + i].setData(pts); lastV["ema" + i] = lastPoint(pts);
    });
    const sp = cfg.sma.on && ind.sma ? (ind.sma[String(cfg.sma.len)] || []) : [];
    S.sma.setData(sp); lastV.sma = lastPoint(sp);
    syncRsi();                                         // create/drop the RSI pane
    const rp = cfg.rsi.on && ind.rsi ? (ind.rsi[String(cfg.rsi.len)] || []) : [];
    if (S.rsi) { S.rsi.setData(rp); lastV.rsi = lastPoint(rp); }
    renderLegend();
  }

  // Live: the forming stream's interim indicators are 1m at the default periods
  // (20/50/200 + RSI). Extend the last point of matching slots (others refresh
  // on the next reload). Backend-supplied — no browser math.
  function updateForming(f, tf) {
    if (!chart || tf !== "1m" || !f) return;
    const t = tsec(f.ts), fi = f.indicators || {};
    SLOTS.forEach(i => {
      const v = fi["ema" + cfg.ema[i].len];
      if (cfg.ema[i].on && v != null) { S["ema" + i].update({ time: t, value: v }); lastV["ema" + i] = v; }
    });
    if (cfg.rsi.on && S.rsi && fi.rsi != null) { S.rsi.update({ time: t, value: fi.rsi }); lastV.rsi = fi.rsi; }
    if (cfg.volume.on) S.volume.update({ time: t, value: f.v, color: f.c >= f.o ? VUP : VDN });
    renderLegend();
  }

  // ---- Indicators menu — onChange(needData): true = reload /api/chart. ----
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
      b.addEventListener("click", () => { set(!get()); paint(); onChange(true); }); return b;
    };
    const num = (get, set, needData) => {
      const i = mk("input", "ind-num"); i.type = "number"; i.min = 1; i.max = 1000; i.value = get();
      i.addEventListener("change", () => { const v = parseInt(i.value, 10); if (v >= 1 && v <= 1000) { set(v); onChange(needData); } }); return i;
    };
    const color = (get, set) => {
      const i = mk("input", "ind-color"); i.type = "color"; i.value = get();
      i.addEventListener("input", () => { set(i.value); onChange(false); }); return i;
    };
    const sub = (t, el) => { const s = mk("span", "ind-sub", t); s.appendChild(el); return s; };
    SLOTS.forEach(i => row("MA", [
      toggle(() => cfg.ema[i].on, v => cfg.ema[i].on = v),
      num(() => cfg.ema[i].len, v => cfg.ema[i].len = v, true),
      color(() => cfg.ema[i].color, v => cfg.ema[i].color = v),
    ]));
    row("SMA", [toggle(() => cfg.sma.on, v => cfg.sma.on = v), num(() => cfg.sma.len, v => cfg.sma.len = v, true), color(() => cfg.sma.color, v => cfg.sma.color = v)]);
    row("RSI", [toggle(() => cfg.rsi.on, v => cfg.rsi.on = v), num(() => cfg.rsi.len, v => cfg.rsi.len = v, true),
      sub("OB", num(() => cfg.rsi.ob, v => { cfg.rsi.ob = v; if (obLine) obLine.applyOptions({ price: v }); }, false)),
      sub("OS", num(() => cfg.rsi.os, v => { cfg.rsi.os = v; if (osLine) osLine.applyOptions({ price: v }); }, false))]);
    row("Volume", [toggle(() => cfg.volume.on, v => cfg.volume.on = v)]);
  }

  window.Indicators = { init, ensure, paramsQuery, render, updateForming, renderMenu, applyVisibility, config: () => cfg, volumeSeries: () => S.volume };
})();

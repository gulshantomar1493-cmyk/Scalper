/* MarketScalper — drawing tools (chart UX item 11): trend line, horizontal
 * line, rectangle, fibonacci, text.
 *
 * Display-only chart annotations. Nothing here touches data, the engine, or the
 * network — drawings live in an in-memory list anchored in {time, price} so
 * they stay pinned as the chart pans/zooms. Rendered with a Lightweight Charts
 * series primitive (mirrors overlays.js). app.js provides the chart+series and
 * wires the Draw menu; the crosshair click supplies the anchor points.
 */
(function () {
  "use strict";
  const COLORS = { line: "#22D3EE", rectFill: "rgba(34,211,238,0.10)", fib: "#F5C518", text: "#E7ECF5" };
  const FIBS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];

  class DrawPrimitive {
    constructor() {
      this._items = []; this._pending = null;
      this._chart = null; this._series = null; this._req = null;
      const self = this;
      this._pv = { renderer() { return { draw(t) { self._draw(t); } }; } };
    }
    attached(p) { this._chart = p.chart; this._series = p.series; this._req = p.requestUpdate; }
    detached() { this._chart = this._series = this._req = null; }
    paneViews() { return [this._pv]; }
    setItems(items, pending) { this._items = items; this._pending = pending; if (this._req) this._req(); }
    _draw(target) {
      if (!this._chart || !this._series) return;
      const chart = this._chart, series = this._series;
      const X = (tm) => chart.timeScale().timeToCoordinate(tm);
      const Y = (pr) => series.priceToCoordinate(pr);
      target.useMediaCoordinateSpace((scope) => {
        const ctx = scope.context, W = scope.mediaSize.width;
        const all = this._pending ? this._items.concat([this._pending]) : this._items;
        for (const d of all) {
          const ax = X(d.a.time), ay = Y(d.a.price);
          if (ax == null || ay == null) continue;
          if (d.type === "hline") {
            ctx.beginPath(); ctx.strokeStyle = COLORS.line; ctx.lineWidth = 1; ctx.setLineDash([]);
            ctx.moveTo(0, ay); ctx.lineTo(W, ay); ctx.stroke();
            ctx.fillStyle = COLORS.line; ctx.font = "10px ui-monospace, monospace";
            ctx.fillText(Number(d.a.price).toLocaleString("en-US", { maximumFractionDigits: 2 }), 4, ay - 3);
            continue;
          }
          if (d.type === "text") {
            ctx.fillStyle = COLORS.text; ctx.font = "12px ui-monospace, monospace";
            ctx.fillText(d.text || "", ax + 4, ay - 4);
            continue;
          }
          if (!d.b) continue;
          const bx = X(d.b.time), by = Y(d.b.price);
          if (bx == null || by == null) continue;
          if (d.type === "trendline") {
            ctx.beginPath(); ctx.strokeStyle = COLORS.line; ctx.lineWidth = 1.5; ctx.setLineDash([]);
            ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
          } else if (d.type === "rect") {
            const x = Math.min(ax, bx), y = Math.min(ay, by), w = Math.abs(bx - ax), h = Math.abs(by - ay);
            ctx.fillStyle = COLORS.rectFill; ctx.fillRect(x, y, w, h);
            ctx.strokeStyle = COLORS.line; ctx.lineWidth = 1; ctx.setLineDash([]); ctx.strokeRect(x, y, w, h);
          } else if (d.type === "fib") {
            const x0 = Math.min(ax, bx), x1 = Math.max(ax, bx);
            for (const f of FIBS) {
              const price = d.a.price + (d.b.price - d.a.price) * f;
              const y = Y(price); if (y == null) continue;
              ctx.beginPath(); ctx.strokeStyle = COLORS.fib; ctx.globalAlpha = 0.7; ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
              ctx.moveTo(x0, y); ctx.lineTo(x1, y); ctx.stroke(); ctx.globalAlpha = 1; ctx.setLineDash([]);
              ctx.fillStyle = COLORS.fib; ctx.font = "10px ui-monospace, monospace";
              ctx.fillText(f.toFixed(3), x1 + 3, y + 3);
            }
          }
        }
      });
    }
  }

  let chart = null, series = null, prim = null, tool = "none", pending = null, onDoneCb = null;
  const items = [];

  function init(c, s) {
    chart = c; series = s;
    prim = new DrawPrimitive(); series.attachPrimitive(prim);
    chart.subscribeClick(onClick);
  }
  function setTool(t) { tool = t; pending = null; refresh(); }
  function onClick(param) {
    if (tool === "none" || !param.point || param.time == null || !series) return;
    const price = series.coordinateToPrice(param.point.y);
    if (price == null) return;
    const pt = { time: param.time, price };
    if (tool === "hline") { items.push({ type: "hline", a: pt }); finish(); return; }
    if (tool === "text") { const txt = window.prompt("Text label:"); if (txt) items.push({ type: "text", a: pt, text: txt }); finish(); return; }
    if (!pending) { pending = { type: tool, a: pt }; refresh(); return; }   // first point
    items.push({ type: tool, a: pending.a, b: pt }); finish();              // second point
  }
  function finish() { pending = null; tool = "none"; refresh(); if (onDoneCb) onDoneCb(); }
  function refresh() { if (prim) prim.setItems(items, pending); }
  function clear() { items.length = 0; pending = null; refresh(); }
  function undo() { items.pop(); refresh(); }

  window.Drawing = { init, setTool, clear, undo, onDone: (cb) => { onDoneCb = cb; }, tool: () => tool, count: () => items.length };
})();

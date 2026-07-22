/* MarketScalper — drawing tools (chart UX item 11): trend line, horizontal
 * line, rectangle, fibonacci, text. TradingView-style: LIVE PREVIEW while
 * drawing, and SELECT + DRAG to adjust any drawing (endpoints or whole shape)
 * after it is placed.
 *
 * Display-only chart annotations. Nothing here touches data, the engine, or the
 * network — drawings live in an in-memory list anchored in {time, price} so
 * they stay pinned as the chart pans/zooms. Rendered with a Lightweight Charts
 * series primitive; placement uses the chart click/crosshair, adjustment uses
 * DOM mouse events (in the capture phase, so a drag on a drawing doesn't pan
 * the chart).
 */
(function () {
  "use strict";
  const COLORS = { line: "#22D3EE", rectFill: "rgba(34,211,238,0.10)", fib: "#F5C518", text: "#E7ECF5", handle: "#22D3EE", sel: "#F5C518",
    riskFill: "rgba(239,68,68,0.13)", riskLine: "#EF4444", rewardFill: "rgba(34,197,94,0.13)", rewardLine: "#22C55E" };
  const FIBS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
  const HANDLE = 6;      // anchor hit/paint radius (px)
  const LINE_HIT = 6;    // line/edge hit distance (px)
  const RR_DEFAULT = 2;  // R:R target auto-projected at 2R on placement (then draggable)
  const fnum = (v) => Number(v).toLocaleString("en-US", { maximumFractionDigits: 2 });

  class DrawPrimitive {
    constructor() {
      this._chart = null; this._series = null; this._req = null;
      this._items = []; this._pending = null; this._selected = -1;
      const self = this;
      this._pv = { renderer() { return { draw(t) { self._draw(t); } }; } };
    }
    attached(p) { this._chart = p.chart; this._series = p.series; this._req = p.requestUpdate; }
    detached() { this._chart = this._series = this._req = null; }
    paneViews() { return [this._pv]; }
    set(items, pending, selected) { this._items = items; this._pending = pending; this._selected = selected; if (this._req) this._req(); }
    _draw(target) {
      if (!this._chart || !this._series) return;
      const chart = this._chart, series = this._series;
      const X = (tm) => chart.timeScale().timeToCoordinate(tm);
      const Y = (pr) => series.priceToCoordinate(pr);
      target.useMediaCoordinateSpace((scope) => {
        const ctx = scope.context, W = scope.mediaSize.width;
        const all = this._pending ? this._items.concat([this._pending]) : this._items;
        for (let i = 0; i < all.length; i++) {
          const d = all[i], isSel = (i === this._selected);
          const ax = X(d.a.time), ay = Y(d.a.price);
          if (ax == null || ay == null) continue;
          ctx.setLineDash([]); ctx.globalAlpha = 1;
          if (d.type === "hline") {
            ctx.beginPath(); ctx.strokeStyle = COLORS.line; ctx.lineWidth = 1;
            ctx.moveTo(0, ay); ctx.lineTo(W, ay); ctx.stroke();
            ctx.fillStyle = COLORS.line; ctx.font = "10px ui-monospace, monospace";
            ctx.fillText(Number(d.a.price).toLocaleString("en-US", { maximumFractionDigits: 2 }), 4, ay - 3);
            if (isSel) this._handle(ctx, Math.min(W - 8, Math.max(8, ax)), ay);
            continue;
          }
          if (d.type === "text") {
            ctx.fillStyle = COLORS.text; ctx.font = "13px ui-monospace, monospace";
            ctx.fillText(d.text || "", ax + 5, ay - 4);
            if (isSel) this._handle(ctx, ax, ay);
            continue;
          }
          const bx = X(d.b.time), by = Y(d.b.price);
          if (bx == null || by == null) continue;
          if (d.type === "rr") {                                 // risk/reward position tool
            const cx = X(d.c.time), cy = Y(d.c.price);
            if (cx == null || cy == null) continue;
            const xL = Math.min(ax, bx), w = Math.max(Math.abs(bx - ax), 64), xR = xL + w;   // min visible width
            ctx.fillStyle = COLORS.riskFill;   ctx.fillRect(xL, Math.min(ay, by), w, Math.abs(by - ay));   // entry -> stop
            ctx.fillStyle = COLORS.rewardFill; ctx.fillRect(xL, Math.min(ay, cy), w, Math.abs(cy - ay));   // entry -> target
            const hl = (yy, col) => { ctx.beginPath(); ctx.setLineDash([]); ctx.strokeStyle = col; ctx.lineWidth = 1; ctx.moveTo(xL, yy); ctx.lineTo(xR, yy); ctx.stroke(); };
            hl(ay, COLORS.line); hl(by, COLORS.riskLine); hl(cy, COLORS.rewardLine);
            const risk = Math.abs(d.a.price - d.b.price), rr = risk ? Math.abs(d.c.price - d.a.price) / risk : 0;
            ctx.font = "10px ui-monospace, monospace";
            ctx.fillStyle = COLORS.line;       ctx.fillText("Entry " + fnum(d.a.price) + "  ·  R:R " + rr.toFixed(2), xR + 4, ay - 3);
            ctx.fillStyle = COLORS.riskLine;   ctx.fillText("Stop " + fnum(d.b.price), xR + 4, by + 11);
            ctx.fillStyle = COLORS.rewardLine; ctx.fillText("Target " + fnum(d.c.price), xR + 4, cy - 3);
            if (isSel) { this._handle(ctx, ax, ay); this._handle(ctx, bx, by); this._handle(ctx, cx, cy); }
            continue;
          }
          if (d.type === "trendline") {
            ctx.beginPath(); ctx.strokeStyle = COLORS.line; ctx.lineWidth = 1.6;
            ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
          } else if (d.type === "rect") {
            const x = Math.min(ax, bx), y = Math.min(ay, by), w = Math.abs(bx - ax), h = Math.abs(by - ay);
            ctx.fillStyle = COLORS.rectFill; ctx.fillRect(x, y, w, h);
            ctx.strokeStyle = COLORS.line; ctx.lineWidth = 1; ctx.strokeRect(x, y, w, h);
          } else if (d.type === "fib") {
            const x0 = Math.min(ax, bx), x1 = Math.max(ax, bx);
            ctx.strokeStyle = COLORS.line; ctx.lineWidth = 1; ctx.setLineDash([2, 3]);
            ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke(); ctx.setLineDash([]);
            for (const f of FIBS) {
              const price = d.a.price + (d.b.price - d.a.price) * f;
              const y = Y(price); if (y == null) continue;
              ctx.beginPath(); ctx.strokeStyle = COLORS.fib; ctx.globalAlpha = 0.75; ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
              ctx.moveTo(x0, y); ctx.lineTo(x1, y); ctx.stroke(); ctx.globalAlpha = 1; ctx.setLineDash([]);
              ctx.fillStyle = COLORS.fib; ctx.font = "10px ui-monospace, monospace";
              ctx.fillText(f.toFixed(3), x1 + 3, y + 3);
            }
          }
          if (isSel) { this._handle(ctx, ax, ay); this._handle(ctx, bx, by); }
        }
      });
    }
    _handle(ctx, x, y) {
      ctx.beginPath(); ctx.setLineDash([]); ctx.fillStyle = "#0A0F1E"; ctx.strokeStyle = COLORS.sel; ctx.lineWidth = 1.5;
      ctx.arc(x, y, HANDLE, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    }
  }

  let chart = null, series = null, chartEl = null, prim = null, onDoneCb = null, onChangeCb = null;
  let tool = "none", pending = null, selected = -1, drag = null;
  const items = [];
  function changed() { if (onChangeCb) onChangeCb(); }   // fired on every item mutation (app.js persists)

  // ---- coordinate helpers ----
  function toScreen(pt) { return { x: chart.timeScale().timeToCoordinate(pt.time), y: series.priceToCoordinate(pt.price) }; }
  function fromXY(x, y) { const t = chart.timeScale().coordinateToTime(x), p = series.coordinateToPrice(y); return (t != null && p != null) ? { time: t, price: p } : null; }
  function relXY(e) { const r = chartEl.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; }
  function refresh() { if (prim) prim.set(items, pending, selected); }

  // ---- hit testing (screen coords) ----
  function distToSeg(px, py, ax, ay, bx, by) {
    const dx = bx - ax, dy = by - ay, L = dx * dx + dy * dy;
    let t = L ? ((px - ax) * dx + (py - ay) * dy) / L : 0; t = Math.max(0, Math.min(1, t));
    return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
  }
  function hitItem(d, mx, my) {
    const a = toScreen(d.a); if (a.x == null) return null;
    if (Math.hypot(mx - a.x, my - a.y) <= HANDLE + 3) return "a";
    if (d.b) { const b = toScreen(d.b); if (b.x != null && Math.hypot(mx - b.x, my - b.y) <= HANDLE + 3) return "b"; }
    if (d.type === "hline") return Math.abs(my - a.y) <= LINE_HIT ? "body" : null;
    if (d.type === "text") return (mx >= a.x - 4 && mx <= a.x + 90 && Math.abs(my - a.y) <= 12) ? "body" : null;
    if (!d.b) return null;
    const b = toScreen(d.b); if (b.x == null) return null;
    if (d.type === "rr") {
      const c = toScreen(d.c);
      if (c.x != null && Math.hypot(mx - c.x, my - c.y) <= HANDLE + 3) return "c";   // target handle
      if (c.x == null) return null;
      const xL = Math.min(a.x, b.x), xR = Math.max(a.x, b.x);
      const yT = Math.min(a.y, b.y, c.y), yB = Math.max(a.y, b.y, c.y);
      return (mx >= xL - LINE_HIT && mx <= xR + LINE_HIT && my >= yT - LINE_HIT && my <= yB + LINE_HIT) ? "body" : null;
    }
    if (d.type === "trendline" || d.type === "fib") return distToSeg(mx, my, a.x, a.y, b.x, b.y) <= LINE_HIT ? "body" : null;
    if (d.type === "rect") {
      const x0 = Math.min(a.x, b.x), x1 = Math.max(a.x, b.x), y0 = Math.min(a.y, b.y), y1 = Math.max(a.y, b.y), n = LINE_HIT;
      const onH = mx >= x0 - n && mx <= x1 + n && (Math.abs(my - y0) <= n || Math.abs(my - y1) <= n);
      const onV = my >= y0 - n && my <= y1 + n && (Math.abs(mx - x0) <= n || Math.abs(mx - x1) <= n);
      return (onH || onV) ? "body" : null;
    }
    return null;
  }
  function hitTest(mx, my) { for (let i = items.length - 1; i >= 0; i--) { const p = hitItem(items[i], mx, my); if (p) return { i, part: p }; } return null; }

  // ---- placement (with live preview) ----
  function onClick(param) {
    if (tool === "none" || !param.point || param.time == null || !series) return;
    const price = series.coordinateToPrice(param.point.y); if (price == null) return;
    const pt = { time: param.time, price };
    if (tool === "hline") { items.push({ type: "hline", a: pt }); finish(); return; }
    if (tool === "text") { const txt = window.prompt("Text label:"); if (txt) items.push({ type: "text", a: pt, text: txt }); finish(); return; }
    if (!pending) { pending = { type: tool, a: pt, b: pt }; refresh(); return; }   // 1st point -> preview
    if (tool === "rr") {                                                            // entry -> stop, target auto-projected at 2R
      const target = pending.a.price + (pending.a.price - pt.price) * RR_DEFAULT;
      items.push({ type: "rr", a: pending.a, b: pt, c: { time: pt.time, price: target } });
    } else {
      items.push({ type: tool, a: pending.a, b: pt });                              // 2nd point
    }
    finish();
  }
  function onCrosshair(param) {                       // preview: 2nd point follows the mouse
    if (!pending || !param.point || param.time == null) return;
    const price = series.coordinateToPrice(param.point.y);
    if (price != null) { pending.b = { time: param.time, price }; refresh(); }
  }
  function finish() { pending = null; tool = "none"; selected = items.length - 1; refresh(); changed(); if (onDoneCb) onDoneCb(); }

  // ---- select + drag (DOM, capture phase so a hit blocks the chart pan) ----
  function onMouseDown(e) {
    if (tool !== "none" || !chartEl) return;          // placing mode uses clicks
    const { x, y } = relXY(e), hit = hitTest(x, y);
    if (hit) {
      selected = hit.i;
      drag = { i: hit.i, part: hit.part, startTP: fromXY(x, y), orig: JSON.parse(JSON.stringify(items[hit.i])) };
      e.preventDefault(); e.stopPropagation();        // don't let LWC pan
      refresh();
    } else if (selected !== -1) { selected = -1; refresh(); }
  }
  function onMouseMove(e) {
    if (!drag || !chartEl) return;
    const { x, y } = relXY(e), tp = fromXY(x, y); if (!tp) return;
    const it = items[drag.i], o = drag.orig;
    if (drag.part === "a") it.a = tp;
    else if (drag.part === "b") it.b = tp;
    else if (drag.part === "c") it.c = tp;             // R:R target
    else if (drag.startTP) {                           // move whole shape by the drag delta
      const dT = tp.time - drag.startTP.time, dP = tp.price - drag.startTP.price;
      it.a = { time: o.a.time + dT, price: o.a.price + dP };
      if (o.b) it.b = { time: o.b.time + dT, price: o.b.price + dP };
      if (o.c) it.c = { time: o.c.time + dT, price: o.c.price + dP };
    }
    drag.moved = true; e.preventDefault(); refresh();
  }
  function onMouseUp() { if (drag && drag.moved) changed(); drag = null; }
  function onKey(e) {
    if ((e.key === "Delete" || e.key === "Backspace") && selected >= 0 && tool === "none") {
      items.splice(selected, 1); selected = -1; refresh(); changed();
    } else if (e.key === "Escape") { pending = null; tool = "none"; selected = -1; refresh(); if (onDoneCb) onDoneCb(); }
  }

  function init(c, s) {
    chart = c; series = s; chartEl = document.getElementById("chart");
    prim = new DrawPrimitive(); series.attachPrimitive(prim);
    chart.subscribeClick(onClick);
    chart.subscribeCrosshairMove(onCrosshair);
    if (chartEl) chartEl.addEventListener("mousedown", onMouseDown, true);   // capture phase
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
    window.addEventListener("keydown", onKey);
  }
  function setTool(t) { tool = t; pending = null; selected = -1; refresh(); }
  function clear() { items.length = 0; pending = null; selected = -1; refresh(); changed(); }
  function undo() { items.pop(); selected = -1; refresh(); changed(); }

  // ---- serialize / restore (M3 persistence; storage lives in ui.js, not here) ----
  function getItems() { return JSON.parse(JSON.stringify(items)); }   // a plain-data snapshot
  function setItems(arr) {                                            // replace (e.g. on a symbol switch); NOT a user edit
    items.length = 0;
    if (Array.isArray(arr)) arr.forEach((d) => { if (d && d.type && d.a) items.push(d); });
    pending = null; selected = -1; drag = null; refresh();
  }

  window.Drawing = { init, setTool, clear, undo, getItems, setItems,
    onDone: (cb) => { onDoneCb = cb; }, onChange: (cb) => { onChangeCb = cb; },
    tool: () => tool, count: () => items.length };
})();

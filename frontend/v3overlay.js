/* MarketScalper V3 — chart overlay: the active timeframe's trader read.
 *
 * PURE RENDERER. app.js fetches GET /api/v3/analysis?symbol&tf and passes the
 * read here; this file only DRAWS it on the price chart via an LWC series
 * primitive: zones (with lifecycle state), trendlines (with state), and ranked
 * liquidity levels. Switch the TF -> app.js fetches that TF's read -> this
 * redraws. No fetch / WS / storage / engine math here. Muted by design — price
 * stays the anchor (DESIGN LAW).
 */
(function () {
  "use strict";

  let chart = null, series = null, prim = null, read = null;

  function cssVar(name, fallback) {
    try {
      const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return v || fallback;
    } catch (e) { return fallback; }
  }
  function rgba(hex, a) {
    const h = hex.replace("#", "");
    const n = parseInt(h.length === 3 ? h.split("").map(c => c + c).join("") : h, 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  }

  // per-kind zone styling (fill alpha, stroke alpha) — muted, price-forward
  const ZONE_STYLE = {
    DEMAND: { var: "--up", fill: 0.07, line: 0.30 },
    SUPPLY: { var: "--down", fill: 0.07, line: 0.30 },
    ORDER_BLOCK: { var: "--accent", fill: 0.06, line: 0.28 },
    FVG: { var: "--accent", fill: 0.04, line: 0.18 },
    SR: { var: "--text-dim", fill: 0.05, line: 0.25 },
    TRENDLINE: { var: "--warn", fill: 0.04, line: 0.20 },
  };

  class V3Primitive {
    constructor() {
      const self = this;
      this._req = null;
      this._pv = { renderer() { return { draw(t) { self._draw(t); } }; } };
    }
    attached(p) { this._req = p.requestUpdate; }
    detached() { this._req = null; }
    paneViews() { return [this._pv]; }
    refresh() { if (this._req) this._req(); }

    _draw(target) {
      if (!read || !chart || !series) return;
      const ts = chart.timeScale();
      const X = (t) => ts.timeToCoordinate(t);
      const Y = (p) => series.priceToCoordinate(p);
      target.useMediaCoordinateSpace((scope) => {
        const ctx = scope.context, W = scope.mediaSize.width;
        const up = cssVar("--up", "#22C55E"), down = cssVar("--down", "#EF4444");
        const dim = cssVar("--text-dim", "#8B93A7");
        ctx.font = "9px ui-monospace, monospace";

        // ---- zones: band from creation time to the right edge ----
        for (const z of (read.zones || [])) {
          if (z.state === "RETIRED") continue;
          const st = ZONE_STYLE[z.kind] || ZONE_STYLE.SR;
          const col = cssVar(st.var, "#8B93A7");
          const yA = Y(z.hi), yB = Y(z.lo);
          if (yA == null || yB == null) continue;
          let x0 = X(z.created_at); if (x0 == null) x0 = 0;
          const weakish = z.state === "WEAK" || z.state === "BROKEN";
          const mul = weakish ? 0.45 : 1;
          ctx.fillStyle = rgba(col, st.fill * mul);
          ctx.fillRect(x0, yA, W - x0, yB - yA);
          ctx.strokeStyle = rgba(col, st.line * mul);
          ctx.lineWidth = 1;
          ctx.setLineDash(z.kind === "FVG" ? [3, 3] : []);
          ctx.strokeRect(x0, yA, W - x0, yB - yA);
          ctx.setLineDash([]);
          ctx.fillStyle = rgba(col, 0.75 * mul);
          ctx.fillText(`${z.kind} · ${z.state}`, x0 + 4, yA + 9);
        }

        // ---- trendlines: segment a -> b (log-interp clamp at left edge) ----
        for (const t of (read.trendlines || [])) {
          if (t.state === "BROKEN" || t.state === "INVALID") continue;
          const col = t.side === "SUPPORT" ? up : down;
          let xa = X(t.a.ts), ya = Y(t.a.price);
          const xb = X(t.b.ts), yb = Y(t.b.price);
          if (xb == null || yb == null) continue;
          if (xa == null || ya == null) {
            // a is off-screen left: interpolate the price at the left edge time
            const t0 = ts.coordinateToTime(0);
            if (t0 == null || t.b.ts === t.a.ts) continue;
            const f = (t0 - t.a.ts) / (t.b.ts - t.a.ts);
            const la = Math.log(t.a.price), lb = Math.log(t.b.price);
            ya = Y(Math.exp(la + (lb - la) * f));
            xa = 0;
            if (ya == null) continue;
          }
          ctx.beginPath();
          ctx.strokeStyle = rgba(col, t.state === "WEAK" ? 0.35 : 0.65);
          ctx.lineWidth = t.state === "STRONG" ? 1.6 : 1;
          ctx.setLineDash(t.state === "WEAK" ? [4, 4] : []);
          ctx.moveTo(xa, ya); ctx.lineTo(xb, yb); ctx.stroke();
          ctx.setLineDash([]);
          ctx.fillStyle = rgba(col, 0.8);
          ctx.fillText(`TL ${t.state} ·${t.touches}`, Math.max(xb - 74, 4), yb - 4);
        }

        // ---- liquidity: full-width levels, priority-styled ----
        for (const p of (read.liquidity || [])) {
          const y = Y(p.price);
          if (y == null) continue;
          const swept = p.state === "SWEPT";
          const strong = p.priority >= 4;
          const col = swept ? dim : (p.side === "BUYSIDE" ? down : up);
          ctx.beginPath();
          ctx.strokeStyle = rgba(col, swept ? 0.25 : strong ? 0.55 : 0.35);
          ctx.lineWidth = strong && !swept ? 1.2 : 1;
          ctx.setLineDash(swept ? [2, 4] : strong ? [] : [5, 4]);
          ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
          ctx.setLineDash([]);
          ctx.fillStyle = rgba(col, swept ? 0.5 : 0.85);
          const stars = "★".repeat(p.priority);
          ctx.fillText(`${p.kind} ${stars}${swept ? " swept" : ""}`, W - 118, y - 3);
        }
      });
    }
  }

  function init(c, s) {
    chart = c; series = s;
    prim = new V3Primitive();
    series.attachPrimitive(prim);
  }

  // data = the /api/v3/analysis payload for the ACTIVE chart TF (or null to clear)
  function set(data) {
    read = (data && data.ready) ? data : null;
    if (prim) prim.refresh();
  }

  window.V3Overlay = { init, set };
})();

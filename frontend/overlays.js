/* MarketScalper overlays + replay-audit tool (roadmap P1.19 + P1.20 +
 * P2.20).
 *
 * PURE CONSUMER of the backend structure payload (state_diff.structure):
 * every number drawn here — pivot prices, labels, trend state, BOS/CHOCH
 * events, trendline/channel endpoints, order block / FVG zones, liquidity
 * pool and key-level prices, sweep events (already computed server-side)
 * — comes from the frozen engines. No calculations beyond canvas
 * rendering.
 *
 * P2.20: order block + breaker + FVG zones as filled boxes, EQH/EQL pools
 * and promoted key levels as full-width horizontal lines, sweep events as
 * chart markers. Boxes/lines extend to the current pane edge using the
 * renderer's own pixel width — no engine math, only chart-space geometry
 * already exposed by the LWC v5 primitive API.
 *
 * Audit tool (P1.20): jump-to-random-trendline + accept/reject tally.
 * Session-local UI state only (the owner records the tally in the gate
 * record); Math.random here is UI selection for the human audit and is
 * outside the deterministic engine surface.
 */

"use strict";

const Overlays = (() => {
  /* ------------------------------------------ trendline canvas primitive */

  class LinesPrimitive {
    constructor() {
      this._lines = [];
      this._chart = null;
      this._series = null;
      this._requestUpdate = null;
      const self = this;
      this._paneView = {
        renderer() {
          return { draw(target) { self._draw(target); } };
        },
      };
    }
    attached(params) {
      this._chart = params.chart;
      this._series = params.series;
      this._requestUpdate = params.requestUpdate;
    }
    detached() { this._chart = this._series = this._requestUpdate = null; }
    paneViews() { return [this._paneView]; }
    setLines(lines) {
      this._lines = lines;
      if (this._requestUpdate) this._requestUpdate();
    }
    _draw(target) {
      if (!this._chart || !this._series) return;
      const chart = this._chart;
      const series = this._series;
      const lines = this._lines;
      target.useMediaCoordinateSpace((scope) => {
        const ctx = scope.context;
        for (const ln of lines) {
          // P2.20: "full" lines (liquidity pools / key levels) span the
          // pane edge to edge — the payload gives only a price, no time
          // range, since these are not anchored to a specific candle.
          const x1 = ln.full ? 0 : chart.timeScale().timeToCoordinate(ln.t1);
          const x2 = ln.full
            ? scope.mediaSize.width
            : chart.timeScale().timeToCoordinate(ln.t2);
          const y1 = series.priceToCoordinate(ln.p1);
          const y2 = series.priceToCoordinate(ln.p2);
          if (x1 === null || x2 === null || y1 === null || y2 === null) continue;
          ctx.beginPath();
          ctx.strokeStyle = ln.color;
          ctx.lineWidth = ln.width;
          ctx.setLineDash(ln.dash ? [4, 4] : []);
          ctx.moveTo(x1, y1);
          ctx.lineTo(x2, y2);
          ctx.stroke();
          ctx.setLineDash([]);
          if (ln.label) {
            ctx.fillStyle = ln.color;
            ctx.font = "10px sans-serif";
            ctx.textAlign = "right";
            ctx.fillText(ln.label, scope.mediaSize.width - 4, y1 - 3);
            ctx.textAlign = "left";
          }
        }
      });
    }
  }

  /* P2.20: order block / breaker / FVG filled-box primitive. Boxes open
   * at their created timestamp and extend to the current pane edge
   * (mediaSize.width) — the payload gives a zone's price bounds and its
   * creation time only, not a "current bar" endpoint. */
  class BoxesPrimitive {
    constructor() {
      this._boxes = [];
      this._chart = null;
      this._series = null;
      this._requestUpdate = null;
      const self = this;
      this._paneView = {
        renderer() {
          return { draw(target) { self._draw(target); } };
        },
      };
    }
    attached(params) {
      this._chart = params.chart;
      this._series = params.series;
      this._requestUpdate = params.requestUpdate;
    }
    detached() { this._chart = this._series = this._requestUpdate = null; }
    paneViews() { return [this._paneView]; }
    setBoxes(boxes) {
      this._boxes = boxes;
      if (this._requestUpdate) this._requestUpdate();
    }
    _draw(target) {
      if (!this._chart || !this._series) return;
      const chart = this._chart;
      const series = this._series;
      const boxes = this._boxes;
      target.useMediaCoordinateSpace((scope) => {
        const ctx = scope.context;
        for (const b of boxes) {
          const x1 = chart.timeScale().timeToCoordinate(b.t1);
          const yTop = series.priceToCoordinate(b.hi);
          const yBot = series.priceToCoordinate(b.lo);
          if (x1 === null || yTop === null || yBot === null) continue;
          const x2 = scope.mediaSize.width;
          ctx.fillStyle = b.fill;
          ctx.fillRect(x1, yTop, x2 - x1, yBot - yTop);
          ctx.strokeStyle = b.border;
          ctx.lineWidth = 1;
          ctx.setLineDash(b.dashPattern);
          ctx.strokeRect(x1, yTop, x2 - x1, yBot - yTop);
          ctx.setLineDash([]);
        }
      });
    }
  }

  /* ------------------------------------------------------------- state */

  const COLORS = {
    support: "#22C55E", resistance: "#EF4444",
    channel: "rgba(255,255,255,0.35)", highlight: "#22D3EE",
    // P2.20
    obBull: "rgba(34,197,94,0.9)", obBullFill: "rgba(34,197,94,0.16)",
    obBear: "rgba(239,68,68,0.9)", obBearFill: "rgba(239,68,68,0.16)",
    breakerBullFill: "rgba(34,197,94,0.08)",
    breakerBearFill: "rgba(239,68,68,0.08)",
    fvgBullFill: "rgba(34,197,94,0.10)",
    fvgBearFill: "rgba(239,68,68,0.10)",
    pool: "#A78BFA", level: "#64748B", sweep: "#F472B6",
  };
  let chart = null;
  let series = null;
  let primitive = null;
  let boxesPrimitive = null;        // P2.20: OB/breaker/FVG zones
  let markers = null;               // createSeriesMarkers handle
  let trendEl = null;
  let structure = null;             // latest payload for the active symbol
  let auditPick = null;             // index into structure.trendlines
  const tally = { accept: 0, reject: 0 };

  const toTime = (iso) => Math.floor(Date.parse(iso) / 1000);

  /* ------------------------------------------------------------ redraw */

  function redraw() {
    if (!primitive) return;
    const st = structure;
    const lines = [];
    if (st) {
      (st.trendlines || []).forEach((ln, i) => {
        const picked = auditPick !== null && i === auditPick;
        lines.push({
          t1: toTime(ln.x1), p1: ln.y1, t2: toTime(ln.x2), p2: ln.y2,
          color: picked ? COLORS.highlight : COLORS[ln.side],
          width: picked ? 3 : 1, dash: false,
        });
      });
      (st.channels || []).forEach((ch) => {
        lines.push({
          t1: toTime(ch.x1), p1: ch.y1, t2: toTime(ch.x2), p2: ch.y2,
          color: COLORS.channel, width: 1, dash: true,
        });
      });
      // P2.20: EQH/EQL pools + promoted key levels — price-only, span
      // the full pane width (no per-zone time range in the payload).
      const liquidity = st.liquidity || {};
      (liquidity.pools || []).forEach((p) => {
        lines.push({
          full: true, p1: p.price, p2: p.price,
          color: COLORS.pool, width: 1, dash: true, label: p.kind,
        });
      });
      const levels = liquidity.levels || {};
      Object.keys(levels).forEach((name) => {
        lines.push({
          full: true, p1: levels[name], p2: levels[name],
          color: COLORS.level, width: 1, dash: false, label: name,
        });
      });
    }
    primitive.setLines(lines);

    // P2.20: order block / breaker / FVG zones
    const boxes = [];
    if (st) {
      const ob = st.orderblocks || {};
      (ob.blocks || []).forEach((b) => {
        const bull = b.direction === "BULL";
        boxes.push({
          t1: toTime(b.created_ts), lo: b.lo, hi: b.hi,
          fill: bull ? COLORS.obBullFill : COLORS.obBearFill,
          border: bull ? COLORS.obBull : COLORS.obBear, dashPattern: [],
        });
      });
      (ob.breakers || []).forEach((b) => {
        const bull = b.direction === "BULL";
        boxes.push({
          t1: toTime(b.created_ts), lo: b.lo, hi: b.hi,
          fill: bull ? COLORS.breakerBullFill : COLORS.breakerBearFill,
          border: bull ? COLORS.obBull : COLORS.obBear, dashPattern: [6, 3],
        });
      });
      (st.fvgs || []).forEach((g) => {
        const bull = g.direction === "BULL";
        boxes.push({
          t1: toTime(g.created_ts), lo: g.lo, hi: g.hi,
          fill: bull ? COLORS.fvgBullFill : COLORS.fvgBearFill,
          border: bull ? COLORS.obBull : COLORS.obBear, dashPattern: [2, 2],
        });
      });
    }
    boxesPrimitive.setBoxes(boxes);

    const marks = [];
    if (st) {
      for (const p of st.pivots || []) {
        marks.push({
          time: toTime(p.ts),
          position: p.kind === "H" ? "aboveBar" : "belowBar",
          color: p.kind === "H" ? COLORS.resistance : COLORS.support,
          shape: "circle", size: 0.6,
          text: p.label || "",
        });
      }
      for (const e of st.bos || []) {
        marks.push({
          time: toTime(e.ts),
          position: e.direction === "UP" ? "aboveBar" : "belowBar",
          color: COLORS.highlight, shape: "square", size: 0.7,
          text: "BOS" + (e.displacement ? "!" : ""),
        });
      }
      for (const e of st.choch || []) {
        marks.push({
          time: toTime(e.ts),
          position: e.direction === "UP" ? "aboveBar" : "belowBar",
          color: "#F59E0B", shape: "square", size: 0.7,
          text: "CHOCH",
        });
      }
      // P2.20: sweep events
      for (const sw of (st.liquidity && st.liquidity.sweeps) || []) {
        marks.push({
          time: toTime(sw.ts),
          position: sw.side === "HIGH" ? "aboveBar" : "belowBar",
          color: COLORS.sweep,
          shape: sw.side === "HIGH" ? "arrowDown" : "arrowUp",
          size: 0.6,
          text: "SWEEP " + sw.target,
        });
      }
    }
    marks.sort((a, b) => a.time - b.time);
    markers.setMarkers(marks);

    if (trendEl) trendEl.textContent = "trend: " + ((st && st.trend) || "—");
  }

  /* --------------------------------------------------------- audit tool */

  function tallyText() {
    return tally.accept + " / " + tally.reject +
      " (" + (tally.accept + tally.reject) + ")";
  }

  function setAuditButtons() {
    const on = auditPick !== null;
    document.getElementById("audit-accept").disabled = !on;
    document.getElementById("audit-reject").disabled = !on;
    document.getElementById("audit-tally").textContent = tallyText();
  }

  function pickRandomLine() {
    const lines = (structure && structure.trendlines) || [];
    if (!lines.length) return;
    auditPick = Math.floor(Math.random() * lines.length);
    const ln = lines[auditPick];
    const t1 = toTime(ln.x1);
    const t2 = toTime(ln.x2);
    const pad = Math.max(60, Math.floor((t2 - t1) * 0.15));
    chart.timeScale().setVisibleRange({ from: t1 - pad, to: t2 + pad });
    setAuditButtons();
    redraw();
  }

  function vote(kind) {
    if (auditPick === null) return;
    tally[kind] += 1;
    auditPick = null;
    setAuditButtons();
    redraw();
  }

  /* -------------------------------------------------------------- API */

  return {
    init(mainChart, mainSeries) {
      chart = mainChart;
      series = mainSeries;
      primitive = new LinesPrimitive();
      series.attachPrimitive(primitive);
      boxesPrimitive = new BoxesPrimitive();
      series.attachPrimitive(boxesPrimitive);
      markers = LightweightCharts.createSeriesMarkers(series, []);
      trendEl = document.getElementById("trend-state");
      document.getElementById("audit-pick")
        .addEventListener("click", pickRandomLine);
      document.getElementById("audit-accept")
        .addEventListener("click", () => vote("accept"));
      document.getElementById("audit-reject")
        .addEventListener("click", () => vote("reject"));
      setAuditButtons();
    },
    setStructure(payload) {     // latest payload for the ACTIVE symbol
      structure = payload || null;
      if (auditPick !== null &&
          (!structure || auditPick >= (structure.trendlines || []).length)) {
        auditPick = null;       // the picked line no longer exists
        setAuditButtons();
      }
      redraw();
    },
  };
})();

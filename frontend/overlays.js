/* MarketScalper overlays + replay-audit tool (roadmap P1.19 + P1.20).
 *
 * PURE CONSUMER of the backend structure payload (state_diff.structure):
 * every number drawn here — pivot prices, labels, trend state, BOS/CHOCH
 * events, trendline/channel endpoints (already projected server-side) —
 * comes from the frozen engines. No calculations beyond canvas rendering.
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
          const x1 = chart.timeScale().timeToCoordinate(ln.t1);
          const x2 = chart.timeScale().timeToCoordinate(ln.t2);
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
        }
        ctx.setLineDash([]);
      });
    }
  }

  /* ------------------------------------------------------------- state */

  const COLORS = {
    support: "#22C55E", resistance: "#EF4444",
    channel: "rgba(255,255,255,0.35)", highlight: "#22D3EE",
  };
  let chart = null;
  let series = null;
  let primitive = null;
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
    }
    primitive.setLines(lines);

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

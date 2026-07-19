/* MarketScalper overlays + replay-audit tool (roadmap P1.19 + P1.20 +
 * P2.20 + P2.21 + P2.22).
 *
 * PURE CONSUMER of the backend structure payload (state_diff.structure):
 * every number drawn here — pivot prices, labels, trend state, BOS/CHOCH
 * events, trendline/channel endpoints, order block / FVG zones, liquidity
 * pool and key-level prices, sweep events, session VWAP + bands, the
 * premium/discount label (already computed server-side) — comes from the
 * frozen engines. No calculations beyond canvas rendering.
 *
 * P2.20: order block + breaker + FVG zones as filled boxes, EQH/EQL pools
 * and promoted key levels as full-width horizontal lines, sweep events as
 * chart markers. Boxes/lines extend to the current pane edge using the
 * renderer's own pixel width — no engine math, only chart-space geometry
 * already exposed by the LWC v5 primitive API.
 *
 * P2.21: session VWAP + +-1sigma/+-2sigma bands rendered the SAME way as
 * P2.20's pool/level lines — the current value only, never accumulated
 * into a history (the payload carries no historical VWAP series; see
 * D19/P2.21 planning record). Premium/discount renders as a split-pane
 * tint at the latest candle's close price (passed in by app.js — pure
 * transport, no computation there either) rather than a uniform wash,
 * using only priceToCoordinate — the same coordinate mapping already
 * used everywhere else in this module.
 *
 * Audit tool (P1.20 + P2.22): jump-to-random-trendline/sweep/order-block
 * + accept/reject tally, tracked per kind. Session-local UI state only
 * (the owner records the tallies in the gate record); Math.random here
 * is UI selection for the human audit and is outside the deterministic
 * engine surface. All three jump operations share ONE navigation helper
 * (jumpToWindow) and point-event kinds (sweep/OB — a single timestamp,
 * no natural span) share ONE centralized padding constant
 * (AUDIT_JUMP_WINDOW_S); the trendline kind keeps its own pre-existing
 * span-scaled floor unchanged, since it already has real behavior to
 * preserve bit-for-bit.
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
          ctx.lineWidth = b.lineWidth || 1;   // P2.22: audit-pick highlight
          ctx.setLineDash(b.dashPattern);
          ctx.strokeRect(x1, yTop, x2 - x1, yBot - yTop);
          ctx.setLineDash([]);
        }
      });
    }
  }

  /* P2.21: premium/discount split-pane shading. Splits the pane at the
   * latest close price — top half tinted for premium, bottom half for
   * discount — rather than a uniform wash, since the payload carries no
   * numeric zone boundary, only the qualitative label. Drawn BEHIND the
   * candles (zOrder "bottom") so price action stays readable. */
  class ShadingPrimitive {
    constructor() {
      this._state = null;           // {closePrice, premiumDiscount}
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
      this._series = params.series;
      this._requestUpdate = params.requestUpdate;
    }
    detached() { this._series = this._requestUpdate = null; }
    paneViews() { return [this._paneView]; }
    zOrder() { return "bottom"; }
    setShading(state) {
      this._state = state;
      if (this._requestUpdate) this._requestUpdate();
    }
    _draw(target) {
      const s = this._state;
      if (!this._series || !s || !s.premiumDiscount || s.closePrice == null) return;
      const y = this._series.priceToCoordinate(s.closePrice);
      if (y === null) return;
      target.useMediaCoordinateSpace((scope) => {
        const ctx = scope.context;
        const w = scope.mediaSize.width;
        const h = scope.mediaSize.height;
        const premium = s.premiumDiscount === "premium";
        ctx.fillStyle = premium ? COLORS.premiumFill : COLORS.discountFill;
        if (premium) ctx.fillRect(0, 0, w, y);
        else ctx.fillRect(0, y, w, h - y);
        ctx.fillStyle = premium ? COLORS.resistance : COLORS.support;
        ctx.font = "10px sans-serif";
        ctx.fillText(s.premiumDiscount.toUpperCase(), 4, premium ? y - 4 : y + 12);
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
    // P2.21
    vwap: "#FB923C", band: "rgba(251,146,60,0.45)",
    premiumFill: "rgba(239,68,68,0.06)", discountFill: "rgba(34,197,94,0.06)",
  };
  let chart = null;
  let series = null;
  let primitive = null;
  let boxesPrimitive = null;        // P2.20: OB/breaker/FVG zones
  let shadingPrimitive = null;      // P2.21: premium/discount split
  let markers = null;               // createSeriesMarkers handle
  // Step 6: execution charts stay CLEAN by default. Structure (HH/HL/LH/LL,
  // BOS/CHOCH, trendlines, OB/FVG, pools/levels/VWAP) is an ADVANCED opt-in via
  // the toolbar toggle — never littered on 1m/5m by default. The streaming
  // Market Structure box (app.js) carries the events; confirmed trade setups
  // draw their own entry/SL/TP lines (setSetup) regardless of this toggle.
  let structureOn = false;          // default OFF — clean execution chart
  let trendEl = null;
  let structure = null;             // latest payload for the active symbol
  let lastClose = null;             // P2.21: latest close, passed by app.js
  let auditPick = null;             // P2.22: {kind: 'trendline'|'sweep'|'ob', index} | null
  const tally = {                   // P2.22: one accept/reject counter per kind
    trendline: { accept: 0, reject: 0 },
    sweep: { accept: 0, reject: 0 },
    ob: { accept: 0, reject: 0 },
  };

  const toTime = (iso) => Math.floor(Date.parse(iso) / 1000);

  // P2.22: centralized replay-jump navigation. UI convenience only, not
  // trading logic. jumpToWindow is the single shared viewport-setting
  // helper for every "random X" pick; AUDIT_JUMP_WINDOW_S is the single
  // padding constant for point-event kinds (sweep/OB — one timestamp,
  // no natural span) — change it once to retune every point-event jump.
  // The trendline kind passes its own pre-existing 60s floor unchanged.
  const AUDIT_JUMP_WINDOW_S = 30 * 60;
  const TRENDLINE_MIN_PAD_S = 60;

  function jumpToWindow(t1, t2, minPadS) {
    const pad = Math.max(minPadS, Math.floor((t2 - t1) * 0.15));
    chart.timeScale().setVisibleRange({ from: t1 - pad, to: t2 + pad });
  }

  /* ------------------------------------------------------------ redraw */

  function redraw() {
    if (!primitive) return;
    // OFF -> `st` is null so every structure layer below builds an empty set
    // (clean chart). The trend readout still uses the real payload.
    const st = structureOn ? structure : null;
    const lines = [];
    if (st) {
      (st.trendlines || []).forEach((ln, i) => {
        const picked = !!(auditPick && auditPick.kind === "trendline" &&
                          auditPick.index === i);
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
      // P2.21: session VWAP + bands — current value only, reusing the
      // same "full" pane-wide line mode as pools/levels (see module
      // docstring: no historical VWAP series exists in the payload).
      const volume = st.volume || {};
      if (volume.session_vwap != null) {
        lines.push({
          full: true, p1: volume.session_vwap, p2: volume.session_vwap,
          color: COLORS.vwap, width: 1, dash: false, label: "VWAP",
        });
        const bandLines = [
          [volume.band_1_up, "+1σ"], [volume.band_1_dn, "-1σ"],
          [volume.band_2_up, "+2σ"], [volume.band_2_dn, "-2σ"],
        ];
        bandLines.forEach(([price, label]) => {
          if (price == null) return;
          lines.push({
            full: true, p1: price, p2: price,
            color: COLORS.band, width: 1, dash: true, label,
          });
        });
      }
    }
    primitive.setLines(lines);

    // P2.21: premium/discount split-pane shading at the latest close
    shadingPrimitive.setShading({
      closePrice: lastClose,
      premiumDiscount: st && st.liquidity && st.liquidity.premium_discount,
    });

    // P2.20: order block / breaker / FVG zones
    const boxes = [];
    if (st) {
      const ob = st.orderblocks || {};
      const obBlocks = ob.blocks || [];
      const obBreakers = ob.breakers || [];
      // P2.22: blocks+breakers form ONE combined pool for the "Random OB"
      // audit pick — same array, same order, used again below in
      // pickRandomOB() so picked indices always match what's rendered.
      const obZones = obBlocks.concat(obBreakers);
      obZones.forEach((b, i) => {
        const bull = b.direction === "BULL";
        const isBreaker = i >= obBlocks.length;
        const picked = !!(auditPick && auditPick.kind === "ob" &&
                          auditPick.index === i);
        boxes.push({
          t1: toTime(b.created_ts), lo: b.lo, hi: b.hi,
          fill: bull
            ? (isBreaker ? COLORS.breakerBullFill : COLORS.obBullFill)
            : (isBreaker ? COLORS.breakerBearFill : COLORS.obBearFill),
          border: picked ? COLORS.highlight : (bull ? COLORS.obBull : COLORS.obBear),
          dashPattern: picked ? [] : (isBreaker ? [6, 3] : []),
          lineWidth: picked ? 3 : 1,
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
      if (structureOn) {                      // item 10: market-structure markers
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
      // P2.20 + P2.22: sweep events, audit-pick highlighted
      const sweeps = (st.liquidity && st.liquidity.sweeps) || [];
      sweeps.forEach((sw, i) => {
        const picked = !!(auditPick && auditPick.kind === "sweep" &&
                          auditPick.index === i);
        marks.push({
          time: toTime(sw.ts),
          position: sw.side === "HIGH" ? "aboveBar" : "belowBar",
          color: picked ? COLORS.highlight : COLORS.sweep,
          shape: sw.side === "HIGH" ? "arrowDown" : "arrowUp",
          size: picked ? 1.0 : 0.6,
          text: (picked ? "* " : "") + "SWEEP " + sw.target,
        });
      });
    }
    marks.sort((a, b) => a.time - b.time);
    markers.setMarkers(marks);

    if (trendEl) trendEl.textContent = "trend: " + ((structure && structure.trend) || "—");
  }

  /* --------------------------------------------------------- audit tool */

  function tallyText() {
    const t = tally.trendline, s = tally.sweep, o = tally.ob;
    return "TL " + t.accept + "/" + t.reject + " (" + (t.accept + t.reject) + ")" +
      "  SWP " + s.accept + "/" + s.reject + " (" + (s.accept + s.reject) + ")" +
      "  OB " + o.accept + "/" + o.reject + " (" + (o.accept + o.reject) + ")";
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
    const index = Math.floor(Math.random() * lines.length);
    auditPick = { kind: "trendline", index };
    const ln = lines[index];
    jumpToWindow(toTime(ln.x1), toTime(ln.x2), TRENDLINE_MIN_PAD_S);
    setAuditButtons();
    redraw();
  }

  function pickRandomSweep() {
    const sweeps = (structure && structure.liquidity && structure.liquidity.sweeps) || [];
    if (!sweeps.length) return;
    const index = Math.floor(Math.random() * sweeps.length);
    auditPick = { kind: "sweep", index };
    const t = toTime(sweeps[index].ts);
    jumpToWindow(t, t, AUDIT_JUMP_WINDOW_S);
    setAuditButtons();
    redraw();
  }

  function pickRandomOB() {
    const ob = (structure && structure.orderblocks) || {};
    const zones = (ob.blocks || []).concat(ob.breakers || []);
    if (!zones.length) return;
    const index = Math.floor(Math.random() * zones.length);
    auditPick = { kind: "ob", index };
    const t = toTime(zones[index].created_ts);
    jumpToWindow(t, t, AUDIT_JUMP_WINDOW_S);
    setAuditButtons();
    redraw();
  }

  function vote(result) {
    if (auditPick === null) return;
    tally[auditPick.kind][result] += 1;
    auditPick = null;
    setAuditButtons();
    redraw();
  }

  // P2.22: is the current pick still present in a fresh payload?
  function auditPickStillValid(st) {
    if (!auditPick || !st) return false;
    if (auditPick.kind === "trendline") {
      return auditPick.index < (st.trendlines || []).length;
    }
    if (auditPick.kind === "sweep") {
      return auditPick.index <
        ((st.liquidity && st.liquidity.sweeps) || []).length;
    }
    if (auditPick.kind === "ob") {
      const ob = st.orderblocks || {};
      return auditPick.index < (ob.blocks || []).length + (ob.breakers || []).length;
    }
    return false;
  }

  /* ---- setup annotations (Step 6). The ONLY thing that draws on a clean
   * execution chart, and only when there's a CONFIRMED recommendation: its
   * entry / SL / TP as price lines. Independent of the structure toggle. ---- */
  let setupLines = [];
  let setupKey = null;
  function clearSetup() {
    for (const pl of setupLines) { try { series.removePriceLine(pl); } catch (e) { /* gone */ } }
    setupLines = [];
  }
  function setSetup(rec) {
    const key = rec ? [rec.id, rec.entry, rec.sl, rec.tp1, rec.tp2].join("|") : null;
    if (key === setupKey) return;         // unchanged -> don't churn price lines
    setupKey = key;
    clearSetup();
    if (!rec) return;
    const add = (price, color, title) => {
      if (price == null) return;
      setupLines.push(series.createPriceLine({
        price, color, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title,
      }));
    };
    add(rec.entry, COLORS.highlight, "Entry");
    add(rec.sl, COLORS.resistance, "SL");
    add(rec.tp1, COLORS.support, "TP1");
    add(rec.tp2, COLORS.support, "TP2");
  }

  /* -------------------------------------------------------------- API */

  return {
    setSetup,
    init(mainChart, mainSeries) {
      chart = mainChart;
      series = mainSeries;
      primitive = new LinesPrimitive();
      series.attachPrimitive(primitive);
      boxesPrimitive = new BoxesPrimitive();
      series.attachPrimitive(boxesPrimitive);
      shadingPrimitive = new ShadingPrimitive();
      series.attachPrimitive(shadingPrimitive);
      markers = LightweightCharts.createSeriesMarkers(series, []);
      trendEl = document.getElementById("trend-state");
      document.getElementById("audit-pick")
        .addEventListener("click", pickRandomLine);
      document.getElementById("audit-pick-sweep")
        .addEventListener("click", pickRandomSweep);
      document.getElementById("audit-pick-ob")
        .addEventListener("click", pickRandomOB);
      document.getElementById("audit-accept")
        .addEventListener("click", () => vote("accept"));
      document.getElementById("audit-reject")
        .addEventListener("click", () => vote("reject"));
      setAuditButtons();
    },
    setStructure(payload, closePrice) {   // latest payload for the ACTIVE symbol
      structure = payload || null;
      // P2.21: the latest close, used only for the premium/discount
      // split — a single current-value slot (mirrors `structure` itself),
      // never accumulated; omitted -> shading absent for this render.
      lastClose = (typeof closePrice === "number") ? closePrice : null;
      if (auditPick !== null && !auditPickStillValid(structure)) {
        auditPick = null;       // the picked object no longer exists
        setAuditButtons();
      }
      redraw();
    },
    setStructureVisible(on) {            // item 10: toggle HH/HL/LH/LL/BOS/CHOCH
      structureOn = !!on;
      redraw();
    },
  };
})();

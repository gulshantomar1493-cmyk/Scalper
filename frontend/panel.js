/* MarketScalper quality panel — the v3 right rail (Phase 2 Step 2; §9).
 *
 * Pure consumer: it renders values the backend already computed (qualification
 * verdict/score/gates/components, the §7 plan numbers, the §8 rule-trace) and
 * performs NO engine math. Every displayed number arrives in the WS payload;
 * the panel only formats and positions. Backend strings go through textContent
 * (never innerHTML). Global `Panel`, wired thinly from app.js.
 *
 * v3 layout: three compact cards — Recommendation (dir · grade · % · stars ·
 * verdict, with a collapsible gates+reason "Why?"), Trade Plan, and Market
 * Context (trend + the four weighted components). On 15m+ the analysis cards
 * are hidden and a "market context only" card is shown (the backend has no
 * higher-TF analysis — never fabricated). */

"use strict";

const Panel = (function () {
  const COMPONENTS = [
    { key: "structure", label: "Structure", weight: "0.30" },
    { key: "liquidity", label: "Liquidity", weight: "0.30" },
    { key: "volume", label: "Volume", weight: "0.25" },
    { key: "momentum", label: "Momentum", weight: "0.15" },
  ];
  const GATE_NAMES = ["G1", "G2", "G3", "G4", "G5", "G6"];
  const VERDICT_CLASS = {
    A_PLUS: "v-aplus", TRADEABLE: "v-tradeable",
    BELOW_THRESHOLD: "v-below", NO_SIGNAL: "v-none",
  };
  const GRADE = { A_PLUS: "A+", TRADEABLE: "A", BELOW_THRESHOLD: "B", NO_SIGNAL: "—" };
  const INVALID_AFTER_DEFAULT = 5;

  let el = {};
  let lastCandleTs = null;
  let onQuickLog = null;
  let lastPlanKey = null;

  function init(quickLogSubmit) {
    onQuickLog = quickLogSubmit || null;
    el = {
      panel: document.getElementById("quality-panel"),
      analysis: document.getElementById("rail-analysis"),
      ctxonly: document.getElementById("rail-ctxonly"),
      recoDir: document.getElementById("reco-dir"),
      recoGrade: document.getElementById("reco-grade"),
      recoPct: document.getElementById("reco-pct"),
      recoStars: document.getElementById("reco-stars"),
      recoVerdict: document.getElementById("reco-verdict"),
      recoNote: document.getElementById("reco-note"),
      recoWhy: document.getElementById("reco-why"),
      ctxTrend: document.getElementById("ctx-trend"),
      components: document.getElementById("panel-components"),
      plan: document.getElementById("panel-plan"),
      ctxonlyTf: document.getElementById("ctxonly-tf"),
      ctxonlyTrend: document.getElementById("ctxonly-trend"),
    };
  }

  /* -------------------------------------------------------- small helpers */
  function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }
  function elem(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function num(v, digits) {
    if (v === null || v === undefined) return "—";
    return Number(v).toLocaleString("en-US",
      { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }

  /* ------------------------------------------------------- context mode */
  // Called by app.js on a timeframe switch. Analysis timeframes (1m/5m) show
  // the three cards; higher timeframes show only the context card. The trend
  // string is the backend's (cached) — never recomputed here.
  function setContextMode(isAnalysisTf, tf, trend) {
    if (!el.analysis || !el.ctxonly) return;
    el.analysis.hidden = !isAnalysisTf;
    el.ctxonly.hidden = isAnalysisTf;
    if (!isAnalysisTf) {
      if (el.ctxonlyTf) el.ctxonlyTf.textContent = tf || "—";
      if (el.ctxonlyTrend) {
        el.ctxonlyTrend.textContent = "Trend: " + (trend || "—");
        el.ctxonlyTrend.className = "lv-lab " + trendClass(trend);
      }
      if (el.panel) el.panel.classList.remove("empty");
    }
  }

  function trendClass(t) {
    return t === "BULLISH" ? "up" : t === "BEARISH" ? "down" : "";
  }

  // Higher-timeframe CONTEXT (chart UX item 9) — backend-computed, rendered
  // here so 15m..1D never show "analysis unavailable". Display-only context.
  function ctxTrendClass(t) {
    return t === "Bullish" ? "up" : t === "Bearish" ? "down" : "";
  }
  function setContext(ctx) {
    const body = document.getElementById("ctxonly-body");
    if (!body) return;
    body.textContent = "";
    if (!ctx) return;
    if (el.ctxonlyTrend) {
      el.ctxonlyTrend.textContent = "Trend: " + (ctx.trend || "—");
      el.ctxonlyTrend.className = "lv-lab " + ctxTrendClass(ctx.trend);
    }
    const nm = (v) => (v == null ? "—" : Number(v).toLocaleString("en-US", { maximumFractionDigits: 2 }));
    const rows = [
      ["EMA Alignment", ctx.ema_alignment || "—"],
      ["RSI", ctx.rsi == null ? "—" : String(ctx.rsi)],
      ["Support", nm(ctx.support)],
      ["Resistance", nm(ctx.resistance)],
      ["Bias", ctx.bias || "—"],
    ];
    for (const [k, v] of rows) {
      const r = document.createElement("div"); r.className = "ctx-row";
      const a = document.createElement("span"); a.className = "ctx-k"; a.textContent = k;
      const b = document.createElement("span"); b.className = "ctx-v"; b.textContent = v;
      r.appendChild(a); r.appendChild(b); body.appendChild(r);
    }
    const ex = document.createElement("div"); ex.className = "ctx-exec";
    ex.textContent = "▸ " + (ctx.execution || "Wait for confirmation on 1m / 5m.");
    body.appendChild(ex);
  }

  /* ---------------------------------------------------------- rendering */
  function setStructure(structure, candleTs) {
    if (candleTs) lastCandleTs = candleTs;
    if (!structure || !structure.qualification) {
      if (el.panel) el.panel.classList.add("empty");
      return;
    }
    if (el.panel) el.panel.classList.remove("empty");
    const q = structure.qualification;
    renderReco(q, structure);
    renderComponents(q.components, structure.trend);
    renderPlan(structure.recommendations || []);
  }

  function recoDirection(structure) {
    const recs = structure.recommendations || [];
    if (recs.length) return recs[recs.length - 1].direction;
    const sigs = structure.signals || [];
    if (sigs.length) return sigs[sigs.length - 1].direction;
    return null;
  }

  function renderReco(q, structure) {
    const verdict = q.verdict || "NO_SIGNAL";
    const scored = typeof q.score === "number";
    const dir = recoDirection(structure);
    if (el.recoDir) {
      el.recoDir.textContent = dir || "—";
      el.recoDir.className = "lv-dir " + (dir === "LONG" ? "up" : dir === "SHORT" ? "down" : "");
    }
    if (el.recoGrade) el.recoGrade.textContent = GRADE[verdict] || "—";
    if (el.recoPct) el.recoPct.textContent = scored ? Math.round(q.score) + "%" : "—";
    if (el.recoStars) {
      const n = scored ? Math.max(0, Math.min(5, Math.round(q.score / 20))) : 0;
      el.recoStars.textContent = "★★★★★".slice(0, n) + "☆☆☆☆☆".slice(0, 5 - n);
    }
    if (el.recoVerdict) {
      el.recoVerdict.textContent = verdict.replace("_", " ");
      el.recoVerdict.className = "lv-pill " + (VERDICT_CLASS[verdict] || "");
    }
    if (el.recoNote) {
      el.recoNote.textContent = q.agreement
        || (q.data_integrity === "PASS" ? "" : "data integrity: DEGRADED");
    }
    renderWhy(q, structure.signals || []);
  }

  // Gates + §8 rule-trace, folded into the collapsible "Why?" (kept accessible
  // but out of the beginner's default view).
  function renderWhy(q, signals) {
    if (!el.recoWhy) return;
    clear(el.recoWhy);
    const byName = {};
    for (const g of (q.gates || [])) byName[g.name] = g;
    const gates = elem("div", "why-gates");
    for (const name of GATE_NAMES) {
      const g = byName[name];
      const chip = elem("span", "why-gate " + (g && g.passed ? "pass" : "fail"),
        name + (g && g.passed ? " ✓" : " ✗"));
      if (g && g.flagged) chip.classList.add("prov");
      gates.appendChild(chip);
    }
    el.recoWhy.appendChild(gates);
    if (signals.length) {
      const s = signals[signals.length - 1];
      const h = elem("div", "why-sig");
      h.appendChild(elem("span", "why-strat", s.strategy || ""));
      h.appendChild(elem("span", "why-dir " + (s.direction === "LONG" ? "up" : "down"), s.direction || ""));
      el.recoWhy.appendChild(h);
      for (const f of (s.facts || [])) el.recoWhy.appendChild(elem("div", "why-fact", "• " + f));
    }
    for (const line of (q.reasons || [])) {
      const cls = line.charAt(0) === "✗" ? "why-line fail" : "why-line pass";
      el.recoWhy.appendChild(elem("div", cls, line));
    }
  }

  function renderComponents(components, trend) {
    if (el.ctxTrend) {
      el.ctxTrend.textContent = trend || "—";
      el.ctxTrend.className = "lv-trend " + trendClass(trend);
    }
    clear(el.components);
    if (!el.components) return;
    for (const c of COMPONENTS) {
      const value = components ? components[c.key] : null;
      const row = elem("div", "comp-row");
      const head = elem("div", "comp-head");
      head.appendChild(elem("span", "comp-label", c.label));
      head.appendChild(elem("span", "comp-weight", "×" + c.weight));
      head.appendChild(elem("span", "comp-value mono",
        value === null || value === undefined ? "—" : num(value, 0)));
      const track = elem("div", "comp-track");
      const fill = elem("div", "comp-fill");
      fill.style.width = (value ? Math.max(0, Math.min(100, value)) : 0) + "%";
      track.appendChild(fill);
      row.appendChild(head); row.appendChild(track);
      el.components.appendChild(row);
    }
  }

  function renderPlan(recommendations) {
    if (!el.plan) return;
    if (!recommendations.length) {
      lastPlanKey = null; clear(el.plan);
      el.plan.appendChild(elem("div", "plan-empty", "No active plan — no recommendation at threshold."));
      return;
    }
    const r = recommendations[recommendations.length - 1];
    const key = (r.id != null ? r.id : r.created_ts) + "|" + (r.status || "");
    if (key === lastPlanKey) {
      const t = el.plan.querySelector(".plan-timer");
      const text = invalidationTimer(r, r.status || "active");
      if (t && text) t.textContent = text;
      return;
    }
    lastPlanKey = key;
    clear(el.plan);
    const head = elem("div", "plan-head");
    head.appendChild(elem("span", "plan-status " + statusClass(r.status || "active"), r.status || "active"));
    const timer = invalidationTimer(r, r.status || "active");
    if (timer) head.appendChild(elem("span", "plan-timer", timer));
    el.plan.appendChild(head);

    const rail = elem("div", "plan-rail");
    railRow(rail, "Entry", num(r.entry, 2));
    railRow(rail, "Stop Loss", num(r.sl, 2), "stop");
    railRow(rail, "Target 1", num(r.tp1, 2), "tp");
    if (r.tp2 !== null && r.tp2 !== undefined) railRow(rail, "Target 2", num(r.tp2, 2), "tp");
    const rr = elem("div", "rail-row");
    rr.appendChild(elem("span", "rail-label", "Risk : Reward"));
    rr.appendChild(elem("span", "rail-rr", "1 : " + num(r.net_rr_tp1, 2)));
    rail.appendChild(rr);
    el.plan.appendChild(rail);

    if (r.eval_outcome !== undefined && r.eval_outcome !== null) {
      const ev = elem("div", "plan-eval");
      ev.appendChild(elem("span", "eval-label", "Hypothetical"));
      ev.appendChild(elem("span", "eval-outcome " + (r.eval_r >= 0 ? "tp" : "stop"), r.eval_outcome.toUpperCase()));
      ev.appendChild(elem("span", "eval-r mono", (r.eval_r >= 0 ? "+" : "") + num(r.eval_r, 2) + "R"));
      el.plan.appendChild(ev);
    }
    if (r.guidance && r.guidance.length) {
      const g = elem("div", "plan-guidance");
      g.appendChild(elem("div", "guidance-head", "Suggested management"));
      for (const line of r.guidance) g.appendChild(elem("div", "guidance-line", line));
      el.plan.appendChild(g);
    }
    el.plan.appendChild(elem("div", "plan-disclaimer", "Display only — you place any order manually on your exchange."));
    if (r.id != null && onQuickLog) el.plan.appendChild(quickLogForm(r));
  }

  function statusClass(s) {
    return { active: "st-active", evaluated: "st-evaluated",
      invalidated: "st-invalidated", expired: "st-expired" }[s] || "";
  }

  /* ---- quick-log form (§8 manual outcome; PATCHes via the app.js callback) ---- */
  function quickLogForm(r) {
    const form = elem("div", "quicklog");
    form.appendChild(elem("div", "quicklog-head", "Quick log"));
    const state = { taken: null, result: null };
    const takenRow = elem("div", "ql-row");
    const takenBtns = {};
    for (const [label, val] of [["Taken", true], ["Skipped", false]]) {
      const b = elem("button", "ql-toggle", label); b.type = "button";
      b.onclick = () => {
        state.taken = val;
        for (const k in takenBtns) takenBtns[k].classList.toggle("on", takenBtns[k] === b);
        resultRow.style.display = val ? "flex" : "none";
      };
      takenBtns[label] = b; takenRow.appendChild(b);
    }
    form.appendChild(takenRow);
    const resultRow = elem("div", "ql-row"); resultRow.style.display = "none";
    const resultBtns = {};
    for (const val of ["win", "loss", "be"]) {
      const b = elem("button", "ql-result r-" + val, val.toUpperCase()); b.type = "button";
      b.onclick = () => { state.result = val; for (const k in resultBtns) resultBtns[k].classList.toggle("on", resultBtns[k] === b); };
      resultBtns[val] = b; resultRow.appendChild(b);
    }
    form.appendChild(resultRow);
    const entryIn = qlInput(form, "Actual entry", "number");
    const exitIn = qlInput(form, "Actual exit", "number");
    const notesIn = qlInput(form, "Notes", "text");
    const tagsIn = qlInput(form, "Tags (comma-separated)", "text");
    const submit = elem("button", "ql-submit", "Log outcome"); submit.type = "button";
    const status = elem("span", "ql-status", "");
    submit.onclick = async () => {
      const fields = { taken: state.taken };
      if (state.taken && state.result) fields.result = state.result;
      if (entryIn.value !== "") fields.actual_entry = Number(entryIn.value);
      if (exitIn.value !== "") fields.actual_exit = Number(exitIn.value);
      if (notesIn.value !== "") fields.notes = notesIn.value;
      if (tagsIn.value.trim() !== "") fields.tags = tagsIn.value.split(",").map((t) => t.trim()).filter((t) => t);
      status.textContent = "saving…";
      try { await onQuickLog(r.id, fields); status.textContent = "logged ✓"; }
      catch (err) { status.textContent = "error — retry"; }
    };
    const foot = elem("div", "ql-foot"); foot.appendChild(submit); foot.appendChild(status);
    form.appendChild(foot);
    return form;
  }
  function qlInput(form, placeholder, type) {
    const row = elem("div", "ql-field");
    const input = document.createElement("input");
    input.type = type; input.placeholder = placeholder; input.className = "ql-input mono";
    if (type === "number") input.step = "any";
    row.appendChild(input); form.appendChild(row); return input;
  }

  function invalidationTimer(r, status) {
    if (status !== "active" || !lastCandleTs || !r.created_ts) return null;
    const window = r.invalid_after_bars || INVALID_AFTER_DEFAULT;
    const elapsed = Math.floor((Date.parse(lastCandleTs) - Date.parse(r.created_ts)) / 60000);
    const left = window - elapsed;
    if (left <= 0) return "entry window elapsed";
    return "entry window: " + left + " candle" + (left === 1 ? "" : "s") + " left";
  }
  function railRow(rail, label, value, cls) {
    const row = elem("div", "rail-row");
    row.appendChild(elem("span", "rail-label", label));
    row.appendChild(elem("span", "rail-value mono " + (cls || ""), value));
    rail.appendChild(row);
  }

  return { init, setStructure, setContextMode, setContext };
})();

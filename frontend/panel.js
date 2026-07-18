/* MarketScalper quality panel (roadmap P3.19; Architecture §9 right rail).
 *
 * Pure consumer — exactly the overlays.js contract: it renders values the
 * backend already computed (qualification score/gates/components, the §7
 * plan numbers, the §8 rule-trace) and performs NO engine math. Every
 * displayed number arrives in the WS payload; the panel only formats and
 * positions. Backend strings are written via textContent (never innerHTML)
 * so a reason/detail line can never inject markup.
 *
 * Global `Panel`, mirroring `Overlays`. Wired thinly from app.js.
 */

"use strict";

const Panel = (function () {
  /* --------------------------------------------------------- gauge geometry */
  // Display-only: a ring whose filled fraction is score/100. The score is a
  // backend value; dividing by the 100-point scale to fill an arc is the same
  // class of pure rendering as placing a box at a backend coordinate.
  const GAUGE_R = 52;
  const GAUGE_CIRC = 2 * Math.PI * GAUGE_R;
  const COUNT_UP_MS = 420;

  // Weights are the frozen §6 display labels only (never used to compute the
  // score — the backend owns that); shown so the rail is self-describing.
  const COMPONENTS = [
    { key: "structure", label: "Structure", weight: "0.30" },
    { key: "liquidity", label: "Liquidity", weight: "0.30" },
    { key: "volume", label: "Volume", weight: "0.25" },
    { key: "momentum", label: "Momentum", weight: "0.15" },
  ];
  const GATE_NAMES = ["G1", "G2", "G3", "G4", "G5", "G6"];

  let el = {};                 // resolved DOM slots
  let animHandle = null;       // count-up frame handle
  let shownScore = 0;          // last painted gauge value (for the count-up)

  function init() {
    el = {
      panel: document.getElementById("quality-panel"),
      arc: document.getElementById("gauge-arc"),
      score: document.getElementById("gauge-score"),
      verdict: document.getElementById("gauge-verdict"),
      integrity: document.getElementById("gauge-integrity"),
      agreement: document.getElementById("gauge-agreement"),
      gates: document.getElementById("panel-gates"),
      components: document.getElementById("panel-components"),
      plan: document.getElementById("panel-plan"),
      reasons: document.getElementById("panel-reasons"),
    };
    if (el.arc) {
      el.arc.style.strokeDasharray = String(GAUGE_CIRC);
      el.arc.style.strokeDashoffset = String(GAUGE_CIRC);
    }
  }

  /* --------------------------------------------------------- small helpers */

  function clear(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
  }

  function elem(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  // Fixed-precision price/number formatting is presentation only. Numbers are
  // shown as-is from the payload; we just pick a readable width.
  function num(v, digits) {
    if (v === null || v === undefined) return "—";
    return Number(v).toLocaleString("en-US", {
      minimumFractionDigits: digits, maximumFractionDigits: digits,
    });
  }

  const VERDICT_CLASS = {
    A_PLUS: "v-aplus",
    TRADEABLE: "v-tradeable",
    BELOW_THRESHOLD: "v-below",
    NO_SIGNAL: "v-none",
  };

  /* ------------------------------------------------------------- rendering */

  function setStructure(structure) {
    if (!structure || !structure.qualification) {
      if (el.panel) el.panel.classList.add("empty");
      return;
    }
    if (el.panel) el.panel.classList.remove("empty");
    const q = structure.qualification;
    renderGauge(q);
    renderGates(q.gates || []);
    renderComponents(q.components);
    renderPlan(structure.recommendations || []);
    renderReasons(q.reasons || [], structure.signals || []);
  }

  function renderGauge(q) {
    const scored = typeof q.score === "number";
    const target = scored ? q.score : 0;
    // fraction of the 100-point scale (display normalization, not a metric)
    const frac = Math.max(0, Math.min(1, target / 100));
    animateArc(scored ? target : 0, frac, scored);

    el.verdict.textContent = (q.verdict || "—").replace("_", " ");
    el.verdict.className = "gauge-verdict " + (VERDICT_CLASS[q.verdict] || "");
    el.integrity.textContent = q.data_integrity || "—";
    el.integrity.className =
      "gauge-integrity " + (q.data_integrity === "PASS" ? "ok" : "warn");
    el.agreement.textContent = q.agreement || "";
  }

  function animateArc(target, frac, scored) {
    if (animHandle) cancelAnimationFrame(animHandle);
    const from = shownScore;
    const start = performance.now();
    function frame(now) {
      const t = Math.min(1, (now - start) / COUNT_UP_MS);
      const eased = 1 - Math.pow(1 - t, 3);          // easeOutCubic (UI only)
      const value = from + (target - from) * eased;
      const f = Math.max(0, Math.min(1, value / 100));
      if (el.arc) el.arc.style.strokeDashoffset = String(GAUGE_CIRC * (1 - f));
      el.score.textContent = scored ? Math.round(value).toString() : "—";
      if (t < 1) {
        animHandle = requestAnimationFrame(frame);
      } else {
        shownScore = target;
        if (el.arc && !scored) el.arc.style.strokeDashoffset = String(GAUGE_CIRC);
      }
    }
    animHandle = requestAnimationFrame(frame);
  }

  function renderGates(gates) {
    clear(el.gates);
    const byName = {};
    for (const g of gates) byName[g.name] = g;
    for (const name of GATE_NAMES) {
      const g = byName[name];
      const row = elem("div", "gate-row");
      const mark = elem("span", "gate-mark " + (g && g.passed ? "pass" : "fail"),
        g && g.passed ? "✓" : "✗");
      const label = elem("span", "gate-name", name);
      const detail = elem("span", "gate-detail", g ? g.detail : "");
      row.appendChild(mark);
      row.appendChild(label);
      if (g && g.flagged) row.appendChild(elem("span", "gate-flag", "prov"));
      row.appendChild(detail);
      el.gates.appendChild(row);
    }
  }

  function renderComponents(components) {
    clear(el.components);
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
      // width % is the 0-100 component value as a bar length — presentation
      fill.style.width = (value ? Math.max(0, Math.min(100, value)) : 0) + "%";
      track.appendChild(fill);
      row.appendChild(head);
      row.appendChild(track);
      el.components.appendChild(row);
    }
  }

  function renderPlan(recommendations) {
    clear(el.plan);
    if (!recommendations.length) {
      el.plan.appendChild(elem("div", "plan-empty",
        "No active plan — no recommendation at threshold."));
      return;
    }
    const r = recommendations[recommendations.length - 1];  // most recent
    const head = elem("div", "plan-head");
    head.appendChild(elem("span", "plan-strategy", r.strategy || "—"));
    head.appendChild(elem("span",
      "plan-dir " + (r.direction === "LONG" ? "long" : "short"),
      r.direction || ""));
    if (typeof r.score === "number") {
      head.appendChild(elem("span", "plan-score mono", "score " + num(r.score, 0)));
    }
    el.plan.appendChild(head);

    const rail = elem("div", "plan-rail");
    railRow(rail, "Entry", num(r.entry, 2));
    railRow(rail, "Stop", num(r.sl, 2), "stop");
    railRow(rail, "TP1", num(r.tp1, 2), "tp");
    if (r.tp2 !== null && r.tp2 !== undefined) railRow(rail, "TP2", num(r.tp2, 2), "tp");
    railRow(rail, "Qty", num(r.qty, 4) + " (suggested)");
    railRow(rail, "Risk", num(r.risk_amt, 2));
    railRow(rail, "Net RR", num(r.net_rr_tp1, 2) +
      (r.net_rr_tp2 !== null && r.net_rr_tp2 !== undefined
        ? " / " + num(r.net_rr_tp2, 2) : ""));
    el.plan.appendChild(rail);

    if (r.guidance && r.guidance.length) {
      const g = elem("div", "plan-guidance");
      g.appendChild(elem("div", "guidance-head", "Suggested management"));
      for (const line of r.guidance) g.appendChild(elem("div", "guidance-line", line));
      el.plan.appendChild(g);
    }
    el.plan.appendChild(elem("div", "plan-disclaimer",
      "Display only — you place any order manually on your exchange."));
  }

  function railRow(rail, label, value, cls) {
    const row = elem("div", "rail-row");
    row.appendChild(elem("span", "rail-label", label));
    row.appendChild(elem("span", "rail-value mono " + (cls || ""), value));
    rail.appendChild(row);
  }

  function renderReasons(reasons, signals) {
    clear(el.reasons);
    if (signals.length) {
      const s = signals[signals.length - 1];
      const head = elem("div", "reason-signal");
      head.appendChild(elem("span", "reason-strategy", s.strategy || ""));
      head.appendChild(elem("span",
        "reason-dir " + (s.direction === "LONG" ? "long" : "short"),
        s.direction || ""));
      el.reasons.appendChild(head);
      for (const fact of (s.facts || [])) {
        el.reasons.appendChild(elem("div", "reason-fact", "• " + fact));
      }
    }
    for (const line of reasons) {
      const cls = line.charAt(0) === "✗" ? "reason-line fail" : "reason-line pass";
      el.reasons.appendChild(elem("div", cls, line));
    }
    if (!signals.length && !reasons.length) {
      el.reasons.appendChild(elem("div", "reason-empty", "No rule trace yet."));
    }
  }

  return { init, setStructure };
})();

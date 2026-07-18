/* MarketScalper analytics dashboard + journal tab (roadmap P4.12; §9/§11).
 *
 * Pure consumer — the overlays.js/panel.js contract: it renders the data
 * app.js already fetched from GET /analytics and GET /journal and performs
 * NO network and NO engine math. Backend strings go through textContent
 * (never innerHTML). app.js owns the fetch + the refresh trigger.
 *
 * Global `Dashboard`. Two tabs: Analytics (win rate / expectancy /
 * system-vs-actual, overall + per strategy + per session) and Journal
 * (recent recommendations with the rule-trace, outcome and manual log).
 */

"use strict";

const Dashboard = (function () {
  let el = {};
  let tab = "analytics";

  function init() {
    el = {
      overlay: document.getElementById("dashboard"),
      analytics: document.getElementById("dash-analytics"),
      journal: document.getElementById("dash-journal"),
      tabAnalytics: document.getElementById("dash-tab-analytics"),
      tabJournal: document.getElementById("dash-tab-journal"),
      close: document.getElementById("dash-close"),
    };
    if (el.tabAnalytics) el.tabAnalytics.addEventListener("click", () => setTab("analytics"));
    if (el.tabJournal) el.tabJournal.addEventListener("click", () => setTab("journal"));
    if (el.close) el.close.addEventListener("click", hide);
  }

  function show() { if (el.overlay) el.overlay.classList.add("open"); }
  function hide() { if (el.overlay) el.overlay.classList.remove("open"); }

  function setTab(which) {
    tab = which;
    el.analytics.style.display = which === "analytics" ? "block" : "none";
    el.journal.style.display = which === "journal" ? "block" : "none";
    el.tabAnalytics.classList.toggle("active", which === "analytics");
    el.tabJournal.classList.toggle("active", which === "journal");
  }

  /* ------------------------------------------------------------- helpers */

  function clear(n) { while (n && n.firstChild) n.removeChild(n.firstChild); }

  function elem(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function pct(v) { return v === null || v === undefined ? "—" : (v * 100).toFixed(0) + "%"; }
  function r(v) { return v === null || v === undefined ? "—" : (v >= 0 ? "+" : "") + v.toFixed(2) + "R"; }
  function px(v) { return v === null || v === undefined ? "—"
    : Number(v).toLocaleString("en-US", { maximumFractionDigits: 2 }); }

  /* ----------------------------------------------------------- analytics */

  function render(analytics, journal) {
    renderAnalytics(analytics);
    renderJournal(journal || []);
    setTab(tab);
  }

  function renderAnalytics(a) {
    clear(el.analytics);
    if (!a || !a.n_recommendations) {
      el.analytics.appendChild(elem("div", "dash-empty",
        "No recommendations logged yet."));
      return;
    }
    el.analytics.appendChild(elem("div", "dash-count",
      a.n_recommendations + " recommendation" +
      (a.n_recommendations === 1 ? "" : "s")));
    el.analytics.appendChild(statBlock("Overall", a.overall));
    el.analytics.appendChild(statTable("By strategy", a.by_strategy));
    el.analytics.appendChild(statTable("By session", a.by_session));
  }

  function statBlock(title, s) {
    const wrap = elem("div", "stat-block");
    wrap.appendChild(elem("h3", "dash-h3", title));
    const grid = elem("div", "stat-grid");
    stat(grid, "Hypothetical win", pct(s.hypothetical.win_rate));
    stat(grid, "Hypothetical exp.", r(s.hypothetical.expectancy));
    stat(grid, "Manual win", pct(s.manual.win_rate));
    stat(grid, "Manual exp.", r(s.manual.expectancy));
    stat(grid, "Sys vs actual Δ", r(s.system_vs_actual.delta));
    stat(grid, "Avg MFE / MAE",
      r(s.hypothetical.avg_mfe) + " / " + r(s.hypothetical.avg_mae));
    wrap.appendChild(grid);
    return wrap;
  }

  function stat(grid, label, value) {
    const cell = elem("div", "stat-cell");
    cell.appendChild(elem("div", "stat-label", label));
    cell.appendChild(elem("div", "stat-value mono", value));
    grid.appendChild(cell);
  }

  function statTable(title, groups) {
    const wrap = elem("div", "stat-block");
    wrap.appendChild(elem("h3", "dash-h3", title));
    const table = elem("table", "dash-table");
    const head = elem("tr");
    for (const h of ["", "n", "Hyp win", "Hyp exp", "Man win", "Man exp", "Δ"]) {
      head.appendChild(elem("th", null, h));
    }
    table.appendChild(head);
    for (const key of Object.keys(groups)) {
      const s = groups[key];
      const row = elem("tr");
      row.appendChild(elem("td", "row-key", key));
      row.appendChild(elem("td", "mono", String(s.n)));
      row.appendChild(elem("td", "mono", pct(s.hypothetical.win_rate)));
      row.appendChild(elem("td", "mono", r(s.hypothetical.expectancy)));
      row.appendChild(elem("td", "mono", pct(s.manual.win_rate)));
      row.appendChild(elem("td", "mono", r(s.manual.expectancy)));
      row.appendChild(elem("td", "mono", r(s.system_vs_actual.delta)));
      table.appendChild(row);
    }
    wrap.appendChild(table);
    return wrap;
  }

  /* ------------------------------------------------------------- journal */

  function renderJournal(list) {
    clear(el.journal);
    if (!list.length) {
      el.journal.appendChild(elem("div", "dash-empty", "No journal entries yet."));
      return;
    }
    for (const j of list) el.journal.appendChild(journalCard(j));
  }

  function journalCard(j) {
    const card = elem("div", "jcard");
    const head = elem("div", "jcard-head");
    head.appendChild(elem("span", "jcard-strategy", j.strategy || "—"));
    head.appendChild(elem("span",
      "jcard-dir " + (j.direction === "LONG" ? "long" : "short"),
      j.direction || ""));
    head.appendChild(elem("span", "jcard-status", j.status || ""));
    head.appendChild(elem("span", "jcard-ts mono", (j.ts || "").slice(0, 16)));
    card.appendChild(head);

    const rail = elem("div", "jcard-rail mono");
    rail.appendChild(elem("span", null, "E " + px(j.entry)));
    rail.appendChild(elem("span", "stop", "SL " + px(j.sl)));
    rail.appendChild(elem("span", "tp", "TP " + px(j.tp1)));
    card.appendChild(rail);

    // outcomes: hypothetical + manual
    const out = elem("div", "jcard-out");
    if (j.eval_outcome) {
      out.appendChild(elem("span", "j-eval",
        "sys " + j.eval_outcome.toUpperCase() + " " + r(j.eval_r)));
    }
    if (j.taken === true && j.result) {
      out.appendChild(elem("span", "j-manual j-" + j.result,
        "you " + j.result.toUpperCase() +
        (j.actual_r !== null && j.actual_r !== undefined
          ? " " + r(j.actual_r) : "")));
    } else if (j.taken === false) {
      out.appendChild(elem("span", "j-manual", "skipped"));
    }
    if (out.childNodes.length) card.appendChild(out);

    if (j.tags && j.tags.length) {
      const tags = elem("div", "jcard-tags");
      for (const t of j.tags) tags.appendChild(elem("span", "jtag", t));
      card.appendChild(tags);
    }
    if (j.notes) card.appendChild(elem("div", "jcard-notes", j.notes));
    if (j.reason_text) {
      const rt = elem("details", "jcard-trace");
      rt.appendChild(elem("summary", null, "rule trace"));
      const pre = elem("pre", "jtrace-pre", j.reason_text);
      rt.appendChild(pre);
      card.appendChild(rt);
    }
    return card;
  }

  return { init, show, hide, render };
})();

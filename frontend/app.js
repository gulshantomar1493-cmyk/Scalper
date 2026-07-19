/* MarketScalper frontend — v3 Live terminal (Phase 2 Step 2) + Replay page (Step 3).
 *
 * Thin client, exactly per §9: the backend owns ALL data correctness. This file
 * is the ONLY one that does fetch / WebSocket. It NEVER aggregates candles,
 * builds candles, computes indicators, or caches a candle history — history for
 * ANY timeframe comes from GET /api/chart (the backend ChartService), and live
 * 1m/5m candles apply as diff-only series.update() calls. Higher timeframes
 * (15m+) are candle-only market context — the backend has no analysis for them,
 * so overlays and the analysis rail are hidden (never fabricated).
 *
 * Vanilla JS. No frameworks. Token lives in memory only. UI prefs (theme,
 * beginner, last timeframe) are persisted by ui.js — this file is storage-free.
 */

"use strict";

/* ---------------------------------------------------------- configuration */

const params = new URLSearchParams(window.location.search);
const API_HOST = params.get("api") || window.location.host || "127.0.0.1:8000";
const TOKEN = params.get("token") || window.prompt("MarketScalper API token") || "";

// The API scheme follows the page's scheme: a page served over HTTPS MUST call
// the API over HTTPS/WSS — browsers block mixed http/ws content. So a same-origin
// deployment behind a TLS reverse proxy "just works" (open https://host/, no
// params), while plain-http local dev (?api=127.0.0.1:8000) is unchanged.
const SECURE = window.location.protocol === "https:";
const HTTP_BASE = `${SECURE ? "https" : "http"}://${API_HOST}`;
const WS_BASE = `${SECURE ? "wss" : "ws"}://${API_HOST}`;

const SYMBOLS = ["BTCUSDT", "ETHUSDT"];          // frozen v1 pair (§0)
const ANALYSIS_TFS = ["1m", "5m"];               // only these carry engine analysis
const LOOKBACK_MS = 24 * 3600 * 1000;            // history bootstrap depth (1m/5m)
// coarser TFs need a wider window to show enough closed buckets
const LOOKBACK_BY_TF = {
  "1m": 24 * 3600e3, "5m": 3 * 24 * 3600e3, "15m": 7 * 24 * 3600e3,
  "30m": 14 * 24 * 3600e3, "1h": 30 * 24 * 3600e3, "4h": 90 * 24 * 3600e3,
  "1d": 365 * 24 * 3600e3, "1w": 2 * 365 * 24 * 3600e3, "1M": 5 * 365 * 24 * 3600e3,
};

const urlSymbol = (params.get("symbol") || "").toUpperCase();
let activeSymbol = SYMBOLS.includes(urlSymbol) ? urlSymbol : SYMBOLS[0];
let activeTf = (window.__msTf && LOOKBACK_BY_TF[window.__msTf]) ? window.__msTf : "1m";

const $ = (id) => document.getElementById(id);
const isAnalysisTf = (tf) => ANALYSIS_TFS.indexOf(tf) >= 0;

/* ----------------------------------------------------------------- charts */

const SERIES_OPTS = {
  upColor: "#22C55E", downColor: "#EF4444",       // semantic token colors
  wickUpColor: "#22C55E", wickDownColor: "#EF4444", borderVisible: false,
};

function chartTheme() {
  const cs = getComputedStyle(document.documentElement);
  const v = (name, fb) => (cs.getPropertyValue(name).trim() || fb);
  return {
    layout: {
      background: { color: v("--chart-bg", "#0A0F1E") },
      textColor: v("--chart-text", "#8B93A7"),
      fontFamily: 'ui-monospace, "JetBrains Mono", "SF Mono", Consolas, monospace',
    },
    grid: {
      vertLines: { color: v("--chart-grid", "rgba(255,255,255,0.06)") },
      horzLines: { color: v("--chart-grid", "rgba(255,255,255,0.06)") },
    },
    rightPriceScale: { borderColor: v("--chart-border", "rgba(255,255,255,0.14)") },
    // Axis + crosshair rendered in IST. The chart's time MODEL stays UTC
    // (values fed to setData/update are unchanged) — only the labels convert,
    // so ranges, live updates and overlays keep working on true UTC.
    localization: { timeFormatter: (t) => window.IST.crosshair(t) },
    timeScale: {
      borderColor: v("--chart-border", "rgba(255,255,255,0.14)"), timeVisible: true,
      tickMarkFormatter: (t, tt) => window.IST.tick(t, tt),
    },
  };
}
function makeChart(el) {
  return LightweightCharts.createChart(el, Object.assign({ autoSize: true }, chartTheme()));
}

const mainChart = makeChart($("chart"));
const mainSeries = mainChart.addSeries(LightweightCharts.CandlestickSeries, SERIES_OPTS);
Overlays.init(mainChart, mainSeries);            // overlays draw on the Live chart
Panel.init(quickLogSubmit);
Dashboard.init();
const lastStructure = {};                        // latest engine payload per symbol

// A dedicated, candle-only chart for the Replay page (Step 3). Overlays/analysis
// on the replay chart are a documented follow-up; the replay page shows the
// deterministic price replay + progress (the engine still runs server-side).
// Created once (never recreated). Guarded: the replay page is hidden at load,
// so a 0-size-init hiccup must never take down the Live page.
const replayEl = $("replay-chart");
let replayChart = null, replaySeries = null;
try {
  if (replayEl) {
    replayChart = makeChart(replayEl);
    replaySeries = replayChart.addSeries(LightweightCharts.CandlestickSeries, SERIES_OPTS);
  }
} catch (e) { console.warn("replay chart init deferred:", e && e.message); }

window.addEventListener("ms-theme-change", function () {
  const t = chartTheme();
  mainChart.applyOptions(t);
  if (replayChart) replayChart.applyOptions(t);
});

function toBar(c) {
  return { time: Math.floor(Date.parse(c.ts) / 1000), open: c.o, high: c.h, low: c.l, close: c.c };
}

/* -------------------------------------------------- backend chart history */

const lastEvent = $("last-event");
function note(text) { if (lastEvent) lastEvent.textContent = text; }

// GET /api/chart — the backend ChartService aggregates canonical 1m -> tf.
async function fetchChart(symbol, tf) {
  const end = new Date();
  const start = new Date(end.getTime() - (LOOKBACK_BY_TF[tf] || LOOKBACK_MS));
  const qs = new URLSearchParams({
    symbol, timeframe: tf, from: start.toISOString(), to: end.toISOString(),
  });
  const resp = await fetch(`${HTTP_BASE}/api/chart?${qs}`, {
    headers: { Authorization: `Bearer ${TOKEN}` },
  });
  if (!resp.ok) throw new Error(`chart ${symbol}/${tf}: HTTP ${resp.status}`);
  const body = await resp.json();
  return body.candles || [];
}

// Preserve the visible time window (zoom + pan) across a setData — keeps the
// user looking at the same period when reloading or switching timeframe.
function preserveView(chart, fn) {
  const ts = chart.timeScale();
  let range = null;
  try { range = ts.getVisibleRange(); } catch (e) { /* empty chart */ }
  fn();
  if (range) { try { ts.setVisibleRange(range); } catch (e) { /* snap failed */ } }
}

async function loadHistory(symbol) {
  try {
    const candles = await fetchChart(symbol, activeTf);
    preserveView(mainChart, () => mainSeries.setData(candles.map(toBar)));
    setChartTitle(symbol, activeTf);
    note(`history: ${symbol} ${activeTf} (${candles.length} candles)`);
  } catch (err) {
    note(String(err));
  }
}

function setChartTitle(symbol, tf) {
  const t = $("chart-title");
  if (t) t.textContent = `${symbol} · ${tf} · Binance`;
}

/* --------------------------------------------------------- timeframe switch */

function applyAnalysisMode() {
  const on = isAnalysisTf(activeTf);
  const trend = (lastStructure[activeSymbol] || {}).trend;
  Panel.setContextMode(on, activeTf, trend);
  if (on) {
    Overlays.setStructure(lastStructure[activeSymbol]);   // redraw overlays
    Panel.setStructure(lastStructure[activeSymbol]);
  } else {
    Overlays.setStructure(null);                          // no fabricated analysis
  }
}

function setTimeframe(tf) {
  if (!LOOKBACK_BY_TF[tf]) return;
  activeTf = tf;
  if (window.__msSaveTf) window.__msSaveTf(tf);
  for (const b of document.querySelectorAll(".lv-tf")) {
    b.classList.toggle("on", b.getAttribute("data-tf") === tf);
  }
  loadHistory(activeSymbol);      // fetch the tf via /api/chart (view preserved)
  applyAnalysisMode();
}

for (const b of document.querySelectorAll(".lv-tf")) {
  b.addEventListener("click", () => setTimeframe(b.getAttribute("data-tf")));
}
// mark the initial active timeframe button
for (const b of document.querySelectorAll(".lv-tf")) {
  if (b.getAttribute("data-tf") === activeTf) b.classList.add("on");
}

/* -------------------------------------------------------- symbol switcher */

function setSymbol(symbol) {
  activeSymbol = symbol;
  for (const s of SYMBOLS) {
    const el = $(`sym-${s}`);
    if (el) el.classList.toggle("active", s === symbol);
  }
  loadHistory(symbol);
  applyAnalysisMode();
}
for (const s of SYMBOLS) {
  const el = $(`sym-${s}`);
  if (el) el.addEventListener("click", () => setSymbol(s));
}
{ const a = $(`sym-${activeSymbol}`); if (a) a.classList.add("active"); }

/* ---------------------------------------------------- stats strip (§9 top) */

function updateStats(c) {
  const set = (id, txt) => { const e = $(id); if (e) e.textContent = txt; };
  set("st-price", fmt(c.c));
  set("st-o", fmt(c.o)); set("st-h", fmt(c.h)); set("st-l", fmt(c.l)); set("st-c", fmt(c.c));
  set("st-vol", fmt(c.v));
  set("st-session", sessionLabel(c.ts));   // display-only time-of-day label
}
function fmt(v) {
  if (v === null || v === undefined) return "—";
  return Number(v).toLocaleString("en-US", { maximumFractionDigits: 2 });
}
// Display-only UTC time-of-day label (A9 map) — pure formatting of a timestamp,
// not analysis: it never feeds any decision, mirrors the backend session names.
function sessionLabel(ts) {
  const h = new Date(ts).getUTCHours();
  return h < 8 ? "ASIA" : h < 13 ? "LONDON" : h < 21 ? "NY" : "LATE";
}

/* ------------------------------------------------------------ bottom tabs */

for (const t of document.querySelectorAll(".lv-tab")) {
  t.addEventListener("click", () => {
    const name = t.getAttribute("data-tab");
    for (const x of document.querySelectorAll(".lv-tab")) x.classList.toggle("active", x === t);
    for (const p of document.querySelectorAll(".lv-tab-panel")) {
      p.hidden = p.getAttribute("data-tabpanel") !== name;
    }
  });
}

function renderSignalsTab(structure) {
  const host = $("tab-signals");
  if (!host) return;
  const sigs = (structure && structure.signals) || [];
  while (host.firstChild) host.removeChild(host.firstChild);
  if (!sigs.length) {
    const e = document.createElement("div");
    e.className = "lv-tab-empty";
    e.textContent = "No signals yet — waiting for the engine.";
    host.appendChild(e);
    return;
  }
  const table = document.createElement("table");
  table.className = "lv-sigtable mono";
  const th = document.createElement("tr");
  for (const h of ["Strategy", "Dir", "Entry", "SL", "TP1", "Invalidates in"]) {
    const c = document.createElement("th"); c.textContent = h; th.appendChild(c);
  }
  table.appendChild(th);
  for (const s of sigs.slice(-10).reverse()) {
    const tr = document.createElement("tr");
    const cells = [s.strategy, s.direction, s.entry, s.sl, s.tp1,
      (s.invalid_after_bars != null ? s.invalid_after_bars + " bars" : "—")];
    cells.forEach((val, i) => {
      const td = document.createElement("td");
      td.textContent = (val === null || val === undefined) ? "—"
        : (typeof val === "number" ? fmt(val) : String(val));
      if (i === 1) td.className = s.direction === "LONG" ? "g" : "r";
      tr.appendChild(td);
    });
    table.appendChild(tr);
  }
  host.appendChild(table);
}

/* --------------------------------------------------------- replay (Step 3) */

let replayMode = false;
let replayPoll = null;

function enterReplayMode() {
  replayMode = true;
  if (replaySeries) replaySeries.setData([]);
  setRpStatus("running");
  if (replayPoll) clearInterval(replayPoll);
  replayPoll = setInterval(async () => {
    try {
      const st = await api("/replay/status", { method: "GET" });
      const p = $("replay-progress");
      if (p) p.textContent = st.running ? `running · ${st.symbol} ×${st.speed}` : "complete";
      if (!st.running) exitReplayMode("replay finished");
    } catch (e) { /* transient */ }
  }, 1500);
}
function exitReplayMode(text) {
  if (!replayMode) return;
  replayMode = false;
  if (replayPoll) { clearInterval(replayPoll); replayPoll = null; }
  setRpStatus("idle");
  note(text);
  loadHistory(activeSymbol);         // re-bootstrap the live chart
}
function setRpStatus(s) {
  const st = $("rp-status"); if (st) st.textContent = s;
  const dot = $("rp-dot"); if (dot) dot.className = "dot" + (s === "running" ? " live" : "");
}

async function api(path, options) {
  const resp = await fetch(`${HTTP_BASE}${path}`, {
    headers: { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json" },
    ...options,
  });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(body.detail || `HTTP ${resp.status}`);
  return body;
}

async function quickLogSubmit(recId, fields) {
  return api(`/journal/${recId}`, { method: "PATCH", body: JSON.stringify(fields) });
}

function replayBody() {
  const from = $("replay-from").value, to = $("replay-to").value;
  const raw = $("replay-speed").value;
  return JSON.stringify({
    symbol: activeSymbol,
    start: new Date(from).toISOString(),
    end: new Date(to).toISOString(),
    speed: raw === "max" ? "max" : Number(raw),
  });
}
function wireReplay() {
  const start = $("replay-start"), stop = $("replay-stop"), restart = $("replay-restart");
  if (start) start.addEventListener("click", async () => {
    try { const st = await api("/replay/start", { method: "POST", body: replayBody() });
      enterReplayMode(); note(`replay: ${st.symbol} ×${st.speed}`); }
    catch (e) { note(`replay: ${e.message}`); }
  });
  if (stop) stop.addEventListener("click", async () => {
    try { await api("/replay/stop", { method: "POST" }); exitReplayMode("replay stopped"); }
    catch (e) { note(`replay: ${e.message}`); }
  });
  if (restart) restart.addEventListener("click", async () => {
    try { await api("/replay/stop", { method: "POST" }).catch(() => {}); }
    finally { const s = $("replay-start"); if (s) s.click(); }
  });
}
wireReplay();

/* P4.12 dashboard overlay — kept for the current build; Analytics/Journal pages
 * (Steps 6/5) reuse the same endpoints. */
async function openDashboard() {
  try {
    const [analytics, journal] = await Promise.all([
      api("/analytics", { method: "GET" }),
      api("/journal?limit=100", { method: "GET" }),
    ]);
    Dashboard.render(analytics, journal); Dashboard.show();
  } catch (err) { note(`dashboard: ${err.message}`); }
}
{ const d = $("dash-open"); if (d) d.addEventListener("click", openDashboard); }

/* ---------------------------------------------- data pages (Steps 4-6) */
// Thin: app.js owns the fetch; dashboard.js renders (pure consumer). Loaded
// on demand when a data page is shown, or via its Refresh button.
async function loadDataPages() {
  try {
    const [analytics, journal] = await Promise.all([
      api("/analytics", { method: "GET" }),
      api("/journal?limit=200", { method: "GET" }),
    ]);
    Dashboard.renderAnalytics($("page-analytics"), analytics);
    Dashboard.renderReview($("page-review"), analytics, journal);
    Dashboard.renderJournal($("page-journal"), journal);
  } catch (err) {
    for (const id of ["page-analytics", "page-review", "page-journal"]) {
      const e = $(id); if (e) e.textContent = "Could not load: " + err.message;
    }
  }
}
const DATA_PAGES = ["review", "journal", "analytics"];
window.addEventListener("ms-page", (e) => {
  if (DATA_PAGES.indexOf(e.detail) >= 0) loadDataPages();
});
for (const btn of document.querySelectorAll("[data-refresh]")) {
  btn.addEventListener("click", loadDataPages);
}

/* -------------------------------------------------- settings (Step 7) */
{
  const dark = $("set-theme-dark"), light = $("set-theme-light");
  if (dark && window.__msSetTheme) dark.addEventListener("click", () => window.__msSetTheme("dark"));
  if (light && window.__msSetTheme) light.addEventListener("click", () => window.__msSetTheme("light"));
  const sb = $("set-beginner"), bt = $("beginner-toggle");
  if (sb && bt) {
    const sync = () => sb.classList.toggle("on",
      document.documentElement.getAttribute("data-beginner") !== "off");
    sync();
    sb.addEventListener("click", () => { bt.click(); sync(); });
  }
  const apiEl = $("set-api"); if (apiEl) apiEl.textContent = API_HOST;
  const tokEl = $("set-token"); if (tokEl) tokEl.textContent = TOKEN ? TOKEN.slice(0, 3) + "•••" : "(none)";
}

/* ------------------------------------------- WebSocket client + reconnect */

const BACKOFF_INITIAL_MS = 1000;
const BACKOFF_CAP_MS = 30000;
const connDot = $("conn-dot"), connText = $("conn-text"), wsTarget = $("ws-target");
let backoffMs = BACKOFF_INITIAL_MS;
let lastWsMs = 0;

function setStatus(state, detail) {
  if (connText) connText.textContent = detail ? `${state} ${detail}` : state;
  if (connDot) connDot.className = "dot" + (state === "LIVE" ? " live" : state === "RECONNECTING" ? " down" : "");
}

function connect() {
  setStatus("CONNECTING");
  if (wsTarget) wsTarget.textContent = `${WS_BASE}/ws`;
  const ws = new WebSocket(`${WS_BASE}/ws?token=${encodeURIComponent(TOKEN)}`);

  ws.onopen = () => {
    backoffMs = BACKOFF_INITIAL_MS;
    setStatus("LIVE");
    if (!replayMode) loadHistory(activeSymbol);   // initial + reconnect reload
  };

  ws.onmessage = (event) => {
    const now = Date.now();
    if (lastWsMs) { const lat = $("st-lat"); if (lat) lat.textContent = (now - lastWsMs) + " ms"; }
    lastWsMs = now;
    const upd = $("lv-update"); if (upd) upd.textContent = window.IST.now();
    note(`last event: ${window.IST.full(new Date())}`);

    const msg = JSON.parse(event.data);
    const diff = msg.state_diff || {};
    for (const sym of Object.keys(diff)) {
      if (diff[sym].structure) {
        detectActivity(sym, diff[sym].structure);       // activity feed + notifications
        lastStructure[sym] = diff[sym].structure;
      }
    }
    const candle = msg.candle;
    if (!candle) return;
    if (candle.tf === "1m") Ops.pushActivity("Scanning " + candle.symbol);   // item 4
    if (candle.symbol === activeSymbol) updateStats(candle);
    if (candle.symbol !== activeSymbol) return;

    try {
      if (replayMode) {
        if (replaySeries && candle.tf === "1m") replaySeries.update(toBar(candle));
      } else if (isAnalysisTf(activeTf) && candle.tf === activeTf) {
        mainSeries.update(toBar(candle));            // diff-only live update (§9)
      }
      // higher-TF Live charts are aggregated history; no live forming bar yet.
    } catch (err) { console.warn("chart update skipped:", err.message); }

    const st = diff[activeSymbol] && diff[activeSymbol].structure;
    if (st) {
      renderSignalsTab(st);
      if (!replayMode && isAnalysisTf(activeTf)) {
        Overlays.setStructure(st, candle.c);
        Panel.setStructure(st, candle.ts);
      }
    }
  };

  ws.onclose = () => {
    setStatus("RECONNECTING", `in ${Math.round(backoffMs / 1000)}s`);
    window.setTimeout(connect, backoffMs);
    backoffMs = Math.min(backoffMs * 2, BACKOFF_CAP_MS);
  };
  ws.onerror = () => ws.close();
}

/* ------------------------------------- operations status + activity feed */
/* app.js owns the network (§9): it polls GET /ops and derives activity from the
 * live stream; ops.js only renders. Notifications (window.Notify) are wired in
 * a later file and called guardedly here. */
Ops.initActivity($("activity-feed"));
let opsData = null, opsActIdx = 0, lastFeedConnected = null;
const seenRec = {};                                    // last announced rec per symbol

function detectActivity(sym, st) {
  const recs = (st && st.recommendations) || [];
  if (!recs.length) return;
  const r = recs[recs.length - 1];                     // newest recommendation
  if (!r || !r.created_ts) return;
  const key = sym + "|" + r.strategy + "|" + r.created_ts;
  if (seenRec[sym] === key) return;                    // already announced
  seenRec[sym] = key;
  const high = r.verdict === "A_PLUS";
  Ops.pushActivity((high ? "⚡ High-conviction setup: " : "⚡ Trade setup: ")
    + sym + " " + r.direction + " (" + r.strategy + ")", "setup");
  if (window.Notify) window.Notify.tradeSetup(sym, r);   // notifications (guarded)
}

async function pollOps() {
  try {
    opsData = await api("/ops");
    Ops.renderPill($("ops-pill"), opsData, Ops.ACTIVITIES[opsActIdx % Ops.ACTIVITIES.length]);
    Ops.renderDashboard($("ops-dashboard"), opsData);
    const conn = opsData.feed && opsData.feed.connected;
    if (lastFeedConnected !== null && conn !== lastFeedConnected) {
      Ops.pushActivity(conn ? "Feed reconnected" : "Feed disconnected", conn ? "up" : "down");
      if (window.Notify) window.Notify.feed(conn);       // notifications (guarded)
    }
    lastFeedConnected = conn;
  } catch (e) { /* transient — keep the last good pill/dashboard */ }
}
setInterval(pollOps, 8000); pollOps();
// rotate the pill's live activity so the app never appears idle (items 3/4)
setInterval(function () {
  opsActIdx++;
  if (opsData) Ops.renderPill($("ops-pill"), opsData, Ops.ACTIVITIES[opsActIdx % Ops.ACTIVITIES.length]);
}, 3000);

/* --------------------------------- notifications + Telegram settings (6/7/8) */
Notify.registerSW();                                   // PWA / installable
let notifPrefs = { desktop: true, telegram: true, trade_alerts: true, system_alerts: true, push: false };

function setSwitch(id, on) { const b = $(id); if (b) b.classList.toggle("on", !!on); }
function paintNotifUI() {
  setSwitch("ntf-desktop", notifPrefs.desktop);
  setSwitch("ntf-telegram", notifPrefs.telegram);
  setSwitch("ntf-trade", notifPrefs.trade_alerts);
  setSwitch("ntf-system", notifPrefs.system_alerts);
  const perm = $("ntf-perm"); if (perm) perm.textContent = Notify.permission();
  Notify.setPrefs(notifPrefs);                         // apply toggles to the notifier
}
async function saveNotif() {
  try {
    const r = await api("/settings/notifications", { method: "PUT", body: JSON.stringify(notifPrefs) });
    notifPrefs = Object.assign(notifPrefs, r.notifications || {});
  } catch (e) { /* keep local prefs on failure */ }
  paintNotifUI();
}
function bindSwitch(id, key) {
  const b = $(id); if (!b) return;
  b.addEventListener("click", () => { notifPrefs[key] = !notifPrefs[key]; saveNotif(); });
}
bindSwitch("ntf-desktop", "desktop"); bindSwitch("ntf-telegram", "telegram");
bindSwitch("ntf-trade", "trade_alerts"); bindSwitch("ntf-system", "system_alerts");
if ($("ntf-enable")) $("ntf-enable").addEventListener("click", () => Notify.request().then(paintNotifUI));

function paintTgStatus(tg) {
  const st = $("tg-status"); if (!st) return;
  if (tg && tg.verified) st.textContent = "connected"
    + (tg.bot_username ? " · @" + tg.bot_username : "") + (tg.chat_id ? " · chat " + tg.chat_id : "");
  else if (tg && tg.has_token) st.textContent = "token saved — not verified";
  else st.textContent = "not configured";
}
function tgMsg(text, ok) {
  const m = $("tg-msg"); if (m) { m.textContent = text || ""; m.className = "tg-msg" + (ok === true ? " ok" : ok === false ? " bad" : ""); }
}
if ($("tg-verify")) $("tg-verify").addEventListener("click", async () => {
  const t = $("tg-token"), token = (t && t.value || "").trim();
  if (!token) { tgMsg("Paste your bot token first.", false); return; }
  tgMsg("Verifying…");
  try {
    const r = await api("/settings/telegram/verify", { method: "POST", body: JSON.stringify({ token }) });
    if (r.ok) { paintTgStatus(r); tgMsg("Connected — check Telegram for a confirmation message.", true); if (t) t.value = ""; }
    else tgMsg(r.error || "Verification failed.", false);
  } catch (e) { tgMsg(e.message, false); }
});
if ($("tg-test")) $("tg-test").addEventListener("click", async () => {
  tgMsg("Sending…");
  try { const r = await api("/settings/telegram/test", { method: "POST", body: "{}" }); tgMsg(r.ok ? "Test sent — check Telegram." : "Send failed.", r.ok); }
  catch (e) { tgMsg(e.message, false); }
});
if ($("tg-clear")) $("tg-clear").addEventListener("click", async () => {
  try { const r = await api("/settings/telegram", { method: "DELETE" }); paintTgStatus(r); tgMsg("Removed.", true); }
  catch (e) { tgMsg(e.message, false); }
});
(async function loadSettings() {
  try {
    const s = await api("/settings");
    notifPrefs = Object.assign(notifPrefs, s.notifications || {});
    paintTgStatus(s.telegram);
  } catch (e) { /* settings unavailable — keep defaults */ }
  paintNotifUI();
})();

applyAnalysisMode();   // set the initial rail mode before the first payload
connect();

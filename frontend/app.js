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
// Auth (login-based): a ?token= URL override wins (dev / bookmarks); otherwise
// the token remembered at login. No token -> the login overlay gates the app
// (no raw-token prompt). Storage lives in ui.js (app.js is storage-free);
// TOKEN is mutable — set on a successful login.
let TOKEN = params.get("token") || (window.__msToken ? window.__msToken.get() : "");
if (params.get("token") && window.__msToken) window.__msToken.save(TOKEN);

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
// Per-TF chart window (owner decision — fast UI, backend unchanged): 1W/1M show
// FULL history (few candles, instant); 1D = 1 year; 4H = 180 days. Replay and
// analytics always read the full DB (this only bounds the Live chart's default
// fetch). A "Show full history" chart control is a planned future addition.
const FULL_HISTORY_MS = 20 * 365 * 24 * 3600e3;   // covers all stored 1m (2017+)
const LOOKBACK_BY_TF = {
  "1m": 24 * 3600e3, "5m": 3 * 24 * 3600e3, "15m": 7 * 24 * 3600e3,
  "30m": 14 * 24 * 3600e3, "1h": 30 * 24 * 3600e3, "4h": 180 * 24 * 3600e3,
  "1d": 365 * 24 * 3600e3, "1w": FULL_HISTORY_MS, "1M": FULL_HISTORY_MS,
};

const urlSymbol = (params.get("symbol") || "").toUpperCase();
let activeSymbol = SYMBOLS.includes(urlSymbol) ? urlSymbol : SYMBOLS[0];
let activeTf = (window.__msTf && LOOKBACK_BY_TF[window.__msTf]) ? window.__msTf : "1m";

const $ = (id) => document.getElementById(id);
const isAnalysisTf = (tf) => ANALYSIS_TFS.indexOf(tf) >= 0;

// Live forming-candle state (chart UX items 5/6/7) — display-only. The backend
// streams the current 1m bar's OHLCV; we fold it into the active TF's last bar.
const TF_SEC = { "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800, "1M": 2592000 };
let liveBar = null;                 // the active TF's current bar (baseline + forming)
let lastLivePrice = 0, liveTickMs = 0;

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
// Bigger, TradingView-sized candles: a fixed bar spacing + a small right gap,
// set ONCE at creation (chartTheme() is re-applied on theme toggle and must NOT
// carry barSpacing, or a theme switch would reset the user's zoom).
const BAR_SPACING = 9, RIGHT_OFFSET = 4;
function makeChart(el) {
  const opts = Object.assign({ autoSize: true }, chartTheme());
  opts.timeScale = Object.assign({}, opts.timeScale,
    { barSpacing: BAR_SPACING, rightOffset: RIGHT_OFFSET, minBarSpacing: 1.5 });
  // Premium interaction feel. Evidence: zero long-tasks during zoom/pan, so the
  // gap vs TradingView is the DEFAULT interaction model, not perf. LWC's mouse
  // kinetic scroll is OFF by default — enabling it gives drag momentum/inertia
  // (the "momentum drag" feel). handleScroll/handleScale are already enabled.
  opts.kineticScroll = { mouse: true, touch: true };
  return LightweightCharts.createChart(el, opts);
}

const mainChart = makeChart($("chart"));
const mainSeries = mainChart.addSeries(LightweightCharts.CandlestickSeries, SERIES_OPTS);
// Tight price-scale margins so candles fill the pane (kills the dead band the
// default 0.2/0.1 margins left at top+bottom); volume tucks into the lowest 14%.
mainChart.priceScale("right").applyOptions({ scaleMargins: { top: 0.06, bottom: 0.16 } });
Overlays.init(mainChart, mainSeries);            // overlays draw on the Live chart
Panel.init(quickLogSubmit);
Dashboard.init();
Indicators.init(mainChart, window.__msIndicators);   // display-only EMA/SMA/RSI/Volume
Drawing.init(mainChart, mainSeries);                 // display-only drawing tools

// Crosshair OHLC readout (item 12) — reads the hovered bar from LWC, no caching.
mainChart.subscribeCrosshairMove((param) => {
  const box = $("crosshair-box"); if (!box) return;
  const d = (param.time && param.seriesData) ? param.seriesData.get(mainSeries) : null;
  if (!d) { box.hidden = true; return; }
  box.hidden = false; box.textContent = "";
  const put = (k, v, cls) => {
    const w = document.createElement("span"); w.className = "cx-item " + (cls || "");
    if (k) { const a = document.createElement("b"); a.textContent = k; w.appendChild(a); }
    const b = document.createElement("span"); b.textContent = v; w.appendChild(b);
    box.appendChild(w);
  };
  put("", window.IST.dateTime((typeof param.time === "number" ? param.time : 0) * 1000), "cx-time");
  put("O", fmt(d.open)); put("H", fmt(d.high)); put("L", fmt(d.low)); put("C", fmt(d.close));
  const vs = Indicators.volumeSeries && Indicators.volumeSeries();
  const vd = (vs && param.seriesData) ? param.seriesData.get(vs) : null;   // item 12: volume
  if (vd && vd.value != null) put("V", fmt(vd.value));
});

// Candle countdown (item 7) — time until the active TF's bar closes (intraday).
setInterval(() => {
  const el = $("chart-countdown"); if (!el) return;
  const dur = TF_SEC[activeTf] || 60;
  if (dur >= 86400) { el.textContent = ""; return; }
  const left = dur - (Math.floor(Date.now() / 1000) % dur);
  const pad = (n) => String(n).padStart(2, "0");
  const h = Math.floor(left / 3600), m = Math.floor(left / 60) % 60, s = left % 60;
  el.textContent = "⏱ " + (h ? pad(h) + ":" : "") + pad(m) + ":" + pad(s);
}, 500);
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
  const iq = Indicators.paramsQuery();             // backend computes the enabled ones
  const resp = await fetch(`${HTTP_BASE}/api/chart?${qs}${iq ? "&" + iq : ""}`, {
    headers: { Authorization: `Bearer ${TOKEN}` },
  });
  if (resp.status === 401) onAuthFail();
  if (!resp.ok) throw new Error(`chart ${symbol}/${tf}: HTTP ${resp.status}`);
  return await resp.json();                         // full body {candles, indicators, ...}
}

// Show the most recent N bars at a comfortable width (like TradingView) — the
// rest of history stays scrollable to the left. Used for a FRESH view (first
// load, timeframe/symbol switch) instead of cramming ~1440 bars into <1px each
// (which is what fitContent does and why candles looked like thin lines).
const RECENT_BARS = 130;
function showRecent(chart, n) {
  if (!n) return;
  try { chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - RECENT_BARS), to: n + RIGHT_OFFSET }); }
  catch (e) { try { chart.timeScale().fitContent(); } catch (e2) { /* empty */ } }
}
let hasView = false;   // false until the first successful history load
async function loadHistory(symbol, opts) {
  // A fresh recent-window view on the first load and on every timeframe/symbol
  // switch; the user's zoom is only preserved on a same-tf reconnect/indicator
  // refetch. Preserving a 1m window onto sparse 5m/1h data (the old bug) produced
  // 2 giant blocks — so a stale/degenerate range falls back to the recent window.
  const resetView = !!(opts && opts.resetView) || !hasView;
  try {
    const body = await fetchChart(symbol, activeTf);
    const candles = body.candles || [];
    const bars = candles.map(toBar);
    const ts = mainChart.timeScale();
    let keep = null;
    if (!resetView) { try { keep = ts.getVisibleRange(); } catch (e) { /* empty */ } }
    mainSeries.setData(bars);                          // single bootstrap setData (§9)
    if (keep) { try { ts.setVisibleRange(keep); } catch (e) { showRecent(mainChart, bars.length); } }
    else showRecent(mainChart, bars.length);
    hasView = true;
    liveBar = bars.length ? bars[bars.length - 1] : null;   // baseline for forming
    Indicators.render(body);                                // EMA/SMA/RSI/Volume
    Panel.setContext(body.context);                         // HTF context (item 9)
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
    Overlays.setSetup(activeRec(lastStructure[activeSymbol]));   // Step 6: setup lines
    Panel.setStructure(lastStructure[activeSymbol]);
  } else {
    Overlays.setStructure(null);                          // no fabricated analysis
    Overlays.setSetup(null);                              // clear setup lines on 15m+
  }
}

function setTimeframe(tf) {
  if (!LOOKBACK_BY_TF[tf]) return;
  activeTf = tf;
  if (window.__msSaveTf) window.__msSaveTf(tf);
  for (const b of document.querySelectorAll(".lv-tf")) {
    b.classList.toggle("on", b.getAttribute("data-tf") === tf);
  }
  loadHistory(activeSymbol, { resetView: true });   // fresh recent-window per tf
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
  clearMarketStructure();          // Step 6: box streams the active symbol only
  loadHistory(symbol, { resetView: true });
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

// Live forming candle (items 5/6): fold the streamed current-1m OHLCV into the
// active TF's last bar. DISPLAY-ONLY — the engine never sees this; it only moves
// the chart + the live stats, exactly like Binance/TradingView.
function handleForming(f) {
  if (f.symbol !== activeSymbol) return;
  liveTickMs = Date.now();
  updateLiveStats(f);
  if (!liveBar || replayMode) return;
  const fMs = Date.parse(f.ts), dur = TF_SEC[activeTf] || 60;
  if (activeTf === "1m") {
    liveBar = { time: Math.floor(fMs / 1000), open: f.o, high: f.h, low: f.l, close: f.c };
  } else {
    const barT = Math.floor(fMs / 1000 / dur) * dur;     // active-TF bucket for this minute
    if (dur >= 86400 || barT <= liveBar.time) {          // same period -> fold the 1m in
      liveBar = { time: liveBar.time, open: liveBar.open,
                  high: Math.max(liveBar.high, f.h), low: Math.min(liveBar.low, f.l), close: f.c };
    } else {                                             // new intraday HTF bar
      liveBar = { time: barT, open: f.o, high: f.h, low: f.l, close: f.c };
    }
  }
  try { mainSeries.update(liveBar); } catch (e) { /* history not loaded yet */ }
  Indicators.updateForming(f, activeTf);           // extend indicator lines (backend values)
}
function updateLiveStats(f) {
  const set = (id, v) => { const e = $(id); if (e) e.textContent = fmt(v); };
  set("st-price", f.c); set("st-o", f.o); set("st-h", f.h); set("st-l", f.l); set("st-c", f.c); set("st-vol", f.v);
  const p = $("st-price");
  if (p) { p.classList.toggle("tick-up", f.c >= lastLivePrice); p.classList.toggle("tick-down", f.c < lastLivePrice); }
  lastLivePrice = f.c;
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
  loadHistory(activeSymbol, { resetView: true });   // re-bootstrap the live chart
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
  if (resp.status === 401) onAuthFail();          // token invalid -> re-login
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
    const upd = $("lv-update"); if (upd) upd.textContent = window.IST.now();    // last tick time (item 8)

    const msg = JSON.parse(event.data);
    if (msg.forming) { handleForming(msg.forming); return; }    // live forming candle (item 5)
    note(`last event: ${window.IST.full(new Date())}`);
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
      } else if (candle.tf === activeTf) {
        const bar = toBar(candle);
        mainSeries.update(bar);                      // diff-only close (§9)
        liveBar = bar;                               // forming folds onto the closed bar
      }
    } catch (err) { console.warn("chart update skipped:", err.message); }

    const st = diff[activeSymbol] && diff[activeSymbol].structure;
    if (st) {
      renderSignalsTab(st);
      detectStructureEvents(activeSymbol, st);         // Step 6: stream to Market Structure box
      if (!replayMode && isAnalysisTf(activeTf)) {
        Overlays.setStructure(st, candle.c);
        Overlays.setSetup(activeRec(st));              // Step 6: setup-only chart annotations
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
const seenEv = { sweep: {}, choch: {} };               // last announced event ts

/* ---------------------------------- Market Structure box (Step 6) --------- */
// Streams the underlying 1m structure events (HH/HL, sweeps, BOS/CHoCH, OB, FVG)
// into the rail box — replacing the always-on chart labels. app.js owns the
// detection (§9): it compares the latest event id per kind to the last seen.
const msSeen = { pivot: {}, bos: {}, choch: {}, sweep: {}, ob: {}, fvg: {} };
const MS_MAX = 14;
function msPush(text, cls) {
  const host = $("ms-stream"); if (!host) return;
  const empty = host.querySelector(".ms-empty"); if (empty) empty.remove();
  const row = document.createElement("div");
  row.className = "ms-row " + (cls || "");
  const t = document.createElement("span"); t.className = "ms-t"; t.textContent = window.IST.now();
  const m = document.createElement("span"); m.className = "ms-m"; m.textContent = text;
  row.appendChild(t); row.appendChild(m);
  host.insertBefore(row, host.firstChild);             // newest on top
  while (host.children.length > MS_MAX) host.removeChild(host.lastChild);
}
function clearMarketStructure() {
  const host = $("ms-stream");
  if (host) {
    host.textContent = "";
    const e = document.createElement("div"); e.className = "ms-empty";
    e.textContent = "Waiting for structure…"; host.appendChild(e);
  }
  for (const k in msSeen) msSeen[k] = {};
}
function activeRec(st) {
  const recs = (st && st.recommendations) || [];
  if (!recs.length) return null;
  const r = recs[recs.length - 1];
  return (!r.status || r.status === "active") ? r : null;    // draw only an active setup
}
function detectStructureEvents(sym, st) {
  if (!st || sym !== activeSymbol) return;             // stream the active symbol only
  const seen = (key, id, msg, cls) => {
    if (id == null || msSeen[key][sym] === id) return;
    if (msSeen[key][sym] !== undefined) msPush(msg, cls);    // skip the baseline (first payload)
    msSeen[key][sym] = id;
  };
  const last = (a) => (a && a.length) ? a[a.length - 1] : null;
  const piv = last(st.pivots);
  if (piv) seen("pivot", piv.ts, (piv.label || piv.kind) + " @ " + fmt(piv.price), "struct");
  const bos = last(st.bos);
  if (bos) seen("bos", bos.ts, "BOS " + bos.direction + (bos.displacement ? " (strong)" : ""), "bos");
  const ch = last(st.choch);
  if (ch) seen("choch", ch.ts, "CHoCH " + ch.direction, "choch");
  const sw = last((st.liquidity || {}).sweeps);
  if (sw) seen("sweep", sw.ts, "Liquidity sweep " + sw.side, "sweep");
  const ob = last((st.orderblocks || {}).blocks);
  if (ob) seen("ob", ob.created_ts, "Order block (" + String(ob.direction).toLowerCase() + ")", "ob");
  const fv = last(st.fvgs);
  if (fv) seen("fvg", fv.created_ts, "FVG (" + String(fv.direction).toLowerCase() + ")", "fvg");
}

// Meaningful activity from the live payload (item 16): trade setups, liquidity
// sweeps, CHOCH. No debug logs — only structural events the engine produced.
function detectActivity(sym, st) {
  if (!st) return;
  const sweeps = (st.liquidity && st.liquidity.sweeps) || [];
  if (sweeps.length) {
    const s = sweeps[sweeps.length - 1];
    if (s.ts && seenEv.sweep[sym] !== s.ts) {
      if (seenEv.sweep[sym] !== undefined) Ops.pushActivity("Liquidity sweep: " + sym + " " + s.side, "setup");
      seenEv.sweep[sym] = s.ts;
    }
  }
  const choch = st.choch || [];
  if (choch.length) {
    const c = choch[choch.length - 1];
    if (c.ts && seenEv.choch[sym] !== c.ts) {
      if (seenEv.choch[sym] !== undefined) Ops.pushActivity("CHOCH detected: " + sym + " " + c.direction, "setup");
      seenEv.choch[sym] = c.ts;
    }
  }
  const recs = st.recommendations || [];
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
// ops polling + activity-pill rotation — started by boot() after login.
function _startOps() {
  pollOps(); setInterval(pollOps, 8000);
  setInterval(function () {              // rotate live activity (items 3/4)
    opsActIdx++;
    if (opsData) Ops.renderPill($("ops-pill"), opsData, Ops.ACTIVITIES[opsActIdx % Ops.ACTIVITIES.length]);
  }, 3000);
}

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

// Multiple Telegram bots — a verified bot list; alerts fan out to all of them
// at once (backend), reaching every device/chat the owner adds. Each row can be
// removed independently.
function renderTgList(bots) {
  const st = $("tg-status"), list = $("tg-list");
  bots = bots || [];
  if (st) st.textContent = bots.length
    ? bots.length + " bot" + (bots.length === 1 ? "" : "s") + " connected"
    : "not configured";
  if (!list) return;
  list.textContent = "";
  for (const b of bots) {
    const row = document.createElement("div"); row.className = "tg-bot";
    const info = document.createElement("span"); info.className = "tg-bot-info mono";
    info.textContent = (b.label ? b.label + " · " : "")
      + (b.bot_username ? "@" + b.bot_username : "bot")
      + (b.chat_id ? " · chat " + b.chat_id : "")
      + (b.verified ? "" : " · unverified");
    const rm = document.createElement("button");
    rm.className = "chrome-btn tg-rm"; rm.type = "button"; rm.textContent = "Remove";
    rm.addEventListener("click", async () => {
      try { const r = await api("/settings/telegram/" + b.id, { method: "DELETE" });
        renderTgList(r.telegram_bots); tgMsg("Removed.", true); }
      catch (e) { tgMsg(e.message, false); }
    });
    row.appendChild(info); row.appendChild(rm); list.appendChild(row);
  }
}
function tgMsg(text, ok) {
  const m = $("tg-msg"); if (m) { m.textContent = text || ""; m.className = "tg-msg" + (ok === true ? " ok" : ok === false ? " bad" : ""); }
}
if ($("tg-verify")) $("tg-verify").addEventListener("click", async () => {
  const t = $("tg-token"), token = (t && t.value || "").trim();
  const lbl = $("tg-label"), label = (lbl && lbl.value || "").trim();
  if (!token) { tgMsg("Paste your bot token first.", false); return; }
  tgMsg("Verifying…");
  try {
    const r = await api("/settings/telegram/verify", { method: "POST", body: JSON.stringify({ token, label }) });
    if (r.ok) { renderTgList(r.telegram_bots); tgMsg("Bot added — check Telegram for a confirmation message.", true); if (t) t.value = ""; if (lbl) lbl.value = ""; }
    else tgMsg(r.error || "Verification failed.", false);
  } catch (e) { tgMsg(e.message, false); }
});
if ($("tg-test")) $("tg-test").addEventListener("click", async () => {
  tgMsg("Sending…");
  try { const r = await api("/settings/telegram/test", { method: "POST", body: "{}" });
    tgMsg(r.ok ? ("Test sent to " + r.sent + "/" + r.total + " — check Telegram.") : "Send failed.", r.ok); }
  catch (e) { tgMsg(e.message, false); }
});
async function loadSettings() {           // called by boot() after login
  try {
    const s = await api("/settings");
    notifPrefs = Object.assign(notifPrefs, s.notifications || {});
    renderTgList(s.telegram_bots);
  } catch (e) { /* settings unavailable — keep defaults */ }
  paintNotifUI();
}

/* -------------------------------------------------- chart toolbar (item 1) */
(function wireToolbar() {
  const btn = $("ind-btn"), panel = $("ind-panel");
  if (btn && panel) {
    Indicators.renderMenu(panel, function (needData) {
      window.__msSaveIndicators(Indicators.config());
      if (needData) loadHistory(activeSymbol);       // refetch with new indicator params
      else Indicators.applyVisibility();
    });
    btn.addEventListener("click", (e) => { e.stopPropagation(); panel.hidden = !panel.hidden; });
    document.addEventListener("click", (e) => {
      if (!panel.hidden && !panel.contains(e.target) && e.target !== btn) panel.hidden = true;
    });
  }
  // Draw menu (item 11)
  const drawBtn = $("draw-btn"), drawPanel = $("draw-panel");
  if (drawBtn && drawPanel) {
    drawBtn.addEventListener("click", (e) => { e.stopPropagation(); drawPanel.hidden = !drawPanel.hidden; });
    document.addEventListener("click", (e) => {
      if (!drawPanel.hidden && !drawPanel.contains(e.target) && e.target !== drawBtn) drawPanel.hidden = true;
    });
    drawPanel.querySelectorAll(".draw-tool[data-tool]").forEach((b) => b.addEventListener("click", () => {
      Drawing.setTool(b.getAttribute("data-tool"));
      drawPanel.hidden = true; drawBtn.classList.add("on");
      note("draw: " + b.textContent.trim() + " — click the chart to place points");
    }));
    const u = $("draw-undo"); if (u) u.addEventListener("click", () => Drawing.undo());
    const cl = $("draw-clear"); if (cl) cl.addEventListener("click", () => Drawing.clear());
    Drawing.onDone(() => drawBtn.classList.remove("on"));
  }
  let structOn = false;    // Step 6: clean execution chart by default (advanced opt-in)
  const struct = $("tb-structure");
  if (struct) struct.addEventListener("click", () => {
    structOn = !structOn; struct.classList.toggle("on", structOn);
    Overlays.setStructureVisible(structOn);          // advanced: full structure overlay
  });
  const reset = $("tb-reset");
  if (reset) reset.addEventListener("click", () => mainChart.timeScale().fitContent());
  let autoOn = true;
  const auto = $("tb-autoscale");
  if (auto) {
    auto.classList.add("on");
    auto.addEventListener("click", () => {
      autoOn = !autoOn; auto.classList.toggle("on", autoOn);
      mainChart.priceScale("right").applyOptions({ autoScale: autoOn });
    });
  }
  const shot = $("tb-screenshot");
  if (shot) shot.addEventListener("click", () => {
    try {
      const canvas = mainChart.takeScreenshot();
      const a = document.createElement("a");
      a.download = `${activeSymbol}_${activeTf}.png`;
      a.href = canvas.toDataURL(); a.click();
    } catch (e) { note("screenshot unavailable"); }
  });
})();

/* ----------------------------------------------- auth + boot (login gate) */
/* The app stays dormant (no WS, no polling, no data fetch) until there is a
 * token — a fresh visitor sees the login overlay, logs in once, and the token
 * is remembered (via ui.js) for next time. Logout / a 401 clears it. */
let booted = false, authFailed = false;
function boot() {
  if (booted) return; booted = true;
  connect();          // WebSocket (its onopen bootstraps history)
  _startOps();        // /ops polling + activity pill
  loadSettings();     // notifications + telegram (settings page)
}
function resetToLogin() {
  if (window.__msToken) window.__msToken.clear();
  const p = new URLSearchParams(window.location.search); p.delete("token");
  const qs = p.toString();
  window.location.replace(window.location.pathname + (qs ? "?" + qs : "") + window.location.hash);
}
function onAuthFail() { if (authFailed) return; authFailed = true; resetToLogin(); }
// app.js owns the network (§9) — login.js only renders the form and calls this.
async function doLogin(username, password) {
  try {
    const resp = await fetch(`${HTTP_BASE}/login`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    const body = await resp.json().catch(() => ({}));
    if (resp.ok && body.token) {
      TOKEN = body.token;
      if (window.__msToken) window.__msToken.save(TOKEN);
      if (window.Login) Login.hide();
      boot();
      return { ok: true };
    }
    if (resp.status === 503) return { ok: false, error: "Login is not set up on the server yet." };
    return { ok: false, error: "Invalid username or password." };
  } catch (e) { return { ok: false, error: "Cannot reach the server." }; }
}
window.__msAuth = { login: doLogin, logout: resetToLogin };
{ const lo = $("logout-btn"); if (lo) lo.addEventListener("click", resetToLogin); }

applyAnalysisMode();   // set the initial rail mode before the first payload
if (TOKEN) boot();
else if (window.Login) Login.show();
else connect();        // no login.js present -> legacy behavior

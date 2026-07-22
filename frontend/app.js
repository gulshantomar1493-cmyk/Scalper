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
// B2: ?symbol= wins, else the last symbol you were on (persisted in ui.js), else default
const savedSymbol = (window.__msSym || "").toUpperCase();
let activeSymbol = SYMBOLS.includes(urlSymbol) ? urlSymbol
  : (SYMBOLS.includes(savedSymbol) ? savedSymbol : SYMBOLS[0]);
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
  // Crosshair follows the cursor EXACTLY (Normal), not snapping to the nearest
  // OHLC (Magnet is the LWC default) — so the price under the pointer is the
  // exact price you point at, like Delta / TradingView.
  opts.crosshair = { mode: (LightweightCharts.CrosshairMode ? LightweightCharts.CrosshairMode.Normal : 0) };
  return LightweightCharts.createChart(el, opts);
}

const mainChart = makeChart($("chart"));
const mainSeries = mainChart.addSeries(LightweightCharts.CandlestickSeries, SERIES_OPTS);
// Tight price-scale margins so candles fill the pane (kills the dead band the
// default 0.2/0.1 margins left at top+bottom); volume tucks into the lowest 14%.
mainChart.priceScale("right").applyOptions({ scaleMargins: { top: 0.06, bottom: 0.16 } });
Overlays.init(mainChart, mainSeries);            // overlays draw on the Live chart
Panel.init(quickLogSubmit);
if (window.Htf) Htf.init($("htf-panel"));            // HTF V1.1 panel (pure renderer)
if (window.Setups) Setups.init($("setups-panel"));   // Trade Setup V2 panel (pure renderer)
if (window.Strip) Strip.init($("context-strip"));    // M2.5 context strip (pure renderer)
Dashboard.init();
Indicators.init(mainChart, window.__msIndicators);   // display-only EMA/SMA/RSI/Volume
Drawing.init(mainChart, mainSeries);                 // display-only drawing tools
if (window.__msDrawings) {                           // M3: persist per-symbol (storage in ui.js)
  Drawing.setItems(window.__msDrawings.get(activeSymbol));                              // restore on load
  Drawing.onChange(() => window.__msDrawings.save(activeSymbol, Drawing.getItems()));   // save on every edit
}

// Crosshair OHLC readout (item 12) — reads the hovered bar from LWC, no caching.
mainChart.subscribeCrosshairMove((param) => {
  const box = $("crosshair-box"); if (!box) return;
  if (!param.point) { box.hidden = true; return; }
  const d = param.seriesData ? param.seriesData.get(mainSeries) : null;
  box.hidden = false; box.textContent = "";
  const put = (k, v, cls) => {
    const w = document.createElement("span"); w.className = "cx-item " + (cls || "");
    if (k) { const a = document.createElement("b"); a.textContent = k; w.appendChild(a); }
    const b = document.createElement("span"); b.textContent = v; w.appendChild(b);
    box.appendChild(w);
  };
  const px = mainSeries.coordinateToPrice(param.point.y);   // EXACT price under the pointer
  if (px != null) put("@", fmt(px), "cx-price");
  if (typeof param.time === "number") put("", window.IST.dateTime(param.time * 1000), "cx-time");
  if (d) { put("O", fmt(d.open)); put("H", fmt(d.high)); put("L", fmt(d.low)); put("C", fmt(d.close)); }
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
let loadSeq = 0;       // P2-A3: monotonic token so out-of-order loads can't clobber

// Price-axis auto-fit, shared by the toolbar "Auto" button and every symbol/TF
// switch (P3). LWC turns the right scale to manual (autoScale=false) the moment
// the user drags the price axis and keeps it off across setData — so a symbol
// switch must re-enable it or ETH (~1800) renders inside BTC's (~70000) band.
let priceAutoScale = true;
function setPriceAutoScale(on) {
  priceAutoScale = on;
  try { mainChart.priceScale("right").applyOptions({ autoScale: on }); } catch (e) { /* empty */ }
  const b = $("tb-autoscale"); if (b) b.classList.toggle("on", on);
}
// Force an immediate price re-fit. LWC recomputes the range only on a data op
// (setData/update), never on a bare applyOptions once the scale is manual — so
// enable autoScale THEN nudge the current bar. Used by the Auto toggle.
function refitPrice() {
  setPriceAutoScale(true);
  if (liveBar) { try { mainSeries.update(liveBar); } catch (e) { /* empty */ } }
}

async function loadHistory(symbol, opts) {
  // Fresh recent-window view on first load + every symbol/TF switch (which also
  // re-fits the price axis, P3). The exact view is preserved ONLY for an
  // indicator refetch (preserveView); a reconnect snaps to the live edge (P2-A2).
  const resetView = !!(opts && opts.resetView) || !hasView;
  const preserveView = !!(opts && opts.preserveView);
  const loadEl = $("chart-loading");
  if (loadEl) loadEl.textContent = `Loading ${symbol} ${activeTf}…`;
  const seq = ++loadSeq;
  try {
    const body = await fetchChart(symbol, activeTf);
    if (seq !== loadSeq || symbol !== activeSymbol) return;   // P2-A3: superseded / symbol changed mid-fetch
    const candles = body.candles || [];
    const bars = candles.map(toBar);
    const ts = mainChart.timeScale();
    let keep = null;
    if (preserveView && !resetView) { try { keep = ts.getVisibleRange(); } catch (e) { /* empty */ } }
    // P3: re-enable price autoScale BEFORE setData on a symbol/TF switch so the
    // new symbol's range is recomputed (LWC re-fits on the data op, not on a
    // bare applyOptions once the scale is manual from a user price-drag).
    if (resetView) setPriceAutoScale(true);
    mainSeries.setData(bars);                          // single bootstrap setData (§9)
    // P2-A1: setData replaces the whole series and each closed candle is
    // broadcast once — so re-apply the live edge that arrived over WS during the
    // fetch (same symbol/TF only; a symbol/TF switch resets it to the REST tail).
    if (!resetView && liveBar && (!bars.length || liveBar.time >= bars[bars.length - 1].time)) {
      try { mainSeries.update(liveBar); } catch (e) { /* empty */ }
    } else {
      liveBar = bars.length ? bars[bars.length - 1] : null;
    }
    if (keep) { try { ts.setVisibleRange(keep); } catch (e) { showRecent(mainChart, bars.length); } }
    else if (resetView) showRecent(mainChart, bars.length);
    else { try { ts.scrollToRealTime(); } catch (e) { showRecent(mainChart, bars.length); } }   // P2-A2: reconnect -> latest
    hasView = true;
    Indicators.render(body);                                // EMA/SMA/RSI/Volume
    Panel.setContext(body.context);                         // HTF context (item 9)
    setChartTitle(symbol, activeTf);
    note(`history: ${symbol} ${activeTf} (${candles.length} candles)`);
  } catch (err) {
    note(String(err));
  } finally {
    if (loadEl) loadEl.textContent = "";   // hide the spinner (:empty)
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
  const prev = activeSymbol;
  // M3: drawings follow the symbol — bank the ones we're leaving, load the ones we're entering
  if (window.__msDrawings && prev && prev !== symbol) window.__msDrawings.save(prev, Drawing.getItems());
  activeSymbol = symbol;
  if (window.__msDrawings && prev !== symbol) Drawing.setItems(window.__msDrawings.get(symbol));
  if (window.__msSaveSym) window.__msSaveSym(symbol);   // B2: reopen here after refresh
  for (const s of SYMBOLS) {
    const el = $(`sym-${s}`);
    if (el) el.classList.toggle("active", s === symbol);
  }
  clearMarketStructure();          // Step 6: box streams the active symbol only
  clearPaperLines();               // paper markers are per-symbol
  updatePaperMarkers();
  renderHtf();                     // show cached HTF for the new symbol, then refresh
  loadHtf();
  renderSetups();                  // show cached setups for the new symbol, then refresh
  loadSetups();
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
  updateTradePnl(f.c);                             // running paper P&L follows the live price
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

// P5: standalone user-journal CRUD network. app.js owns the fetch; journal.js
// renders + calls these callbacks (the pure-consumer boundary).
const journalApi = {
  list: (f) => {
    const q = new URLSearchParams();
    if (f && f.search) q.set("search", f.search);
    if (f && f.symbol) q.set("symbol", f.symbol);
    if (f && f.direction) q.set("direction", f.direction);
    const qs = q.toString();
    return api("/api/journal" + (qs ? "?" + qs : ""), { method: "GET" });
  },
  create: (body) => api("/api/journal", { method: "POST", body: JSON.stringify(body) }),
  update: (id, body) => api("/api/journal/" + id, { method: "PATCH", body: JSON.stringify(body) }),
  remove: (id) => api("/api/journal/" + id, { method: "DELETE" }),
};
if (window.Journal) Journal.init(journalApi);

// P6: simulation-only paper-trading CRUD network. app.js owns the fetch;
// paper.js renders + calls these (the pure-consumer boundary).
const paperApi = {
  state: () => api("/api/paper", { method: "GET" }),
  order: (b) => api("/api/paper/order", { method: "POST", body: JSON.stringify(b) }),
  close: (b) => api("/api/paper/close", { method: "POST", body: JSON.stringify(b) }),
  cancel: (b) => api("/api/paper/order/cancel", { method: "POST", body: JSON.stringify(b) }),
  wallet: (b) => api("/api/paper/wallet", { method: "POST", body: JSON.stringify(b) }),
  sltp: (b) => api("/api/paper/sltp", { method: "POST", body: JSON.stringify(b) }),
};
if (window.Paper) Paper.init(paperApi);

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
  } catch (err) {
    for (const id of ["page-analytics", "page-review"]) {
      const e = $(id); if (e) e.textContent = "Could not load: " + err.message;
    }
  }
}
// The Journal page is the standalone user journal (P5, full CRUD via journal.js);
// Review + Analytics stay on the recommendation-performance dashboard.
window.addEventListener("ms-page", (e) => {
  if (e.detail === "journal") { if (window.Journal) Journal.mount($("page-journal")); }
  else if (e.detail === "paper") { if (window.Paper) Paper.mount($("page-paper")); }
  else if (e.detail === "review" || e.detail === "analytics") loadDataPages();
});
for (const btn of document.querySelectorAll("[data-refresh]")) {
  btn.addEventListener("click", () => {
    const t = btn.getAttribute("data-refresh");
    if (t === "journal" && window.Journal) Journal.reload();
    else if (t === "paper" && window.Paper) Paper.reload();
    else loadDataPages();
  });
}

// P6: paper-position markers on the Live chart — entry + liquidation price lines
// for the active symbol's open simulated position (display-only).
let paperLines = [];
let paperPos = null;                 // active symbol's open paper position (for the on-chart widget)
let tradeMode = null;                // "flat" | "pos" — rebuild the widget only on a state change
let ctQty = "0.01";                  // remembered quantity across rebuilds
const CHART_TRADE_LEV = 10;          // default leverage for on-chart quick trades

function clearPaperLines() {
  paperLines.forEach((l) => { try { mainSeries.removePriceLine(l); } catch (e) { /* empty */ } });
  paperLines = [];
}
async function updatePaperMarkers() {
  if (replayMode || !window.Paper || !TOKEN) { paperPos = null; syncTradeWidget(); return; }
  try {
    const st = await paperApi.state();
    clearPaperLines();
    const here = (st.positions || []).filter((p) => p.symbol === activeSymbol);
    here.forEach((p) => {                                 // LIQ tag on the price axis; entry/SL/TP are the draggable HTML overlay
      if (p.liq_price) paperLines.push(mainSeries.createPriceLine({ price: p.liq_price,
        color: "#f59e0b", lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "LIQ" }));
    });
    paperPos = here[0] || null;
    syncTradeWidget();
  } catch (e) { /* not configured / no positions */ }
}
setInterval(updatePaperMarkers, 5000);

/* ---------- on-chart quick trade (scalper): BUY / SELL + live P&L + Close ---------- */
function livePrice() { return liveBar ? liveBar.close : (paperPos ? paperPos.avg_entry : null); }
function money2(v) { return (v < 0 ? "-$" : "+$") + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }

async function chartTrade(side) {
  const qi = $("ct-qty");
  const qty = qi ? parseFloat(qi.value) : NaN;
  if (!(qty > 0)) { note("enter a quantity"); return; }
  try {
    await paperApi.order({ symbol: activeSymbol, side, type: "market", qty, leverage: CHART_TRADE_LEV });
    note((side === "BUY" ? "Bought " : "Sold ") + qty + " " + activeSymbol + " (paper)");
    await updatePaperMarkers();                         // immediate sync (don't wait for the poll)
  } catch (e) { note("order: " + (e.message || e)); }
}
async function chartClose() {
  if (!paperPos) return;
  try { await paperApi.close({ position_id: paperPos.id }); note("position closed — logged"); await updatePaperMarkers(); }
  catch (e) { note("close: " + (e.message || e)); }
}

function _ct(tag, cls, txt) { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; }
function buildTradeWidget(w) {
  const cur = $("ct-qty"); if (cur) ctQty = cur.value || ctQty;   // preserve the typed qty
  w.textContent = "";
  if (paperPos) {
    const p = paperPos;
    w.appendChild(_ct("span", "ct-pos " + (p.side === "LONG" ? "up" : "down"), p.side + " " + p.qty));
    w.appendChild(_ct("span", "ct-at", "@ " + fmt(p.avg_entry)));
    w.appendChild(_ct("span", "ct-pnl", "—"));
    const close = _ct("button", "ct-close", "Close");
    close.addEventListener("click", chartClose);
    w.appendChild(close);
  } else {
    w.appendChild(_ct("span", "ct-lbl", "Qty"));
    const qi = _ct("input", "ct-qty"); qi.id = "ct-qty"; qi.type = "number"; qi.step = "any";
    qi.value = ctQty; qi.title = "Quantity to buy / sell (paper)";
    qi.addEventListener("input", () => { ctQty = qi.value; });
    w.appendChild(qi);
    const buy = _ct("button", "ct-buy", "BUY"); buy.addEventListener("click", () => chartTrade("BUY"));
    const sell = _ct("button", "ct-sell", "SELL"); sell.addEventListener("click", () => chartTrade("SELL"));
    w.appendChild(buy); w.appendChild(sell);
  }
}
function syncTradeWidget() {
  const w = $("chart-trade"); if (!w) return;
  const mode = paperPos ? "pos" : "flat";
  if (mode !== tradeMode || (paperPos && w.__pid !== paperPos.id)) {
    tradeMode = mode; w.__pid = paperPos ? paperPos.id : null;
    buildTradeWidget(w);
    olBuild();                                            // (re)build the on-chart entry / SL / TP lines
  }
  updateTradePnl(livePrice());
}
function updateTradePnl(price) {
  const el = $("ct-pnl"); if (!el || !paperPos || price == null) return;
  const pnl = (paperPos.side === "LONG" ? price - paperPos.avg_entry : paperPos.avg_entry - price) * paperPos.qty;
  el.textContent = money2(pnl);
  el.className = "ct-pnl " + (pnl > 0 ? "up" : pnl < 0 ? "down" : "");
}

/* -------- draggable on-chart order lines (Delta-style): entry + P&L, drag SL / TP --------
   HTML rows positioned each animation frame by priceToCoordinate; SL/TP drag to a price
   and commit via /api/paper/sltp. Pointer math only — no engine math, no network here
   beyond the paperApi callback (app.js owns the call; the modules stay pure). */
const olEls = {};                         // { entry, sl, tp } row elements
let olRAF = null;
let olDrag = null;                        // { kind:"sl"|"tp", price } while dragging

function olClear() {
  const c = $("chart-orders"); if (c) c.textContent = "";
  olEls.entry = olEls.sl = olEls.tp = null;
  if (olRAF) { cancelAnimationFrame(olRAF); olRAF = null; }
}
function olMakeRow(kind, sideCls) {
  const c = $("chart-orders"); if (!c) return null;
  const row = document.createElement("div");
  row.className = "ol-row ol-" + kind + (sideCls ? " " + sideCls : "");
  const label = document.createElement("span"); label.className = "ol-label";
  row.appendChild(label);
  if (kind === "sl" || kind === "tp") {
    row.classList.add("ol-drag");
    row.title = "Drag to set " + (kind === "sl" ? "stop-loss" : "take-profit");
    row.addEventListener("mousedown", (e) => { e.preventDefault(); e.stopPropagation(); olDrag = { kind, price: null }; });
  }
  c.appendChild(row);
  return row;
}
function olSet(row, price, text, labelCls) {
  if (!row) return;
  const y = price == null ? null : mainSeries.priceToCoordinate(price);
  if (y == null) { row.style.display = "none"; return; }
  row.style.display = ""; row.style.top = y + "px";
  const l = row.querySelector(".ol-label");
  if (l) { l.textContent = text; l.className = "ol-label" + (labelCls ? " " + labelCls : ""); }
}
function olDefault(kind) {                 // suggested bracket when none is set yet (±1% / ±2% of entry)
  const p = paperPos; if (!p) return null;
  const d = kind === "sl" ? 0.01 : 0.02;
  const long = p.side === "LONG";
  return (long === (kind === "tp")) ? p.avg_entry * (1 + d) : p.avg_entry * (1 - d);
}
function olTick() {
  if (!paperPos) { olClear(); return; }
  const p = paperPos, price = livePrice();
  const pnl = price != null ? (p.side === "LONG" ? price - p.avg_entry : p.avg_entry - price) * p.qty : 0;
  olSet(olEls.entry, p.avg_entry, "ENTRY " + fmt(p.avg_entry) + "   " + money2(pnl),
        pnl > 0 ? "up" : pnl < 0 ? "down" : "");
  if (!(olDrag && olDrag.kind === "sl")) {
    const v = p.sl != null ? p.sl : olDefault("sl");
    olSet(olEls.sl, v, (p.sl != null ? "SL " : "SL · drag ") + fmt(v));
    if (olEls.sl) olEls.sl.classList.toggle("ol-unset", p.sl == null);
  }
  if (!(olDrag && olDrag.kind === "tp")) {
    const v = p.tp != null ? p.tp : olDefault("tp");
    olSet(olEls.tp, v, (p.tp != null ? "TP " : "TP · drag ") + fmt(v));
    if (olEls.tp) olEls.tp.classList.toggle("ol-unset", p.tp == null);
  }
  olRAF = requestAnimationFrame(olTick);
}
function olBuild() {
  olClear();
  if (!paperPos || !$("chart-orders")) return;
  olEls.entry = olMakeRow("entry", paperPos.side === "LONG" ? "up" : "down");
  olEls.sl = olMakeRow("sl");
  olEls.tp = olMakeRow("tp");
  olTick();
}
document.addEventListener("mousemove", (e) => {
  if (!olDrag || !paperPos) return;
  const c = $("chart-orders"); if (!c) return;
  const price = mainSeries.coordinateToPrice(e.clientY - c.getBoundingClientRect().top);
  if (price == null || price <= 0) return;
  olDrag.price = price;
  const row = olEls[olDrag.kind];
  if (row) { row.classList.remove("ol-unset"); olSet(row, price, (olDrag.kind === "sl" ? "SL " : "TP ") + fmt(price)); }
});
document.addEventListener("mouseup", () => {
  const d = olDrag; olDrag = null;
  if (!d || !paperPos || d.price == null) return;
  const body = { position_id: paperPos.id, sl: paperPos.sl, tp: paperPos.tp };
  body[d.kind] = d.price;
  paperApi.sltp(body).then(() => updatePaperMarkers())
    .catch((err) => note("sl/tp: " + (err.message || err)));
});

/* ---------- Active Trade Setup ON the chart (Phase 3 M2) ----------
   Read-only visualization of the top /api/setups setup, drawn on price so the
   trade is understood by looking at the chart. Pure render of backend values —
   entry (brightest) / stop / TP1 / TP2 lines + a subtle R:R shaded region + a
   direction·grade badge. No derivation. ONE rAF loop; rebuilt only when the
   setup IDENTITY changes (no flicker/duplication); cleared on no-setup/replay. */
const suEls = {};                    // { reward, risk, entry, sl, tp1, tp2 }
let suRAF = null, suId = null;
function activeSetup() {
  const d = lastSetups[activeSymbol];
  return (d && d.setups && d.setups.length) ? d.setups[0] : null;
}
function suClear() {
  const c = $("chart-setup"); if (c) c.textContent = "";
  suEls.reward = suEls.risk = suEls.entry = suEls.sl = suEls.tp1 = suEls.tp2 = null;
  if (suRAF) { cancelAnimationFrame(suRAF); suRAF = null; }
}
function suNode(cls) {
  const c = $("chart-setup"); if (!c) return null;
  const d = document.createElement("div"); d.className = cls;
  if (cls.indexOf("su-line") === 0) d.appendChild(Object.assign(document.createElement("span"), { className: "su-tag" }));
  c.appendChild(d); return d;
}
function suSetRegion(div, pa, pb) {
  if (!div) return;
  const ya = pa == null ? null : mainSeries.priceToCoordinate(pa);
  const yb = pb == null ? null : mainSeries.priceToCoordinate(pb);
  if (ya == null || yb == null) { div.style.display = "none"; return; }
  div.style.display = ""; div.style.top = Math.min(ya, yb) + "px"; div.style.height = Math.abs(ya - yb) + "px";
}
function suSetLine(row, price, build) {
  if (!row) return;
  const y = price == null ? null : mainSeries.priceToCoordinate(price);
  if (y == null) { row.style.display = "none"; return; }
  row.style.display = ""; row.style.top = y + "px";
  const l = row.querySelector(".su-tag"); if (l && build) { l.textContent = ""; build(l); }
}
function _chip(cls, txt) { return Object.assign(document.createElement("span"), { className: cls, textContent: txt }); }
function suTick() {
  const s = activeSetup();
  if (!s) { suClear(); return; }                         // vanished mid-loop -> stop (banner on next render)
  suSetRegion(suEls.reward, s.entry, s.tp1);             // reward: entry -> TP1 (defines the stated R:R)
  suSetRegion(suEls.risk, s.entry, s.sl);                // risk: entry -> stop
  suSetLine(suEls.entry, s.entry, (l) => {               // brightest, with the direction·grade badge
    l.appendChild(_chip("su-badge " + (s.direction === "LONG" ? "su-b-long" : "su-b-short"), s.direction + " " + s.grade));
    l.appendChild(_chip("su-k", "ENTRY"));
    l.appendChild(_chip("su-p", fmt(s.entry)));
    if (s.rr != null) l.appendChild(_chip("su-rr", "R:R " + s.rr));
  });
  suSetLine(suEls.sl, s.sl, (l) => { l.appendChild(_chip("su-p", "STOP " + fmt(s.sl))); l.appendChild(_chip("su-k", "· invalidation")); });
  suSetLine(suEls.tp1, s.tp1, (l) => { l.appendChild(_chip("su-k", "TP1")); l.appendChild(_chip("su-p", fmt(s.tp1))); });
  if (suEls.tp2) suSetLine(suEls.tp2, s.tp2, (l) => { l.appendChild(_chip("su-k", "TP2")); l.appendChild(_chip("su-p", fmt(s.tp2))); });
  suRAF = requestAnimationFrame(suTick);
}
function suBuild() {
  suClear();
  const c = $("chart-setup"); if (!c) return;
  const s = activeSetup();
  if (!s) {                                              // no setup -> clean chart + a calm banner
    const b = _chip("su-banner", (lastSetups[activeSymbol] && lastSetups[activeSymbol].message) || "No high-probability setup available.");
    c.appendChild(b);
    return;
  }
  suEls.reward = suNode("su-region su-reward");
  suEls.risk = suNode("su-region su-risk");
  suEls.tp1 = suNode("su-line su-tp1");                  // targets under entry/stop in the DOM;
  suEls.tp2 = (s.tp2 != null) ? suNode("su-line su-tp2") : null;
  suEls.sl = suNode("su-line su-sl");
  suEls.entry = suNode("su-line su-entry");              // entry appended last = brightest, on top
  suTick();
}
// Rebuild only when the setup identity changes -> the running loop just tracks
// zoom/pan otherwise (survives polling/symbol/timeframe; no duplicate loops).
function renderSetupOverlay() {
  if (replayMode) { suClear(); suId = "__replay"; return; }
  const s = activeSetup();
  const id = s ? s.id : (lastSetups[activeSymbol] ? "__none" : "__wait");
  if (id === suId) return;
  suId = id;
  suBuild();
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
      renderHtf();                                     // HTF alignment tracks the live signal
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
/* ---------------------------------------------------------- HTF panel (V1.1) */
// Higher-timeframe intelligence from GET /api/htf (the isolated HtfService).
// Polled slowly (the analysis only changes when an HTF candle closes); the
// alignment badge re-renders on every live signal via the cached result.
const HTF_POLL_MS = 45000;
let lastHtf = {};
function htfDirection() {
  const st = lastStructure[activeSymbol];
  const recs = (st && st.recommendations) || [];
  if (recs.length) return recs[recs.length - 1].direction;
  const sigs = (st && st.signals) || [];
  return sigs.length ? sigs[sigs.length - 1].direction : null;
}
function renderHtf() {
  if (window.Htf) Htf.render(lastHtf[activeSymbol] || null, htfDirection());
  renderStrip();                   // the context strip reads HTF + the top setup
}
// M2.5 context strip — the market conversation Q1..Q5, straight from the two frozen
// caches (/api/htf + /api/setups). Re-rendered whenever either source updates.
function renderStrip() {
  if (window.Strip) Strip.render(lastHtf[activeSymbol] || null, activeSetup());
}
async function loadHtf() {
  if (!window.Htf) return;
  try {
    const resp = await fetch(`${HTTP_BASE}/api/htf?symbol=${encodeURIComponent(activeSymbol)}`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
    });
    if (resp.status === 401) { onAuthFail(); return; }
    if (!resp.ok) return;
    const data = await resp.json();
    lastHtf[data.symbol] = data;
    if (data.symbol === activeSymbol) renderHtf();
  } catch (e) { /* transient — the next poll retries */ }
}
setInterval(loadHtf, HTF_POLL_MS);

// Trade Setups from GET /api/setups (Trade Engine V2, frozen contract v1.0).
// Event-driven (a fresh sweep->shift trigger), so polled a little faster than HTF.
const SETUPS_POLL_MS = 20000;
let lastSetups = {};
function renderSetups() {
  if (window.Setups) Setups.render(lastSetups[activeSymbol] || null);
  renderSetupOverlay();            // draw the active setup ON the chart (M2)
  renderStrip();                   // and the strip's Trend/Draw/Setup tiles
}
async function loadSetups() {
  if (!window.Setups || replayMode) return;            // live only; replay clears it
  try {
    const resp = await fetch(`${HTTP_BASE}/api/setups?symbol=${encodeURIComponent(activeSymbol)}`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
    });
    if (resp.status === 401) { onAuthFail(); return; }
    if (!resp.ok) return;
    const data = await resp.json();
    lastSetups[data.symbol] = data;
    if (data.symbol === activeSymbol) renderSetups();
  } catch (e) { /* transient — the next poll retries */ }
}
setInterval(loadSetups, SETUPS_POLL_MS);

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
      if (needData) loadHistory(activeSymbol, { preserveView: true });   // keep the view on an indicator refetch
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
  if (reset) reset.addEventListener("click", () => loadHistory(activeSymbol, { resetView: true }));   // fresh recent view + auto price
  const auto = $("tb-autoscale");
  if (auto) {
    auto.classList.add("on");
    auto.addEventListener("click", () => { if (priceAutoScale) setPriceAutoScale(false); else refitPrice(); });
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
  loadHtf();          // HTF V1.1 panel (then polled every HTF_POLL_MS)
  loadSetups();       // Trade Setup V2 panel (then polled every SETUPS_POLL_MS)
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

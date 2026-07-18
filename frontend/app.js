/* MarketScalper frontend (roadmap P0.22 shell + P0.23 live chart).
 *
 * Thin client, exactly per §9: history bootstraps once through the existing
 * REST /candles endpoint; live candles apply as diff-only series.update()
 * calls — no full redraws. On reconnect: reconnect, reload history through
 * the same REST endpoint, continue. Nothing else: no client-side gap
 * detection, replay, deduplication, buffering, recovery logic, caching or
 * storage — the backend owns data correctness.
 *
 * Vanilla JS. No frameworks. Token lives in memory only.
 */

"use strict";

/* ---------------------------------------------------------- configuration */

const params = new URLSearchParams(window.location.search);
const API_HOST =
  params.get("api") || window.location.host || "127.0.0.1:8000";
const TOKEN = params.get("token") || window.prompt("MarketScalper API token") || "";

const SYMBOLS = ["BTCUSDT", "ETHUSDT"];        // frozen v1 pair (Architecture §0)
const LOOKBACK_1M_MS = 24 * 3600 * 1000;       // history bootstrap depth
const LOOKBACK_5M_MS = 24 * 3600 * 1000;

const urlSymbol = (params.get("symbol") || "").toUpperCase();
let activeSymbol = SYMBOLS.includes(urlSymbol) ? urlSymbol : SYMBOLS[0];

/* ----------------------------------------------------------------- charts */

const SERIES_OPTS = {
  upColor: "#22C55E",                          // semantic green (token)
  downColor: "#EF4444",                        // semantic red (token)
  wickUpColor: "#22C55E",
  wickDownColor: "#EF4444",
  borderVisible: false,
};

function makeChart(el) {
  return LightweightCharts.createChart(el, {
    autoSize: true,
    layout: {
      background: { color: "#0A0F1E" },        // --surface
      textColor: "#8B93A7",
      fontFamily:
        'ui-monospace, "SF Mono", "Cascadia Mono", "JetBrains Mono", Consolas, monospace',
    },
    grid: {
      vertLines: { color: "rgba(255,255,255,0.06)" },
      horzLines: { color: "rgba(255,255,255,0.06)" },
    },
    rightPriceScale: { borderColor: "rgba(255,255,255,0.14)" },
    timeScale: { borderColor: "rgba(255,255,255,0.14)", timeVisible: true },
  });
}

const mainChart = makeChart(document.getElementById("chart"));
const mainSeries = mainChart.addSeries(LightweightCharts.CandlestickSeries, SERIES_OPTS);
Overlays.init(mainChart, mainSeries);            // P1.19 overlays + P1.20 audit
Panel.init(quickLogSubmit);                      // P3.19 panel + P4.7 quick-log
const lastStructure = {};                        // latest payload per symbol

const stripChart = makeChart(document.getElementById("strip"));
const stripSeries = stripChart.addSeries(LightweightCharts.CandlestickSeries, SERIES_OPTS);

function toBar(c) {
  return {
    time: Math.floor(Date.parse(c.ts) / 1000),
    open: c.o, high: c.h, low: c.l, close: c.c,
  };
}

/* -------------------------------------------------------------- bootstrap */

const lastEvent = document.getElementById("last-event");

async function fetchCandles(symbol, tf, lookbackMs) {
  const end = new Date();
  const start = new Date(end.getTime() - lookbackMs);
  const qs = new URLSearchParams({
    symbol, tf, start: start.toISOString(), end: end.toISOString(),
  });
  const resp = await fetch(`http://${API_HOST}/candles?${qs}`, {
    headers: { Authorization: `Bearer ${TOKEN}` },
  });
  if (!resp.ok) throw new Error(`history ${symbol}/${tf}: HTTP ${resp.status}`);
  return resp.json();
}

async function loadHistory(symbol) {
  try {
    const [oneM, fiveM] = await Promise.all([
      fetchCandles(symbol, "1m", LOOKBACK_1M_MS),
      fetchCandles(symbol, "5m", LOOKBACK_5M_MS),
    ]);
    mainSeries.setData(oneM.map(toBar));       // bootstrap is the ONLY setData path
    stripSeries.setData(fiveM.map(toBar));
    lastEvent.textContent = `history loaded: ${symbol} (${oneM.length}×1m, ${fiveM.length}×5m)`;
  } catch (err) {
    lastEvent.textContent = String(err);       // next switch/reconnect reloads
  }
}

/* -------------------------------------------------------- symbol switcher */

function setSymbol(symbol) {
  activeSymbol = symbol;
  for (const s of SYMBOLS) {
    document.getElementById(`sym-${s}`).classList.toggle("active", s === symbol);
  }
  loadHistory(symbol);
  Overlays.setStructure(lastStructure[symbol]);  // redraw from cached payload
  Panel.setStructure(lastStructure[symbol]);     // P3.19 panel follows symbol
}

for (const s of SYMBOLS) {
  document.getElementById(`sym-${s}`).addEventListener("click", () => setSymbol(s));
}
document.getElementById(`sym-${activeSymbol}`).classList.add("active");

/* --------------------------------------------------- replay controls (P0.25)
 * Start / stop / speed / date-range. Replay candles arrive through the SAME
 * WebSocket payload as live candles. F2: while a replay runs the server
 * suppresses the live push, so the chart is cleared at start and rendered
 * from the replay stream; a status poll detects completion and re-bootstraps
 * the live chart. Still a thin client — no replay data logic here. */

let replayMode = false;
let replayPoll = null;

function enterReplayMode() {
  replayMode = true;
  mainSeries.setData([]);                    // replay owns the chart now
  stripSeries.setData([]);
  delete lastStructure[activeSymbol];
  Overlays.setStructure(null);
  Panel.setStructure(null);                  // P3.19: clear the panel too
  if (replayPoll) clearInterval(replayPoll);
  replayPoll = setInterval(async () => {
    try {
      const status = await replayCall("/replay/status", { method: "GET" });
      if (!status.running) exitReplayMode("replay finished");
    } catch (err) { /* transient poll failure: keep polling */ }
  }, 2000);
}

function exitReplayMode(note) {
  if (!replayMode) return;
  replayMode = false;
  if (replayPoll) { clearInterval(replayPoll); replayPoll = null; }
  lastEvent.textContent = note;
  loadHistory(activeSymbol);                 // re-bootstrap the live chart
}

async function replayCall(path, options) {
  const resp = await fetch(`http://${API_HOST}${path}`, {
    headers: { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json" },
    ...options,
  });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(body.detail || `HTTP ${resp.status}`);
  return body;
}

/* P4.7: the manual quick-log PATCH. panel.js renders the form and calls
 * this (the network stays in app.js so panel.js is a pure consumer). */
async function quickLogSubmit(recId, fields) {
  return replayCall(`/journal/${recId}`, {
    method: "PATCH",
    body: JSON.stringify(fields),
  });
}

document.getElementById("replay-start").addEventListener("click", async () => {
  const from = document.getElementById("replay-from").value;
  const to = document.getElementById("replay-to").value;
  const rawSpeed = document.getElementById("replay-speed").value;
  const speed = rawSpeed === "max" ? "max" : Number(rawSpeed);
  try {
    const body = JSON.stringify({
      symbol: activeSymbol,
      start: new Date(from).toISOString(),
      end: new Date(to).toISOString(),
      speed,
    });
    const status = await replayCall("/replay/start", { method: "POST", body });
    enterReplayMode();                       // F2: replay owns the chart
    lastEvent.textContent = `replay running: ${status.symbol} ×${status.speed}`;
  } catch (err) {
    lastEvent.textContent = `replay: ${err.message}`;
  }
});

document.getElementById("replay-stop").addEventListener("click", async () => {
  try {
    await replayCall("/replay/stop", { method: "POST" });
    exitReplayMode("replay stopped");        // F2: back to the live chart
  } catch (err) {
    lastEvent.textContent = `replay: ${err.message}`;
  }
});

/* ------------------------------------------- WebSocket client + reconnect */

const BACKOFF_INITIAL_MS = 1000;
const BACKOFF_CAP_MS = 30000;

const connDot = document.getElementById("conn-dot");
const connText = document.getElementById("conn-text");
const wsTarget = document.getElementById("ws-target");

let backoffMs = BACKOFF_INITIAL_MS;

function setStatus(state, detail) {
  connText.textContent = detail ? `${state} ${detail}` : state;
  connDot.className = "dot" + (state === "LIVE" ? " live" : state === "RECONNECTING" ? " down" : "");
}

function connect() {
  setStatus("CONNECTING");
  wsTarget.textContent = `ws://${API_HOST}/ws`;
  const ws = new WebSocket(`ws://${API_HOST}/ws?token=${encodeURIComponent(TOKEN)}`);

  ws.onopen = () => {
    backoffMs = BACKOFF_INITIAL_MS;
    setStatus("LIVE");
    loadHistory(activeSymbol);                 // initial load AND reconnect reload
  };

  ws.onmessage = (event) => {
    lastEvent.textContent = `last event: ${new Date().toISOString()}`;
    const msg = JSON.parse(event.data);
    const diff = msg.state_diff || {};
    for (const sym of Object.keys(diff)) {               // cache engine state
      if (diff[sym].structure) lastStructure[sym] = diff[sym].structure;
    }
    const candle = msg.candle;
    if (candle.symbol !== activeSymbol) return;          // client-side filter only
    try {
      if (candle.tf === "1m") mainSeries.update(toBar(candle)); // diff-only (§9)
      else if (candle.tf === "5m") stripSeries.update(toBar(candle));
    } catch (err) {
      // F2: a stray in-flight live bar during the replay-mode transition
      // would be older/newer than the freshly reset series — drop it.
      console.warn("chart update skipped:", err.message);
    }
    if (diff[activeSymbol] && diff[activeSymbol].structure) {
      // P2.21: pass the already-available close through — transport
      // only, no calculation; Overlays uses it for premium/discount.
      Overlays.setStructure(diff[activeSymbol].structure, candle.c);
      Panel.setStructure(diff[activeSymbol].structure, candle.ts);  // P3.19/P4.10
    }
  };

  ws.onclose = () => {
    setStatus("RECONNECTING", `in ${Math.round(backoffMs / 1000)}s`);
    window.setTimeout(connect, backoffMs);
    backoffMs = Math.min(backoffMs * 2, BACKOFF_CAP_MS);
  };

  ws.onerror = () => {
    ws.close();
  };
}

connect();

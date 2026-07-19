/* MarketScalper — desktop + PWA notifications (pre-prod items 6/8).
 *
 * window.Notify. Driven by app.js from the live stream (trade setups, feed
 * up/down). Uses the browser Notification API, which fires even when the tab is
 * open but NOT focused. When the browser is fully closed, Telegram (backend) is
 * the channel — so this is the "at my desk" channel and Telegram is the "away"
 * channel. Registers a minimal service worker for PWA installability + reliable
 * notification display.
 *
 * UI chrome only: it does NOT fetch data (app.js owns the network) and does NOT
 * touch the analysis payload. It respects the owner's toggles (app.js pushes
 * them via Notify.setPrefs after reading GET /settings).
 */
(function () {
  "use strict";

  var prefs = { desktop: true, trade_alerts: true, system_alerts: true };
  var swReg = null;

  function setPrefs(p) { if (p) { for (var k in p) prefs[k] = p[k]; } }

  function supported() { return ("Notification" in window); }

  function request() {
    if (!supported()) return Promise.resolve("unsupported");
    try { return Notification.requestPermission(); }
    catch (e) { return Promise.resolve("default"); }
  }

  function permission() { return supported() ? Notification.permission : "unsupported"; }

  function canShow() {
    return supported() && Notification.permission === "granted" && prefs.desktop;
  }

  function show(title, body, tag) {
    if (!canShow()) return;
    var opts = { body: body, tag: tag, icon: "icon.svg", badge: "icon.svg", renotify: true };
    try {
      if (swReg && swReg.showNotification) { swReg.showNotification(title, opts); return; }
    } catch (e) { /* fall through to the page Notification */ }
    try { new Notification(title, opts); } catch (e2) { /* ignore */ }
  }

  function tradeSetup(sym, rec) {
    if (!prefs.trade_alerts || !rec) return;
    var high = rec.verdict === "A_PLUS";
    show((high ? "🚀 High-conviction setup" : "📈 Trade setup") + " — " + sym,
      (rec.direction || "") + " · " + (rec.strategy || "") + " · "
        + (rec.score != null ? rec.score + "/100" : "") + " · entry " + rec.entry,
      "setup-" + sym);
  }

  function feed(connected) {
    if (!prefs.system_alerts) return;
    show(connected ? "✅ Feed reconnected" : "⚠️ Feed disconnected",
      connected ? "Receiving market data again." : "Auto-reconnect is running.",
      "feed-status");
  }

  function error(msg) {
    if (!prefs.system_alerts) return;
    show("❌ Critical error", msg || "See the app for details.", "error");
  }

  function registerSW() {
    if (!("serviceWorker" in navigator)) return;
    navigator.serviceWorker.register("sw.js")
      .then(function (r) { swReg = r; })
      .catch(function () { /* PWA optional; desktop notifications still work */ });
  }

  window.Notify = {
    setPrefs: setPrefs, request: request, permission: permission,
    supported: supported, show: show, tradeSetup: tradeSetup, feed: feed,
    error: error, registerSW: registerSW,
  };
})();

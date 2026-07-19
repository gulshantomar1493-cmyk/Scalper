/* MarketScalper service worker (pre-prod item 6) — minimal, on purpose.
 *
 * It exists so the app is an installable PWA and can display notifications
 * reliably. It DELIBERATELY does not cache app assets: the terminal is online-
 * first (it needs the API and live feed), and caching JS/CSS would risk serving
 * a stale build after an update. So there is no fetch handler — every request
 * goes straight to the network, exactly as without a worker.
 */
self.addEventListener("install", function () { self.skipWaiting(); });

self.addEventListener("activate", function (e) {
  e.waitUntil(self.clients.claim());
});

// Focus (or open) the app when a notification is clicked.
self.addEventListener("notificationclick", function (e) {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: "window" }).then(function (list) {
    for (var i = 0; i < list.length; i++) {
      if ("focus" in list[i]) return list[i].focus();
    }
    if (self.clients.openWindow) return self.clients.openWindow("./");
  }));
});

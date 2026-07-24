/* MarketScalper — help routing (V3). The old overlay guide is REPLACED by the
 * Dashboard -> Learn tab (the V3 Analysis Guide in simple Hinglish). Both the
 * Live-header ❓ button (help-open) and the global sidebar "Need help?" button
 * (sidebar-help) route there. Pure UI navigation: no data, no network, no
 * engine math, no storage. It never auto-opens. */
(function () {
  "use strict";
  function openLearn() {
    window.location.hash = "#/dashboard";          // shell.js routes the page
    var pick = function () {
      var t = document.getElementById("dash-tab-learn");
      if (t) t.click();
    };
    pick();
    setTimeout(pick, 0);                            // retry after the page switch
  }
  var headerBtn = document.getElementById("help-open");
  if (headerBtn) headerBtn.addEventListener("click", openLearn);
  var sidebarBtn = document.getElementById("sidebar-help");
  if (sidebarBtn) sidebarBtn.addEventListener("click", openLearn);
})();

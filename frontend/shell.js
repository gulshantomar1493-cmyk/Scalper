/* MarketScalper — application shell (Phase 2 Step 1).
 *
 * Sidebar navigation + a tiny hash router that shows one page at a time, plus
 * the beginner-mode toggle. Pure UI/navigation: no data, no network, no candle
 * or indicator logic — the backend stays the single source of truth. Page and
 * beginner-mode choices persist in localStorage (a UI pref, not the token). */
(function () {
  "use strict";
  var PAGES = ["live", "replay", "review", "journal", "analytics", "settings"];
  var PAGE_KEY = "ms_page", BEG_KEY = "ms_beginner";
  var root = document.documentElement;

  function show(name) {
    if (PAGES.indexOf(name) < 0) name = "live";
    var pages = document.querySelectorAll(".page");
    for (var i = 0; i < pages.length; i++) {
      pages[i].classList.toggle("active", pages[i].getAttribute("data-page") === name);
    }
    var navs = document.querySelectorAll(".sb-nav");
    for (var j = 0; j < navs.length; j++) {
      navs[j].classList.toggle("on", navs[j].getAttribute("data-nav") === name);
    }
    try { window.localStorage.setItem(PAGE_KEY, name); } catch (e) { /* ignore */ }
    // let app.js load a data page's content on demand (thin: app.js owns the fetch)
    window.dispatchEvent(new CustomEvent("ms-page", { detail: name }));
  }

  function fromHash() {
    var m = (window.location.hash || "").replace(/^#\/?/, "");
    return PAGES.indexOf(m) >= 0 ? m : null;
  }

  // sidebar clicks -> drive the hash (so back/forward + refresh work)
  var navs = document.querySelectorAll(".sb-nav");
  for (var k = 0; k < navs.length; k++) {
    (function (btn) {
      btn.addEventListener("click", function () {
        var name = btn.getAttribute("data-nav");
        window.location.hash = "#/" + name;   // for back/forward + refresh
        show(name);                            // switch now (don't wait on hashchange)
      });
    })(navs[k]);
  }
  window.addEventListener("hashchange", function () { show(fromHash() || "live"); });

  // initial route: hash > last saved > live
  var start = fromHash();
  if (!start) { try { start = window.localStorage.getItem(PAGE_KEY); } catch (e) {} }
  show(PAGES.indexOf(start) >= 0 ? start : "live");

  // ---- beginner mode (helper text on/off; the head script applied it early) ----
  var bt = document.getElementById("beginner-toggle");
  function setBeginner(on) {
    root.setAttribute("data-beginner", on ? "on" : "off");
    if (bt) { bt.setAttribute("aria-checked", on ? "true" : "false"); bt.classList.toggle("on", on); }
    try { window.localStorage.setItem(BEG_KEY, on ? "on" : "off"); } catch (e) {}
  }
  setBeginner(root.getAttribute("data-beginner") !== "off");
  if (bt) bt.addEventListener("click", function () {
    setBeginner(root.getAttribute("data-beginner") !== "on");
  });

  // ---- sidebar "Need help?" -> route to Live, then open the existing Madad guide ----
  var sh = document.getElementById("sidebar-help");
  if (sh) sh.addEventListener("click", function () {
    window.location.hash = "#/live";
    var open = document.getElementById("help-open");
    if (open) open.click();
  });
})();

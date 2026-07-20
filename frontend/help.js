/* MarketScalper — Help Center (Hinglish). PURE UI.
 *
 * The guide content is STATIC HTML in index.html; this file only shows/hides
 * that overlay and switches between topic sections. No data, no network, no
 * engine math. Reachable from EVERY page via the global sidebar "Need help?"
 * button (and the ❓ in the Live header). It NEVER auto-opens — the owner opens
 * it on demand. */
(function () {
  "use strict";
  const overlay = document.getElementById("help");
  const headerBtn = document.getElementById("help-open");     // ❓ in the Live header
  const sidebarBtn = document.getElementById("sidebar-help"); // global sidebar button (every page)
  const closeBtn = document.getElementById("help-close");
  if (!overlay) return;

  function open() { overlay.classList.add("show"); }
  function close() { overlay.classList.remove("show"); }

  if (headerBtn) headerBtn.addEventListener("click", open);
  if (sidebarBtn) sidebarBtn.addEventListener("click", open);
  if (closeBtn) closeBtn.addEventListener("click", close);
  // click the dim backdrop (outside the panel) to dismiss
  overlay.addEventListener("click", function (e) { if (e.target === overlay) close(); });
  // Esc closes
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") close(); });

  // Topic nav: scroll the content pane to the clicked section + mark it active.
  const navButtons = overlay.querySelectorAll("[data-help-goto]");
  navButtons.forEach(function (btn) {
    btn.addEventListener("click", function () {
      const target = document.getElementById(btn.getAttribute("data-help-goto"));
      if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
      navButtons.forEach(function (b) { b.classList.toggle("active", b === btn); });
    });
  });
})();

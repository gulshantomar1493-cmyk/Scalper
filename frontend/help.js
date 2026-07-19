/* MarketScalper — in-app Hinglish help/guide (P-help; §9 usability).
 *
 * Pure UI: the guide content is STATIC HTML in index.html; this file only
 * shows/hides that overlay. No data, no network, no engine math — it never
 * touches the payload, the live stream, or any computation. The overlay
 * auto-opens once for a first-time visitor (so a new user is not lost) and
 * is always reachable via the header "❓ Madad" button afterwards. */
(function () {
  "use strict";
  const overlay = document.getElementById("help");
  const openBtn = document.getElementById("help-open");
  const closeBtn = document.getElementById("help-close");
  if (!overlay || !openBtn) return;

  function open() { overlay.classList.add("show"); }
  function close() { overlay.classList.remove("show"); }

  openBtn.addEventListener("click", open);
  if (closeBtn) closeBtn.addEventListener("click", close);
  // click the dim backdrop (outside the panel) to dismiss
  overlay.addEventListener("click", function (e) {
    if (e.target === overlay) close();
  });
  // Esc closes
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") close();
  });

  // First visit: auto-open once so a new user gets oriented. A UI-only flag
  // (not sensitive — unlike the API token, which stays in memory). If
  // localStorage is unavailable the button still works.
  try {
    if (!window.localStorage.getItem("ms_help_seen")) {
      open();
      window.localStorage.setItem("ms_help_seen", "1");
    }
  } catch (e) { /* storage blocked — ignore, guide still opens via the button */ }
})();

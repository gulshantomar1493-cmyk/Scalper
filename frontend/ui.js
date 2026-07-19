/* MarketScalper — UI chrome: theme toggle + Replay/Tools drawer.
 *
 * Non-data UI only (no payload, no charts). Owns the light/dark theme
 * preference (persisted) and the collapsible tools drawer (not persisted —
 * home starts clean each load). On a theme change it flips data-theme on the
 * root and dispatches "ms-theme-change" so app.js can re-theme the charts
 * (Lightweight Charts reads its colours from the CSS variables). Default
 * theme is LIGHT CREAM; the header button switches to the locked dark palette. */
(function () {
  "use strict";
  const root = document.documentElement;
  const THEME_KEY = "ms_theme";

  function currentTheme() {
    return root.getAttribute("data-theme") === "dark" ? "dark" : "light";
  }

  // ---- theme toggle ----
  const themeBtn = document.getElementById("theme-toggle");
  function paintThemeBtn() {
    if (!themeBtn) return;
    // label shows the OTHER theme (the action)
    themeBtn.textContent = currentTheme() === "dark" ? "☀️ Light" : "🌙 Dark";
  }
  paintThemeBtn();                                   // head script already set the theme
  if (themeBtn) {
    themeBtn.addEventListener("click", function () {
      const next = currentTheme() === "dark" ? "light" : "dark";
      if (next === "dark") root.setAttribute("data-theme", "dark");
      else root.removeAttribute("data-theme");       // light = default (no attr)
      try { window.localStorage.setItem(THEME_KEY, next); } catch (e) { /* ignore */ }
      paintThemeBtn();
      window.dispatchEvent(new CustomEvent("ms-theme-change"));
    });
  }

  // ---- Replay & Tools drawer (closed by default -> clean live home) ----
  const drawer = document.getElementById("tools-drawer");
  const toolsBtn = document.getElementById("tools-toggle");
  if (drawer && toolsBtn) {
    toolsBtn.addEventListener("click", function () {
      const open = drawer.classList.toggle("open");
      toolsBtn.classList.toggle("active", open);
      toolsBtn.textContent = (open ? "▴ " : "▾ ") + "Replay & Tools";
    });
  }
})();

/* MarketScalper — login overlay (username/password → backend /login → token).
 *
 * Pure UI. It NEVER fetches — app.js owns the network (§9) and exposes
 * window.__msAuth.login(username, password); this file only reads the fields,
 * shows status, and toggles the overlay. On success app.js stores the token
 * (via ui.js) + boots the app and hides the overlay. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);

  function hide() { const o = $("login-overlay"); if (o) o.hidden = true; }
  function show() {
    const o = $("login-overlay"); if (!o) return;
    o.hidden = false;
    const u = $("login-user"); if (u) u.focus();
  }
  function msg(text, bad) {
    const m = $("login-msg");
    if (m) { m.textContent = text || ""; m.className = "login-msg" + (bad ? " bad" : ""); }
  }

  async function submit() {
    const u = ($("login-user").value || "").trim();
    const p = $("login-pass").value || "";
    if (!u || !p) { msg("Enter username and password.", true); return; }
    const btn = $("login-btn"); if (btn) btn.disabled = true;
    msg("Signing in…");
    let res = { ok: false, error: "Login unavailable." };
    try {
      if (window.__msAuth && window.__msAuth.login) res = await window.__msAuth.login(u, p);
    } catch (e) { res = { ok: false, error: "Login failed." }; }
    if (btn) btn.disabled = false;
    if (res && res.ok) { msg(""); $("login-pass").value = ""; }   // app.js hides + boots
    else msg((res && res.error) || "Login failed.", true);
  }

  function init() {
    const btn = $("login-btn"); if (btn) btn.addEventListener("click", submit);
    for (const id of ["login-user", "login-pass"]) {
      const el = $(id);
      if (el) el.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
    }
  }
  init();
  window.Login = { show, hide };
})();

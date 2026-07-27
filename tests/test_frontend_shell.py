"""Frontend contract tests for the V4 terminal.

The frontend is plain static files with no build step and no test runner, so
these guard its structural contracts from Python: the files exist, they parse,
app.js is the ONLY module that touches the network, nothing renders untrusted
strings through innerHTML, and the design tokens the theme depends on are all
defined in one place.

They are cheap and they catch the class of regression a screenshot review
misses (a renamed id, a stray fetch, a token used but never declared).
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess

import pytest

FRONTEND = pathlib.Path(__file__).resolve().parents[1] / "frontend"
HTML = FRONTEND / "index.html"
APP = FRONTEND / "app.js"
CSS = FRONTEND / "styles.css"


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


# ------------------------------------------------------------------ files ---

def test_the_terminal_ships_exactly_the_files_it_references():
    assert HTML.is_file() and APP.is_file() and CSS.is_file()
    html = _read(HTML)
    for src in re.findall(r'(?:src|href)="([^":]+)"', html):
        if src.startswith(("#", "data:")):
            continue
        assert (FRONTEND / src).is_file(), f"index.html references missing {src}"


def test_no_old_ui_files_survived_the_v4_cutover():
    """The V1/V2/V3 UI was removed with its backend. A leftover file would
    ship a page wired to endpoints that no longer exist."""
    gone = ["panel.js", "overlays.js", "setups.js", "strip.js", "v3overlay.js",
            "home.js", "dashboard.js", "history.js", "shell.js", "htf.js",
            "journal.js", "paper.js", "indicators.js", "drawing.js", "ops.js"]
    present = [n for n in gone if (FRONTEND / n).exists()]
    assert present == [], f"old UI files still present: {present}"


def test_app_js_is_valid_javascript():
    try:
        r = subprocess.run(["node", "--check", str(APP)],
                           capture_output=True, text=True)
    except FileNotFoundError:
        pytest.skip("node not available")
    assert r.returncode == 0, r.stderr


# ---------------------------------------------------------------- safety ---

def test_server_data_never_reaches_an_innerhtml_sink():
    """Everything from the backend is written with textContent. innerHTML
    with a server string is the one XSS route this app could have."""
    assert "innerHTML" not in _read(APP)


def test_the_api_token_rides_the_authorization_header():
    src = _read(APP)
    assert "Authorization" in src and "Bearer" in src


def test_every_backend_call_goes_through_the_wrappers():
    """app.js owns network access; api()/post() attach auth and surface
    failures. The only other fetch is the login exchange, which by definition
    runs before a token exists. Any further bare fetch would skip both."""
    src = _read(APP)
    fetches = re.findall(r"\bfetch\(", src)
    assert len(fetches) == 3, f"expected api()/post()/login only, found {len(fetches)}"


def test_an_unauthenticated_visitor_gets_a_login_screen():
    """Production requires credentials; without this the terminal renders
    empty and every call 401s behind a 'backend unreachable' banner."""
    html, src = _read(HTML), _read(APP)
    assert 'id="gate"' in html and 'id="gate-form"' in html
    assert 'type="password"' in html
    assert "/login" in src
    assert "if (TOKEN) boot(); else showGate();" in src


def test_a_rejected_token_returns_the_user_to_the_login_screen():
    """A rotated or stale token must not leave a dead terminal on screen."""
    src = _read(APP)
    assert src.count("if (r.status === 401) { signOut(); ") == 2   # api() and post()
    assert 'localStorage.removeItem("ms_token")' in src


def test_the_password_is_never_persisted():
    src = _read(APP)
    assert "gate-pass" in src
    assert 'setItem("ms_pass' not in src
    assert "password=" not in src


# ------------------------------------------------------------- structure ---

def test_every_screen_exists_and_has_a_nav_button():
    html = _read(HTML)
    pages = set(re.findall(r'data-page="([a-z]+)"', html))
    navs = set(re.findall(r'data-go="([a-z]+)"', html))
    assert pages == {"today", "chart", "strategies", "history", "paper", "journal"}
    assert navs == pages, f"nav/page mismatch: {navs ^ pages}"


def test_every_element_app_js_looks_up_exists_in_the_html():
    html, src = _read(HTML), _read(APP)
    ids = set(re.findall(r'id="([^"]+)"', html))
    for used in set(re.findall(r'\$\("([a-z0-9-]+)"\)', src)):
        assert used in ids, f"app.js reads #{used} which index.html never defines"


def test_a_failed_request_can_surface_a_visible_alert():
    html = _read(HTML)
    assert 'id="banner"' in html and 'role="alert"' in html
    assert "banner(" in _read(APP)


# ----------------------------------------------------------------- theme ---

def test_both_themes_define_every_token_the_stylesheet_uses():
    css = _read(CSS)
    used = set(re.findall(r"var\((--[a-z0-9-]+)", css))
    dark = css[css.index(":root {"):css.index(':root[data-theme="light"]')]
    light = css[css.index(':root[data-theme="light"]'):]
    # tokens may share a line (`--s1: 4px;  --s2: 8px;`), so match every
    # `--name:` in the block rather than only the first on each line
    declared_dark = set(re.findall(r"(--[a-z0-9-]+)\s*:", dark))
    declared_light = set(re.findall(r"(--[a-z0-9-]+)\s*:", light))
    missing = used - declared_dark
    assert not missing, f"tokens used but never declared: {sorted(missing)}"
    # every colour token must be overridden for light; sizes/fonts need not be
    families = {"bg", "ink", "accent", "up", "down", "warn", "long", "short",
                "line", "chart", "glass", "shadow", "scheme"}
    colour_like = {t for t in declared_dark if t[2:].split("-")[0] in families}
    assert not (colour_like - declared_light), \
        f"light theme misses: {sorted(colour_like - declared_light)}"


def test_the_theme_is_applied_before_first_paint():
    """Reading localStorage after the stylesheet would flash the wrong theme."""
    html = _read(HTML)
    head = html[:html.index("</head>")]
    assert "ms_v4_theme" in head and "data-theme" in head


def test_the_chart_reads_its_colours_from_the_token_layer():
    """The chart library cannot read CSS variables, so app.js hands them
    over. A hardcoded hex would not follow the theme."""
    src = _read(APP)
    assert "--chart-bg" in src and "--chart-text" in src
    assert not re.search(r'"#[0-9a-fA-F]{6}"', src), "hardcoded colour in app.js"


# ------------------------------------------------------------------- PWA ---

def test_the_manifest_is_valid_and_points_at_files_that_exist():
    man = json.loads(_read(FRONTEND / "manifest.webmanifest"))
    assert man["name"] and man["start_url"]
    for icon in man["icons"]:
        assert (FRONTEND / icon["src"]).is_file()


def test_the_service_worker_caches_nothing():
    """A caching SW would serve stale JS after every deploy — this one is
    deliberately a no-op (see docs/DEPLOYMENT.md)."""
    sw = _read(FRONTEND / "sw.js")
    assert "caches.open" not in sw and "cache.put" not in sw

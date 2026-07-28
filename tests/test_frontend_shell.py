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
    """Everything from the backend is written with textContent. Assigning
    innerHTML is the one XSS route this app could have — the phrase may still
    appear in a comment, so look for the sink itself."""
    src = _read(APP)
    assert not re.search(r"\.innerHTML\s*=", src)
    assert "insertAdjacentHTML" not in src and "document.write" not in src


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


def test_the_alert_settings_are_reachable_from_the_ui():
    """Telegram is the only channel that works with the app closed, so it must
    be configurable without editing a file on the server."""
    html, src = _read(HTML), _read(APP)
    assert '<section id="settings"' in html and 'id="tg-token"' in html
    assert "/settings/telegram/verify" in src
    assert "/settings/alerts" in src
    assert "proximity_pct" in src


def test_running_trades_are_separated_from_waiting_setups():
    """A filled setup needs managing; a pending one needs deciding. The queue
    lists what is still waiting, above it what is already live."""
    html, src = _read(HTML), _read(APP)
    assert 'id="live-trades"' in html and 'id="queue-rows"' in html
    assert "status=FILLED" in src            # live = the entry actually filled
    assert "Abhi live trades" in src


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


def test_empty_states_are_painted_before_the_first_request():
    """QA B4-B7: with the backend unreachable, every loader's .catch swallowed
    the error and the containers stayed blank — a blank slab reads as "nothing
    is wrong", which is the opposite of true."""
    src = _read(APP)
    boot = src[src.index("function boot()"):]
    for fn in ("renderSetup()", "renderQueue()", "renderHistory()",
               "renderPaper()", "renderJournal([])"):
        assert fn in boot.split("loadCatalogue")[0], f"{fn} not painted before fetching"


def test_a_failing_endpoint_keeps_its_warning_up():
    """QA C2: failures were a single global counter, so a 4-second quotes poll
    succeeding erased the banner raised by an endpoint that was still down."""
    src = _read(APP)
    assert "noteFailure" in src and "noteSuccess" in src
    assert "var failing" in src
    assert "if (!Object.keys(failing).length) banner(null);" in src


def test_placeholders_never_wear_the_money_palette():
    """DESIGN LAW 2: green means money. A dash is not a profit, a zero
    drawdown is not a loss."""
    src = _read(APP)
    assert 'px === null ? "var(--ink-3)"' in src            # ticker placeholder
    assert 'closed.length ? (netR >= 0 ? "up" : "down") : ""' in src
    assert 'o.max_drawdown_r ? "down" : ""' in src


def test_the_sign_in_overlay_is_a_dialog_not_a_second_h1():
    html = _read(HTML)
    assert 'role="dialog"' in html and 'aria-modal="true"' in html
    assert html.count("<h1") == 1, "more than one h1 in the document"


def test_animated_hairlines_are_clipped_to_their_card():
    """QA E: the pipeline sweep travels to 220%; unclipped it widened every
    stage card and overflowed the section at every viewport width."""
    css = _read(CSS)
    block = css[css.index(".stage .hair"):css.index(".stage .idx")]
    assert "overflow: hidden" in block


# --------------------------------------------------- what a trader needs ---

def test_the_card_shows_how_far_price_is_from_the_entry():
    """These are resting STOP orders. Without distance-to-entry the card
    cannot tell you whether it triggers in ten minutes or never."""
    src = _read(APP)
    assert "function distanceToEntry" in src
    assert "dist-strip" in src
    # a LONG entry sits above price, a SHORT below — sign must be direction-aware
    assert "s.direction > 0 ? s.entry - px : px - s.entry" in src


def test_an_expired_setup_is_never_presented_as_actionable():
    """Its validity window has closed; leading the page with it invites
    placing an order the engine already abandoned."""
    src, css = _read(APP), _read(CSS)
    assert "function validity" in src
    assert "var live = rows.filter" in src            # prefer an armed setup
    assert '"rank expired", "Expired"' in src
    assert "Koi armed setup nahi" in src              # when none are armed
    assert ".setup-card.stale" in css


def test_round_trip_cost_is_shown_as_a_share_of_risk():
    """fee/R is the number this entire strategy set turns on — 0.27 kills the
    edge, 0.09 keeps it. It belongs on screen, not in a footnote."""
    src = _read(APP)
    assert "function costOf" in src
    assert "taker_fee" in src and "funding_per_day" in src
    assert "Round-trip cost" in src


def test_position_size_can_be_driven_by_the_traders_own_account():
    src, html = _read(APP), _read(HTML)
    assert 'id="equity-input"' in html
    assert 'localStorage.setItem("ms_equity"' in src
    # the paper book must not overwrite a number the trader typed
    assert '!localStorage.getItem("ms_equity")' in src


def test_aggregate_exposure_is_stated_because_the_setups_correlate():
    """Ten setups on one symbol in one direction is one bet, not ten."""
    src = _read(APP)
    assert "function renderExposure" in src
    assert "Sab liye to risk" in src
    assert "independent nahi hain" in src


def test_the_order_ticket_can_be_copied():
    """The trader retypes entry/stop/target into an exchange by hand; a
    transcription error costs real money."""
    src = _read(APP)
    assert "navigator.clipboard.writeText" in src
    assert "resting STOP order" in src


def test_a_stale_surface_labels_itself_instead_of_looking_live():
    """The banner alone is not enough — the feed pill and the ticker source
    line must both say SIMULATED, because that is what the eye lands on."""
    html, src = _read(HTML), _read(APP)
    assert 'id="feed-label"' in html and 'id="feed-pill"' in html
    assert "simulated feed" in src and "SIMULATED" in src
    assert "setLive(false)" in src          # every failed fetch flips it


def test_motion_is_wrapped_for_reduced_motion():
    css = _read(CSS)
    assert "@media (prefers-reduced-motion: reduce)" in css
    block = css[css.index("@media (prefers-reduced-motion: reduce)"):]
    assert "animation-duration: .001s" in block


def test_scroll_reveals_have_a_fallback_when_the_timeline_is_unsupported():
    """Without animation-timeline the sections would stay at opacity 0 —
    an entirely blank page on every browser that lacks it."""
    css, src = _read(CSS), _read(APP)
    assert "@supports not (animation-timeline: view())" in css
    assert "IntersectionObserver" in src


def test_the_price_row_never_uses_a_fixed_four_column_grid():
    """Mono digits set a large min-content; repeat(4, 1fr) overflows."""
    css = _read(CSS)
    block = css[css.index(".price-cells"):css.index(".pcell")]
    assert "auto-fit" in block and "repeat(4, 1fr)" not in block


# ------------------------------------------------------------- structure ---

SECTIONS = ["hero", "setup", "chart", "pipeline", "queue", "strategies",
            "history", "paper", "journal", "settings", "evidence"]


def test_every_section_exists_in_order_and_has_a_rail_anchor():
    """The shell is one scrolling surface: eleven sections, each reachable
    from the rail. A rail link with no section scrolls nowhere."""
    html = _read(HTML)
    order = re.findall(r'<section id="([a-z]+)"', html)
    assert order == SECTIONS, f"section order is {order}"
    anchors = re.findall(r'data-anchor="([a-z]+)"', html)
    assert anchors == SECTIONS, f"rail anchors are {anchors}"
    for name in SECTIONS:
        assert f'href="#{name}"' in html, f"no rail link to #{name}"


def test_the_scroll_container_is_main_not_the_document():
    """Section anchors and scroll-linked reveals both key off <main>."""
    css = _read(CSS)
    assert "main { overflow-y: auto" in css and "scroll-behavior: smooth" in css


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
    """The chart library cannot read CSS variables, so app.js hands them over
    with tok(). A hardcoded hex would not follow the theme."""
    src = _read(APP)
    for token in ("--chart-text", "--chart-grid", "--chart-border", "--up", "--down"):
        assert 'tok("' + token + '")' in src, token
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

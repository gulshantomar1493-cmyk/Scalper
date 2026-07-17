"""CI import-boundary check (roadmap P0.19).

No module in the marketscalper package — outside the providers package
itself and main.py (the composition point, which the roadmap permits to
construct concrete providers) — may import a concrete provider module.
Engines, strategies, planner, journal, core, api and bootstrap consume
normalized events and marketscalper.providers.base types only.

Smallest faithful implementation: stdlib ast over each package source file
(robust against comments/strings). Runs inside the normal pytest step of
scripts/ci.sh — no extra CI machinery.
"""

from __future__ import annotations

import ast
import pathlib

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "backend" / "marketscalper"

_PROVIDERS_PKG = "marketscalper.providers"
_ALLOWED = f"{_PROVIDERS_PKG}.base"


def _forbidden_provider_imports(path: pathlib.Path) -> list[str]:
    """Concrete-provider imports found in one source file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name.startswith(_PROVIDERS_PKG + ".") and name != _ALLOWED:
                    bad.append(name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == _PROVIDERS_PKG:
                # "from marketscalper.providers import binance" -> concrete
                bad.extend(
                    f"{mod}.{alias.name}"
                    for alias in node.names
                    if alias.name != "base"
                )
            elif mod.startswith(_PROVIDERS_PKG + ".") and mod != _ALLOWED:
                bad.append(mod)
    return bad


def test_no_module_outside_composition_imports_concrete_providers():
    offenders: dict[str, list[str]] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE)
        if rel.parts[0] == "providers" or rel.name == "main.py":
            continue                       # the two roadmap-permitted locations
        bad = _forbidden_provider_imports(path)
        if bad:
            offenders[str(rel)] = bad
    assert offenders == {}, f"import-boundary violations: {offenders}"


def test_checker_detects_forbidden_and_accepts_allowed(tmp_path):
    """The gate itself must be able to fail — proven on scratch files."""
    bad = tmp_path / "engine.py"
    bad.write_text(
        "from marketscalper.providers.binance import BinanceFeed\n"
        "import marketscalper.providers.replay\n"
        "from marketscalper.providers import replay\n",
        encoding="utf-8",
    )
    assert sorted(_forbidden_provider_imports(bad)) == [
        "marketscalper.providers.binance",
        "marketscalper.providers.replay",
        "marketscalper.providers.replay",
    ]

    ok = tmp_path / "ok.py"
    ok.write_text(
        "from marketscalper.providers.base import Candle, FeedProvider\n"
        "from marketscalper.providers import base\n"
        "from marketscalper.core.bus import EventBus\n"
        "import marketscalper.db\n",
        encoding="utf-8",
    )
    assert _forbidden_provider_imports(ok) == []

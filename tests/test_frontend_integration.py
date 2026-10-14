"""Frontend integration: view-registry wiring and screen completeness.

These tests assert the *whole* frontend is coherent, so a view added without
registration (or a route added without a view) fails loudly rather than
silently falling back to a placeholder.

As with the other frontend suites in this repository, no JavaScript runtime is
available (Node is absent; the Python JS engines reject ES modules or crash on
Python 3.13), so these are static structural assertions - they do not execute
the application in a browser.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import FRONTEND_DIR, create_app

JS = FRONTEND_DIR / "js"
VIEWS = JS / "views"

#: Routes the router declares.
ROUTER_ROUTES = (
    "/dashboard",
    "/engines",
    "/targets",
    "/instances",
    "/plan",
    "/jobs",
)

#: Views that are implemented (a view module exists and registers itself).
IMPLEMENTED_VIEWS = (
    "dashboard",
    "targets",
    "instances",
    "plan",
    "jobs",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _exports_of(name: str) -> set[str]:
    src = _read(VIEWS / f"{name}.js")
    exported = set(
        re.findall(
            r"^\s*export\s+(?:default\s+)?(?:const|let|var|function|async\s+function|class)\s+([A-Za-z_$][\w$]*)",
            src,
            re.MULTILINE,
        )
    )
    if re.search(r"^\s*export\s+default", src, re.MULTILINE):
        exported.add("default")
    return exported


@pytest.fixture()
def client() -> TestClient:
    from app.engines.jobs import JobStore
    from app.engines.target_instances import TargetInstanceStore

    return TestClient(
        create_app(job_store=JobStore(), target_instance_store=TargetInstanceStore())
    )


# --------------------------------------------------------------------------- #
# registry wiring
# --------------------------------------------------------------------------- #


def test_router_declares_all_six_destinations() -> None:
    app_src = _read(JS / "app.js")
    for route in ROUTER_ROUTES:
        assert f'path: "{route}"' in app_src, route


def test_every_implemented_view_registers_a_known_route() -> None:
    for name in IMPLEMENTED_VIEWS:
        src = _read(VIEWS / f"{name}.js")
        match = re.search(r'registerView\("([^"]+)"', src)
        assert match, f"{name} does not register itself"
        route = match.group(1)
        assert route in ROUTER_ROUTES, route


def test_every_implemented_view_is_imported_by_the_entry_module() -> None:
    main = _read(JS / "main.js")
    for name in IMPLEMENTED_VIEWS:
        assert f'import "./views/{name}.js";' in main, name
    assert "start()" in main


def test_entry_module_imports_no_view_that_does_not_exist() -> None:
    main = _read(JS / "main.js")
    for target in re.findall(r'import\s+"\./views/([^"]+)\.js";', main):
        assert (VIEWS / f"{target}.js").is_file(), target


def test_every_view_module_exports_a_default_and_a_registered_view() -> None:
    for name in IMPLEMENTED_VIEWS:
        exported = _exports_of(name)
        assert "default" in exported, f"{name} has no default export"


def test_router_rejects_unknown_routes_by_resolving_to_dashboard() -> None:
    app_src = _read(JS / "app.js")
    assert "ROUTE_PATHS.has(path)" in app_src
    assert '"/dashboard"' in app_src


def test_engine_route_has_a_view() -> None:
    """Phase 6A added the engines view; the route must stay declared."""

    app_src = _read(JS / "app.js")
    assert 'path: "/engines"' in app_src, "the route must stay declared"
    assert (VIEWS / "engines.js").exists(), "the engines view must exist"


def test_placeholder_fallback_is_honest_and_not_fabricated() -> None:
    """An unregistered route must say so, never invent data or a success."""

    app_src = _read(JS / "app.js")
    assert "placeholder(" in app_src
    assert "is not implemented yet" in app_src
    assert "nothing is simulated on screen" in app_src


# --------------------------------------------------------------------------- #
# shell and navigation
# --------------------------------------------------------------------------- #


def test_shell_html_links_every_declared_route() -> None:
    html = _read(FRONTEND_DIR / "index.html")
    for route in ROUTER_ROUTES:
        assert f'href="#{route}"' in html, route
    assert html.count('data-nav-link') >= len(ROUTER_ROUTES)


def test_nav_links_mark_the_active_route() -> None:
    app_src = _read(JS / "app.js")
    assert "aria-current" in app_src
    assert "markActiveNav" in app_src


def test_shell_exposes_the_simulation_only_notice() -> None:
    html = _read(FRONTEND_DIR / "index.html")
    assert 'id="simulation-notice"' in html
    assert "Simulation only" in html
    assert "no engine is ever executed" in html


# --------------------------------------------------------------------------- #
# every asset is served
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "rel",
    [
        "index.html",
        "css/styles.css",
        "js/api.js",
        "js/app.js",
        "js/dom.js",
        "js/components.js",
        "js/main.js",
        "js/views/dashboard.js",
        "js/views/targets.js",
        "js/views/instances.js",
        "js/views/plan.js",
        "js/views/jobs.js",
        "js/dashboard.js",
    ],
)
def test_asset_is_served(client, rel: str) -> None:
    res = client.get(f"/static/{rel}")
    assert res.status_code == 200, rel
    assert res.text == (FRONTEND_DIR / rel).read_text(encoding="utf-8"), rel


def test_no_unintended_files_are_served(client) -> None:
    assert client.get("/static/js/views/engines.js").status_code == 200


# --------------------------------------------------------------------------- #
# endpoint coverage across the whole frontend
# --------------------------------------------------------------------------- #


def test_every_backend_route_is_used_by_some_view() -> None:
    """Guard against a screen existing without ever calling its endpoint."""

    api_paths = set()
    for src_path in sorted(VIEWS.glob("*.js")):
        for match in re.findall(r'api\.(?:get|post|delete)\(\s*[`"]([^`"]+)[`"]', _read(src_path)):
            api_paths.add(match.split("${")[0].rstrip("/"))

    expected = {
        "/health",
        "/api/engines",
        "/api/jobs",
        "/api/targets",
        "/api/target-instances",
    }
    assert expected <= api_paths, expected - api_paths


def _code(path: Path) -> str:
    """Source with comments stripped: assertions are about executable code.

    The safety boundary of every module is *documented* in its docstring by
    naming the forbidden sinks, so the raw source legitimately contains them.
    """

    src = _read(path)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return re.sub(r"^\s*//[^\n]*$", "", src, flags=re.MULTILINE)


#: The modules that legitimately perform network calls.
FETCH_OWNERS = ("api.js",)

#: The Phase 2D library, pinned by its own suite.
LEGACY = "dashboard.js"


def test_only_the_api_client_performs_network_calls() -> None:
    """Every non-api, non-legacy module must be free of direct fetch calls."""

    for src_path in sorted(JS.rglob("*.js")):
        if src_path.name in FETCH_OWNERS or src_path.name == LEGACY:
            continue
        assert not re.search(r"\bfetch\s*\(", _code(src_path)), src_path.name
    # and the owner does call fetch
    assert re.search(r"\bfetch\s*\(", _code(JS / "api.js"))


def test_every_non_legacy_module_avoids_unsafe_sinks() -> None:
    for src_path in sorted(JS.rglob("*.js")):
        if src_path.name == LEGACY:
            continue
        lowered = _code(src_path).lower()
        for sink in (
            "innerhtml",
            "outerhtml",
            "insertadjacenthtml",
            "document.write",
            "eval(",
            "new function",
        ):
            assert sink not in lowered, f"{src_path.name}: {sink}"


def test_legacy_dashboard_surface_is_unchanged() -> None:
    """The Phase 2D library keeps its own exports and no Phase 4B wiring.

    ``tests/test_dashboard_js.py`` is the authoritative pin for that file; this
    check guards the other direction - that the new tree has not absorbed it.
    """

    legacy = _read(FRONTEND_DIR / "js" / LEGACY)
    assert "PassOrchDashboard" in legacy
    assert "loadTargets" in legacy
    assert "previewPlan" in legacy
    assert 'from "./dashboard.js"' not in legacy
    for src_path in sorted(VIEWS.glob("*.js")):
        src = _read(src_path)
        for spec in ('"./dashboard.js"', '"../dashboard.js"', '"./js/dashboard.js"'):
            assert spec not in src, src_path.name


def test_legacy_dashboard_is_not_imported_by_any_view() -> None:
    for src_path in sorted(VIEWS.glob("*.js")):
        src = _read(src_path)
        for spec in ('"./dashboard.js"', '"../dashboard.js"', '"./js/dashboard.js"'):
            assert spec not in src, src_path.name

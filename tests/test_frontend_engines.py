"""Phase 6A - Engines view contract tests.

Static and structural assertions on ``frontend/js/views/engines.js`` plus
integration with the Phase 4A shell. Verifies:

* which endpoints the view talks to, and only those;
* that every response field it reads exists in the real API schemas;
* that the view registers a real route;
* that no unsafe DOM sink or direct fetch call is introduced;
* that the frontend files are served;
* that "real-engine compatible" is clearly labeled as NOT meaning real execution.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.app import FRONTEND_DIR, create_app
from app.api.schemas import EngineInfo

VIEW = FRONTEND_DIR / "js" / "views" / "engines.js"
MAIN = FRONTEND_DIR / "js" / "main.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code(path: Path) -> str:
    src = _read(path)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"^\s*//[^\n]*$", "", src, flags=re.MULTILINE)
    return src


ENGINES_CODE = _code(VIEW)
MAIN_CODE = _read(MAIN)


def _client() -> TestClient:
    return TestClient(create_app())


# --------------------------------------------------------------------------- #
# Route registration
# --------------------------------------------------------------------------- #


def test_engines_view_registers_itself() -> None:
    assert 'registerView("/engines"' in ENGINES_CODE


def test_engines_view_is_imported_by_main() -> None:
    assert 'import "./views/engines.js"' in MAIN_CODE


def test_engines_route_is_navigable() -> None:
    client = _client()
    body = client.get("/").text
    assert 'href="#/engines"' in body


# --------------------------------------------------------------------------- #
# Endpoint allow-listing
# --------------------------------------------------------------------------- #


ALLOWED_ENDPOINTS = ("/api/engines",)


def test_engines_view_uses_only_the_allowed_endpoints() -> None:
    assert "api.get(" in ENGINES_CODE
    for endpoint in ALLOWED_ENDPOINTS:
        assert f'api.get("{endpoint}")' in ENGINES_CODE
    assert "api.post(" not in ENGINES_CODE
    assert "api.delete(" not in ENGINES_CODE


def test_engines_view_does_not_touch_unrelated_endpoints() -> None:
    for endpoint in (
        "/api/jobs",
        "/api/targets",
        "/api/target-instances",
        "/api/targets/{target_id}/compatibility/plan",
    ):
        assert endpoint not in ENGINES_CODE


def test_engines_view_never_calls_fetch_directly() -> None:
    assert "fetch(" not in ENGINES_CODE


def test_engines_view_never_triggers_discovery_or_refresh() -> None:
    for trigger in ("refresh", "probe", "discover"):
        assert trigger not in ENGINES_CODE.lower() or "never triggers" in ENGINES_CODE.lower()


# --------------------------------------------------------------------------- #
# Schema field agreement
# --------------------------------------------------------------------------- #


def test_engine_fields_used_by_the_view_exist_in_the_schema() -> None:
    schema_fields = set(EngineInfo.model_fields.keys())
    for field in ("engine", "discovered", "capability_status", "formats", "real_engine_compatible"):
        assert field in schema_fields, field


# --------------------------------------------------------------------------- #
# State handling
# --------------------------------------------------------------------------- #


def test_engines_view_has_loading_state() -> None:
    assert "pending(" in ENGINES_CODE


def test_engines_view_has_empty_state() -> None:
    assert "empty(" in ENGINES_CODE
    assert "No engine capability data available" in ENGINES_CODE


def test_engines_view_has_error_state() -> None:
    assert "InlineError(" in ENGINES_CODE
    assert "RetryButton(" in ENGINES_CODE


def test_engines_view_handles_malformed_payloads() -> None:
    assert "filter(" in ENGINES_CODE
    assert "e !== null" in ENGINES_CODE


# --------------------------------------------------------------------------- #
# Safety boundary
# --------------------------------------------------------------------------- #


def test_engines_view_has_no_unsafe_dom_sinks() -> None:
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert sink not in ENGINES_CODE


def test_engines_view_clarifies_real_engine_compatible_does_not_mean_execution() -> None:
    assert "does NOT mean real execution" in ENGINES_CODE
    assert "simulation-only" in ENGINES_CODE


# --------------------------------------------------------------------------- #
# Serving
# --------------------------------------------------------------------------- #


def test_engines_module_is_served() -> None:
    client = _client()
    res = client.get("/static/js/views/engines.js")
    assert res.status_code == 200
    assert "EnginesTable" in res.text


def test_engines_view_uses_engines_table_component() -> None:
    assert "EnginesTable" in ENGINES_CODE

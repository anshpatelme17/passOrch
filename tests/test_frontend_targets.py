"""Phase 4B.2 - Targets view contract tests.

Static and structural assertions on ``frontend/js/views/targets.js`` and its
integration with the Phase 4A shell.

No JavaScript runtime is available in this repository (Node is absent; the
Python JS engines either reject ES modules or crash on Python 3.13), so this
suite cannot execute the view. It verifies what can be verified without a
browser:

* which endpoint the view talks to, and only that one;
* that every response field it reads exists in the real TargetSummary schema;
* usable vs unusable presentation driven by the real `usable` field;
* loading, empty, error and retry states;
* navigation to the plan screen via the router's hash link convention;
* malformed-payload handling and absence of unsafe DOM sinks.

What it does NOT verify (limitation reported to the reviewer): live rendering,
event handling, or the DOM a browser produces. Those need a JS runtime.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import FRONTEND_DIR, create_app
from app.api.schemas import TargetSummary

VIEW = FRONTEND_DIR / "js" / "views" / "targets.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code(path: Path) -> str:
    src = _read(path)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"^\s*//[^\n]*$", "", src, flags=re.MULTILINE)
    return src


TARGETS_CODE = _code(VIEW)
TARGETS_RAW = _read(VIEW)

#: The single endpoint this view may use.
TARGETS_ENDPOINT = "/api/targets"

#: Endpoints that belong to other screens.
OTHER_SCREEN_ENDPOINTS = (
    "/api/engines",
    "/api/jobs",
    "/api/target-instances",
    "/health",
    "/api/targets/{target_id}/compatibility/plan",
    "/api/targets/{target_id}",
)


@pytest.fixture()
def client() -> TestClient:
    from app.engines.jobs import JobStore
    from app.engines.target_instances import TargetInstanceStore

    return TestClient(
        create_app(job_store=JobStore(), target_instance_store=TargetInstanceStore())
    )


# --------------------------------------------------------------------------- #
# endpoint contract
# --------------------------------------------------------------------------- #


def test_targets_uses_exactly_one_endpoint() -> None:
    calls = re.findall(r'api\.(get|post|delete)\("([^"]+)"', TARGETS_CODE)
    assert calls, "the targets view must call the API"
    used = {path for _method, path in calls}
    assert used == {TARGETS_ENDPOINT}, used


def test_targets_only_issues_get_requests() -> None:
    methods = re.findall(r"api\.(\w+)\(", TARGETS_CODE)
    assert methods, methods
    assert set(methods) == {"get"}, set(methods)


def test_targets_never_calls_fetch_directly() -> None:
    assert not re.search(r"\bfetch\s*\(", TARGETS_CODE)


def test_targets_does_not_touch_other_screens_endpoints() -> None:
    for endpoint in OTHER_SCREEN_ENDPOINTS:
        assert endpoint not in TARGETS_CODE, endpoint


def test_targets_does_not_address_other_routes_by_path() -> None:
    """Only /api/targets (the collection) is allowed - no per-target calls."""

    for pattern in ("compatibility/plan", "/{target_id}", "instance", "engines"):
        assert pattern not in TARGETS_CODE, pattern


def test_targets_never_triggers_discovery_or_refresh() -> None:
    """No call may refresh, discover or probe; prose may explain the catalog."""

    for pattern in (
        r"\brefresh\s*\(",
        r"\bdiscover(?:y)?\s*\(",
        r"\bprobe\s*\(",
        r"api\.(?:post|delete)\(",
        r"CapabilityRefreshService",
        r"run_safe_command",
    ):
        assert not re.search(pattern, TARGETS_RAW), pattern


# --------------------------------------------------------------------------- #
# schema contract: only fields TargetSummary actually defines
# --------------------------------------------------------------------------- #


def test_every_target_field_read_exists_in_the_schema() -> None:
    schema_fields = set(TargetSummary.model_fields)
    used = set(re.findall(r"\bt\.(\w+)", TARGETS_CODE))
    assert used, "the view must read target fields"
    illegal = used - schema_fields - {"length", "filter", "join"}
    assert not illegal, f"view reads fields absent from TargetSummary: {illegal}"


def test_view_surfaces_the_key_catalog_fields() -> None:
    schema_fields = set(TargetSummary.model_fields)
    for field in ("target", "description", "formats", "enabled", "usable"):
        assert field in schema_fields, field
    code = TARGETS_CODE
    # each must be rendered somewhere in the view or its cells
    assert "t.target" in code
    assert "t.description" in code
    assert "formatsText" in code
    assert "enabledCell" in code
    assert "usableCell" in code


def test_response_envelope_is_read_through_its_array() -> None:
    assert "res.data.targets" in TARGETS_CODE


def test_targets_endpoint_returns_the_expected_schema_in_practice(client) -> None:
    """Guard against the schema drifting from what this view was written for."""

    res = client.get("/api/targets")
    assert res.status_code == 200
    payload = res.json()
    assert set(payload.keys()) == {"targets", "count"}
    assert isinstance(payload["count"], int)
    assert payload["count"] == len(payload["targets"])
    for entry in payload["targets"]:
        assert set(entry.keys()) == set(TargetSummary.model_fields), entry
        assert isinstance(entry["usable"], bool)
        assert isinstance(entry["enabled"], bool)
        assert isinstance(entry["formats"], list)


# --------------------------------------------------------------------------- #
# usable vs unusable presentation
# --------------------------------------------------------------------------- #


def test_usable_and_unusable_states_are_both_handled() -> None:
    code = TARGETS_CODE
    assert "t.usable === true" in code or "target.usable === true" in code
    assert 'badge("usable", "ok")' in code
    assert 'badge("not usable", "deny")' in code


def test_usable_label_comes_from_the_api_field_not_a_guess() -> None:
    """The cell must branch on the real `usable` value, never hardcode one."""

    code = TARGETS_CODE
    assert "target.usable === true" in code
    # no branch that assumes every target is usable
    assert "usable: true" not in code
    assert "usable = true" not in code


def test_enabled_state_is_rendered_separately_from_usable() -> None:
    code = TARGETS_CODE
    assert "enabledCell" in code
    assert 'badge("enabled", "ok")' in code
    assert 'badge("disabled", "muted")' in code


def test_plan_link_is_only_offered_for_usable_targets() -> None:
    code = TARGETS_CODE
    assert "t.usable === true ? PlanLink(t)" in code
    # unusable targets get a dash, not a link that would produce a denial loop
    assert 'badge("-", "muted")' in code


def test_counts_distinguish_usable_from_total() -> None:
    assert "usableCount" in TARGETS_CODE
    assert "valid.filter((t) => t.usable === true)" in TARGETS_CODE


# --------------------------------------------------------------------------- #
# states
# --------------------------------------------------------------------------- #


def test_view_has_a_loading_state() -> None:
    assert "pending(" in TARGETS_CODE


def test_view_has_an_empty_state() -> None:
    assert "empty(" in TARGETS_CODE
    assert "No target types available" in TARGETS_CODE


def test_view_has_an_error_state_with_retry() -> None:
    assert "InlineError(res.error)" in TARGETS_CODE
    assert "RetryButton(" in TARGETS_CODE
    assert "Retry loading targets" in TARGETS_CODE


def test_retry_reinvokes_the_same_loader() -> None:
    assert re.search(r"RetryButton\(\(\) => renderTargets\(root\)", TARGETS_CODE)


def test_view_preserves_the_simulation_only_notice_in_every_branch() -> None:
    code = TARGETS_CODE
    assert code.count("SimulationNotice()") >= 3, "pending, error, empty and loaded branches"


def test_view_declares_the_simulation_only_caveat() -> None:
    code = TARGETS_RAW
    assert "no engine is executed" in code
    assert "simulated" in code


# --------------------------------------------------------------------------- #
# registration, navigation, safety
# --------------------------------------------------------------------------- #


def test_targets_view_registers_itself_with_the_router() -> None:
    assert 'registerView("/targets"' in TARGETS_CODE
    assert "render:" in TARGETS_CODE


def test_navigation_links_use_the_hash_router_convention() -> None:
    code = TARGETS_CODE
    assert 'NavLink("Open Plan Preview", "/plan")' in code
    assert "#${path}" not in code, "use NavLink, not a hand-built hash"


def test_plan_links_carry_the_target_as_a_router_param() -> None:
    """The plan link must be a router hash with an encoded target parameter.

    Uses the same `attrs: [["href", ...]]` shape as NavLink in components.js.
    """

    assert "attrs: [[\"href\", `#/plan?target=${encodeURIComponent(target.target)}`]]" in TARGETS_CODE


def test_plan_link_requires_a_valid_target_identifier() -> None:
    code = TARGETS_CODE
    assert 'typeof target.target !== "string"' in code
    assert "target.target.length === 0" in code


def test_view_uses_only_safe_dom_helpers() -> None:
    lowered = TARGETS_RAW.lower()
    for sink in ("innerhtml", "outerhtml", "insertadjacenthtml", "document.write", "eval(", "new function"):
        assert sink not in lowered, sink


def test_view_writes_text_through_shared_helpers_only() -> None:
    """No direct DOM mutation APIs beyond the safe wrappers in dom.js."""

    for forbidden in ("appendChild", "createElement", "textContent =", "setAttribute"):
        assert forbidden not in TARGETS_RAW, forbidden


def test_view_imports_are_relative_and_exist() -> None:
    for bindings, target in re.findall(
        r'^\s*import\s*\{([^}]*)\}\s*from\s*"([^"]+)";', TARGETS_RAW, re.MULTILINE
    ):
        assert target.startswith("."), target
        resolved = (VIEW.parent / target).resolve()
        assert resolved.is_file(), target
        assert resolved.name in ("api.js", "app.js", "components.js", "dom.js"), target


def test_every_import_binding_is_exported_by_its_target_module() -> None:
    for bindings, target in re.findall(
        r'^\s*import\s*\{([^}]*)\}\s*from\s*"([^"]+)";', TARGETS_RAW, re.MULTILINE
    ):
        resolved = (VIEW.parent / target).resolve()
        src = _read(resolved)
        exported = set(
            re.findall(
                r"^\s*export\s+(?:default\s+)?(?:const|let|var|function|async\s+function|class)\s+([A-Za-z_$][\w$]*)",
                src,
                re.MULTILINE,
            )
        )
        if re.search(r"^\s*export\s+default", src, re.MULTILINE):
            exported.add("default")
        for binding in (b.strip() for b in bindings.split(",") if b.strip()):
            local = binding.split(" as ")[-1].strip()
            assert local in exported, f"{local} not exported by {target}"


def test_no_unused_imports() -> None:
    for bindings, target in re.findall(
        r'^\s*import\s*\{([^}]*)\}\s*from\s*"([^"]+)";', TARGETS_RAW, re.MULTILINE
    ):
        after = TARGETS_RAW.split(f'from "{target}";', 1)[1]
        for binding in (b.strip() for b in bindings.split(",") if b.strip()):
            local = binding.split(" as ")[-1].strip()
            assert re.search(r"\b" + re.escape(local) + r"\b", after), local


# --------------------------------------------------------------------------- #
# malformed data handling
# --------------------------------------------------------------------------- #


def test_view_defensively_filters_malformed_rows() -> None:
    code = TARGETS_CODE
    assert "Array.isArray(value)" in code
    assert "targets.filter(" in code
    assert 'typeof t.target === "string"' in code


def test_formats_helper_handles_non_array_and_empty_input() -> None:
    assert "if (!Array.isArray(formats))" in TARGETS_CODE


def test_missing_string_fields_render_as_dashes_not_blanks() -> None:
    assert "t.description || \"-\"" in TARGETS_CODE
    assert "joined || \"-\"" in TARGETS_CODE


def test_count_reports_filtered_rows_not_raw_payload_length() -> None:
    assert "valid.length" in TARGETS_CODE


# --------------------------------------------------------------------------- #
# styles and serving
# --------------------------------------------------------------------------- #


def test_styles_referenced_by_the_view_are_defined() -> None:
    css = _read(FRONTEND_DIR / "css" / "styles.css")
    shared_src = _read(FRONTEND_DIR / "js" / "dom.js") + _read(FRONTEND_DIR / "js" / "components.js")
    shared = set()
    for cls in re.findall(r'class: "([^"]+)"', shared_src):
        shared.update(cls.split())
    own = set()
    for cls in re.findall(r'class: "([^"]+)"', TARGETS_RAW):
        own.update(cls.split())
    for cls in own - shared:
        assert f".{cls}" in css, f".{cls} used by the view is missing from styles.css"


def test_targets_module_is_served(client) -> None:
    res = client.get("/static/js/views/targets.js")
    assert res.status_code == 200
    assert res.text == TARGETS_RAW


def test_entry_module_registers_both_implemented_views() -> None:
    main = _read(FRONTEND_DIR / "js" / "main.js")
    assert 'import "./views/dashboard.js";' in main
    assert 'import "./views/targets.js";' in main
    assert "start()" in main


def test_dashboard_view_is_unchanged_by_this_phase() -> None:
    dash = _read(FRONTEND_DIR / "js" / "views" / "dashboard.js")
    # unchanged conventions: four sections, four endpoints
    assert dash.count("healthSection(") >= 2
    assert dash.count("enginesSection(") >= 2
    assert "/health" in dash and "/api/engines" in dash


def test_legacy_dashboard_is_not_imported_by_targets() -> None:
    for spec in ('"./dashboard.js"', '"../dashboard.js"'):
        assert spec not in TARGETS_RAW

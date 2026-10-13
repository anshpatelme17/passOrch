"""Phase 4B.4 - Plan-preview view contract tests.

Static and structural assertions on ``frontend/js/views/plan.js`` plus its
integration with the shared ``PlanPreviewView`` component and the existing shell.

No JavaScript runtime is available in this repository (Node is absent; the Python
JS engines either reject ES modules or crash on Python 3.13), so this suite cannot
execute the view. It verifies what can be verified without a browser:

* which endpoints the view uses, and only those;
* that the request body matches PlanPreviewRequest exactly;
* that the authorization outcome branches on the body, not on response.ok;
* loading, empty, error, retry and malformed-response handling;
* that the arriving `#/plan?target=...` link auto-submits;
* absence of unsafe DOM sinks and direct fetch calls.

What it does NOT verify (limitation reported to the reviewer): live rendering,
event handling, or the DOM a browser produces. Those require a JS runtime.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import FRONTEND_DIR, create_app
from app.api.schemas import (
    AuthorizationInfo,
    PlanInfo,
    PlanPreviewRequest,
    PlanPreviewResponse,
    SimulationInfo,
)
from app.api.routes.targets import router as targets_router

VIEW = FRONTEND_DIR / "js" / "views" / "plan.js"
COMPONENTS = FRONTEND_DIR / "js" / "components.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code(path: Path) -> str:
    src = _read(path)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"^\s*//[^\n]*$", "", src, flags=re.MULTILINE)
    return src


PLAN_CODE = _code(VIEW)
PLAN_RAW = _read(VIEW)

#: Endpoints this view may use.
ALLOWED = ("/api/targets", "/api/targets/{target_id}/compatibility/plan")

#: Endpoints owned by other phases.
FOREIGN = ("/api/jobs", "/api/target-instances", "/api/engines", "/health")


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


def test_plan_view_uses_only_the_plan_and_catalog_endpoints() -> None:
    calls = set(re.findall(r'api\.(?:get|post)\(\s*"([^"]+)"', PLAN_RAW))
    calls |= set(re.findall(r"api\.(?:get|post)\(`([^`]+)`", PLAN_RAW))
    assert calls, "the view must call the API"
    normalised = {re.sub(r"\$\{[^}]*\}", "{target_id}", p) for p in calls}
    assert normalised == set(ALLOWED), normalised


def test_plan_issues_get_and_post() -> None:
    assert set(re.findall(r"api\.(get|post)\(", PLAN_CODE)) == {"get", "post"}


def test_plan_path_is_built_from_the_submitted_target() -> None:
    assert "/api/targets/${target}/compatibility/plan" in PLAN_CODE


def test_plan_never_calls_fetch_directly() -> None:
    assert not re.search(r"\bfetch\s*\(", PLAN_CODE)


def test_plan_does_not_touch_other_phases_endpoints() -> None:
    for endpoint in FOREIGN:
        assert endpoint not in PLAN_CODE, endpoint


def test_plan_never_triggers_discovery_or_refresh() -> None:
    for pattern in (
        r"\brefresh\s*\(",
        r"\bdiscover(?:y)?\s*\(",
        r"\bprobe\s*\(",
        r"CapabilityRefreshService",
        r"run_safe_command",
    ):
        assert not re.search(pattern, PLAN_RAW), pattern


# --------------------------------------------------------------------------- #
# request contract
# --------------------------------------------------------------------------- #


def test_request_body_matches_the_schema_exactly() -> None:
    """Only mode and reference may be sent - extras are rejected with 422."""

    assert set(PlanPreviewRequest.model_fields) == {"mode", "reference"}
    code = PLAN_CODE
    assert "mode: formState.mode || \"simulated\"" in code
    assert "reference: formState.reference || \"\"" in code


def test_target_is_sent_as_the_path_not_the_body() -> None:
    """The route path carries the target; the body must not duplicate it."""

    assert "target: formState.target" not in PLAN_CODE
    assert "/api/targets/${target}/compatibility/plan" in PLAN_CODE


def test_only_the_simulation_mode_is_offered() -> None:
    assert "MODES = [\"simulated\"]" in PLAN_CODE


def test_reference_input_length_is_bounded() -> None:
    assert '["maxlength", "256"]' in PLAN_RAW


def test_submit_is_guarded_when_no_target_is_chosen() -> None:
    code = PLAN_CODE
    assert 'if (!isNonEmptyString(target))' in code
    assert "submit.setAttribute(\"disabled\", \"\")" in code


def test_target_options_come_from_the_api_catalog() -> None:
    assert 'api.get("/api/targets")' in PLAN_CODE
    assert "res.data.targets" in PLAN_CODE


# --------------------------------------------------------------------------- #
# outcome handling: HTTP 200 for both authorized and denied
# --------------------------------------------------------------------------- #


def test_view_branches_on_the_authorization_body_not_response_ok() -> None:
    """A denied preview is HTTP 200; only the body can tell them apart."""

    code = PLAN_CODE
    assert "preview.authorization" in code
    assert "authorization.authorized === true" in code
    # the outcome must be chosen from the body, not from res.ok
    assert "res.ok ?" not in code
    assert "res.ok &&" not in code


def test_authorized_branch_renders_an_authorized_summary() -> None:
    code = PLAN_CODE
    assert "authorizedSummary" in code
    assert "Authorized - this is what a simulated run would look like." in code
    assert "No engine ran." in code


def test_denied_branch_renders_the_reason_and_no_plan() -> None:
    code = PLAN_CODE
    assert "deniedSummary" in code
    assert "Denied - no simulation was produced." in code
    assert 'StatusBadge(auth.reason || "")' in code
    assert "auth.detail" in code


def test_both_branches_render_the_full_preview() -> None:
    assert "PlanPreviewView(preview)" in PLAN_CODE


def test_component_renders_all_five_sections_of_the_schema() -> None:
    """PlanPreviewView must cover every schema section, including `request`."""

    code = _read(COMPONENTS)
    for section in ("Selection", "Authorization", "Request", "Simulation plan", "Simulation result"):
        assert f'text: "{section}"' in code, section


def test_component_shows_none_denied_when_plan_is_null() -> None:
    code = _read(COMPONENTS)
    assert "none (denied)" in code
    assert "No simulation was produced for this request." in code


# --------------------------------------------------------------------------- #
# states
# --------------------------------------------------------------------------- #


def test_view_has_a_pending_state() -> None:
    assert 'pending("Requesting plan preview...")' in PLAN_CODE


def test_view_has_an_error_state_with_retry() -> None:
    assert "InlineError(res.error)" in PLAN_CODE
    assert 'RetryButton(submitPreview, "Retry preview")' in PLAN_CODE


def test_view_has_an_empty_state_before_any_submission() -> None:
    assert "No preview yet" in PLAN_CODE


def test_view_handles_a_missing_response_body() -> None:
    code = PLAN_CODE
    assert "typeof preview !== \"object\"" in code
    assert "The API answered without a usable preview body." in code


def test_view_guards_a_null_response_data() -> None:
    assert "preview === null || typeof preview !== \"object\"" in PLAN_CODE


# --------------------------------------------------------------------------- #
# router params and navigation
# --------------------------------------------------------------------------- #


def test_view_reads_the_incoming_target_from_the_router() -> None:
    code = PLAN_CODE
    assert "initialTarget(params)" in PLAN_CODE
    assert 'params.target' in code


def test_arriving_target_auto_submits_the_preview() -> None:
    code = PLAN_CODE
    idx = code.find("if (formState.target) {")
    assert idx > 0
    block = code[idx : idx + 200]
    assert "await submitPreview()" in block


def test_arriving_target_unknown_to_the_catalog_is_cleared() -> None:
    code = PLAN_CODE
    assert "!targetOptions.some((o) => o.target === formState.target)" in code


def test_view_links_back_to_targets() -> None:
    assert 'NavLink("Back to Targets", "/targets")' in PLAN_CODE


def test_view_preserves_the_simulation_only_notice() -> None:
    assert "SimulationNotice()" in PLAN_CODE
    assert "Nothing is ever executed." in PLAN_RAW


def test_plan_view_registers_itself() -> None:
    assert 'registerView("/plan"' in PLAN_CODE
    assert "render:" in PLAN_CODE


# --------------------------------------------------------------------------- #
# safety
# --------------------------------------------------------------------------- #


def test_view_uses_only_safe_dom_helpers() -> None:
    lowered = PLAN_RAW.lower()
    for sink in ("innerhtml", "outerhtml", "insertadjacenthtml", "document.write", "eval(", "new function"):
        assert sink not in lowered, sink


def test_view_has_no_direct_dom_mutation_apis() -> None:
    for forbidden in ("createElement", "appendChild", "textContent ="):
        assert forbidden not in PLAN_RAW, forbidden


def test_form_controls_are_built_via_the_shared_helper() -> None:
    code = PLAN_RAW
    assert 'h("select", {' in code
    assert 'h("input", {' in code
    assert '["selected", ""]' in code


def test_view_has_lf_line_endings_only() -> None:
    assert "\r\n" not in PLAN_RAW


def test_every_import_binding_resolves_and_is_used() -> None:
    IMP = re.compile(r'^\s*import\s*\{([^}]*)\}\s*from\s*"([^"]+)";', re.M)
    EXP = re.compile(
        r"^\s*export\s+(?:default\s+)?(?:const|let|var|function|async\s+function|class)\s+([A-Za-z_$][\w$]*)",
        re.M,
    )
    for bindings, target in IMP.findall(PLAN_RAW):
        resolved = (VIEW.parent / target).resolve()
        assert resolved.is_file(), target
        after = PLAN_RAW.split(f'from "{target}";', 1)[1]
        exported = set(EXP.findall(resolved.read_text(encoding="utf-8")))
        if re.search(r"^\s*export\s+default", resolved.read_text(encoding="utf-8"), re.M):
            exported.add("default")
        for b in [x.strip() for x in bindings.split(",") if x.strip()]:
            assert b in exported, f"{b} not exported by {target}"
            assert re.search(r"\b" + re.escape(b) + r"\b", after), f"unused import {b}"


def test_entry_module_registers_all_four_views() -> None:
    main = _read(FRONTEND_DIR / "js" / "main.js")
    for view in ("dashboard", "targets", "instances", "plan"):
        assert f'import "./views/{view}.js";' in main, view
    assert "start()" in main


def test_shell_html_links_the_plan_route() -> None:
    assert 'href="#/plan"' in _read(FRONTEND_DIR / "index.html")


def test_targets_view_links_to_plan_with_the_target_param() -> None:
    targets = _read(FRONTEND_DIR / "js" / "views" / "targets.js")
    assert "#/plan?target=${encodeURIComponent(target.target)}" in targets


def test_styles_referenced_by_the_view_are_defined() -> None:
    css = _read(FRONTEND_DIR / "css" / "styles.css")
    shared_src = _read(FRONTEND_DIR / "js" / "dom.js") + _read(COMPONENTS)
    shared = set()
    for cls in re.findall(r'class: "([^"]+)"', shared_src):
        shared.update(cls.split())
    own = set()
    for cls in re.findall(r'class: "([^"]+)"', PLAN_RAW):
        own.update(cls.split())
    for cls in own - shared:
        assert f".{cls}" in css, f".{cls} used by the view is missing from styles.css"


def test_plan_module_is_served(client) -> None:
    res = client.get("/static/js/views/plan.js")
    assert res.status_code == 200
    assert res.text == PLAN_RAW


# --------------------------------------------------------------------------- #
# live API agreement
# --------------------------------------------------------------------------- #


def test_live_denied_plan_is_http_200_with_null_plan_and_simulation(client) -> None:
    """The default registry denies; the screen must not treat that as an error."""

    res = client.post("/api/targets/pdf-document/compatibility/plan", json={"mode": "simulated"})
    assert res.status_code == 200
    body = res.json()
    assert set(body.keys()) == set(PlanPreviewResponse.model_fields)
    assert body["authorization"]["authorized"] is False
    assert body["plan"] is None
    assert body["simulation"] is None
    assert body["authorization"]["reason"] == "capability_not_verified"
    assert set(body["selection"].keys()) == set(SelectionInfo_schema())
    assert set(body["request"].keys()) == set(RequestInfo_schema())
    assert set(body["authorization"].keys()) == set(AuthorizationInfo.model_fields)


def test_live_authorized_plan_returns_plan_and_simulation(client) -> None:
    from app.engines.discovery.models import (
        CapabilityStatus,
        EngineCapabilities,
        EngineSnapshot,
        JOHN,
    )
    from app.engines.discovery.registry import DiscoveryRegistry
    from app.engines.jobs import JobStore

    reg = DiscoveryRegistry()
    reg.register(EngineSnapshot(engine=JOHN, discovered=True, binary_found=True))
    reg.set_capabilities(
        JOHN,
        EngineCapabilities(engine=JOHN, capability_status=CapabilityStatus.VERIFIED, formats=("pdf",)),
    )
    c = TestClient(create_app(discovery_registry=reg, job_store=JobStore()))
    res = c.post("/api/targets/pdf-document/compatibility/plan", json={"mode": "simulated"})
    assert res.status_code == 200
    body = res.json()
    assert body["authorization"]["authorized"] is True
    assert set(body["plan"].keys()) == set(PlanInfo.model_fields)
    assert set(body["simulation"].keys()) == set(SimulationInfo.model_fields)
    assert body["simulation"]["simulated"] is True


def test_live_non_simulation_mode_is_denied_not_executed(client) -> None:
    res = client.post("/api/targets/pdf-document/compatibility/plan", json={"mode": "real"})
    assert res.status_code == 200
    body = res.json()
    assert body["authorization"]["authorized"] is False
    assert body["authorization"]["reason"] == "non_simulation_request"
    assert body["plan"] is None
    assert body["simulation"] is None


def test_live_unmapped_target_is_404(client) -> None:
    res = client.post("/api/targets/nope/compatibility/plan", json={})
    assert res.status_code == 404
    assert res.json() == {"detail": "target_not_mapped"}


def test_live_extra_request_field_is_422(client) -> None:
    res = client.post("/api/targets/pdf-document/compatibility/plan", json={"command": "x"})
    assert res.status_code == 422


def test_live_plan_route_is_post_only() -> None:
    paths = {route.path for route in targets_router.routes}
    methods = {m for route in targets_router.routes for m in route.methods}
    assert "/api/targets/{target_id}/compatibility/plan" in paths
    assert methods == {"GET", "POST"}


def SelectionInfo_schema():
    from app.api.schemas import SelectionInfo

    return SelectionInfo.model_fields


def RequestInfo_schema():
    from app.api.schemas import RequestInfo

    return RequestInfo.model_fields


# --------------------------------------------------------------------------- #
# previous views unchanged
# --------------------------------------------------------------------------- #


def test_previous_views_are_unchanged_by_this_phase() -> None:
    from app.api.schemas import JobSummary

    dash = _read(FRONTEND_DIR / "js" / "views" / "dashboard.js")
    assert dash.count("api.get(") == 4
    targets = _read(FRONTEND_DIR / "js" / "views" / "targets.js")
    assert targets.count("api.get(") == 1
    instances = _read(FRONTEND_DIR / "js" / "views" / "instances.js")
    assert "api.post(" in instances and "api.delete(" in instances
    # JobSummary is untouched even though it is not used by these views
    assert set(JobSummary.model_fields) == {"job_id", "status", "created_at", "target"}


def test_legacy_dashboard_is_not_imported_by_plan() -> None:
    for spec in ('"./dashboard.js"', '"../dashboard.js"'):
        assert spec not in PLAN_RAW

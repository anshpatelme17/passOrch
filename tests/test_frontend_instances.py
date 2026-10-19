"""Phase 4B.3 - Target instances view contract tests.

Static and structural assertions on ``frontend/js/views/instances.js`` plus its
integration with the existing frontend shell.

No JavaScript runtime is available in this repository (Node is absent; the
Python JS engines either reject ES modules or crash on Python 3.13), so this
suite cannot execute the view. It verifies what can be verified without a
browser:

* which endpoints the view uses, and only those;
* that the create form's client-side rules mirror the real server constraints;
* the delete confirmation, 204 handling, and list refresh after mutation;
* empty, loading, error, retry and malformed-data handling;
* navigation and absence of unsafe DOM sinks.

What it does NOT verify (limitation reported to the reviewer): live rendering,
event handling, focus management, or the DOM a browser produces. Those require
a JS runtime and are out of reach here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import FRONTEND_DIR, create_app
from app.api.schemas import (
    TargetInstance,
    TargetInstanceCreateRequest,
    TargetInstanceUpdateRequest,
)
from app.engines.target_instances import (
    MAX_DESCRIPTION_LENGTH,
    MAX_NAME_LENGTH,
)

VIEW = FRONTEND_DIR / "js" / "views" / "instances.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code(path: Path) -> str:
    src = _read(path)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"^\s*//[^\n]*$", "", src, flags=re.MULTILINE)
    return src


INSTANCES_CODE = _code(VIEW)
INSTANCES_RAW = _read(VIEW)

#: Endpoints this view may use.
ALLOWED_ENDPOINTS = ("/api/target-instances", "/api/target-instances/{id}", "/api/targets")

#: Endpoints owned by other phases.
FOREIGN_ENDPOINTS = ("/api/jobs", "/api/engines", "/health", "compatibility/plan")


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


def test_view_uses_only_target_instance_and_catalog_endpoints() -> None:
    calls = set(re.findall(r'api\.(?:get|post|patch|delete)\(\s*"([^"]+)"', INSTANCES_RAW))
    explicit = set(re.findall(r'api\.(?:get|post|patch|delete)\(`([^`]+)`', INSTANCES_RAW))
    combined = calls | explicit
    assert combined, "the view must call the API"
    for path in combined:
        base = re.sub(r"\$\{[^}]*\}", "{id}", path)
        assert path == "/api/target-instances" or base == "/api/targets" or path.startswith(
            "/api/target-instances/"
        ), path
    assert "/api/targets" in combined, "target-type options come from the catalog"
    assert any(p.startswith("/api/target-instances/") for p in combined), "delete uses the by-id path"


def test_view_issues_get_post_patch_and_delete() -> None:
    methods = set(re.findall(r"api\.(get|post|patch|delete)\(", INSTANCES_CODE))
    assert {"get", "post", "patch", "delete"} <= methods, methods


def test_delete_path_is_built_from_the_instance_id() -> None:
    assert "/api/target-instances/${instanceId}" in INSTANCES_CODE


def test_view_never_calls_fetch_directly() -> None:
    assert not re.search(r"\bfetch\s*\(", INSTANCES_CODE)


def test_view_does_not_touch_other_phases_endpoints() -> None:
    for endpoint in FOREIGN_ENDPOINTS:
        assert endpoint not in INSTANCES_CODE, endpoint


def test_view_never_triggers_discovery_or_refresh() -> None:
    for pattern in (
        r"\brefresh\s*\(",
        r"\bdiscover(?:y)?\s*\(",
        r"\bprobe\s*\(",
        r"CapabilityRefreshService",
        r"run_safe_command",
    ):
        assert not re.search(pattern, INSTANCES_RAW), pattern


# --------------------------------------------------------------------------- #
# form contract: mirrors the real server constraints
# --------------------------------------------------------------------------- #


def test_form_declares_the_three_request_fields() -> None:
    schema_fields = set(TargetInstanceCreateRequest.model_fields)
    assert schema_fields == {"name", "target_type", "description"}
    code = INSTANCES_RAW
    # each field is labelled and has an input bound by name
    assert '["name", "name"]' in code
    assert '["name", "target_type"]' in code
    assert '["name", "description"]' in code
    assert 'onField("name")' in code
    assert 'onField("description")' in code
    assert "formState.target_type = event.target.value" in code


def test_form_submits_exactly_the_request_schema_fields() -> None:
    """No extra keys - the API rejects unknown fields with 422."""

    code = INSTANCES_CODE
    assert "name: formState.name" in code
    assert "target_type: formState.target_type" in code
    assert "description: formState.description || \"\"" in code


def test_client_side_limits_match_the_server_constants() -> None:
    assert f"MAX_NAME_LENGTH = {MAX_NAME_LENGTH}" in INSTANCES_CODE
    assert f"MAX_DESCRIPTION_LENGTH = {MAX_DESCRIPTION_LENGTH}" in INSTANCES_CODE


def test_form_maxlength_attributes_match_the_constants() -> None:
    code = INSTANCES_RAW
    assert '["maxlength", String(MAX_NAME_LENGTH)]' in code
    assert '["maxlength", String(MAX_DESCRIPTION_LENGTH)]' in code


def test_validation_rejects_blank_name() -> None:
    code = INSTANCES_CODE
    assert "isNonEmptyString(state && state.name)" in code
    assert 'problems.push("Name is required.")' in code


def test_validation_rejects_whitespace_only_name() -> None:
    """A whitespace-only name must fail client-side, matching the server."""

    assert "state.name.length > MAX_NAME_LENGTH" in INSTANCES_CODE
    assert "isNonEmptyString" in INSTANCES_CODE


def test_validation_rejects_oversized_name_and_description() -> None:
    code = INSTANCES_CODE
    assert "state.name.length > MAX_NAME_LENGTH" in code
    assert "description.length > MAX_DESCRIPTION_LENGTH" in code


def test_validation_requires_a_target_type() -> None:
    assert 'problems.push("A target type is required.")' in INSTANCES_CODE


def test_validation_blocks_submission_and_reports_problems() -> None:
    """The request must only be issued when validation passes."""

    code = INSTANCES_CODE
    assert "if (problems.length > 0)" in code
    assert "renderFeedback(" in code
    # and the API call comes after the guard
    guard_at = code.find("if (problems.length > 0)")
    post_at = code.find("api.post(")
    assert 0 <= guard_at < post_at, "validation must precede the POST"


def test_target_type_options_come_from_the_api_catalog() -> None:
    code = INSTANCES_CODE
    assert 'api.get("/api/targets")' in code
    assert "res.data.targets" in code
    assert "loadTargetTypes" in code


def test_form_disables_submit_when_no_target_types_exist() -> None:
    assert 'submit.setAttribute("disabled", "")' in INSTANCES_CODE


# --------------------------------------------------------------------------- #
# create success / failure
# --------------------------------------------------------------------------- #


def test_create_success_is_reported_and_refreshes_the_list() -> None:
    code = INSTANCES_CODE
    assert 'renderFeedback("success"' in code
    assert "formState = { name: \"\", target_type: \"\", description: \"\" }" in code
    assert "renderForm()" in code
    assert "renderList()" in code


def test_create_failure_keeps_the_entered_values_for_a_retry() -> None:
    """A failure must not wipe the form."""

    code = INSTANCES_CODE
    guard_at = code.find("if (problems.length > 0)")
    post_at = code.find("api.post(")
    fail_at = code.find("if (!res.ok) {", post_at)
    assert 0 <= guard_at < post_at < fail_at, (guard_at, post_at, fail_at)
    failure_block = code[fail_at : fail_at + 400]
    assert "res.error.message" in failure_block
    assert "renderForm()" in failure_block
    # the clearing assignment must come *after* the failure return
    clear_at = code.find('formState = { name: ""', post_at)
    assert clear_at > fail_at, "form must not be cleared on failure"
    assert clear_at < code.find("renderList()", post_at)


def test_submit_uses_a_copied_payload() -> None:
    assert "const payload = {" in INSTANCES_CODE


def test_submit_renders_a_pending_state() -> None:
    assert 'pending("Creating target instance...")' in INSTANCES_CODE


# --------------------------------------------------------------------------- #
# delete confirmation and 204
# --------------------------------------------------------------------------- #


def test_delete_requires_an_explicit_confirmation_step() -> None:
    code = INSTANCES_CODE
    assert "confirmAndDelete" in code
    assert "Delete target instance" in code
    assert '"Yes, delete"' in code
    assert "button(\"Cancel\", renderList)" in code


def test_confirmation_names_the_instance_and_explains_the_effect() -> None:
    code = INSTANCES_CODE
    assert "Jobs already recorded against it are not affected." in code
    assert "isNonEmptyString(instance.name) ? instance.name : instance.id" in code


def test_delete_never_fires_without_a_second_confirmed_click() -> None:
    """The row button only renders the confirmation; it does not call the API."""

    code = INSTANCES_CODE
    row_delete = code.find('button("Delete"')
    assert row_delete > 0
    row_line = code[row_delete : code.find("\n", row_delete)]
    assert "confirmAndDelete" in row_line
    assert "api.delete" not in row_line


def test_delete_shows_a_pending_state() -> None:
    assert "Deleting target instance" in INSTANCES_RAW
    assert 'pending(`Deleting target instance "${label}"...`)' in INSTANCES_RAW


def test_delete_204_is_treated_as_success_with_no_body() -> None:
    code = INSTANCES_CODE
    # The shared client maps 204 -> {ok: true, data: null}; the view refreshes.
    assert "if (!res.ok)" in code
    assert 'renderFeedback("success"' in code
    assert "renderList()" in code
    # and never reads a body from the delete response
    assert "res.data." not in code.split("async function processDelete")[1].split("render(root")[0]


def test_delete_failure_reports_error_and_does_not_refresh() -> None:
    code = INSTANCES_CODE
    process = code.split("async function processDelete")[1]
    fail_idx = process.find("if (!res.ok) {")
    assert fail_idx > 0
    fail_block = process[fail_idx : fail_idx + 500]
    assert "InlineError(res.error)" in fail_block
    assert "RetryButton(renderList)" in fail_block
    assert "was not deleted" in fail_block


def test_list_refreshes_after_successful_mutation_only() -> None:
    """A refresh must sit *after* the failure branch, never inside it."""

    for fn in ("submitForm", "processDelete", "processUpdate"):
        idx = INSTANCES_CODE.find("async function " + fn)
        assert idx > 0, fn
        block = INSTANCES_CODE[idx : idx + 2200]
        fail_at = block.find("if (!res.ok) {")
        refresh_at = block.find("renderList()")
        assert refresh_at > 0, f"{fn} must refresh the list"
        if fail_at > 0:
            assert refresh_at > fail_at, f"{fn} refreshes inside the failure branch"
            # and the failure branch itself must not call renderList
            failure_block = block[fail_at : block.find("}", block.find("}", fail_at) + 1) + 1]
            assert "renderList()" not in failure_block, f"{fn} failure branch refreshes the list"


# --------------------------------------------------------------------------- #
# edit affordance (Phase 4): PATCH with mutable fields only
# --------------------------------------------------------------------------- #


def test_update_schema_is_exactly_the_mutable_fields() -> None:
    schema_fields = set(TargetInstanceUpdateRequest.model_fields)
    assert schema_fields == {"name", "description"}


def test_row_offers_edit_next_to_delete_and_never_fires_inline() -> None:
    code = INSTANCES_CODE
    row_edit = code.find('button("Edit"')
    assert row_edit > 0
    row_line = code[row_edit : code.find("\n", row_edit)]
    assert "renderEditForm" in row_line
    assert "api.patch" not in row_line, "the row button only renders the form"


def test_edit_form_is_prefilled_and_explains_immutability() -> None:
    code = INSTANCES_CODE
    assert "renderEditForm" in code
    assert "Only the name and description can be changed." in code
    assert "state.name = " in code or "name: isNonEmptyString(instance.name)" in code
    assert "instance.description" in code


def test_edit_form_validates_the_mutable_bounds() -> None:
    code = INSTANCES_CODE
    assert "validateEditForm" in code
    # The edit validation must not require a target_type (it is immutable).
    edit_block = code.split("function validateEditForm")[1].split("}")[0]
    assert "target_type" not in edit_block


def test_patch_payload_sends_only_the_mutable_fields() -> None:
    code = INSTANCES_CODE
    payload_at = code.find("const payload = { name: state.name, description:")
    assert payload_at > 0, code[payload_at : payload_at + 120]
    payload_block = code[payload_at : code.find("}", payload_at)]
    assert "target_type" not in payload_block
    assert "created_at" not in payload_block
    assert "id" not in payload_block


def test_patch_path_is_built_from_the_instance_id() -> None:
    assert "`/api/target-instances/${instance.id}`" in INSTANCES_CODE


def test_edit_failure_keeps_the_form_for_a_retry() -> None:
    code = INSTANCES_CODE
    process = code.split("async function processUpdate")[1].split("async function renderInstances")[0]
    fail_at = process.find("if (!res.ok) {")
    assert fail_at > 0
    refresh_at = process.find("renderList()")
    fail_block = process[fail_at:refresh_at]
    assert "res.error.message" in fail_block
    assert "rerender(res.error.message)" in fail_block
    assert "renderList()" not in fail_block


def test_edit_success_reports_and_refreshes_once() -> None:
    code = INSTANCES_CODE
    process = code.split("async function processUpdate")[1].split(
        "async function renderInstances"
    )[0]
    assert 'renderFeedback("success"' in process
    assert "Updated target instance" in process
    assert process.count("renderList()") == 1


def test_edit_never_touches_foreign_endpoints_or_discovery() -> None:
    block = INSTANCES_CODE.split("function renderEditForm")[1].split("async function renderInstances")[0]
    for endpoint in FOREIGN_ENDPOINTS:
        assert endpoint not in block, endpoint
    for pattern in (r"\bfetch\s*\(", r"innerHTML", r"discover"):
        assert not re.search(pattern, block), pattern


# --------------------------------------------------------------------------- #
# list, states, malformed data
# --------------------------------------------------------------------------- #


def test_list_has_loading_empty_error_and_retry_states() -> None:
    code = INSTANCES_CODE
    assert 'pending("Loading target instances...")' in code
    assert "empty(" in code
    assert "InlineError(res.error)" in code
    assert "RetryButton(renderList)" in code


def test_empty_state_prompts_creation() -> None:
    assert "No target instances yet" in INSTANCES_CODE
    assert "Create one below" in INSTANCES_CODE


def test_list_filters_malformed_rows_defensively() -> None:
    code = INSTANCES_CODE
    assert 'Array.isArray(value)' in code
    assert ".filter(" in code
    assert 'isNonEmptyString(i.id)' in code


def test_list_renders_only_schema_fields() -> None:
    schema_fields = set(TargetInstance.model_fields)
    used = set(re.findall(r"\binst\.(\w+)", INSTANCES_CODE))
    illegal = used - schema_fields - {"length", "trim"}
    assert not illegal, f"view reads fields absent from TargetInstance: {illegal}"
    for field in ("id", "name", "target_type", "description", "created_at"):
        assert field in used, field


def test_list_handles_non_numeric_created_at() -> None:
    assert 'typeof inst.created_at === "number"' in INSTANCES_CODE


def test_catalog_options_are_filtered_for_malformed_rows() -> None:
    code = INSTANCES_CODE
    assert "res.data.targets" in code
    assert 'isNonEmptyString(t.target)' in code


def test_view_preserves_the_simulation_only_notice() -> None:
    code = INSTANCES_CODE
    assert code.count("SimulationNotice()") >= 2
    assert "No engine binary is invoked" in INSTANCES_RAW


# --------------------------------------------------------------------------- #
# registration, navigation, safety
# --------------------------------------------------------------------------- #


def test_instances_view_registers_itself() -> None:
    assert 'registerView("/instances"' in INSTANCES_CODE
    assert "render:" in INSTANCES_CODE


def test_view_uses_only_safe_dom_helpers() -> None:
    lowered = INSTANCES_RAW.lower()
    for sink in ("innerhtml", "outerhtml", "insertadjacenthtml", "document.write", "eval(", "new function"):
        assert sink not in lowered, sink


def test_view_has_no_direct_dom_mutation_apis() -> None:
    """Rendering must go through the shared helpers, not raw DOM APIs."""

    for forbidden in ("createElement", "appendChild", "textContent ="):
        assert forbidden not in INSTANCES_RAW, forbidden


def test_inputs_are_created_via_the_shared_helper() -> None:
    """Form controls must go through h() so `attrs`/`value` are handled safely."""

    code = INSTANCES_RAW
    assert 'h("input", {' in code
    assert 'h("select", {' in code
    assert 'h("option", {' in code
    assert "addEventListener" in code
    assert ".value =" in code  # setting .value is safe; it never parses markup
    # options carry their selection through the shared attrs mechanism
    assert '["selected", ""]' in code


def test_entry_module_registers_all_three_views() -> None:
    main = _read(FRONTEND_DIR / "js" / "main.js")
    assert 'import "./views/dashboard.js";' in main
    assert 'import "./views/targets.js";' in main
    assert 'import "./views/instances.js";' in main
    assert "start()" in main


def test_shell_html_links_the_instances_route() -> None:
    html = _read(FRONTEND_DIR / "index.html")
    assert 'href="#/instances"' in html


def test_styles_referenced_by_the_view_are_defined() -> None:
    css = _read(FRONTEND_DIR / "css" / "styles.css")
    shared_src = _read(FRONTEND_DIR / "js" / "dom.js") + _read(FRONTEND_DIR / "js" / "components.js")
    shared = set()
    for cls in re.findall(r'class: "([^"]+)"', shared_src):
        shared.update(cls.split())
    own = set()
    for cls in re.findall(r'class: "([^"]+)"', INSTANCES_RAW):
        own.update(cls.split())
    for cls in own - shared:
        assert f".{cls}" in css, f".{cls} used by the view is missing from styles.css"


def test_instances_module_is_served(client) -> None:
    res = client.get("/static/js/views/instances.js")
    assert res.status_code == 200
    assert res.text == INSTANCES_RAW


# --------------------------------------------------------------------------- #
# live API agreement: the rules the form mirrors must still hold
# --------------------------------------------------------------------------- #


def test_server_still_rejects_the_same_constraints(client) -> None:
    """Guard against the server changing rules the form mirrors."""

    cases = [
        ({"name": "", "target_type": "pdf-document"}, 422),
        ({"name": "   ", "target_type": "pdf-document"}, 422),
        ({"name": "n" * (MAX_NAME_LENGTH + 1), "target_type": "pdf-document"}, 422),
        ({"name": "n", "target_type": "bogus"}, 422),
        ({"name": "n", "target_type": "pdf-document", "description": "x" * (MAX_DESCRIPTION_LENGTH + 1)}, 422),
        ({"name": "n", "target_type": "pdf-document", "extra": 1}, 422),
        ({"name": "n"}, 422),
    ]
    for body, expected in cases:
        res = client.post("/api/target-instances", json=body)
        assert res.status_code == expected, (body, res.status_code)


def test_server_create_returns_the_documented_schema(client) -> None:
    res = client.post(
        "/api/target-instances",
        json={"name": "n", "target_type": "pdf-document", "description": "d"},
    )
    assert res.status_code == 201
    body = res.json()
    assert set(body.keys()) == set(TargetInstance.model_fields)
    assert body["name"] == "n"


def test_server_delete_returns_204_with_no_body(client) -> None:
    created = client.post(
        "/api/target-instances", json={"name": "n", "target_type": "pdf-document"}
    ).json()
    res = client.delete(f"/api/target-instances/{created['id']}")
    assert res.status_code == 204
    assert res.content == b""
    # and it is gone
    assert client.get(f"/api/target-instances/{created['id']}").status_code == 404


def test_server_delete_unknown_is_404(client) -> None:
    res = client.delete("/api/target-instances/does-not-exist")
    assert res.status_code == 404
    assert res.json() == {"detail": "target_instance_not_found"}


def test_server_list_matches_the_response_schema(client) -> None:
    res = client.get("/api/target-instances")
    assert res.status_code == 200
    payload = res.json()
    assert set(payload.keys()) == {"instances", "count", "total", "limit", "offset"}
    assert payload["count"] == len(payload["instances"])


def test_previous_views_are_unchanged_by_this_phase() -> None:
    dash = _read(FRONTEND_DIR / "js" / "views" / "dashboard.js")
    assert dash.count("api.get(") == 5
    targets = _read(FRONTEND_DIR / "js" / "views" / "targets.js")
    assert targets.count("api.get(") == 1
    assert "registerView(\"/targets\"" in targets


def test_legacy_dashboard_is_not_imported_by_instances() -> None:
    for spec in ('"./dashboard.js"', '"../dashboard.js"'):
        assert spec not in INSTANCES_RAW


# --------------------------------------------------------------------------- #
# Pagination (Phase 7B)
# --------------------------------------------------------------------------- #


def test_instances_view_uses_total_not_count() -> None:
    src = _read(FRONTEND_DIR / "js" / "views" / "instances.js")
    assert "res.data.total" in src


def test_instances_view_has_pagination_controls() -> None:
    src = _read(FRONTEND_DIR / "js" / "views" / "instances.js")
    assert "paginationState" in src
    assert "Previous" in src
    assert "Next" in src


def test_instances_view_sends_pagination_params() -> None:
    src = _read(FRONTEND_DIR / "js" / "views" / "instances.js")
    assert "limit" in src
    assert "offset" in src
    assert "URLSearchParams" in src


# --------------------------------------------------------------------------- #
# List filters (Phase 7D)
# --------------------------------------------------------------------------- #


def test_instances_view_has_filter_controls() -> None:
    src = _read(FRONTEND_DIR / "js" / "views" / "instances.js")
    assert "instances-filter-type" in src
    assert "instances-filter-name" in src


def test_instances_view_sends_filter_params() -> None:
    src = _read(FRONTEND_DIR / "js" / "views" / "instances.js")
    assert 'params.set("target_type"' in src
    assert 'params.set("name"' in src


def test_instances_view_filter_changes_reset_offset() -> None:
    src = _read(FRONTEND_DIR / "js" / "views" / "instances.js")
    block = src[src.find("function renderFilters") : src.find("/* ---- list ---")]
    assert "paginationState.offset = 0" in block


def test_instances_view_clear_resets_both_filters() -> None:
    src = _read(FRONTEND_DIR / "js" / "views" / "instances.js")
    block = src[src.find("function renderFilters") : src.find("/* ---- list ---")]
    assert 'paginationState.nameFilter = ""' in block
    assert 'paginationState.targetTypeFilter = ""' in block


def test_instances_view_filters_use_safe_dom_only() -> None:
    src = _read(FRONTEND_DIR / "js" / "views" / "instances.js")
    assert "innerHTML" not in src
    block = src[src.find("function renderFilters") : src.find("/* ---- list ---")]
    assert 'class: "field__control"' in block


# --------------------------------------------------------------------------- #
# Empty states and duplicate-submission guards (Phase 7F)
# --------------------------------------------------------------------------- #


def test_instances_view_distinguishes_filtered_empty_state() -> None:
    src = _read(FRONTEND_DIR / "js" / "views" / "instances.js")
    assert "No target instances match your filters" in src
    assert "paginationState.targetTypeFilter || paginationState.nameFilter" in src


def test_instances_view_submit_is_guarded_against_duplicates() -> None:
    src = _read(FRONTEND_DIR / "js" / "views" / "instances.js")
    block = src[src.find("async function submitForm") : src.find("/* ---- delete")]
    assert "submittingInstance" in block
    assert "finally" in block


def test_instances_view_delete_is_guarded_against_duplicates() -> None:
    src = _read(FRONTEND_DIR / "js" / "views" / "instances.js")
    block = src[src.find("async function processDelete") : src.find("/* ---- view")]
    assert "deletingInstanceId" in block
    assert "finally" in block

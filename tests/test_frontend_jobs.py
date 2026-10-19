"""Phase 4B.5 - Simulated jobs view contract tests.

Static and structural assertions on ``frontend/js/views/jobs.js`` plus its
integration with the shared shell and the ``JobDetailView`` component.

No JavaScript runtime is available in this repository (Node is absent; the Python
JS engines either reject ES modules or crash on Python 3.13), so this suite cannot
execute the view. It verifies what can be verified without a browser:

* endpoint allow-listing;
* that the request body matches JobCreateRequest exactly;
* that the list reads only JobSummary fields (never JobInfo-only ones);
* that a denied job (HTTP 200) is treated as a recorded outcome, not an error;
* the optional instance association and its compatibility labelling;
* domain, validation, transport and malformed-response handling;
* empty, loading, retry and mutation-result states;
* absence of unsafe DOM sinks and direct fetch calls.

Live API agreement tests at the bottom execute against the real server, so the
request/response contract itself *is* verified - it is only the browser behaviour
that is not.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import FRONTEND_DIR, create_app
from app.api.schemas import JobCreateRequest, JobInfo, JobSummary

VIEW = FRONTEND_DIR / "js" / "views" / "jobs.js"
COMPONENTS = FRONTEND_DIR / "js" / "components.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code(path: Path) -> str:
    src = _read(path)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"^\s*//[^\n]*$", "", src, flags=re.MULTILINE)
    return src


JOBS_CODE = _code(VIEW)
JOBS_RAW = _read(VIEW)

#: Endpoints this view may use.
ALLOWED = ("/api/targets", "/api/target-instances", "/api/jobs", "/api/jobs/{job_id}")

#: Endpoints owned by nothing else (there are none left) - sanity check.
FOREIGN = ("/api/engines", "/health", "compatibility/plan")


@pytest.fixture()
def client() -> TestClient:
    """An app with fresh stores so list/create assertions cannot pollute."""

    from app.engines.jobs import JobStore
    from app.engines.target_instances import TargetInstanceStore

    return TestClient(
        create_app(job_store=JobStore(), target_instance_store=TargetInstanceStore())
    )


# --------------------------------------------------------------------------- #
# endpoints
# --------------------------------------------------------------------------- #


def test_jobs_view_uses_only_the_allowed_endpoints() -> None:
    calls = set(re.findall(r'api\.(?:get|post)\(\s*"([^"]+)"', JOBS_RAW))
    calls |= set(re.findall(r"api\.(?:get|post)\(`([^`]+)`", JOBS_RAW))
    assert calls, "the view must call the API"
    normalised = {re.sub(r"\$\{[^}]*\}", "{job_id}", p) for p in calls}
    assert normalised == set(ALLOWED), normalised


def test_job_detail_path_is_built_from_the_selected_job_id() -> None:
    assert "/api/jobs/${jobId}" in JOBS_CODE


def test_jobs_never_calls_fetch_directly() -> None:
    assert not re.search(r"\bfetch\s*\(", JOBS_CODE)


def test_jobs_does_not_touch_unrelated_endpoints() -> None:
    for endpoint in FOREIGN:
        assert endpoint not in JOBS_CODE, endpoint


def test_jobs_never_triggers_discovery_or_refresh() -> None:
    for pattern in (
        r"\brefresh\s*\(",
        r"\bdiscover(?:y)?\s*\(",
        r"\bprobe\s*\(",
        r"CapabilityRefreshService",
        r"run_safe_command",
    ):
        assert not re.search(pattern, JOBS_RAW), pattern


# --------------------------------------------------------------------------- #
# create: request body
# --------------------------------------------------------------------------- #


def test_request_body_matches_job_create_request() -> None:
    assert set(JobCreateRequest.model_fields) == {"target", "instance_id", "mode", "reference"}
    code = JOBS_CODE
    assert "const payload = { target: target };" in code
    assert "payload.instance_id = formState.instance_id;" in code
    assert 'payload.mode = formState.mode || "simulated";' in code
    assert 'payload.reference = formState.reference || "";' in code


def test_instance_id_is_omitted_rather_than_sent_empty() -> None:
    """An empty association must not be sent - it would be rejected as blank."""

    assert 'if (isNonEmptyString(formState.instance_id)) {' in JOBS_CODE


def test_only_the_simulation_mode_is_offered() -> None:
    assert 'text: "simulated"' in JOBS_RAW
    # bound the slice to the mode select itself, not the rest of the file
    start = JOBS_RAW.find("const modeSelect")
    assert start > 0
    mode_block = JOBS_RAW[start : start + 450]
    assert mode_block.count('h("option"') == 1
    assert '["value", "simulated"]' in mode_block


def test_target_is_required_before_submit() -> None:
    assert 'if (!isNonEmptyString(target))' in JOBS_CODE
    assert 'submit.setAttribute("disabled", "")' in JOBS_CODE


def test_target_and_instance_options_come_from_the_api() -> None:
    code = JOBS_CODE
    assert 'api.get("/api/targets")' in code
    assert 'api.get("/api/target-instances")' in code
    assert "res.data.targets" in code
    assert "res.data.instances" in code


# --------------------------------------------------------------------------- #
# denied job is a recorded outcome, not a request failure
# --------------------------------------------------------------------------- #


def test_view_branches_on_status_not_response_ok() -> None:
    """POST /api/jobs returns HTTP 200 for a denied job; only the body tells."""

    code = JOBS_CODE
    assert 'denied = job.status !== "simulated"' in code
    assert 'job.status !== "simulated"' in code
    # the outcome must come from the body, never from res.ok
    assert "res.ok ?" not in code


def test_denied_outcome_is_labelled_and_shows_the_reason() -> None:
    code = JOBS_CODE
    assert "Job recorded with a denied outcome." in code
    assert "No engine ran and no plan was authorized." in code
    assert "StatusBadge(job.failure_reason)" in code


def test_simulated_outcome_is_labelled_distinctly() -> None:
    assert "Simulated job created." in JOBS_CODE
    assert "No engine ran. This is a recorded simulation result." in JOBS_CODE


def test_denied_outcome_uses_the_error_styling_but_not_an_error() -> None:
    code = JOBS_CODE
    assert "class: denied ? \"state state--error\" : \"state\"" in code


def test_details_panel_distinguishes_simulated_from_denied() -> None:
    code = JOBS_CODE
    assert 'job.status === "simulated"' in code
    assert "This job was denied by the execution policy." in code


# --------------------------------------------------------------------------- #
# list: JobSummary fields only
# --------------------------------------------------------------------------- #


def test_list_reads_only_job_summary_fields() -> None:
    """The list must not read instance_id / failure_reason / preview."""

    schema_fields = set(JobSummary.model_fields)
    list_block = JOBS_CODE[JOBS_CODE.find("async function renderList") :]
    list_block = list_block[: list_block.find("async function renderDetail")]
    used = set(re.findall(r"job\.(\w+)", list_block))
    assert used, "the list must read job fields"
    illegal = used - schema_fields - {"length"}
    assert not illegal, f"list reads non-JobSummary fields: {illegal}"


def test_list_renders_the_summary_columns_including_an_action() -> None:
    code = JOBS_CODE
    assert '["Job", "Status", "Target", "Created", "", ""]' in code
    assert 'button("Details", () => renderDetail(job.job_id))' in code
    assert 'h("code", { text: job.job_id })' in code
    assert 'job.created_at === undefined ? "-" : String(job.created_at)' in code


def test_list_reports_the_total_from_the_payload() -> None:
    assert "res.data.total" in JOBS_CODE


def test_detail_uses_job_info_fields_only() -> None:
    detail_fields = set(JobInfo.model_fields)
    block = JOBS_CODE[JOBS_CODE.find("const job = res.data;", JOBS_CODE.find("async function renderDetail")) :]
    block = block[: block.find("render(detailSlot, [")]
    used = set(re.findall(r"job\.(\w+)", block))
    assert used <= detail_fields, used - detail_fields


def test_component_job_row_and_table_avoid_info_only_fields() -> None:
    """Pin the shared component: the list must not expose association columns."""

    detail_only = set(JobInfo.model_fields) - set(JobSummary.model_fields)
    comp = _read(COMPONENTS)
    row_at = comp.find("export function JobRow")
    assert row_at > 0
    row_block = comp[row_at : comp.find("export function JobTable")]
    for field in detail_only:
        assert f"job.{field}" not in row_block, field
    table_call = comp[comp.find("export function JobTable") :]
    for field in detail_only:
        assert field not in table_call.split("\n")[0], field


def test_detail_view_shows_the_association_snapshot() -> None:
    comp = _read(COMPONENTS)
    assert "instance_id" in comp
    assert "instance_target_type" in comp
    assert "Created without a target-instance association." in comp


# --------------------------------------------------------------------------- #
# instance compatibility
# --------------------------------------------------------------------------- #


def test_instances_are_all_listed_and_mismatches_labelled() -> None:
    code = JOBS_CODE
    assert "- not this target)" in code
    assert "compatibleInstances" in code


def test_mismatch_is_warned_without_changing_the_selection() -> None:
    code = JOBS_CODE
    assert "const mismatch =" in code
    assert "your selection has not been changed for you" in code
    # target/instance selects never rewrite the other's value
    assert 'formState.instance_id = ""' not in code
    assert 'formState.target = ""' not in code


def test_compatible_count_is_shown_for_the_chosen_target() -> None:
    assert "compatible target instance(s) for this target." in JOBS_CODE


# --------------------------------------------------------------------------- #
# states
# --------------------------------------------------------------------------- #


def test_view_has_pending_empty_error_and_retry_states() -> None:
    code = JOBS_CODE
    assert 'pending("Creating simulated job...")' in code
    assert 'pending("Loading jobs...")' in code
    assert 'pending("Loading job details...")' in code
    assert "empty(" in code
    assert "InlineError(res.error)" in code
    assert "RetryButton(submitJob" in code
    assert "RetryButton(renderList)" in code
    assert "RetryButton(() => renderDetail(jobId))" in code


def test_view_handles_malformed_create_response() -> None:
    code = JOBS_CODE
    assert "typeof job !== \"object\" || !isNonEmptyString(job.job_id)" in code
    assert "No job returned" in code


def test_view_handles_malformed_list_rows() -> None:
    assert "isNonEmptyString(j.job_id)" in JOBS_CODE


def test_view_handles_a_detail_id_that_is_not_a_job() -> None:
    code = JOBS_CODE
    assert "async function renderDetail" in code
    assert 'if (!isNonEmptyString(jobId))' in code
    assert "No job selected" in code


def test_list_refreshes_after_successful_creation() -> None:
    code = JOBS_CODE
    idx = code.find("async function submitJob")
    block = code[idx : idx + 2200]
    assert "await renderList()" in block
    assert "await renderDetail(job.job_id)" in block
    # and the refresh is after the early-return failure branches
    fail_at = block.find("if (!res.ok) {")
    refresh_at = block.find("await renderList()")
    assert fail_at > 0 and refresh_at > fail_at, "refresh must follow the failure return"


def test_view_preserves_the_simulation_only_notice() -> None:
    assert JOBS_CODE.count("SimulationNotice()") >= 2
    assert "SimulatedOnlyBadge()" in JOBS_CODE
    assert "No engine binary is ever invoked" in JOBS_RAW


def test_jobs_view_registers_itself() -> None:
    assert 'registerView("/jobs"' in JOBS_CODE
    assert "render:" in JOBS_CODE


# --------------------------------------------------------------------------- #
# safety
# --------------------------------------------------------------------------- #


def test_view_uses_only_safe_dom_sinks() -> None:
    lowered = JOBS_RAW.lower()
    for sink in ("innerhtml", "outerhtml", "insertadjacenthtml", "document.write", "eval(", "new function"):
        assert sink not in lowered, sink


def test_view_has_no_direct_dom_mutation_apis() -> None:
    for forbidden in ("createElement", "appendChild", "textContent ="):
        assert forbidden not in JOBS_RAW, forbidden


def test_view_has_lf_line_endings_only() -> None:
    assert "\r\n" not in JOBS_RAW


def test_every_import_binding_resolves_and_is_used() -> None:
    IMP = re.compile(r'^\s*import\s*\{([^}]*)\}\s*from\s*"([^"]+)";', re.M)
    EXP = re.compile(
        r"^\s*export\s+(?:default\s+)?(?:const|let|var|function|async\s+function|class)\s+([A-Za-z_$][\w$]*)",
        re.M,
    )
    for bindings, target in IMP.findall(JOBS_RAW):
        resolved = (VIEW.parent / target).resolve()
        assert resolved.is_file(), target
        after = JOBS_RAW.split(f'from "{target}";', 1)[1]
        exported = set(EXP.findall(resolved.read_text(encoding="utf-8")))
        if re.search(r"^\s*export\s+default", resolved.read_text(encoding="utf-8"), re.M):
            exported.add("default")
        for b in [x.strip() for x in bindings.split(",") if x.strip()]:
            assert b in exported, f"{b} not exported by {target}"
            assert re.search(r"\b" + re.escape(b) + r"\b", after), f"unused import {b}"


def test_styles_referenced_by_the_view_are_defined() -> None:
    css = _read(FRONTEND_DIR / "css" / "styles.css")
    shared_src = _read(FRONTEND_DIR / "js" / "dom.js") + _read(COMPONENTS)
    shared = set()
    for cls in re.findall(r'class: "([^"]+)"', shared_src):
        shared.update(cls.split())
    own = set()
    for cls in re.findall(r'class: "([^"]+)"', JOBS_RAW):
        own.update(cls.split())
    for cls in own - shared:
        assert f".{cls}" in css, f".{cls} used by the view is missing from styles.css"


def test_entry_module_registers_all_five_views() -> None:
    main = _read(FRONTEND_DIR / "js" / "main.js")
    for view in ("dashboard", "targets", "instances", "plan", "jobs"):
        assert f'import "./views/{view}.js";' in main, view
    assert "start()" in main


def test_shell_html_links_all_five_destinations() -> None:
    html = _read(FRONTEND_DIR / "index.html")
    for path in ("/dashboard", "/engines", "/targets", "/instances", "/plan", "/jobs"):
        assert f'href="#{path}"' in html, path


def test_jobs_module_is_served(client) -> None:
    res = client.get("/static/js/views/jobs.js")
    assert res.status_code == 200
    assert res.text == JOBS_RAW


# --------------------------------------------------------------------------- #
# live API agreement (executes against the real server)
# --------------------------------------------------------------------------- #


def test_live_create_returns_the_documented_job_info(client) -> None:
    res = client.post("/api/jobs", json={"target": "pdf-document", "reference": "r"})
    assert res.status_code == 200
    body = res.json()
    assert set(body.keys()) == set(JobInfo.model_fields)
    # the default registry is unverified, so the outcome is denied
    assert body["status"] == "denied"
    assert body["failure_reason"] == "capability_not_verified"
    assert body["instance_id"] == ""
    assert body["instance_target_type"] == ""
    # a DENIED job still carries the preview; the denied plan/simulation inside
    # it are null. (Only a FAILED job has preview === None.)
    assert body["preview"] is not None
    assert body["preview"]["plan"] is None
    assert body["preview"]["simulation"] is None


def test_live_denied_job_is_http_200(client) -> None:
    """A denial must be handled as data, never as a request failure."""

    for mode in ("simulated", "real", "banana"):
        res = client.post("/api/jobs", json={"target": "pdf-document", "mode": mode})
        assert res.status_code == 200, mode
        body = res.json()
        assert body["status"] == "denied"
        assert body["preview"] is not None
        assert body["preview"]["plan"] is None
        assert body["preview"]["simulation"] is None
        assert body["failure_reason"]


def test_live_domain_errors_have_stable_codes(client) -> None:
    from app.api.routes.jobs import (
        INSTANCE_NOT_FOUND_ERROR,
        INSTANCE_TYPE_MISMATCH_ERROR,
        UNKNOWN_TARGET_ERROR,
    )

    unknown = client.post("/api/jobs", json={"target": "nope"})
    assert unknown.status_code == 422
    assert unknown.json()["detail"]["error"] == UNKNOWN_TARGET_ERROR

    bad_instance = client.post("/api/jobs", json={"target": "pdf-document", "instance_id": "x"})
    assert bad_instance.status_code == 422
    assert bad_instance.json()["detail"]["error"] == INSTANCE_NOT_FOUND_ERROR

    # a real instance, but of a different target type
    inst = client.post(
        "/api/target-instances", json={"name": "z", "target_type": "zip-archive"}
    ).json()
    mismatch = client.post(
        "/api/jobs", json={"target": "pdf-document", "instance_id": inst["id"]}
    )
    assert mismatch.status_code == 422
    detail = mismatch.json()["detail"]
    assert detail["error"] == INSTANCE_TYPE_MISMATCH_ERROR
    assert detail["target"] == "pdf-document"
    assert detail["instance_target_type"] == "zip-archive"


def test_live_validation_errors_are_arrays(client) -> None:
    res = client.post("/api/jobs", json={"target": "pdf-document", "command": "x"})
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert isinstance(detail, list), "Pydantic errors are array-shaped"


def test_live_list_and_detail_contract(client) -> None:
    created = client.post("/api/jobs", json={"target": "pdf-document"}).json()
    listing = client.get("/api/jobs")
    assert listing.status_code == 200
    payload = listing.json()
    assert set(payload.keys()) == {"jobs", "count", "total", "limit", "offset"}
    summary = payload["jobs"][0]
    # JobSummary has exactly four fields - no association or preview
    assert set(summary.keys()) == set(JobSummary.model_fields)
    assert summary["job_id"] == created["job_id"]

    detail = client.get(f"/api/jobs/{created['job_id']}")
    assert detail.status_code == 200
    assert detail.json()["job_id"] == created["job_id"]


def test_live_unknown_job_is_404(client) -> None:
    res = client.get("/api/jobs/does-not-exist")
    assert res.status_code == 404
    assert res.json() == {"detail": "job_not_found"}


def test_live_instance_cannot_be_stored_via_a_list_row(client) -> None:
    """The list row schema must never carry an association field."""

    client.post(
        "/api/target-instances", json={"name": "n", "target_type": "pdf-document"}
    )
    inst_id = client.get("/api/target-instances").json()["instances"][0]["id"]
    client.post("/api/jobs", json={"target": "pdf-document", "instance_id": inst_id})
    summary = client.get("/api/jobs").json()["jobs"][0]
    assert "instance_id" not in summary
    # but the detail does carry it
    detail = client.get(f"/api/jobs/{summary['job_id']}").json()
    assert detail["instance_id"] == inst_id
    assert detail["instance_target_type"] == "pdf-document"


def test_live_jobs_do_not_leak_forbidden_fields(client) -> None:
    from app.engines.adapter import FORBIDDEN_REQUEST_FIELDS
    from app.engines.discovery.registry import FORBIDDEN_PAYLOAD_KEYS

    client.post("/api/jobs", json={"target": "pdf-document"})
    payload = client.get("/api/jobs").json()
    detail = client.get(f"/api/jobs/{payload['jobs'][0]['job_id']}").json()

    def scan(value):
        found = set()
        if isinstance(value, dict):
            for key, item in value.items():
                if key in (FORBIDDEN_REQUEST_FIELDS | FORBIDDEN_PAYLOAD_KEYS):
                    found.add(key)
                found |= scan(item)
        elif isinstance(value, list):
            for item in value:
                found |= scan(item)
        return found

    assert not scan(payload)
    assert not scan(detail)


# --------------------------------------------------------------------------- #
# previous views and legacy unchanged
# --------------------------------------------------------------------------- #


def test_previous_views_are_unchanged_by_this_phase() -> None:
    counts = {
        "dashboard": 5,
        "targets": 1,
        "plan": 1,
    }
    for name, expected in counts.items():
        src = _read(FRONTEND_DIR / "js" / "views" / f"{name}.js")
        assert src.count("api.get(") == expected, name
    instances = _read(FRONTEND_DIR / "js" / "views" / "instances.js")
    assert instances.count("api.get(") == 2
    assert "api.post(" in instances and "api.delete(" in instances


def test_legacy_dashboard_is_not_imported_by_jobs() -> None:
    for spec in ('"./dashboard.js"', '"../dashboard.js"'):
        assert spec not in JOBS_RAW


# --------------------------------------------------------------------------- #
# Job deletion (Phase 5 Step 3)
# --------------------------------------------------------------------------- #


def test_jobs_view_has_delete_button_in_list() -> None:
    src = _read(FRONTEND_DIR / "js" / "views" / "jobs.js")
    assert 'button("Delete", () => confirmAndDeleteJob(job))' in src


def test_jobs_view_has_confirmation_dialog() -> None:
    src = _read(FRONTEND_DIR / "js" / "views" / "jobs.js")
    assert "confirmAndDeleteJob" in src
    assert "Yes, delete" in src
    assert "Cancel" in src


def test_jobs_view_delete_calls_api_delete() -> None:
    src = _read(FRONTEND_DIR / "js" / "views" / "jobs.js")
    assert 'api.delete(`/api/jobs/${jobId}`)' in src


def test_jobs_view_delete_refreshes_list_on_success() -> None:
    src = _read(FRONTEND_DIR / "js" / "views" / "jobs.js")
    assert "await renderList()" in src


def test_jobs_view_delete_clears_detail_if_selected() -> None:
    src = _read(FRONTEND_DIR / "js" / "views" / "jobs.js")
    assert "detailSelectedJobId" in src
    assert 'if (detailSelectedJobId === jobId)' in src


def test_jobs_view_delete_handles_404() -> None:
    src = _read(FRONTEND_DIR / "js" / "views" / "jobs.js")
    assert "if (!res.ok)" in src
    assert "InlineError(res.error)" in src
    assert "The job was not deleted." in src


def test_jobs_view_prevents_duplicate_delete_submissions() -> None:
    src = _read(FRONTEND_DIR / "js" / "views" / "jobs.js")
    assert "deletingJobId" in src
    assert "if (deletingJobId)" in src


# --------------------------------------------------------------------------- #
# Pagination (Phase 7B)
# --------------------------------------------------------------------------- #


def test_jobs_view_uses_total_not_count() -> None:
    src = _read(FRONTEND_DIR / "js" / "views" / "jobs.js")
    assert "res.data.total" in src


def test_jobs_view_has_pagination_controls() -> None:
    src = _read(FRONTEND_DIR / "js" / "views" / "jobs.js")
    assert "paginationState" in src
    assert "Previous" in src
    assert "Next" in src


def test_jobs_view_sends_pagination_params() -> None:
    src = _read(FRONTEND_DIR / "js" / "views" / "jobs.js")
    assert "limit" in src
    assert "offset" in src
    assert "URLSearchParams" in src


# --------------------------------------------------------------------------- #
# List filters and sorting (Phase 7C)
# --------------------------------------------------------------------------- #


def test_jobs_view_has_filter_controls() -> None:
    src = _read(FRONTEND_DIR / "js" / "views" / "jobs.js")
    assert "jobs-filter-status" in src
    assert "jobs-filter-target" in src
    assert "jobs-filter-instance" in src
    assert "jobs-filter-sort" in src


def test_jobs_view_sends_filter_and_sort_params() -> None:
    src = _read(FRONTEND_DIR / "js" / "views" / "jobs.js")
    assert 'params.set("status"' in src
    assert 'params.set("target"' in src
    assert 'params.set("instance_id"' in src
    assert 'params.set("sort"' in src


def test_jobs_view_instance_filter_none_sends_empty_instance_id() -> None:
    src = _read(FRONTEND_DIR / "js" / "views" / "jobs.js")
    assert 'paginationState.instanceFilter === "none"' in src


def test_jobs_view_filter_changes_reset_offset() -> None:
    src = _read(FRONTEND_DIR / "js" / "views" / "jobs.js")
    block = src[src.find("function renderFilters") : src.find("/* ---- form")]
    assert block.count("paginationState.offset = 0") == 4


def test_jobs_view_sort_defaults_to_created_desc() -> None:
    src = _read(FRONTEND_DIR / "js" / "views" / "jobs.js")
    assert '"created_desc"' in src
    assert '"created_asc"' in src


def test_jobs_view_filter_bar_uses_defined_styles() -> None:
    css = _read(FRONTEND_DIR / "css" / "styles.css")
    assert ".filters" in css
    assert ".filters__group" in css


def test_jobs_view_filter_controls_use_safe_selects() -> None:
    src = _read(FRONTEND_DIR / "js" / "views" / "jobs.js")
    assert "innerHTML" not in src
    block = src[src.find("function filterSelect") : src.find("/* ---- form")]
    assert 'class: "field__control"' in block


# --------------------------------------------------------------------------- #
# Empty states and duplicate-submission guards (Phase 7F)
# --------------------------------------------------------------------------- #


def test_jobs_view_distinguishes_filtered_empty_state() -> None:
    src = _read(FRONTEND_DIR / "js" / "views" / "jobs.js")
    assert "No jobs match your filters" in src
    assert "hasActiveFilters" in src


def test_jobs_view_submit_is_guarded_against_duplicates() -> None:
    src = _read(FRONTEND_DIR / "js" / "views" / "jobs.js")
    block = src[src.find("async function submitJob") : src.find("function createdSummary")]
    assert "submittingJob" in block
    assert "finally" in block


def test_jobs_view_submit_guard_resets_in_finally() -> None:
    src = _read(FRONTEND_DIR / "js" / "views" / "jobs.js")
    block = src[src.find("async function submitJob") : src.find("function createdSummary")]
    assert "submittingJob = false" in block

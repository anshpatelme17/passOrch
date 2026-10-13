"""Phase 4B.1 - Dashboard view contract tests.

These are *static and structural* assertions on ``frontend/js/views/dashboard.js``
plus its integration with the Phase 4A shell. No JavaScript runtime is available
in this repository (Node is absent; the Python JS engines either reject ES
modules or crash on Python 3.13), so this suite cannot execute the view. It
verifies what can be verified without a browser:

* which endpoints the view talks to, and only those;
* that every response field it reads exists in the real API schemas;
* that each section has its own slot, state and retry;
* that navigation uses the router's hash links;
* that no unsafe DOM sink or direct fetch call is introduced;
* that the frontend files are served, and the view registers a real route.

What it does NOT verify (limitation reported to the reviewer): live rendering,
event handling, or the actual DOM produced by a browser. Those need a JS
runtime and are out of reach here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import FRONTEND_DIR, create_app
from app.api.schemas import (
    EngineInfo,
    JobInfo,
    JobSummary,
    TargetInstance,
)

VIEW = FRONTEND_DIR / "js" / "views" / "dashboard.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code(path: Path) -> str:
    src = _read(path)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"^\s*//[^\n]*$", "", src, flags=re.MULTILINE)
    return src


DASHBOARD_CODE = _code(VIEW)

#: The only endpoints the dashboard may call.
ALLOWED_ENDPOINTS = ("/health", "/api/engines", "/api/jobs", "/api/target-instances")

#: Endpoints the dashboard must NOT touch.
FORBIDDEN_ENDPOINTS = (
    "/api/targets/{target_id}/compatibility/plan",
    "/api/target-instances/{instance_id}",
    "/api/jobs/{job_id}",
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


def test_dashboard_uses_exactly_the_four_overview_endpoints() -> None:
    calls = re.findall(r'api\.(get|post|delete)\("([^"]+)"', DASHBOARD_CODE)
    assert calls, "the dashboard must make API calls"
    used = {path for _method, path in calls}
    assert used == set(ALLOWED_ENDPOINTS), used


def test_dashboard_never_posts_or_deletes() -> None:
    """The dashboard is read-only."""

    assert not re.search(r"api\.post\(", DASHBOARD_CODE)
    assert not re.search(r"api\.delete\(", DASHBOARD_CODE)


def test_dashboard_only_reads_with_get() -> None:
    for method in re.findall(r"api\.(\w+)\(", DASHBOARD_CODE):
        assert method == "get", method


def test_dashboard_never_calls_fetch_directly() -> None:
    assert not re.search(r"\bfetch\s*\(", DASHBOARD_CODE)


def test_dashboard_does_not_touch_detail_or_write_endpoints() -> None:
    for endpoint in FORBIDDEN_ENDPOINTS:
        assert endpoint not in DASHBOARD_CODE, endpoint


def test_dashboard_never_triggers_discovery_or_refresh() -> None:
    """No call may refresh, discover or probe an engine.

    Explanatory prose *may* mention refresh (the empty state explains how data
    arrives), so this asserts call shapes rather than words.
    """

    code = _read(VIEW)  # comments kept: a call in a comment is still a red flag
    for pattern in (
        r"\brefresh\s*\(",
        r"\bdiscover(?:y)?\s*\(",
        r"\bprobe\s*\(",
        r"api\.(?:post|delete)\(",
        r"CapabilityRefreshService",
        r"run_safe_command",
    ):
        assert not re.search(pattern, code), pattern


# --------------------------------------------------------------------------- #
# response-field contract: every field the view reads must exist in the schemas
# --------------------------------------------------------------------------- #


def test_health_fields_used_by_the_view_exist() -> None:
    from app.api.app import create_app as _create_app

    spec = _create_app().openapi()
    # /health is an untyped dict response; assert the documented keys instead
    doc = spec["paths"]["/health"]["get"]
    assert doc["responses"]["200"] is not None
    code = DASHBOARD_CODE
    for field in ("status", "mode"):
        assert field in code, f"view must read /health.{field}"


def test_engine_fields_used_by_the_view_exist_in_the_schema() -> None:
    """The engine fields surfaced must be real EngineInfo fields.

    The view delegates to ``EnginesTable`` in components.js, so the field reads
    are asserted there; the view is asserted to use that table.
    """

    schema_fields = set(EngineInfo.model_fields)
    assert "EnginesTable" in DASHBOARD_CODE, "the dashboard must use the shared table"

    table_code = _read(FRONTEND_DIR / "js" / "components.js")
    engine_section = table_code[table_code.find("export function EnginesTable") :]
    engine_section = engine_section[: engine_section.find("\n}\n")]
    used = set(re.findall(r"\be\.(\w+)", engine_section))
    assert used, "EnginesTable must read engine fields"
    assert used <= schema_fields, used - schema_fields
    for field in ("engine", "discovered", "capability_status", "formats", "real_engine_compatible"):
        assert field in used, field


def test_job_summary_fields_used_by_the_view_exist_in_the_schema() -> None:
    schema_fields = set(JobSummary.model_fields)
    # The dashboard reads the list payload, so only JobSummary fields are legal.
    used = set(re.findall(r"job\.(\w+)", DASHBOARD_CODE))
    assert used, "the dashboard must read job fields"
    illegal = used - schema_fields
    assert not illegal, f"dashboard reads fields absent from JobSummary: {illegal}"


def test_instance_fields_used_by_the_view_exist_in_the_schema() -> None:
    schema_fields = set(TargetInstance.model_fields)
    used = set(re.findall(r"inst\.(\w+)", DASHBOARD_CODE))
    assert used, "the dashboard must read instance fields"
    illegal = used - schema_fields
    assert not illegal, f"dashboard reads fields absent from TargetInstance: {illegal}"


def test_dashboard_does_not_read_job_detail_only_fields() -> None:
    """Job list rows are JobSummary; instance/job detail fields are out of scope."""

    detail_only = set(JobInfo.model_fields) - set(JobSummary.model_fields)
    used = set(re.findall(r"job\.(\w+)", DASHBOARD_CODE))
    assert not (used & detail_only), used & detail_only


def test_response_envelopes_are_read_through_their_arrays() -> None:
    for envelope in ("res.data.jobs", "res.data.instances", "res.data.engines"):
        assert envelope in DASHBOARD_CODE, envelope


# --------------------------------------------------------------------------- #
# resilience: independent sections, states and retry
# --------------------------------------------------------------------------- #


def test_each_section_has_its_own_slot() -> None:
    slots = re.findall(r"const (\w+Slot) = ", DASHBOARD_CODE)
    assert len(slots) >= 4, slots
    assert "healthSlot" in slots
    assert "enginesSlot" in slots
    assert "jobsSlot" in slots
    assert "instancesSlot" in slots


def test_each_section_renderer_exists_and_is_invoked_once() -> None:
    for section in (
        "healthSection(",
        "enginesSection(",
        "jobsSection(",
        "instancesSection(",
    ):
        definition = f"async function {section}"
        assert definition in DASHBOARD_CODE, definition
        assert DASHBOARD_CODE.count(section) >= 2, section  # defined + invoked


def test_every_section_renders_a_loading_state() -> None:
    assert DASHBOARD_CODE.count("pending(") >= 4


def test_every_section_has_a_retry_action() -> None:
    assert DASHBOARD_CODE.count("RetryButton(") >= 4


def test_retries_reinvoke_their_own_section() -> None:
    """A retry must re-run the section's own loader, not a sibling's."""

    for section in ("healthSection", "enginesSection", "jobsSection", "instancesSection"):
        pattern = rf"RetryButton\(\(\) => {section}\("
        assert re.search(pattern, DASHBOARD_CODE), f"{section} has no self-retry"


def test_failed_sections_render_an_inline_error_and_continue() -> None:
    # Each section's failure branch renders InlineError + RetryButton and
    # returns, leaving the other sections' slots untouched.
    assert DASHBOARD_CODE.count("InlineError(res.error)") >= 4


def test_empty_states_are_handled_per_section() -> None:
    assert DASHBOARD_CODE.count("empty(") >= 3


def test_engine_empty_state_does_not_fabricate_data() -> None:
    """The engines panel must explain the empty case, never invent engines."""

    code = DASHBOARD_CODE
    assert "No engine capability data available" in code
    assert "operator-side refresh" in code


def test_job_and_instance_empty_states_link_to_their_screens() -> None:
    code = DASHBOARD_CODE
    assert 'NavLink("Go to Jobs", "/jobs")' in code
    assert 'NavLink("Go to Instances", "/instances")' in code


# --------------------------------------------------------------------------- #
# registration, navigation, safety
# --------------------------------------------------------------------------- #


def test_dashboard_registers_itself_with_the_router() -> None:
    assert 'registerView("/dashboard"' in DASHBOARD_CODE
    assert "render: renderDashboard" in DASHBOARD_CODE


def test_dashboard_keeps_the_simulation_only_notice_visible() -> None:
    assert "SimulationNotice()" in DASHBOARD_CODE
    assert "Simulation only" in DASHBOARD_CODE


def test_dashboard_uses_only_safe_dom_helpers() -> None:
    lowered = DASHBOARD_CODE.lower()
    for sink in ("innerhtml", "outerhtml", "insertadjacenthtml", "document.write", "eval(", "new function"):
        assert sink not in lowered, sink


def test_dashboard_imports_are_relative_and_exist() -> None:
    for bindings, target in re.findall(
        r'^\s*import\s*\{([^}]*)\}\s*from\s*"([^"]+)";', _read(VIEW), re.MULTILINE
    ):
        assert target.startswith("."), target
        resolved = (VIEW.parent / target).resolve()
        assert resolved.is_file(), target
        assert resolved.name in ("api.js", "app.js", "components.js", "dom.js"), target


def test_entry_module_starts_the_shell_with_the_dashboard_registered() -> None:
    main = _read(FRONTEND_DIR / "js" / "main.js")
    assert 'import "./views/dashboard.js";' in main, "entry must import the view"
    assert "start()" in main, "entry must start the shell"
    assert 'from "./app.js"' in main


def test_router_still_declares_all_six_destinations() -> None:
    app = _read(FRONTEND_DIR / "js" / "app.js")
    for path in (
        "/dashboard",
        "/engines",
        "/targets",
        "/instances",
        "/plan",
        "/jobs",
    ):
        assert f'"{path}"' in app, path


def test_shell_html_links_every_destination_and_the_notice() -> None:
    html = _read(FRONTEND_DIR / "index.html")
    for path in ("/dashboard", "/engines", "/targets", "/instances", "/plan", "/jobs"):
        assert f'href="#{path}"' in html, path
    assert "Simulation only" in html
    assert 'id="simulation-notice"' in html
    assert 'src="/static/js/main.js"' in html


# --------------------------------------------------------------------------- #
# styles referenced by the view exist
# --------------------------------------------------------------------------- #


def test_styles_referenced_by_the_view_are_defined() -> None:
    css = _read(FRONTEND_DIR / "css" / "styles.css")
    used_classes = set(re.findall(r'class: "([^"]+)"', _read(VIEW)))
    # These classes come from shared helpers/dom.js and must be defined there.
    shared = set(re.findall(r'class: "([^"]+)"', _read(FRONTEND_DIR / "js" / "dom.js")))
    shared |= set(re.findall(r'class: "([^"]+)"', _read(FRONTEND_DIR / "js" / "components.js")))
    own = {"section-body"}
    for cls in used_classes - shared - own:
        assert f".{cls}" in css, f".{cls} is used by the view but not defined"
    for cls in own:
        assert f".{cls}" in css, f".{cls} used by the view is missing from styles.css"


# --------------------------------------------------------------------------- #
# serving integration
# --------------------------------------------------------------------------- #


def test_dashboard_module_is_served() -> None:
    client = TestClient(create_app())
    res = client.get("/static/js/views/dashboard.js")
    assert res.status_code == 200
    assert res.text == _read(VIEW)


def test_entry_module_is_served() -> None:
    client = TestClient(create_app())
    assert client.get("/static/js/main.js").status_code == 200


def test_dashboard_route_is_a_known_router_path() -> None:
    app = _read(FRONTEND_DIR / "js" / "app.js")
    assert '"/dashboard"' in app

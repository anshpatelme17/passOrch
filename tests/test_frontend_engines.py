"""Phase 6A / M1.3 - Engines view contract tests.

Static and structural assertions on ``app/frontend/js/views/engines.js`` plus
integration with the Phase 4A shell. Verifies:

* which endpoints the view talks to, and only those;
* that every response field it reads exists in the real API schemas;
* that the view registers a real route;
* that no unsafe DOM sink or direct fetch call is introduced;
* that the frontend files are served;
* that "real-engine compatible" is clearly labeled as NOT meaning real execution;
* (M1.3) that the refresh button exists, posts to exactly
  ``POST /api/engines/refresh`` with **no body**, re-fetches the list only
  after a successful refresh, presents per-engine outcomes (including
  partial success), handles every documented failure (401/409/422/429/503,
  network, and refresh-ok-but-list-failed), carries the metadata-only
  wording, and never refreshes automatically on page load.
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

#: M1.3: the single additional call the refresh button may make. It is sent
#: with no body (see ``test_refresh_posts_with_no_body``).
REFRESH_ENDPOINT = "/api/engines/refresh"

#: Every ``api.<method>("<path>")`` call site in the view, captured by scan.
_API_CALL_PATTERN = re.compile(r'api\.(get|post|patch|put|delete)\("([^"]*)"')


def _api_call_sites() -> set[tuple[str, str]]:
    return set(_API_CALL_PATTERN.findall(ENGINES_CODE))


def test_engines_view_uses_only_the_allowed_endpoints() -> None:
    assert "api.get(" in ENGINES_CODE
    for endpoint in ALLOWED_ENDPOINTS:
        assert f'api.get("{endpoint}")' in ENGINES_CODE
    assert "api.delete(" not in ENGINES_CODE
    assert "api.patch(" not in ENGINES_CODE
    assert "api.put(" not in ENGINES_CODE


def test_engines_view_api_calls_are_exactly_the_allowlist() -> None:
    """The set of (method, path) call sites is closed: GET /api/engines and
    POST /api/engines/refresh - nothing else can ever be requested."""

    assert _api_call_sites() == {
        ("get", "/api/engines"),
        ("post", REFRESH_ENDPOINT),
    }


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


def test_engines_view_refreshes_only_via_the_dedicated_endpoint() -> None:
    """The word "refresh" appears in the view only as the allowlisted
    endpoint, its button and safe copy - never as a probe operation."""

    assert 'api.post("/api/engines/refresh")' in ENGINES_CODE


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
# M1.3 - refresh interaction contract
# --------------------------------------------------------------------------- #


def test_engines_view_has_a_refresh_button() -> None:
    assert '"Refresh engines"' in ENGINES_CODE
    assert 'data-refresh-button' in ENGINES_CODE


def test_refresh_posts_with_no_body() -> None:
    """The button may never send engine names, paths, commands, timeouts or
    limits: the post call carries no payload argument at all."""

    assert 'api.post("/api/engines/refresh")' in ENGINES_CODE
    assert "api.post(" in ENGINES_CODE
    # No second argument: exactly the endpoint string between the parens.
    assert not re.search(r'api\.post\(\s*"[^"]*"\s*,', ENGINES_CODE)


def test_refresh_disables_the_button_while_in_flight() -> None:
    """The busy render marks the button disabled and the guard variable
    prevents a second concurrent refresh from the same page."""

    assert 'renderControls(ctx, true)' in ENGINES_CODE
    assert '[["disabled", ""]]' in ENGINES_CODE
    assert "let refreshing = false" in ENGINES_CODE
    assert "if (refreshing)" in ENGINES_CODE


def test_refresh_shows_a_loading_state() -> None:
    assert 'pending("Discovering engine metadata...")' in ENGINES_CODE


def test_list_is_refetched_only_after_a_successful_refresh() -> None:
    """Ordering inside runRefresh: POST -> the ``res.ok`` guard (which
    returns on failure) -> only then the awaited list GET."""

    post_index = ENGINES_CODE.index('api.post("/api/engines/refresh")')
    guard_index = ENGINES_CODE.index("if (!res.ok)", post_index)
    get_index = ENGINES_CODE.index('await api.get("/api/engines")', post_index)
    assert post_index < guard_index < get_index


def test_refresh_outcome_distinguishes_capability_statuses() -> None:
    """VERIFIED / UNKNOWN / ERROR are rendered as the backend's own values
    via StatusBadge, and non-verified engines are named explicitly."""

    assert 'r.capability_status === "verified"' in ENGINES_CODE
    assert "StatusBadge(r.capability_status" in ENGINES_CODE
    assert "remains unverified" in ENGINES_CODE


def test_partial_success_is_not_presented_as_total_failure() -> None:
    """The mixed outcome shows both results: a per-engine table plus a
    named explanation for each unverified engine - never an error state."""

    assert "Metadata discovery finished: ${verified.length} of ${reports.length}" in ENGINES_CODE
    outcome_body = ENGINES_CODE.split("function refreshOutcomeNodes")[1].split("/* ---- view")[0]
    assert 'class: "state"' in outcome_body
    assert "state--error" not in outcome_body


def test_refresh_handles_every_documented_failure_code() -> None:
    """401 / 409 / 422 / 429 / 503 each map to a safe hint keyed on the
    backend's machine code; unknown codes fall back to a generic sentence."""

    for code in (
        "unauthorized",
        "refresh_in_progress",
        "rate_limited",
        "discovery_refresh_failed",
        "unknown_engine",
    ):
        assert code in ENGINES_CODE
    assert "res.status === 422" in ENGINES_CODE
    assert "res.error.kind === \"transport\"" in ENGINES_CODE


def test_refresh_failure_hints_never_leak_internals() -> None:
    """Hints are fixed strings; nothing reads exception text or raw bodies."""

    for leak in (".stack", "traceback", "exc_message", "response.raw"):
        assert leak not in ENGINES_CODE


def test_refresh_success_with_list_failure_is_explicitly_communicated() -> None:
    assert (
        '"Discovery completed, but the engine list could not be updated."' in ENGINES_CODE
    )
    assert "The refresh itself succeeded; only the list below could not be reloaded." in ENGINES_CODE
    assert '"Retry loading the engine list"' in ENGINES_CODE


def test_metadata_only_wording_is_present() -> None:
    """The exact product sentence, and no wording that could suggest an
    audit, a crack or a real attack."""

    assert (
        '"Discovery checks installed engine metadata and supported capabilities. "' in ENGINES_CODE
    )
    assert '"It does not perform password auditing or cracking."' in ENGINES_CODE
    lower = ENGINES_CODE.lower()
    for banned in ("cracked", "password found", "hash recovered", "audit completed"):
        assert banned not in lower


def test_no_automatic_refresh_on_page_load() -> None:
    """Rendering the view issues exactly one request - the GET. No POST is
    dispatched until the button's onClick handler runs."""

    assert ENGINES_CODE.count("api.post(") == 1
    assert "setInterval" not in ENGINES_CODE
    assert "setTimeout" not in ENGINES_CODE
    # The only *call site* of runRefresh is the button's onClick binding;
    # the other textual occurrence is the function's own definition.
    assert ENGINES_CODE.count("=> runRefresh(ctx)") == 1
    assert "onClick: () => runRefresh(ctx)" in ENGINES_CODE


def test_no_token_storage_or_persistence_is_introduced() -> None:
    for sink in ("localStorage", "sessionStorage", "document.cookie", "Authorization"):
        assert sink not in ENGINES_CODE


# --------------------------------------------------------------------------- #
# M1.4 - truncated discovery evidence is visibly identified
# --------------------------------------------------------------------------- #


def test_truncated_discovery_results_are_surfaced_with_a_warning() -> None:
    """``truncated: true`` renders an explicit caveat on the evidence."""

    assert "report.truncated !== true" in ENGINES_CODE  # the guard itself
    assert "Truncated discovery output for" in ENGINES_CODE
    assert "the captured metadata was incomplete" in ENGINES_CODE
    assert "must not be interpreted as verified capability evidence" in ENGINES_CODE


def test_truncation_warning_never_alters_the_capability_status() -> None:
    """The backend status is preserved verbatim: the warning names the
    unchanged status and no new status value is ever written."""

    assert "The reported capability status (" in ENGINES_CODE
    assert "StatusBadge(r.capability_status" in ENGINES_CODE
    # No assignment to capability_status anywhere (comparisons like `===`
    # and `!==` are excluded by the `[^=]`).
    assert re.search(r"capability_status\s*=[^=]", ENGINES_CODE) is None


def test_truncation_warning_is_a_caveat_not_a_failure() -> None:
    """Truncated evidence never turns a success or partial outcome into an
    error state, and the caveat renders as neutral hint text."""

    outcome = ENGINES_CODE.split("function refreshOutcomeNodes")[1].split(
        "/* ---- view"
    )[0]
    assert "state--error" not in outcome
    assert "Truncated discovery output for" in outcome
    # The caveat uses the same neutral hint style as every other explanatory
    # line in the outcome block (the module never builds an error state here).
    assert 'class: "panel__hint"' in outcome


def test_view_never_displays_raw_probe_output() -> None:
    """No raw discovery output can be surfaced by the truncation caveat (or
    anywhere else): the module never references probe-output fields."""

    for raw in ("stdout", "stderr", "argv"):
        assert raw not in ENGINES_CODE


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

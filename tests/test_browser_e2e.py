"""Phase 11B - real-browser end-to-end tests (Playwright + Chromium).

These tests drive the actual bundled SPA in headless Chromium against a
real uvicorn server bound to127.0.0.1 on an ephemeral port, covering the
cross-layer workflows the Node harness (frontend-tests/) and the API suites
cannot cover alone: routing, DOM rendering, form interaction, the
client-side handling of structured API errors, and (M2.3) the export
JSON file download flow.

Safety contracts:

* simulation-only - every exercised request is a plan preview or a
  simulated-mode job; nothing here can execute an engine;
* no bearer token ever reaches the page, console, or assertions - the
  auth scenario keeps the token server-side and only checks that the SPA
  surfaces the generic 401 message;
* fixtures use in-memory stores and ephemeral ports only - no database
  files, no user data, nothing outside pytest's control;
* the module skips itself (importorskip) when the optional ``playwright``
  package is absent, so environments without a browser framework stay
  green without claiming browser coverage;
* ``pytestmark`` opts into ``allow_subprocess`` because Playwright's driver
  and Chromium are separate processes - the server itself runs in-thread.

Flakiness policy: all waits use Playwright's auto-waiting expectations on
text, roles and element ids; no sleeps, no pixel/position assertions.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

pytest.importorskip(
    "playwright", reason="optional browser E2E dependency (pip install playwright)"
)

from playwright.sync_api import Page, expect, sync_playwright  # noqa: E402

from app.api.app import create_app  # noqa: E402
from app.engines.jobs import JobStore  # noqa: E402
from app.engines.target_instances import TargetInstanceStore  # noqa: E402

pytestmark = pytest.mark.allow_subprocess

TARGET = "pdf-document"
#: Never rendered, never sent to the browser; asserted absent from the page.
AUTH_TOKEN = "browser-e2e-token-must-never-reach-the-page"
_PAGE_TIMEOUT_MS = 10_000


def _fresh_app(**overrides: Any):
    return create_app(
        job_store=JobStore(),
        target_instance_store=TargetInstanceStore(),
        **overrides,
    )


@contextlib.contextmanager
def _live_server(application: Any) -> Iterator[str]:
    """Run uvicorn in a daemon thread; yield the127.0.0.1 base URL."""

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])

    config = uvicorn.Config(
        application,
        host="127.0.0.1",
        port=port,
        log_level="error",
        access_log=False,
        lifespan="off",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if server.started:
            try:
                httpx.get(f"{base}/health", timeout=0.5)
                break
            except httpx.HTTPError:
                pass
        time.sleep(0.05)
    else:  # pragma: no cover - only on a broken startup
        raise RuntimeError("uvicorn did not become ready in time")

    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=20)


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    driver = sync_playwright().start()
    try:
        instance = driver.chromium.launch(headless=True)
        try:
            yield instance
        finally:
            instance.close()
    finally:
        driver.stop()


@pytest.fixture
def page(browser: Any) -> Iterator[Page]:
    context = browser.new_context()
    try:
        new_page = context.new_page()
        new_page.set_default_timeout(_PAGE_TIMEOUT_MS)
        yield new_page
    finally:
        context.close()


def _seed_jobs(base: str, count: int) -> list[str]:
    ids: list[str] = []
    for index in range(count):
        response = httpx.post(
            f"{base}/api/jobs",
            json={"target": TARGET, "mode": "simulated", "reference": f"seed-{index}"},
        )
        assert response.status_code == 200, response.text
        ids.append(str(response.json()["job_id"]))
    return ids


def test_dashboard_loads_health_engines_and_empty_states(page: Page, browser: Any) -> None:
    console_errors: list[str] = []
    page.on(
        "console",
        lambda message: console_errors.append(message.text) if message.type == "error" else None,
    )
    with _live_server(_fresh_app()) as base:
        page.goto(f"{base}/#/dashboard")

        expect(page.get_by_role("heading", name="PassOrch")).to_be_visible()
        expect(page.locator("#simulation-notice")).to_contain_text(
            "Simulation only — no engine is ever executed."
        )
        for heading in (
            "Application health",
            "Engines",
            "Job statistics",
            "Recent jobs",
            "Target instances",
        ):
            expect(page.get_by_role("heading", name=heading)).to_be_visible()

        # /health rendered into the dashboard (liveness + operating mode).
        expect(page.get_by_text("mode: simulated")).to_be_visible()

        # Fresh stores: honest empty states, not invented data.
        expect(page.get_by_text("No jobs recorded yet")).to_be_visible()
        expect(page.get_by_text("No target instances defined")).to_be_visible()

    # The served Content-Security-Policy must be accepted by a real browser:
    # violations surface as console errors naming the policy.
    assert [text for text in console_errors if "Content Security Policy" in text] == []


def test_instance_create_filter_and_empty_filter_state(page: Page) -> None:
    with _live_server(_fresh_app()) as base:
        page.goto(f"{base}/#/instances")
        expect(page.locator("#instance-name")).to_be_visible()

        expect(page.get_by_text("No target instances yet")).to_be_visible()

        page.locator("#instance-name").fill("Browser Probe")
        page.locator("#instance-type").select_option(TARGET)
        page.locator("#instance-description").fill("created by browser e2e")
        page.get_by_role("button", name="Create target instance").click()

        expect(page.get_by_text('Created target instance "Browser Probe".')).to_be_visible()
        expect(page.get_by_text("Browser Probe").first).to_be_visible()
        expect(page.get_by_text("1 target instance(s) defined.")).to_be_visible()

        # Filter that matches nothing -> the filtered empty state; clear restores.
        page.locator("#instances-filter-name").fill("no-such-probe-name")
        page.get_by_role("button", name="Apply filters").click()
        expect(page.get_by_text("No target instances match your filters")).to_be_visible()

        page.get_by_role("button", name="Clear").click()
        # Scope to the table cell: the create-success toast also contains the
        # instance name and would make a bare get_by_text ambiguous.
        expect(page.get_by_role("cell", name="Browser Probe")).to_be_visible()


def test_instance_edit_renames_through_the_dedicated_form(page: Page) -> None:
    with _live_server(_fresh_app()) as base:
        page.goto(f"{base}/#/instances")
        page.locator("#instance-name").fill("Editable Probe")
        page.locator("#instance-type").select_option(TARGET)
        page.get_by_role("button", name="Create target instance").click()
        expect(page.get_by_text('Created target instance "Editable Probe".')).to_be_visible()

        # The row offers Edit next to Delete; the form opens prefilled and
        # explains that only the name and description are mutable.
        page.get_by_role("button", name="Edit").click()
        expect(page.get_by_text('Edit target instance "Editable Probe"')).to_be_visible()
        expect(page.get_by_text("Only the name and description can be changed.")).to_be_visible()
        expect(page.locator("#instance-edit-name")).to_have_value("Editable Probe")

        page.locator("#instance-edit-name").fill("Editable Probe (edited)")
        page.get_by_role("button", name="Save changes").click()

        expect(page.get_by_text('Updated target instance "Editable Probe (edited)".')).to_be_visible()
        expect(page.get_by_text("Editable Probe (edited)").first).to_be_visible()
        # The immutable target type survived the update in the refreshed row.
        expect(page.get_by_role("table").get_by_text(TARGET)).to_be_visible()


def test_job_submit_records_denial_and_detail_stays_denied(page: Page) -> None:
    with _live_server(_fresh_app()) as base:
        page.goto(f"{base}/#/jobs")
        expect(page.locator("#job-target")).to_be_visible()
        expect(page.get_by_text("No jobs recorded yet")).to_be_visible()

        page.locator("#job-target").select_option(TARGET)
        page.get_by_role("button", name="Create simulated job").click()

        # With no verified engines present the policy denies - a valid,
        # recorded terminal outcome shown as a success-state, not a transport error.
        expect(page.get_by_text("Job recorded with a denied outcome.")).to_be_visible()
        expect(page.get_by_text("No engine ran and no plan was authorized")).to_be_visible()
        expect(page.get_by_text("1 recorded job(s), latest first.")).to_be_visible()

        page.get_by_role("button", name="Details").first.click()
        expect(page.get_by_text(
            "This job was denied by the execution policy. No engine was executed."
        )).to_be_visible()
        expect(page.get_by_text("simulated only").first).to_be_visible()


def test_jobs_list_pagination_filters_and_sort(page: Page) -> None:
    with _live_server(_fresh_app()) as base:
        seeded = _seed_jobs(base, 51)

        page.goto(f"{base}/#/jobs")
        expect(page.get_by_text("51 recorded job(s), latest first.")).to_be_visible()
        expect(page.locator(".pagination__info")).to_have_text("Page 1 of 2 (51 total)")

        page.get_by_role("button", name="Next").click()
        expect(page.locator(".pagination__info")).to_have_text("Page 2 of 2 (51 total)")

        # Target filter with no matches -> filtered empty state; reset restores.
        page.locator("#jobs-filter-target").select_option("zip-archive")
        expect(page.get_by_text("No jobs match your filters")).to_be_visible()
        page.locator("#jobs-filter-target").select_option("")
        expect(page.get_by_text("51 recorded job(s), latest first.")).to_be_visible()

        # Sort oldest-first: the first row becomes the first seeded job.
        page.locator("#jobs-filter-sort").select_option("created_asc")
        expect(page.get_by_text(seeded[0])).to_be_visible()
        first_code = page.locator("table code").first.text_content()
        assert first_code == seeded[0]

        # Instance filter "No instance": all seeded jobs are unassociated.
        page.locator("#jobs-filter-instance").select_option("none")
        expect(page.get_by_text("51 recorded job(s), latest first.")).to_be_visible()


def test_jobs_detail_clears_when_the_status_filter_excludes_the_selected_job(
    page: Page,
) -> None:
    """Manual acceptance: a filtered-out job must not linger in the detail panel."""

    with _live_server(_fresh_app()) as base:
        _seed_jobs(base, 2)
        page.goto(f"{base}/#/jobs")
        expect(page.get_by_text("2 recorded job(s), latest first.")).to_be_visible()

        selected_id = page.locator("table code").first.text_content()
        page.get_by_role("button", name="Details").first.click()
        expect(page.get_by_text(f"Job {selected_id}")).to_be_visible()
        expect(
            page.get_by_text(
                "This job was denied by the execution policy. No engine was executed."
            )
        ).to_be_visible()

        # Both seeded jobs are denied, so this filter has no matches.
        page.locator("#jobs-filter-status").select_option("simulated")
        expect(page.get_by_text("No jobs match your filters")).to_be_visible()
        expect(page.get_by_text("No job selected", exact=True)).to_be_visible()
        expect(
            page.get_by_text(
                "Select a job from the current list to inspect its details.", exact=True
            )
        ).to_be_visible()
        expect(page.get_by_text(f"Job {selected_id}")).to_have_count(0)

        # Clearing the filter restores the list but must not restore the
        # cleared selection; a different visible job can then be opened.
        page.locator("#jobs-filter-status").select_option("")
        expect(page.get_by_text("2 recorded job(s), latest first.")).to_be_visible()
        second_id = page.locator("table code").nth(1).text_content()
        page.get_by_role("button", name="Details").nth(1).click()
        expect(page.get_by_text(f"Job {second_id}")).to_be_visible()
        expect(page.get_by_text(f"Job {selected_id}")).to_have_count(0)


def test_export_json_downloads_the_envelope_and_cleans_up(page: Page) -> None:
    """M2.3 - a real browser downloads the M2.1 envelope through the M2.2 UI.

    Proves the download mechanics end to end where the Node harness cannot:
    a JSON file arrives with the UUID-derived filename, its bytes parse as
    the versioned envelope, the temporary anchor is removed again, and every
    object URL is revoked - with the revoke running synchronously right after
    ``click()`` while the download still completes (the reliability question
    this milestone had to answer, demonstrated rather than assumed).
    """
    console_errors: list[str] = []
    page.on(
        "console",
        lambda message: console_errors.append(message.text) if message.type == "error" else None,
    )
    with _live_server(_fresh_app()) as base:
        job_id = _seed_jobs(base, 1)[0]
        page.goto(f"{base}/#/jobs")

        # Record the object-URL lifecycle from inside the page, so the test
        # can assert the temporary resources are cleaned up after the click.
        page.evaluate(
            """
            () => {
              window.__exportUrls = { created: [], revoked: [] };
              const create = URL.createObjectURL.bind(URL);
              const revoke = URL.revokeObjectURL.bind(URL);
              URL.createObjectURL = (blob) => {
                const url = create(blob);
                window.__exportUrls.created.push(url);
                return url;
              };
              URL.revokeObjectURL = (url) => {
                window.__exportUrls.revoked.push(url);
                revoke(url);
              };
            }
            """
        )

        # The export control exists only inside an opened job detail.
        page.get_by_role("button", name="Details").first.click()
        expect(page.get_by_role("button", name="Export JSON")).to_be_visible()

        with page.expect_download() as download_info:
            page.get_by_role("button", name="Export JSON").click()
        download = download_info.value

        assert download.failure() is None, download.failure()
        assert download.suggested_filename == f"passorch-job-{job_id}.json"

        payload = json.loads(Path(str(download.path())).read_text(encoding="utf-8"))
        assert set(payload) == {"schema_version", "exported_at", "simulation_only", "job"}
        assert payload["schema_version"] == "1"
        assert payload["simulation_only"] is True
        assert isinstance(payload["exported_at"], int)
        assert payload["job"]["job_id"] == job_id
        # the operator reference survives into the file unchanged
        assert payload["job"]["preview"]["request"]["reference"] == "seed-0"

        # Success state in the UI; the envelope body itself never enters the page.
        expect(page.get_by_text("Export downloaded as a JSON file.")).to_be_visible()
        assert "schema_version" not in page.content()

        # Temporary download resources are cleaned up: anchor gone, every
        # object URL revoked (the revoke fires synchronously after click).
        expect(page.locator("a[download]")).to_have_count(0)
        urls = page.evaluate("() => window.__exportUrls")
        assert len(urls["created"]) == 1
        assert urls["revoked"] == urls["created"]

        # The strict CSP must accept the blob-URL download (violations would
        # surface as console errors naming the policy).
        assert [text for text in console_errors if "Content Security Policy" in text] == []


def test_plan_preview_denial_is_structured_and_honest(page: Page) -> None:
    with _live_server(_fresh_app()) as base:
        page.goto(f"{base}/#/plan")
        expect(page.locator("#plan-target")).to_be_visible()

        page.locator("#plan-target").select_option(TARGET)
        page.get_by_role("button", name="Preview simulated plan").click()

        expect(page.get_by_role("alert")).to_contain_text(
            "Denied - no simulation was produced."
        )
        expect(page.get_by_text(
            "The execution policy refused this request."
        )).to_be_visible()
        expect(page.get_by_text("simulated only").first).to_be_visible()


def test_bearer_auth_blocks_ui_mutations_with_generic_structured_error(page: Page) -> None:
    with _live_server(_fresh_app(api_token=AUTH_TOKEN)) as base:
        console_messages: list[str] = []
        page.on("console", lambda message: console_messages.append(message.text))

        # Reads stay open: the dashboard (health + lists) loads without a token.
        page.goto(f"{base}/#/dashboard")
        expect(page.get_by_text("mode: simulated")).to_be_visible()
        expect(page.get_by_text("No jobs recorded yet")).to_be_visible()

        # A mutation the SPA cannot authenticate: generic structured 401 in the UI.
        page.goto(f"{base}/#/jobs")
        expect(page.locator("#job-target")).to_be_visible()
        page.locator("#job-target").select_option(TARGET)
        page.get_by_role("button", name="Create simulated job").click()
        expect(page.get_by_role("alert")).to_contain_text("This section failed to load")
        expect(page.get_by_role("alert")).to_contain_text(
            "a valid bearer token is required (unauthorized)"
        )

        page.goto(f"{base}/#/instances")
        expect(page.locator("#instance-name")).to_be_visible()
        page.locator("#instance-name").fill("Blocked Probe")
        page.locator("#instance-type").select_option(TARGET)
        page.get_by_role("button", name="Create target instance").click()
        expect(page.get_by_role("alert")).to_contain_text(
            "a valid bearer token is required (unauthorized)"
        )
        expect(page.get_by_text("Blocked Probe")).to_have_count(0)

        # The token must never appear in the DOM or the console.
        assert AUTH_TOKEN not in page.content()
        assert all(AUTH_TOKEN not in message for message in console_messages)


def test_ui_survives_api_unreachable_with_structured_transport_error(page: Page) -> None:
    # Serve, load, then stop the server so later UI actions fail in a
    # structured (client-side) way rather than crashing the SPA.
    with _live_server(_fresh_app()) as base:
        page.goto(f"{base}/#/jobs")
        expect(page.locator("#job-target")).to_be_visible()
        expect(page.get_by_text("No jobs recorded yet")).to_be_visible()

    # Server is gone: submitting surfaces the client's transport error state.
    page.locator("#job-target").select_option(TARGET)
    page.get_by_role("button", name="Create simulated job").click()
    expect(page.get_by_role("alert")).to_be_visible()
    expect(page.get_by_text("No engine binary is ever invoked")).to_be_visible()

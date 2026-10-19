"""Phase 11B - real-browser end-to-end tests (Playwright + Chromium).

These tests drive the actual bundled SPA in headless Chromium against a
real uvicorn server bound to127.0.0.1 on an ephemeral port, covering the
cross-layer workflows the Node harness (frontend-tests/) and the API suites
cannot cover alone: routing, DOM rendering, form interaction, and the
client-side handling of structured API errors.

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
import socket
import threading
import time
from collections.abc import Iterator
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
        expect(page.get_by_text("Browser Probe")).to_be_visible()


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

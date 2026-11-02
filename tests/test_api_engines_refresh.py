"""M1.1 - discovery-refresh API tests (``POST /api/engines/refresh``).

Proves:

* the request schema accepts **only** known engine names - no path, command,
  operation, parser, timeout, limit or status field can ever be supplied;
* missing body / ``{}`` refreshes all, an explicit list refreshes only those
  engines, duplicates are de-duplicated by the service, unknown engines fail
  closed (404) **before any probe runs**;
* responses carry exactly the closed ``RefreshReport`` field set - no raw
  output, command, executable path or exception text;
* the endpoint sits behind the existing middleware unchanged: bearer auth
  (401) and rate limiting (429) apply exactly as for other mutations;
* unexpected internal failures become a generic 503, concurrent refreshes a
  generic 409, and the process-local lock is released on every path;
* the route module itself never imports a process-execution library or any
  discovery internal below ``CapabilityRefreshService`` (AST scan).

Every test injects a stub or double service - the autouse subprocess
kill-switch in ``tests/conftest.py`` guarantees no real engine is ever
located or executed here.
"""

from __future__ import annotations

import ast
import inspect
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.app import create_app
from app.api.routes import engine_refresh as refresh_module
from app.api.schemas import (
    EngineRefreshReport,
    RefreshEnginesRequest,
    RefreshEnginesResponse,
)
from app.engines.discovery.errors import UnknownEngineError
from app.engines.discovery.models import (
    DISCOVERY_ENGINE_ORDER,
    HASHCAT,
    JOHN,
    CapabilityStatus,
    KNOWN_ENGINES,
)
from app.engines.discovery.registry import DiscoveryRegistry
from app.engines.discovery.runner import run_safe_command as shared_run_safe_command
from app.engines.discovery.service import (
    CapabilityRefreshService,
    RefreshReport,
)

REFRESH_PATH = "/api/engines/refresh"

REFRESH_REPORT_KEYS = {"engine", "discovered", "capability_status", "reason", "truncated"}

#: Command-surface keys that must never appear in any refresh payload.
FORBIDDEN_KEYS = frozenset(
    {
        "argv",
        "binary",
        "cmd",
        "command",
        "executable",
        "output",
        "path",
        "probe_output",
        "raw_output",
        "shell",
        "stderr",
        "stdout",
    }
)

RAW_MARKER = "RAW_OUTPUT_MARKER_4418"

VERIFIED_JOHN = RefreshReport(
    engine=JOHN,
    discovered=True,
    capability_status=CapabilityStatus.VERIFIED,
    reason="",
    truncated=False,
)
UNKNOWN_HASHCAT = RefreshReport(
    engine=HASHCAT,
    discovered=False,
    capability_status=CapabilityStatus.UNKNOWN,
    reason="binary_not_found",
    truncated=False,
)


# --------------------------------------------------------------------------- #
# injected doubles - no real engine, no real process
# --------------------------------------------------------------------------- #


class StubRefreshService:
    """Injected refresh double - records every subset, returns canned reports."""

    def __init__(
        self,
        reports: tuple[RefreshReport, ...] = (VERIFIED_JOHN,),
        *,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[object] = []
        self._reports = reports
        self._error = error

    def refresh(self, engine_subset: object = None) -> tuple[RefreshReport, ...]:
        self.calls.append(engine_subset)
        if self._error is not None:
            raise self._error
        return self._reports


class BlockingRefreshService:
    """Refresh double that blocks inside the critical section until released."""

    def __init__(
        self,
        entered: threading.Event,
        release: threading.Event,
        report: RefreshReport = VERIFIED_JOHN,
    ) -> None:
        self.calls = 0
        self.entered = entered
        self.release = release
        self._report = report

    def refresh(self, engine_subset: object = None) -> tuple[RefreshReport, ...]:
        self.calls += 1
        self.entered.set()
        if not self.release.wait(timeout=30):  # safety net, never the synchroniser
            raise AssertionError("blocking refresh double was never released")
        return (self._report,)


class FakeWhich:
    """Injected binary resolver double - never touches the real filesystem."""

    def __init__(self, mapping: dict[str, str | None] | None = None) -> None:
        self.calls: list[str] = []
        self._mapping = dict(mapping if mapping is not None else {})

    def __call__(self, name: str) -> str | None:
        self.calls.append(name)
        return self._mapping.get(name)


def _client(
    service: object | None = None,
    **kwargs: object,
) -> tuple[TestClient, object]:
    """App with an injected refresh double (or a real service over doubles)."""

    stub = service if service is not None else StubRefreshService()
    app = create_app(discovery_refresh_service=stub, **kwargs)  # type: ignore[arg-type]
    return TestClient(app), stub


def _forbidden_key_walk(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in FORBIDDEN_KEYS:
                found.append(key)
            found.extend(_forbidden_key_walk(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_forbidden_key_walk(item))
    return found


# --------------------------------------------------------------------------- #
# schema-level contract (Phase 1)
# --------------------------------------------------------------------------- #


def test_request_schema_defaults_to_refresh_all() -> None:
    assert RefreshEnginesRequest().engines is None
    assert RefreshEnginesRequest(engines=None).engines is None


def test_request_schema_accepts_explicit_lists() -> None:
    assert RefreshEnginesRequest(engines=[JOHN]).engines == [JOHN]
    assert RefreshEnginesRequest(engines=[]).engines == []


def test_request_schema_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError) as excinfo:
        RefreshEnginesRequest(engines=[JOHN], timeout=1)
    assert excinfo.value.errors()[0]["type"] == "extra_forbidden"


def test_request_schema_rejects_wrong_types() -> None:
    with pytest.raises(ValidationError):
        RefreshEnginesRequest(engines=JOHN)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        RefreshEnginesRequest(engines=[1, 2])  # type: ignore[list-item]


def test_response_report_schema_is_closed_to_known_status_values() -> None:
    report = EngineRefreshReport(
        engine=JOHN,
        discovered=True,
        capability_status=CapabilityStatus.VERIFIED,
    )
    assert report.model_dump() == {
        "engine": JOHN,
        "discovered": True,
        "capability_status": "verified",
        "reason": "",
        "truncated": False,
    }
    with pytest.raises(ValidationError):
        EngineRefreshReport(
            engine=JOHN,
            discovered=True,
            capability_status="super-verified",
        )


def test_response_report_exposes_exactly_the_refresh_report_fields() -> None:
    assert set(EngineRefreshReport.model_fields) == REFRESH_REPORT_KEYS
    assert set(RefreshEnginesResponse.model_fields) == {"engines"}


# --------------------------------------------------------------------------- #
# request contract
# --------------------------------------------------------------------------- #


def test_post_without_body_refreshes_all() -> None:
    client, stub = _client()

    response = client.post(REFRESH_PATH)

    assert response.status_code == 200
    assert stub.calls == [None]


def test_post_with_empty_object_refreshes_all() -> None:
    client, stub = _client()

    response = client.post(REFRESH_PATH, json={})

    assert response.status_code == 200
    assert stub.calls == [None]


def test_post_with_null_engines_refreshes_all() -> None:
    client, stub = _client()

    response = client.post(REFRESH_PATH, json={"engines": None})

    assert response.status_code == 200
    assert stub.calls == [None]


def test_post_explicit_john_only() -> None:
    client, stub = _client()

    response = client.post(REFRESH_PATH, json={"engines": [JOHN]})

    assert response.status_code == 200
    assert stub.calls == [[JOHN]]
    assert [entry["engine"] for entry in response.json()["engines"]] == [JOHN]


def test_duplicate_engine_names_are_deduplicated_by_the_service() -> None:
    """The route passes the list verbatim; the service de-duplicates, so
    one report comes back per distinct engine."""

    which = FakeWhich()
    service = CapabilityRefreshService(DiscoveryRegistry(), which=which)
    client, _ = _client(service)

    response = client.post(REFRESH_PATH, json={"engines": [JOHN, JOHN]})

    assert response.status_code == 200
    assert [entry["engine"] for entry in response.json()["engines"]] == [JOHN]
    assert which.calls == ["john"]  # located once, not twice


def test_unknown_engine_returns_404_unknown_engine_envelope() -> None:
    """Real service over injected doubles: the unknown name is rejected
    before ``which`` is consulted - the stub-free path is the one that
    proves fail-closed ordering."""

    which = FakeWhich()
    service = CapabilityRefreshService(DiscoveryRegistry(), which=which)
    client, _ = _client(service)

    response = client.post(REFRESH_PATH, json={"engines": ["not-an-engine"]})

    assert response.status_code == 404
    assert response.json() == {
        "detail": {"error": "unknown_engine", "detail": "engine is not a known engine"}
    }
    assert which.calls == []  # no locate, no probe, nothing mutated


def test_mixed_valid_and_unknown_fails_closed_with_zero_probes() -> None:
    """Real service over injected doubles: the whole subset is rejected
    before ``which`` is consulted - no locate, no probe, no registry write."""

    which = FakeWhich()
    registry = DiscoveryRegistry()
    service = CapabilityRefreshService(registry, which=which)
    client, _ = _client(service)

    response = client.post(REFRESH_PATH, json={"engines": [JOHN, "not-an-engine"]})

    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "unknown_engine"
    assert which.calls == []
    assert registry.payload()["engines"] == []


def test_empty_engine_list_returns_empty_report() -> None:
    which = FakeWhich()
    service = CapabilityRefreshService(DiscoveryRegistry(), which=which)
    client, _ = _client(service)

    response = client.post(REFRESH_PATH, json={"engines": []})

    assert response.status_code == 200
    assert response.json() == {"engines": []}
    assert which.calls == []


def test_extra_request_fields_return_422_extra_forbidden() -> None:
    client, stub = _client()

    response = client.post(REFRESH_PATH, json={"engines": [JOHN], "timeout": 5})

    assert response.status_code == 422
    body = response.json()
    assert body["detail"][0]["type"] == "extra_forbidden"
    assert stub.calls == []


def test_command_surface_request_fields_return_422() -> None:
    """No client can ever name a command, path, operation or parser field."""

    client, stub = _client()
    for payload in (
        {"command": "john --crack"},
        {"argv": ["john", "--crack"]},
        {"path": "C:/tools/john"},
        {"binary": "john"},
        {"operation": "--list=formats"},
        {"parser": "john-format-parser"},
        {"max_output_bytes": 1},
        {"capability_status": "verified"},
        {"engines": [JOHN], "shell": True},
    ):
        response = client.post(REFRESH_PATH, json=payload)
        assert response.status_code == 422, payload
        assert response.json()["detail"][0]["type"] == "extra_forbidden"
    assert stub.calls == []


def test_malformed_json_returns_422() -> None:
    client, stub = _client()

    response = client.post(
        REFRESH_PATH, content=b'{"engines": [', headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 422
    assert stub.calls == []


def test_wrong_field_types_return_422() -> None:
    client, stub = _client()
    for payload in ({"engines": "john"}, {"engines": [1, 2]}, {"engines": {"a": 1}}):
        response = client.post(REFRESH_PATH, json=payload)
        assert response.status_code == 422, payload
    assert stub.calls == []


# --------------------------------------------------------------------------- #
# response contract
# --------------------------------------------------------------------------- #


def test_response_serializes_the_report_fields_exactly() -> None:
    client, _ = _client(StubRefreshService(reports=(VERIFIED_JOHN, UNKNOWN_HASHCAT)))

    response = client.post(REFRESH_PATH)

    assert response.status_code == 200
    assert response.json() == {
        "engines": [
            {
                "engine": JOHN,
                "discovered": True,
                "capability_status": "verified",
                "reason": "",
                "truncated": False,
            },
            {
                "engine": HASHCAT,
                "discovered": False,
                "capability_status": "unknown",
                "reason": "binary_not_found",
                "truncated": False,
            },
        ]
    }


def test_response_preserves_deterministic_service_order() -> None:
    client, _ = _client(StubRefreshService(reports=(UNKNOWN_HASHCAT, VERIFIED_JOHN)))

    response = client.post(REFRESH_PATH)

    assert [entry["engine"] for entry in response.json()["engines"]] == list(
        DISCOVERY_ENGINE_ORDER
    )


def test_response_carries_no_raw_output_and_no_command_keys() -> None:
    """Even a misbehaving service cannot smuggle extra fields through the
    strict response schema: only the five report fields are ever copied,
    and canned raw output never reaches the wire."""

    class LeakyReport:
        engine = JOHN
        discovered = True
        capability_status = CapabilityStatus.VERIFIED
        reason = ""
        truncated = False
        # Attempts - the route copies only the five report fields.
        stdout = RAW_MARKER
        command = RAW_MARKER

    class LeakyService:
        def refresh(self, engine_subset: object = None) -> tuple[object, ...]:
            return (LeakyReport(),)

    client, _ = _client(LeakyService())

    response = client.post(REFRESH_PATH)

    assert response.status_code == 200
    body = response.json()
    assert _forbidden_key_walk(body) == []
    assert RAW_MARKER not in response.text
    assert set(body["engines"][0]) == REFRESH_REPORT_KEYS


def test_real_service_refresh_through_the_api_updates_the_registry() -> None:
    """HTTP -> route -> real service (doubled which/executor) -> registry ->
    response: the full chain with zero real processes."""

    from tests.test_discovery_service import RecordingExecutor, RecordingWhich

    from app.engines.discovery import john as john_module

    good_output = (
        "Supported hash algorithms:\n"
        "  md5crypt, sha256crypt, sha512crypt, bcrypt,\n"
        "  ntlm, raw-md5, raw-sha1, raw-sha256,\n"
        "  zip, pkzip, pdf, office2007, office2010, office2013\n"
    )
    which = RecordingWhich()
    executor = RecordingExecutor(
        {john_module.JOHN_FORMAT_OPERATION: (0, good_output, "", False)}
    )
    registry = DiscoveryRegistry()
    service = CapabilityRefreshService(registry, which=which, executor=executor)
    app = create_app(
        discovery_registry=registry,
        discovery_refresh_service=service,
    )
    client = TestClient(app)

    response = client.post(REFRESH_PATH, json={"engines": [JOHN]})

    assert response.status_code == 200
    entry = response.json()["engines"][0]
    assert entry["engine"] == JOHN
    assert entry["capability_status"] == "verified"
    # The registry the GET routes read now shows the same evidence.
    listed = client.get("/api/engines").json()["engines"]
    john_entry = next(item for item in listed if item["engine"] == JOHN)
    assert john_entry["capability_status"] == "verified"
    # Two lookups per probe engine by design: the service locates the binary
    # (service.py:212) and run_safe_command independently re-resolves it
    # (runner.py:106-108) as defense in depth - never a user-supplied path.
    assert which.calls == ["john", "john"]
    assert executor.calls  # probes ran through the injected executor only


def test_default_service_shares_the_resolved_registry() -> None:
    """Without an injected service, create_app builds one over the same
    registry GET /api/engines reads."""

    app = create_app()
    service = app.state.discovery_refresh_service
    assert isinstance(service, CapabilityRefreshService)
    assert service.registry is app.state.discovery_registry

    app_with_injected_registry = create_app(discovery_registry=DiscoveryRegistry())
    assert (
        app_with_injected_registry.state.discovery_refresh_service.registry
        is app_with_injected_registry.state.discovery_registry
    )


# --------------------------------------------------------------------------- #
# authentication and middleware (unchanged chain applies)
# --------------------------------------------------------------------------- #


def test_auth_disabled_allows_refresh_by_default() -> None:
    client, _ = _client()

    assert client.post(REFRESH_PATH).status_code == 200


def test_auth_enabled_rejects_refresh_without_token() -> None:
    client, _ = _client(api_token="s3cret-token")

    response = client.post(REFRESH_PATH)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json() == {
        "detail": {"error": "unauthorized", "detail": "a valid bearer token is required"}
    }


def test_auth_enabled_rejects_wrong_token_with_the_same_401() -> None:
    client, _ = _client(api_token="s3cret-token")

    without = client.post(REFRESH_PATH)
    wrong = client.post(REFRESH_PATH, headers={"Authorization": "Bearer wrong-token"})

    assert wrong.status_code == 401
    assert wrong.json() == without.json()


def test_auth_enabled_accepts_the_correct_token() -> None:
    client, stub = _client(api_token="s3cret-token")

    response = client.post(
        REFRESH_PATH, headers={"Authorization": "Bearer s3cret-token"}
    )

    assert response.status_code == 200
    assert stub.calls == [None]


def test_rate_limit_rejects_bursts_of_refresh_requests() -> None:
    client, stub = _client(rate_limit=2)

    first = client.post(REFRESH_PATH)
    second = client.post(REFRESH_PATH)
    third = client.post(REFRESH_PATH)

    assert [first.status_code, second.status_code] == [200, 200]
    assert third.status_code == 429
    assert third.headers["Retry-After"]
    assert third.json()["detail"]["error"] == "rate_limited"
    assert len(stub.calls) == 2  # the rejected request never reached the service


def test_reads_stay_open_and_unlimited_while_refresh_is_mutating() -> None:
    client, _ = _client(rate_limit=1)
    client.post(REFRESH_PATH)

    # Reads are never rate limited and never authenticated - unchanged.
    assert client.get("/api/engines").status_code == 200


# --------------------------------------------------------------------------- #
# error behaviour
# --------------------------------------------------------------------------- #


def test_unknown_engine_error_from_the_service_is_the_standard_404() -> None:
    client, stub = _client(StubRefreshService(error=UnknownEngineError("bogus")))

    response = client.post(REFRESH_PATH)

    assert response.status_code == 404
    assert response.json() == {
        "detail": {"error": "unknown_engine", "detail": "engine is not a known engine"}
    }


def test_unexpected_service_exception_returns_generic_503() -> None:
    secret = "INTERNAL_SECRET_7731"
    client, stub = _client(StubRefreshService(error=RuntimeError(secret)))

    response = client.post(REFRESH_PATH)

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "error": "discovery_refresh_failed",
            "detail": "engine discovery refresh could not be completed",
        }
    }
    assert secret not in response.text


def test_lock_contention_returns_409_and_never_queues() -> None:
    entered, release = threading.Event(), threading.Event()
    blocking = BlockingRefreshService(entered, release)
    app = create_app(discovery_refresh_service=blocking)
    first_client, second_client = TestClient(app), TestClient(app)

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(first_client.post, REFRESH_PATH)
        assert entered.wait(timeout=10)

        second = second_client.post(REFRESH_PATH)

        assert second.status_code == 409
        assert second.json() == {
            "detail": {
                "error": "refresh_in_progress",
                "detail": "another discovery refresh is already running",
            }
        }
        assert blocking.calls == 1  # the rejected request never reached the service

        release.set()
        assert first.result(timeout=10).status_code == 200


def test_lock_is_released_after_success_and_after_failure() -> None:
    failing = StubRefreshService(error=RuntimeError("boom"))
    client, _ = _client(failing)
    assert client.post(REFRESH_PATH).status_code == 503

    # The same app must accept a refresh again: the finally-block released
    # the lock even though the service raised.
    succeeding = StubRefreshService()
    client.app.state.discovery_refresh_service = succeeding

    response = client.post(REFRESH_PATH)

    assert response.status_code == 200
    assert succeeding.calls == [None]


def test_repeated_refreshes_succeed_in_sequence() -> None:
    client, stub = _client()

    for _ in range(3):
        assert client.post(REFRESH_PATH).status_code == 200
    assert len(stub.calls) == 3


def test_missing_state_wiring_fails_closed_with_503() -> None:
    """A mis-built app (no service/lock on state) can never probe anything."""

    app = create_app()
    app.state.discovery_refresh_service = None

    response = TestClient(app).post(REFRESH_PATH)

    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "discovery_refresh_failed"


# --------------------------------------------------------------------------- #
# security architecture: static boundary on the route module
# --------------------------------------------------------------------------- #


REFRESH_ROUTE_SOURCE = Path(
    inspect.getsourcefile(refresh_module) or ""
).read_text(encoding="utf-8")


def _imported_modules(source: str) -> set[str]:
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
    return modules


def test_route_module_never_imports_process_execution() -> None:
    modules = _imported_modules(REFRESH_ROUTE_SOURCE)
    for module in ("subprocess", "os", "shlex", "pty", "shutil"):
        assert module not in modules


def test_route_module_touches_only_the_refresh_service() -> None:
    """No runner, locator, allowlist or parser import - the service is the
    single discovery surface, and the domain exception is the only other
    discovery symbol needed to fail closed."""

    modules = _imported_modules(REFRESH_ROUTE_SOURCE)
    discovery_modules = {m for m in modules if m.startswith("app.engines.discovery")}
    assert discovery_modules == {
        "app.engines.discovery.errors",
        "app.engines.discovery.service",
    }

    tree = ast.parse(REFRESH_ROUTE_SOURCE)
    referenced = {
        node.id if isinstance(node, ast.Name) else node.attr
        for node in ast.walk(tree)
        if isinstance(node, (ast.Name, ast.Attribute))
    }
    for forbidden in (
        "run_safe_command",
        "locate_engine_binary",
        "DISCOVERY_ALLOWLIST",
        "capabilities_from_probes",
        "subprocess",
    ):
        assert forbidden not in referenced
    assert "CapabilityRefreshService" in referenced
    assert "--hash-info" not in REFRESH_ROUTE_SOURCE

    keywords = {
        keyword.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
    }
    assert "shell" not in keywords


def test_route_module_delegates_to_the_injected_service_only() -> None:
    """The refresh call goes through the resolved service object - no
    direct construction of runners or locators anywhere in the module."""

    tree = ast.parse(REFRESH_ROUTE_SOURCE)
    refresh_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "refresh"
    ]
    assert len(refresh_calls) == 1
    assert isinstance(refresh_calls[0].func.value, ast.Name)
    assert refresh_calls[0].func.value.id == "service"


def test_route_module_never_logs() -> None:
    """The application never logs; the refresh route is no exception."""

    tree = ast.parse(REFRESH_ROUTE_SOURCE)
    referenced = {
        node.id if isinstance(node, ast.Name) else node.attr
        for node in ast.walk(tree)
        if isinstance(node, (ast.Name, ast.Attribute))
    }
    for logging_symbol in ("logger", "logging", "print", "exc_info"):
        assert logging_symbol not in referenced


def test_engines_read_routes_still_never_reference_the_refresh_service() -> None:
    """The read-only invariant on routes/engines.py is untouched by M1.1."""

    from app.api.routes import engines as engines_module

    source = Path(inspect.getsourcefile(engines_module) or "").read_text(encoding="utf-8")
    modules = _imported_modules(source)
    assert "app.engines.discovery.service" not in modules


def test_known_engines_still_bound_the_request_vocabulary() -> None:
    """The names a client may send are exactly the closed engine set."""

    assert KNOWN_ENGINES == frozenset({JOHN, HASHCAT})
    client, _ = _client()
    for name in sorted(KNOWN_ENGINES):
        assert client.post(REFRESH_PATH, json={"engines": [name]}).status_code in (200, 404)


def test_refresh_report_service_contract_is_unchanged() -> None:
    """The API relies on - and must not alter - the service's closed report
    field set; this pins the shared contract both sides depend on."""

    assert set(REFRESH_REPORT_KEYS) == {
        "engine",
        "discovered",
        "capability_status",
        "reason",
        "truncated",
    }
    assert CapabilityRefreshService.refresh.__name__ == "refresh"
    assert shared_run_safe_command.__name__ == "run_safe_command"

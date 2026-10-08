"""Phase 3A - end-to-end tests across refresh, API and the simulation plan.

Flow under test (every dependency injected - never a real process)::

    which/executor doubles
      -> CapabilityRefreshService.refresh()      Phase 3A composition
      -> DiscoveryRegistry (VERIFIED / UNKNOWN / ERROR evidence)
      -> GET /api/engines + POST .../plan        read-only HTTP
      -> CapabilityAwareEngineSelector
      -> ExecutionRequest -> ExecutionPolicy -> ExecutionPlan
      -> MockEngineAdapter.simulate() -> SimulationResult

Failure flows (truncated probe, failed probe, missing binary, foreign
output) must never reach ``authorized_simulation``, and raw probe output
must never appear in any HTTP response.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.core.orchestrator.plan import preview_plan
from app.engines.adapter import (
    FORBIDDEN_REQUEST_FIELDS,
    MOCK_JOHN,
    ExecutionMode,
    ExecutionStatus,
)
from app.engines.discovery import john
from app.engines.discovery.allowlist import DISCOVERY_ALLOWLIST
from app.engines.discovery.models import (
    HASHCAT,
    JOHN,
    CapabilityStatus,
)
from app.engines.discovery.registry import (
    FORBIDDEN_PAYLOAD_KEYS,
    DiscoveryRegistry,
)
from app.engines.discovery.service import CapabilityRefreshService
from app.engines.selection import SelectionStatus

PHASE3A_MODULES = (
    Path("app/engines/discovery/service.py"),
    Path("app/api/routes/engines.py"),
)

JOHN_OPERATION = john.JOHN_FORMAT_OPERATION

JOHN_GOOD_OUTPUT = (
    "Supported hash algorithms:\n"
    "  md5crypt, sha256crypt, sha512crypt, bcrypt,\n"
    "  ntlm, raw-md5, raw-sha1, raw-sha256,\n"
    "  zip, pkzip, pdf, office2007, office2010, office2013\n"
)

RAW_MARKER = "RAW_OUTPUT_MARKER_9271"

FAKE_BINARIES = {JOHN: "C:/fake-engines/john", HASHCAT: "C:/fake-engines/hashcat"}

FORBIDDEN_HTTP_KEYS = FORBIDDEN_REQUEST_FIELDS | FORBIDDEN_PAYLOAD_KEYS

UNKNOWN = CapabilityStatus.UNKNOWN
ERROR = CapabilityStatus.ERROR


class RecordingWhich:
    def __init__(self, mapping: dict[str, str | None] | None = None) -> None:
        self.calls: list[str] = []
        self._mapping = dict(mapping if mapping is not None else FAKE_BINARIES)

    def __call__(self, name: str) -> str | None:
        self.calls.append(name)
        return self._mapping.get(name)


class RecordingExecutor:
    def __init__(self, responses: dict[str, tuple[int, str, str, bool]]) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._responses = dict(responses)

    def __call__(
        self, argv: tuple[str, ...], *, timeout: float, max_output_bytes: int
    ) -> tuple[int, str, str, bool]:
        self.calls.append(tuple(argv))
        return self._responses[argv[1]]


def _service(
    outcome: tuple[int, str, str, bool] = (0, JOHN_GOOD_OUTPUT, "", False),
    *,
    binaries: dict[str, str | None] | None = None,
) -> tuple[CapabilityRefreshService, RecordingWhich, RecordingExecutor]:
    registry = DiscoveryRegistry()
    which = RecordingWhich(binaries)
    executor = RecordingExecutor({JOHN_OPERATION: outcome})
    service = CapabilityRefreshService(registry, which=which, executor=executor)
    return service, which, executor


def _forbidden_keys(payload: object) -> set[str]:
    found: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in FORBIDDEN_HTTP_KEYS:
                found.add(key)
            found |= _forbidden_keys(value)
    elif isinstance(payload, list):
        for item in payload:
            found |= _forbidden_keys(item)
    return found


def _client(service: CapabilityRefreshService) -> TestClient:
    return TestClient(create_app(discovery_registry=service.registry))


# --------------------------------------------------------------------------- #
# happy path: probe -> VERIFIED -> authorised simulation
# --------------------------------------------------------------------------- #


def test_probe_to_authorised_simulation_full_flow() -> None:
    service, which, executor = _service()
    (report,) = service.refresh(JOHN)

    assert report.is_verified is True
    assert report.capability_status is CapabilityStatus.VERIFIED

    preview = preview_plan("pdf-document", discovery_registry=service.registry)

    assert preview.selection.status is SelectionStatus.SELECTED
    assert preview.request.engine == MOCK_JOHN
    assert preview.request.capability_status == "verified"
    assert preview.authorization.authorized is True
    assert preview.authorization.reason == "authorized_simulation"
    assert preview.plan is not None
    assert preview.plan.mode is ExecutionMode.SIMULATED
    assert preview.plan.adapter == MOCK_JOHN
    assert preview.simulation is not None
    assert preview.simulation.status is ExecutionStatus.SIMULATED
    assert preview.simulated is True
    assert preview.simulated_only is True

    # the only executor call was the single allowlisted capability operation
    assert len(executor.calls) == 1
    argv = executor.calls[0]
    assert len(argv) == 2
    assert DISCOVERY_ALLOWLIST.allows(JOHN, argv[1])
    assert set(which.calls) == {"john"}


def test_http_engines_list_and_plan_share_service_produced_evidence() -> None:
    service, _, _ = _service()
    service.refresh(JOHN)
    client = _client(service)

    engines = client.get("/api/engines")
    assert engines.status_code == 200
    john_entry = next(
        e for e in engines.json()["engines"] if e["engine"] == JOHN
    )
    assert john_entry["capability_status"] == "verified"
    assert john_entry["real_engine_compatible"] is True
    assert "pdf-document" in john_entry["targets"]

    plan = client.post(
        "/api/targets/pdf-document/compatibility/plan", json={"mode": "simulated"}
    )
    assert plan.status_code == 200
    body = plan.json()
    assert body["authorization"]["authorized"] is True
    assert body["authorization"]["reason"] == "authorized_simulation"
    assert body["plan"]["adapter"] == MOCK_JOHN
    assert body["simulation"]["status"] == "simulated"
    assert body["simulation"]["simulation_id"]

    detail = client.get(f"/api/engines/{JOHN}")
    assert detail.status_code == 200
    assert detail.json()["capability_status"] == "verified"

    for payload in (engines.json(), body, detail.json()):
        assert _forbidden_keys(payload) == set()


def test_full_http_flow_never_exposes_raw_probe_output() -> None:
    service, _, _ = _service((0, JOHN_GOOD_OUTPUT, f"noise {RAW_MARKER}", False))
    service.refresh(JOHN)
    client = _client(service)

    responses = [
        client.get("/api/engines"),
        client.get(f"/api/engines/{JOHN}"),
        client.post("/api/targets/pdf-document/compatibility/plan", json={}),
    ]
    for response in responses:
        assert response.status_code == 200
        assert RAW_MARKER not in response.text
        assert _forbidden_keys(response.json()) == set()


# --------------------------------------------------------------------------- #
# non-VERIFIED evidence can never authorise anything
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("outcome", "binaries", "expected_status"),
    [
        pytest.param(
            (0, JOHN_GOOD_OUTPUT, "", True),
            None,
            UNKNOWN,
            id="truncated_probe",
        ),
        pytest.param((1, "", "boom", False), None, ERROR, id="failed_probe"),
        pytest.param(
            (0, "text without any header\n", "", False),
            None,
            UNKNOWN,
            id="missing_header",
        ),
        pytest.param(
            (0, JOHN_GOOD_OUTPUT, "", False),
            {JOHN: None},
            UNKNOWN,
            id="binary_missing",
        ),
    ],
)
def test_non_verified_evidence_never_reaches_authorized_simulation(
    outcome: tuple[int, str, str, bool],
    binaries: dict[str, str | None] | None,
    expected_status: CapabilityStatus,
) -> None:
    service, _, _ = _service(outcome, binaries=binaries)
    (report,) = service.refresh(JOHN)

    assert report.capability_status is expected_status
    assert report.is_verified is False

    preview = preview_plan("pdf-document", discovery_registry=service.registry)
    assert preview.selection.status is not SelectionStatus.SELECTED
    assert preview.authorization.authorized is False
    assert preview.authorization.reason == "capability_not_verified"
    assert preview.plan is None
    assert preview.simulation is None
    assert preview.simulated is False

    client = _client(service)
    body = client.post(
        "/api/targets/pdf-document/compatibility/plan", json={}
    ).json()
    assert body["authorization"]["authorized"] is False
    assert body["authorization"]["reason"] == "capability_not_verified"
    assert body["plan"] is None
    assert body["simulation"] is None

    engines = client.get("/api/engines").json()
    entry = next(e for e in engines["engines"] if e["engine"] == JOHN)
    assert entry["capability_status"] == expected_status.value
    assert entry["real_engine_compatible"] is False
    assert entry["formats"] == []
    assert entry["targets"] == []


def test_hashcat_refresh_cannot_authorize_any_plan() -> None:
    service, _, executor = _service()
    (report,) = service.refresh(HASHCAT)

    assert report.capability_status is UNKNOWN
    assert report.reason == "formats_not_listed"
    assert executor.calls == []

    preview = preview_plan("pdf-document", discovery_registry=service.registry)
    assert preview.authorization.authorized is False
    assert preview.authorization.reason == "capability_not_verified"
    assert preview.simulation is None

    client = _client(service)
    body = client.post(
        "/api/targets/pdf-document/compatibility/plan", json={}
    ).json()
    assert body["authorization"]["authorized"] is False
    entry = next(
        e for e in client.get("/api/engines").json()["engines"] if e["engine"] == HASHCAT
    )
    assert entry["capability_status"] == "unknown"
    assert entry["real_engine_compatible"] is False


# --------------------------------------------------------------------------- #
# both HTTP surfaces reflect the same registry over time
# --------------------------------------------------------------------------- #


def test_engines_list_and_plan_reflect_a_later_failed_refresh() -> None:
    service, _, _ = _service()
    service.refresh(JOHN)

    client = _client(service)
    first_plan = client.post(
        "/api/targets/pdf-document/compatibility/plan", json={}
    ).json()
    assert first_plan["authorization"]["authorized"] is True

    # a second refresh on the SAME registry fails -> evidence is revoked
    failing = RecordingExecutor({JOHN_OPERATION: (1, "", "boom", False)})
    second = CapabilityRefreshService(
        service.registry, which=RecordingWhich(), executor=failing
    )
    (report,) = second.refresh(JOHN)
    assert report.capability_status is ERROR

    entry = next(
        e for e in client.get("/api/engines").json()["engines"] if e["engine"] == JOHN
    )
    assert entry["capability_status"] == "error"
    assert entry["real_engine_compatible"] is False

    second_plan = client.post(
        "/api/targets/pdf-document/compatibility/plan", json={}
    ).json()
    assert second_plan["authorization"]["authorized"] is False
    assert second_plan["authorization"]["reason"] == "capability_not_verified"
    assert second_plan["simulation"] is None


# --------------------------------------------------------------------------- #
# static safety boundary (Phase 3A modules)
# --------------------------------------------------------------------------- #


def _imported_modules(source: str) -> set[str]:
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
    return modules


@pytest.mark.parametrize("module", PHASE3A_MODULES)
def test_phase3a_module_never_imports_process_execution(module: Path) -> None:
    modules = _imported_modules(module.read_text(encoding="utf-8"))
    for forbidden in ("subprocess", "os", "shlex", "pty", "shutil"):
        assert forbidden not in modules


@pytest.mark.parametrize("module", PHASE3A_MODULES)
def test_phase3a_module_has_no_process_shell_or_eval_calls(module: Path) -> None:
    tree = ast.parse(module.read_text(encoding="utf-8"))
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Attribute, ast.Name))
    }
    for forbidden in ("system", "popen", "Popen", "exec", "eval", "spawnv", "check_output"):
        assert forbidden not in called


@pytest.mark.parametrize("module", PHASE3A_MODULES)
def test_phase3a_module_never_sets_a_shell_keyword(module: Path) -> None:
    tree = ast.parse(module.read_text(encoding="utf-8"))
    keywords = {
        keyword.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
    }
    assert "shell" not in keywords


@pytest.mark.parametrize("module", PHASE3A_MODULES)
def test_phase3a_module_never_references_the_hashcat_mode_list_operation(
    module: Path,
) -> None:
    assert "--hash-info" not in module.read_text(encoding="utf-8")


def test_refresh_service_has_no_http_dependency() -> None:
    source = Path("app/engines/discovery/service.py").read_text(encoding="utf-8")
    assert "fastapi" not in _imported_modules(source)
    assert "create_app" not in source


def test_engines_route_never_touches_the_refresh_service() -> None:
    source = Path("app/api/routes/engines.py").read_text(encoding="utf-8")
    modules = _imported_modules(source)
    assert "app.engines.discovery.service" not in modules
    assert "app.engines.discovery.runner" not in modules
    tree = ast.parse(source)
    referenced = {
        node.id if isinstance(node, ast.Name) else node.attr
        for node in ast.walk(tree)
        if isinstance(node, (ast.Name, ast.Attribute))
    }
    assert "refresh" not in referenced
    assert "run_safe_command" not in referenced

"""Phase 2D - integration tests: pipeline, API plan preview, static boundary.

Flow under test::

    Target -> verified capability -> selection -> ExecutionRequest
           -> ExecutionPolicy -> ExecutionPlan -> MockEngineAdapter.simulate()
           -> SimulationResult

Plus the read-only API surface (`GET /api/targets`,
`POST /api/targets/{id}/compatibility/plan`).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.core.orchestrator.plan import (
    ADAPTER_FOR_ENGINE,
    PlanPreview,
    adapter_for_engine,
    build_request,
    preview_plan,
)
from app.engines.adapter import (
    FORBIDDEN_REQUEST_FIELDS,
    MOCK_HASHCAT,
    MOCK_JOHN,
    ExecutionMode,
    ExecutionStatus,
)
from app.engines.adapter_registry import EngineAdapterRegistry
from app.engines.discovery.capability_map import mapped_targets
from app.engines.discovery.models import (
    HASHCAT,
    JOHN,
    CapabilityStatus,
    EngineCapabilities,
    EngineSnapshot,
)
from app.engines.discovery.registry import DiscoveryRegistry
from app.engines.execution_policy import ExecutionPolicy
from app.engines.selection import SelectionStatus

VERIFIED = CapabilityStatus.VERIFIED
UNKNOWN = CapabilityStatus.UNKNOWN

FULL_FORMATS: tuple[str, ...] = (
    "bcrypt",
    "md5crypt",
    "ntlm",
    "office2007",
    "office2010",
    "office2013",
    "pdf",
    "pkzip",
    "raw-md5",
    "raw-sha1",
    "raw-sha256",
    "sha256crypt",
    "sha512crypt",
    "zip",
)

TWO_D_MODULES = (
    Path("app/engines/adapter.py"),
    Path("app/engines/adapter_registry.py"),
    Path("app/engines/execution_policy.py"),
    Path("app/core/orchestrator/plan.py"),
    Path("app/api/schemas.py"),
    Path("app/api/app.py"),
    Path("app/api/routes/targets.py"),
)


def _registry(
    entries: dict[str, CapabilityStatus],
    *,
    discovered: dict[str, bool] | None = None,
    formats: tuple[str, ...] = FULL_FORMATS,
) -> DiscoveryRegistry:
    registry = DiscoveryRegistry()
    for engine, status in entries.items():
        is_discovered = (discovered or {}).get(engine, True)
        registry.register(
            EngineSnapshot(engine=engine, discovered=is_discovered, binary_found=is_discovered)
        )
        registry.set_capabilities(
            engine,
            EngineCapabilities(engine=engine, capability_status=status, formats=formats),
        )
    return registry


# --------------------------------------------------------------------------- #
# pipeline: authorised simulation
# --------------------------------------------------------------------------- #


def test_verified_selection_produces_authorised_simulation() -> None:
    preview = preview_plan("pdf-document", discovery_registry=_registry({JOHN: VERIFIED}))

    assert preview.selection.status is SelectionStatus.SELECTED
    assert preview.request.engine == MOCK_JOHN
    assert preview.request.capability_status == "verified"
    assert preview.request.selection_status == "selected"
    assert "pdf" in preview.request.matched_formats
    assert preview.authorization.authorized is True
    assert preview.authorization.adapter == MOCK_JOHN
    assert preview.plan is not None
    assert preview.plan.mode is ExecutionMode.SIMULATED
    assert preview.plan.adapter == MOCK_JOHN
    assert preview.simulation is not None
    assert preview.simulation.status is ExecutionStatus.SIMULATED
    assert preview.simulated is True
    assert preview.simulated_only is True


def test_preview_as_dict_is_structured_and_complete() -> None:
    preview = preview_plan("zip-archive", discovery_registry=_registry({JOHN: VERIFIED}))
    payload = preview.as_dict()

    assert set(payload) == {
        "target",
        "selection",
        "request",
        "authorization",
        "plan",
        "simulation",
    }
    assert payload["plan"] is not None
    assert payload["simulation"] is not None
    assert payload["authorization"]["authorized"] is True
    assert not _forbidden_keys(payload)


def test_hashcat_only_registry_maps_to_hashcat_adapter() -> None:
    preview = preview_plan("pdf-document", discovery_registry=_registry({HASHCAT: VERIFIED}))
    assert preview.request.selected_engine == HASHCAT
    assert preview.request.engine == MOCK_HASHCAT
    assert preview.authorization.adapter == MOCK_HASHCAT


def test_verified_john_wins_over_verified_hashcat() -> None:
    preview = preview_plan(
        "pdf-document", discovery_registry=_registry({JOHN: VERIFIED, HASHCAT: VERIFIED})
    )
    assert preview.request.selected_engine == JOHN
    assert preview.authorization.adapter == MOCK_JOHN
    assert [candidate.engine for candidate in preview.selection.candidates] == [
        JOHN,
        HASHCAT,
    ]


def test_simulation_is_deterministic_across_calls() -> None:
    registry = _registry({JOHN: VERIFIED})
    first = preview_plan("pdf-document", discovery_registry=registry)
    second = preview_plan("pdf-document", discovery_registry=registry)
    assert first.simulation is not None and second.simulation is not None
    assert first.simulation.simulation_id == second.simulation.simulation_id


def test_unsupported_target_is_denied_without_plan_or_simulation() -> None:
    preview = preview_plan("unknown-target", discovery_registry=_registry({JOHN: VERIFIED}))
    assert preview.authorization.reason == "invalid_request"
    assert preview.plan is None
    assert preview.simulation is None
    assert preview.simulated_only is True


def test_real_mode_is_denied_and_never_produced_a_plan() -> None:
    preview = preview_plan(
        "pdf-document", discovery_registry=_registry({JOHN: VERIFIED}), mode="real"
    )
    assert preview.authorization.reason == "non_simulation_request"
    assert preview.plan is None
    assert preview.simulation is None
    assert preview.as_dict()["plan"] is None
    assert preview.as_dict()["simulation"] is None


def test_unsupported_mode_value_is_denied() -> None:
    preview = preview_plan(
        "pdf-document", discovery_registry=_registry({JOHN: VERIFIED}), mode="banana"
    )
    assert preview.authorization.reason == "unsupported_execution_mode"
    assert preview.plan is None


def test_unknown_capability_is_denied_capability_not_verified() -> None:
    preview = preview_plan("pdf-document", discovery_registry=_registry({JOHN: UNKNOWN}))
    assert preview.authorization.reason == "capability_not_verified"
    assert preview.selection.status is SelectionStatus.NO_VERIFIED_CAPABILITY
    assert preview.plan is None
    assert preview.simulation is None


def test_empty_discovery_registry_denies_without_a_plan() -> None:
    preview = preview_plan("pdf-document", discovery_registry=DiscoveryRegistry())
    assert preview.authorization.authorized is False
    assert preview.plan is None
    assert preview.simulation is None


def test_undiscovered_engine_is_not_a_candidate() -> None:
    preview = preview_plan(
        "pdf-document",
        discovery_registry=_registry({JOHN: VERIFIED}, discovered={JOHN: False}),
    )
    assert preview.authorization.authorized is False
    assert preview.plan is None


def test_unknown_engine_identity_is_denied_by_the_adapter_registry() -> None:
    preview = preview_plan(
        "pdf-document",
        discovery_registry=_registry({JOHN: VERIFIED}),
        policy=ExecutionPolicy(EngineAdapterRegistry()),
    )
    assert preview.authorization.reason == "unknown_engine"
    assert preview.plan is None


def test_adapter_identity_mapping_is_closed() -> None:
    assert adapter_for_engine(JOHN) == MOCK_JOHN
    assert adapter_for_engine(HASHCAT) == MOCK_HASHCAT
    assert adapter_for_engine("zip") == ""
    assert adapter_for_engine(7) == ""  # type: ignore[arg-type]
    assert dict(ADAPTER_FOR_ENGINE) == {JOHN: MOCK_JOHN, HASHCAT: MOCK_HASHCAT}


def test_build_request_never_carries_command_fields() -> None:
    preview = preview_plan("pdf-document", discovery_registry=_registry({JOHN: VERIFIED}))
    assert preview.request.forbidden_fields == frozenset()
    assert not (set(preview.request.__dataclass_fields__) & FORBIDDEN_REQUEST_FIELDS)


def test_non_string_target_is_denied() -> None:
    preview = preview_plan(42)  # type: ignore[arg-type]
    assert preview.target == ""
    assert preview.authorization.reason == "invalid_request"


def test_preview_plan_type_is_structured() -> None:
    preview = preview_plan("pdf-document", discovery_registry=_registry({JOHN: VERIFIED}))
    assert isinstance(preview, PlanPreview)
    assert build_request.__module__ == "app.core.orchestrator.plan"


# --------------------------------------------------------------------------- #
# API: read-only target list
# --------------------------------------------------------------------------- #


def test_get_targets_lists_the_mapped_policy() -> None:
    client = TestClient(create_app())
    response = client.get("/api/targets")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == len(mapped_targets())
    assert {entry["target"] for entry in payload["targets"]} == set(mapped_targets())
    pdf = next(e for e in payload["targets"] if e["target"] == "pdf-document")
    assert pdf["formats"] == ["pdf"]
    assert pdf["usable"] is True
    assert not _forbidden_keys(payload)


def test_health_endpoint_reports_simulated_mode() -> None:
    response = TestClient(create_app()).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mode": "simulated"}


# --------------------------------------------------------------------------- #
# API: plan preview
# --------------------------------------------------------------------------- #


def test_plan_endpoint_returns_authorised_simulation_for_verified_registry() -> None:
    app = create_app(discovery_registry=_registry({JOHN: VERIFIED}))
    response = TestClient(app).post(
        "/api/targets/pdf-document/compatibility/plan", json={"mode": "simulated"}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["authorization"]["authorized"] is True
    assert payload["authorization"]["reason"] == "authorized_simulation"
    assert payload["plan"]["adapter"] == MOCK_JOHN
    assert payload["plan"]["simulated"] is True
    assert payload["plan"]["steps"]
    assert payload["simulation"]["status"] == "simulated"
    assert payload["simulation"]["simulated"] is True
    assert payload["simulation"]["simulation_id"]
    assert not _forbidden_keys(payload)


def test_plan_endpoint_denies_when_no_capability_is_verified() -> None:
    response = TestClient(create_app()).post(
        "/api/targets/pdf-document/compatibility/plan", json={}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["authorization"]["authorized"] is False
    assert payload["authorization"]["reason"] == "capability_not_verified"
    assert payload["plan"] is None
    assert payload["simulation"] is None


def test_plan_endpoint_reports_real_mode_as_structured_denial() -> None:
    app = create_app(discovery_registry=_registry({JOHN: VERIFIED}))
    response = TestClient(app).post(
        "/api/targets/pdf-document/compatibility/plan", json={"mode": "real"}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["authorization"]["reason"] == "non_simulation_request"
    assert payload["plan"] is None
    assert payload["simulation"] is None


def test_plan_endpoint_reports_unknown_mode_as_structured_denial() -> None:
    response = TestClient(create_app()).post(
        "/api/targets/pdf-document/compatibility/plan", json={"mode": "banana"}
    )
    assert response.status_code == 200
    assert response.json()["authorization"]["reason"] == "unsupported_execution_mode"


def test_plan_endpoint_returns_404_for_unmapped_target() -> None:
    response = TestClient(create_app()).post(
        "/api/targets/not-a-target/compatibility/plan", json={}
    )
    assert response.status_code == 404
    assert response.json() == {
        "detail": {
            "error": "target_not_mapped",
            "detail": "target is not mapped to an enabled format policy",
        }
    }


def test_plan_endpoint_rejects_unknown_body_fields() -> None:
    app = create_app(discovery_registry=_registry({JOHN: VERIFIED}))
    response = TestClient(app).post(
        "/api/targets/pdf-document/compatibility/plan",
        json={"mode": "simulated", "command": "john --wordlist=x"},
    )
    assert response.status_code == 422


def test_plan_endpoint_bounds_reference_length() -> None:
    app = create_app(discovery_registry=_registry({JOHN: VERIFIED}))
    client = TestClient(app)
    base = "/api/targets/pdf-document/compatibility/plan"

    ok = client.post(base, json={"mode": "simulated", "reference": "r" * 256})
    assert ok.status_code == 200

    too_long = client.post(base, json={"mode": "simulated", "reference": "r" * 257})
    assert too_long.status_code == 422
    assert too_long.json()["detail"][0]["loc"] == ["body", "reference"]


def test_plan_endpoint_never_returns_command_shaped_fields() -> None:
    app = create_app(discovery_registry=_registry({JOHN: VERIFIED}))
    client = TestClient(app)
    for body in ({}, {"mode": "simulated"}, {"mode": "real"}):
        payload = client.post(
            "/api/targets/unix-password-hash/compatibility/plan", json=body
        ).json()
        assert not _forbidden_keys(payload)


def test_plan_endpoint_with_hashcat_registry_uses_hashcat_adapter() -> None:
    app = create_app(discovery_registry=_registry({HASHCAT: VERIFIED}))
    payload = TestClient(app).post(
        "/api/targets/pdf-document/compatibility/plan", json={}
    ).json()
    assert payload["plan"]["adapter"] == MOCK_HASHCAT


def test_injected_adapter_registry_is_honoured_by_the_policy() -> None:
    app = create_app(
        discovery_registry=_registry({JOHN: VERIFIED}),
        adapter_registry=EngineAdapterRegistry(),
    )
    payload = TestClient(app).post(
        "/api/targets/pdf-document/compatibility/plan", json={}
    ).json()
    assert payload["authorization"]["reason"] == "unknown_engine"
    assert payload["plan"] is None


# --------------------------------------------------------------------------- #
# static safety boundary (Phase 2D modules)
# --------------------------------------------------------------------------- #


def _forbidden_keys(payload: object) -> set[str]:
    found: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in FORBIDDEN_REQUEST_FIELDS:
                found.add(key)
            found |= _forbidden_keys(value)
    elif isinstance(payload, list):
        for item in payload:
            found |= _forbidden_keys(item)
    return found


@pytest.mark.parametrize("module", TWO_D_MODULES)
def test_phase2d_module_never_imports_process_execution(module: Path) -> None:
    tree = ast.parse(module.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "subprocess" not in imported
    assert "os" not in imported
    assert "shlex" not in imported
    assert "pty" not in imported


@pytest.mark.parametrize("module", TWO_D_MODULES)
def test_phase2d_module_has_no_process_shell_or_eval_calls(module: Path) -> None:
    tree = ast.parse(module.read_text(encoding="utf-8"))
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Attribute, ast.Name))
    }
    for forbidden in ("system", "popen", "Popen", "exec", "eval", "spawnv", "check_output"):
        assert forbidden not in called


def test_phase2d_request_schemas_declare_no_command_fields() -> None:
    from app.api import schemas

    request_model = schemas.PlanPreviewRequest
    assert set(request_model.model_fields) == {"mode", "reference"}
    for model in (
        schemas.SelectionInfo,
        schemas.RequestInfo,
        schemas.AuthorizationInfo,
        schemas.PlanInfo,
        schemas.SimulationInfo,
    ):
        assert not (set(model.model_fields) & FORBIDDEN_REQUEST_FIELDS)

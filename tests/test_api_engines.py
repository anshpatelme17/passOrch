"""Phase 3A - read-only engine capability API tests.

Proves:

* ``GET /api/engines`` and ``GET /api/engines/{engine}`` are read-only:
  no registry mutation, no refresh, no probing, no process execution;
* payloads are exactly the validated registry payload - raw probe output and
  command-surface keys can never appear;
* unknown vs unregistered engines are distinguished with stable 404 details;
* the Phase 2D endpoints keep working unchanged.

Synthetic capability evidence is used only to exercise the API surface in
isolation; ``tests/test_phase3_e2e.py`` drives the real refresh service
end to end.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.routes import engines as engines_module
from app.api.schemas import EngineInfo, EnginesResponse
from app.engines.discovery import service as service_module
from app.engines.discovery.models import (
    HASHCAT,
    JOHN,
    CapabilityStatus,
    EngineCapabilities,
    EngineSnapshot,
    KNOWN_ENGINES,
)
from app.engines.discovery.registry import (
    FORBIDDEN_PAYLOAD_KEYS,
    DiscoveryRegistry,
    default_discovery_registry,
)
from app.engines.discovery.runner import run_safe_command as shared_run_safe_command
from app.engines.discovery.service import (
    FORBIDDEN_REFRESH_FIELDS,
    CapabilityRefreshService,
)

ROUTES_SOURCE = Path(inspect.getsourcefile(engines_module) or "").read_text(
    encoding="utf-8"
)

FORBIDDEN_RESPONSE_KEYS = FORBIDDEN_PAYLOAD_KEYS | FORBIDDEN_REFRESH_FIELDS

RAW_MARKER = "RAW_OUTPUT_MARKER_9271"

JOHN_GOOD_OUTPUT = (
    "Supported hash algorithms:\n"
    "  md5crypt, sha256crypt, sha512crypt, bcrypt,\n"
    "  ntlm, raw-md5, raw-sha1, raw-sha256,\n"
    "  zip, pkzip, pdf, office2007, office2010, office2013\n"
)

ENGINE_INFO_KEYS = {
    "engine",
    "discovered",
    "binary_found",
    "version",
    "operations",
    "snapshot_error",
    "capability_status",
    "formats",
    "targets",
    "source_operation",
    "reason",
    "parser",
    "real_engine_compatible",
}


def _verified_registry() -> DiscoveryRegistry:
    """Registry holding synthetic VERIFIED John + UNKNOWN Hashcat evidence."""

    registry = DiscoveryRegistry()
    registry.register(
        EngineSnapshot(
            engine=JOHN,
            discovered=True,
            binary_found=True,
            operations=("--list=formats",),
        )
    )
    registry.set_capabilities(
        JOHN,
        EngineCapabilities(
            engine=JOHN,
            capability_status=CapabilityStatus.VERIFIED,
            formats=("md5crypt", "pdf", "ntlm"),
            targets=("pdf-document", "unix-password-hash", "windows-password-hash"),
            source_operation="--list=formats",
            parser="john-format-parser",
        ),
    )
    registry.register(
        EngineSnapshot(engine=HASHCAT, discovered=True, binary_found=True)
    )
    registry.set_capabilities(
        HASHCAT,
        EngineCapabilities(
            engine=HASHCAT,
            capability_status=CapabilityStatus.UNKNOWN,
            reason="formats_not_listed",
        ),
    )
    return registry


def _client(registry: DiscoveryRegistry | None = None) -> tuple[TestClient, DiscoveryRegistry]:
    effective = registry if registry is not None else DiscoveryRegistry()
    return TestClient(create_app(discovery_registry=effective)), effective


def _forbidden_key_walk(value: object) -> list[str]:
    """Keys anywhere in a decoded JSON document that must never appear."""

    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in FORBIDDEN_RESPONSE_KEYS:
                found.append(key)
            found.extend(_forbidden_key_walk(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_forbidden_key_walk(item))
    return found


# --------------------------------------------------------------------------- #
# wiring
# --------------------------------------------------------------------------- #


def test_engines_router_is_wired() -> None:
    application = create_app(discovery_registry=DiscoveryRegistry())
    paths = {route.path for route in application.routes}
    assert "/api/engines" in paths
    assert "/api/engines/{engine}" in paths


def test_create_app_wires_the_shared_discovery_registry() -> None:
    application = create_app()
    assert application.state.discovery_registry is default_discovery_registry()


# --------------------------------------------------------------------------- #
# GET /api/engines
# --------------------------------------------------------------------------- #


def test_list_engines_is_empty_for_a_fresh_registry() -> None:
    client, _ = _client()
    response = client.get("/api/engines")

    assert response.status_code == 200
    assert response.json() == {"engines": []}


def test_list_engines_returns_exact_verified_payload_shape() -> None:
    client, _ = _client(_verified_registry())
    payload = client.get("/api/engines").json()

    assert [engine["engine"] for engine in payload["engines"]] == [HASHCAT, JOHN]
    for engine in payload["engines"]:
        assert set(engine) == ENGINE_INFO_KEYS

    john = next(e for e in payload["engines"] if e["engine"] == JOHN)
    assert john["capability_status"] == "verified"
    assert john["real_engine_compatible"] is True
    assert john["formats"] == ["md5crypt", "pdf", "ntlm"]
    assert john["targets"] == [
        "pdf-document",
        "unix-password-hash",
        "windows-password-hash",
    ]
    assert john["reason"] is None
    assert john["snapshot_error"] is None

    hashcat = next(e for e in payload["engines"] if e["engine"] == HASHCAT)
    assert hashcat["capability_status"] == "unknown"
    assert hashcat["real_engine_compatible"] is False
    assert hashcat["formats"] == []
    assert hashcat["targets"] == []
    assert hashcat["reason"] == "formats_not_listed"


def test_list_engines_json_contains_no_forbidden_keys() -> None:
    client, _ = _client(_verified_registry())
    body = client.get("/api/engines").json()

    assert _forbidden_key_walk(body) == []


def test_engines_endpoints_reject_non_get_methods() -> None:
    client, _ = _client(_verified_registry())
    for method in (client.post, client.put, client.delete, client.patch):
        assert method("/api/engines").status_code == 405
        assert method("/api/engines/john").status_code == 405


# --------------------------------------------------------------------------- #
# GET /api/engines/{engine}
# --------------------------------------------------------------------------- #


def test_engine_detail_returns_verified_john() -> None:
    client, _ = _client(_verified_registry())
    response = client.get(f"/api/engines/{JOHN}")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == ENGINE_INFO_KEYS
    assert body["capability_status"] == "verified"
    assert body["real_engine_compatible"] is True
    assert body["operations"] == ["--list=formats"]
    assert body["source_operation"] == "--list=formats"
    assert body["parser"] == "john-format-parser"


def test_engine_detail_returns_unknown_hashcat() -> None:
    client, _ = _client(_verified_registry())
    response = client.get(f"/api/engines/{HASHCAT}")

    assert response.status_code == 200
    body = response.json()
    assert body["capability_status"] == "unknown"
    assert body["reason"] == "formats_not_listed"
    assert body["real_engine_compatible"] is False


@pytest.mark.parametrize("engine", ["zap", "john-the-ripper", "JOHN", "John"])
def test_engine_detail_404_for_unknown_engine(engine: str) -> None:
    client, _ = _client(_verified_registry())
    response = client.get(f"/api/engines/{engine}")

    assert response.status_code == 404
    assert response.json()["detail"] == "unknown_engine"


@pytest.mark.parametrize("engine", [JOHN, HASHCAT])
def test_engine_detail_404_for_known_but_unregistered_engine(engine: str) -> None:
    client, _ = _client(DiscoveryRegistry())
    response = client.get(f"/api/engines/{engine}")

    assert response.status_code == 404
    assert response.json()["detail"] == "engine_not_registered"


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (CapabilityStatus.UNKNOWN, "binary_not_found"),
        (CapabilityStatus.ERROR, "foreign_output"),
    ],
)
def test_engine_detail_exposes_non_verified_evidence_verbatim(
    status: CapabilityStatus, reason: str
) -> None:
    registry = DiscoveryRegistry()
    registry.register(EngineSnapshot(engine=JOHN, discovered=True, binary_found=False))
    registry.set_capabilities(
        JOHN,
        EngineCapabilities(engine=JOHN, capability_status=status, reason=reason),
    )
    client, _ = _client(registry)
    body = client.get(f"/api/engines/{JOHN}").json()

    assert body["capability_status"] == status.value
    assert body["reason"] == reason
    assert body["real_engine_compatible"] is False
    assert body["formats"] == []
    assert body["targets"] == []


# --------------------------------------------------------------------------- #
# no raw probe output can ever surface
# --------------------------------------------------------------------------- #


def test_engines_api_never_exposes_raw_probe_output() -> None:
    class Which:
        def __call__(self, name: str) -> str:
            return f"C:/fake/{name}"

    class Executor:
        def __call__(
            self, argv: tuple[str, ...], *, timeout: float, max_output_bytes: int
        ) -> tuple[int, str, str, bool]:
            return (0, JOHN_GOOD_OUTPUT, f"noise {RAW_MARKER}", False)

    registry = DiscoveryRegistry()
    CapabilityRefreshService(registry, which=Which(), executor=Executor()).refresh(JOHN)
    assert registry.require(JOHN).is_verified is True

    client, _ = _client(registry)
    for path in ("/api/engines", f"/api/engines/{JOHN}"):
        response = client.get(path)
        assert response.status_code == 200
        assert RAW_MARKER not in response.text
        assert _forbidden_key_walk(response.json()) == []


# --------------------------------------------------------------------------- #
# GET is strictly read-only
# --------------------------------------------------------------------------- #


def test_get_engines_never_mutates_the_registry() -> None:
    client, registry = _client(_verified_registry())
    payload_before = json.dumps(registry.payload(), sort_keys=True)
    entries_before = registry.entries()

    assert client.get("/api/engines").status_code == 200
    assert client.get(f"/api/engines/{JOHN}").status_code == 200

    assert json.dumps(registry.payload(), sort_keys=True) == payload_before
    assert registry.entries() == entries_before
    assert all(
        before is after
        for before, after in zip(entries_before, registry.entries(), strict=True)
    )


def test_get_engines_never_triggers_refresh_or_probing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("a GET must never reach engine execution machinery")

    import app.engines.discovery.runner as runner_module

    client, _ = _client(_verified_registry())

    monkeypatch.setattr(service_module, "run_safe_command", boom)
    monkeypatch.setattr(service_module, "locate_engine_binary", boom)
    monkeypatch.setattr(runner_module, "run_safe_command", boom)
    monkeypatch.setattr(CapabilityRefreshService, "refresh", boom)

    assert client.get("/api/engines").status_code == 200
    assert client.get(f"/api/engines/{JOHN}").status_code == 200
    assert client.get(f"/api/engines/{HASHCAT}").status_code == 200
    assert client.get("/api/engines/zap").status_code == 404


def test_runner_is_still_the_single_shared_safe_runner() -> None:
    assert service_module.run_safe_command is shared_run_safe_command


# --------------------------------------------------------------------------- #
# regression: Phase 2D surface keeps working
# --------------------------------------------------------------------------- #


def test_existing_targets_and_plan_endpoints_remain_functional() -> None:
    client, _ = _client()

    targets = client.get("/api/targets")
    assert targets.status_code == 200
    assert isinstance(targets.json()["targets"], list)
    assert targets.json()["count"] == len(targets.json()["targets"])

    plan = client.post("/api/targets/pdf-document/compatibility/plan", json={})
    assert plan.status_code == 200
    body = plan.json()
    assert body["authorization"]["authorized"] is False
    assert body["authorization"]["reason"] == "capability_not_verified"

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "mode": "simulated"}


def test_plan_preview_authorizes_once_the_same_registry_is_verified() -> None:
    registry = _verified_registry()
    client, _ = _client(registry)

    plan = client.post("/api/targets/pdf-document/compatibility/plan", json={})
    assert plan.status_code == 200
    assert plan.json()["authorization"]["authorized"] is True
    assert plan.json()["authorization"]["reason"] == "authorized_simulation"

    engines = {e["engine"]: e for e in client.get("/api/engines").json()["engines"]}
    assert engines[JOHN]["capability_status"] == "verified"


# --------------------------------------------------------------------------- #
# schema-level guarantees
# --------------------------------------------------------------------------- #


def test_engine_schemas_declare_exactly_the_payload_fields() -> None:
    assert set(EngineInfo.model_fields) == ENGINE_INFO_KEYS
    assert set(EnginesResponse.model_fields) == {"engines"}
    assert not (set(EngineInfo.model_fields) & FORBIDDEN_RESPONSE_KEYS)
    assert not (set(EnginesResponse.model_fields) & FORBIDDEN_RESPONSE_KEYS)


def test_engine_schemas_forbid_extra_fields() -> None:
    assert EngineInfo.model_config.get("extra") == "forbid"
    assert EnginesResponse.model_config.get("extra") == "forbid"

    with pytest.raises(ValueError):
        EngineInfo(
            engine=JOHN,
            discovered=True,
            binary_found=True,
            operations=[],
            capability_status="verified",
            formats=[],
            targets=[],
            real_engine_compatible=True,
            stdout="leak",
        )


def test_known_engine_set_still_matches_the_discovery_models() -> None:
    assert KNOWN_ENGINES == frozenset({JOHN, HASHCAT})


# --------------------------------------------------------------------------- #
# static safety boundary on the route module
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


def test_engines_route_module_never_imports_process_execution() -> None:
    modules = _imported_modules(ROUTES_SOURCE)
    for module in ("subprocess", "os", "shlex", "pty", "shutil"):
        assert module not in modules


def test_engines_route_module_never_invokes_refresh_or_runner() -> None:
    tree = ast.parse(ROUTES_SOURCE)
    modules = _imported_modules(ROUTES_SOURCE)
    assert "app.engines.discovery.service" not in modules
    assert "app.engines.discovery.runner" not in modules

    referenced = {
        node.id if isinstance(node, ast.Name) else node.attr
        for node in ast.walk(tree)
        if isinstance(node, (ast.Name, ast.Attribute))
    }
    assert "CapabilityRefreshService" not in referenced
    assert "run_safe_command" not in referenced
    assert "locate_engine_binary" not in referenced
    assert "refresh" not in referenced
    assert "--hash-info" not in ROUTES_SOURCE

    keywords = {
        keyword.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
    }
    assert "shell" not in keywords


def test_engines_route_module_imports_only_read_only_dependencies() -> None:
    modules = _imported_modules(ROUTES_SOURCE)
    assert "app.api.schemas" in modules
    assert "app.engines.discovery.registry" in modules
    assert "app.engines.discovery.models" in modules

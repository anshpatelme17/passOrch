"""Phase 9B - configuration, secrets, and hardening regression tests.

Locks in the audit's demonstrated fixes and invariants:

* the ``PASSORCH_JOB_DB`` environment value is normalised (surrounding
  whitespace ignored) and a blank value stays in-memory;
* invalid numeric security configuration fails fast with a constant
  message that never echoes the raw value;
* configuration is read lazily inside functions - no module-level
  environment access runs at import time;
* every response carries the same-origin safety headers (including a
  strict ``Content-Security-Policy`` and ``Referrer-Policy:
  no-referrer``), covering every rejection class (400 / 401 / 413 /
  429), while no CORS headers are ever issued;
* the configuration and authentication modules never log or print, so
  tokens and env values cannot reach stdout or a log;
* README documents exactly the environment variables the application
  reads - no undocumented settings, no undocumented reads.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import CONTENT_SECURITY_POLICY, create_app
from app.engines.discovery.models import (
    CapabilityStatus,
    EngineCapabilities,
    EngineSnapshot,
    JOHN,
)
from app.engines.discovery.registry import DiscoveryRegistry
from app.engines.jobs import JobStore
from app.engines.persistence import DATABASE_ENV_VAR, configured_database_path
from app.engines.target_instances import TargetInstanceStore

_TOKEN = "correct-horse-battery-staple"
_RAW_CONFIG_ERROR = "rate limit settings must be positive numbers"

_DOCUMENTED_ENV_VARS = {
    "PASSORCH_API_TOKEN",
    "PASSORCH_JOB_DB",
    "PASSORCH_MAX_BODY_BYTES",
    "PASSORCH_RATE_LIMIT",
    "PASSORCH_RATE_WINDOW_SECONDS",
}

_GUARDED_MODULES = (
    Path("app/api/app.py"),
    Path("app/api/auth.py"),
    Path("app/api/rate_limit.py"),
)


def _verified_registry() -> DiscoveryRegistry:
    registry = DiscoveryRegistry()
    registry.register(EngineSnapshot(engine=JOHN, discovered=True, binary_found=True))
    registry.set_capabilities(
        JOHN,
        EngineCapabilities(
            engine=JOHN,
            capability_status=CapabilityStatus.VERIFIED,
            formats=("pdf",),
        ),
    )
    return registry


def _fresh_app(**kwargs):
    return create_app(
        discovery_registry=_verified_registry(),
        job_store=JobStore(),
        target_instance_store=TargetInstanceStore(),
        **kwargs,
    )


def _imported_names(tree: ast.AST) -> set[str]:
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }


def _call_names(tree: ast.AST) -> set[str]:
    return {
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Attribute, ast.Name))
    }


def _safety_headers(response) -> None:  # type: ignore[no-untyped-def]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY


# --------------------------------------------------------------------------- #
# environment configuration
# --------------------------------------------------------------------------- #


def test_database_env_path_is_normalised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "padded.db"
    monkeypatch.setenv(DATABASE_ENV_VAR, f"   {target}   ")
    assert configured_database_path() == target

    monkeypatch.setenv(DATABASE_ENV_VAR, "    ")
    assert configured_database_path() is None

    monkeypatch.delenv(DATABASE_ENV_VAR, raising=False)
    assert configured_database_path() is None


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PASSORCH_RATE_LIMIT", "3.5-topsecret"),
        ("PASSORCH_RATE_WINDOW_SECONDS", "soon"),
        ("PASSORCH_MAX_BODY_BYTES", "lots"),
    ],
)
def test_invalid_env_numeric_config_fails_fast_without_echoing_values(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError) as error:
        create_app()
    message = str(error.value)
    assert message == _RAW_CONFIG_ERROR
    assert value not in message


def test_configuration_reads_happen_inside_functions_not_at_import() -> None:
    for module in (
        Path("app/engines/persistence.py"),
        Path("app/api/auth.py"),
        Path("app/api/rate_limit.py"),
    ):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call) or not isinstance(sub.func, ast.Attribute):
                    continue
                if sub.func.attr != "get":
                    continue
                value = sub.func.value
                if (
                    isinstance(value, ast.Attribute)
                    and value.attr == "environ"
                    and isinstance(value.value, ast.Name)
                    and value.value.id == "os"
                ):
                    raise AssertionError(
                        f"module-level environment read in {module}: {ast.dump(sub)}"
                    )


# --------------------------------------------------------------------------- #
# security headers and CORS
# --------------------------------------------------------------------------- #


def test_safety_headers_on_success_responses() -> None:
    client = TestClient(_fresh_app())
    _safety_headers(client.get("/health"))
    _safety_headers(client.get("/api/jobs"))
    _safety_headers(client.get("/openapi.json"))
    _safety_headers(client.get("/"))  # the SPA shell itself

    created = client.post(
        "/api/target-instances",
        json={"name": "hdr", "target_type": "pdf-document"},
    )
    assert created.status_code == 201
    _safety_headers(created)

    deleted = client.delete(f"/api/target-instances/{created.json()['id']}")
    assert deleted.status_code == 204
    _safety_headers(deleted)

    missing = client.get("/api/jobs/00000000-0000-4000-8000-000000000000")
    assert missing.status_code == 404
    _safety_headers(missing)


def test_safety_headers_on_every_rejection_class() -> None:
    unauthorized = TestClient(_fresh_app(api_token=_TOKEN))
    rejected = unauthorized.post("/api/jobs", json={"target": "pdf-document"})
    assert rejected.status_code == 401
    _safety_headers(rejected)

    limited_app = _fresh_app(rate_limit=1, rate_window_seconds=60)
    limited_client = TestClient(limited_app)
    assert limited_client.post("/api/jobs", json={"target": "pdf-document"}).status_code == 200
    limited = limited_client.post("/api/jobs", json={"target": "pdf-document"})
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "60"
    _safety_headers(limited)

    capped = TestClient(_fresh_app(max_body_bytes=64))
    oversized = capped.post(
        "/api/target-instances",
        json={"name": "y" * 300, "target_type": "pdf-document"},
    )
    assert oversized.status_code == 413
    _safety_headers(oversized)

    framed_client = TestClient(_fresh_app())
    framed = framed_client.post(
        "/api/jobs",
        json={"target": "pdf-document"},
        headers={"Transfer-Encoding": "chunked"},
    )
    assert framed.status_code == 400
    _safety_headers(framed)


def test_no_cors_headers_are_issued() -> None:
    client = TestClient(_fresh_app())
    for response in (
        client.get("/health"),
        client.post("/api/jobs", json={"target": "pdf-document"}),
    ):
        cors_headers = [
            key for key in response.headers if key.lower().startswith("access-control-")
        ]
        assert cors_headers == []


# --------------------------------------------------------------------------- #
# no logging of secrets, docs match implementation
# --------------------------------------------------------------------------- #


def test_config_modules_never_log_or_print() -> None:
    for module in _GUARDED_MODULES:
        tree = ast.parse(module.read_text(encoding="utf-8"))
        imported = _imported_names(tree)
        assert "logging" not in imported, module
        assert "sys" not in imported, module
        called = _call_names(tree)
        assert "print" not in called, module
        assert "pprint" not in called, module


def test_readme_documents_exactly_the_environment_variables_the_app_reads() -> None:
    read_by_app: set[str] = set()
    for module in Path("app").rglob("*.py"):
        text = module.read_text(encoding="utf-8")
        read_by_app.update(re.findall(r"PASSORCH_[A-Z0-9_]+", text))

    assert read_by_app == _DOCUMENTED_ENV_VARS

    readme = Path("README.md").read_text(encoding="utf-8")
    for name in _DOCUMENTED_ENV_VARS:
        assert name in readme, name

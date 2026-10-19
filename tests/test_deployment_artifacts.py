"""Phase 9C - deployment artifact security and correctness tests.

Static contracts for the Dockerfile, Compose file, env example, CI
workflow, and README (Docker runs the real app non-root with no baked
secrets; Compose stays loopback-published with a persistent volume; CI
uses the verified harness invocation and requests no secrets; the env
example only documents known ``PASSORCH_*`` variables).

These checks are deliberately text-level so they run anywhere - Docker
is not required for the test suite (image/Compose validation is a
separate, documented manual step).
"""

from __future__ import annotations

import re
from pathlib import Path

_DOCKERFILE = Path("Dockerfile")
_COMPOSE = Path("compose.yaml")
_ENV_EXAMPLE = Path(".env.example")
_DOCKERIGNORE = Path(".dockerignore")
_CI_WORKFLOW = Path(".github/workflows/ci.yml")
_README = Path("README.md")

_KNOWN_ENV_VARS = {
    "PASSORCH_API_TOKEN",
    "PASSORCH_JOB_DB",
    "PASSORCH_MAX_BODY_BYTES",
    "PASSORCH_RATE_LIMIT",
    "PASSORCH_RATE_WINDOW_SECONDS",
}


def _populated_value(text: str, name: str) -> str | None:
    """The non-empty value assigned to ``NAME`` (ignoring comments), if any."""

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(rf"^(?:ENV\s+)?{re.escape(name)}=(\S.*)$", stripped)
        if match:
            return match.group(1).strip()
    return None


def test_dockerfile_runs_the_real_app_as_non_root() -> None:
    text = _DOCKERFILE.read_text(encoding="utf-8")
    assert "USER passorch" in text
    assert "app.api.app:app" in text
    assert "--host" in text and "0.0.0.0" in text
    assert "COPY app ./app" in text
    assert "COPY frontend ./frontend" in text


def test_dockerfile_contains_no_secrets_and_skips_vcs() -> None:
    text = _DOCKERFILE.read_text(encoding="utf-8")
    assert _populated_value(text, "PASSORCH_API_TOKEN") is None
    assert "COPY .git" not in text
    assert "COPY .env" not in text
    # No password-cracking tool or engine is installed.
    lowered = text.lower()
    for banned in ("hashcat", "john the ripper", "apt-get install"):
        assert banned not in lowered


def test_dockerfile_healthcheck_is_the_read_only_health_endpoint() -> None:
    text = _DOCKERFILE.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith("HEALTHCHECK"))
    healthcheck = lines[start]
    if healthcheck.rstrip().endswith("\\"):
        healthcheck += lines[start + 1]
    assert "/health" in healthcheck
    assert "/api/" not in healthcheck


def test_compose_publishes_loopback_only_and_persists_data_on_a_volume() -> None:
    text = _COMPOSE.read_text(encoding="utf-8")
    assert '"127.0.0.1:8000:8000"' in text
    assert "0.0.0.0:8000" not in text
    assert "passorch-data:/data" in text
    assert "PASSORCH_JOB_DB: /data/passorch.db" in text
    assert _populated_value(text, "PASSORCH_API_TOKEN") is None
    # Runtime hardening: read-only root filesystem (database on the named
    # volume, /tmp as tmpfs), no capabilities, no privilege escalation.
    assert "read_only: true" in text
    assert "- /tmp" in text
    assert "- ALL" in text  # cap_drop: ALL
    assert "no-new-privileges:true" in text
    # One application service - no database server or cache.
    services = re.search(r"^services:\n((?:  \S.*\n|    .*\n|\n)+)", text, re.MULTILINE)
    assert services is not None
    assert re.findall(r"^  (\w[\w-]*):$", services.group(1), re.MULTILINE) == ["passorch"]


def test_env_example_only_documents_known_variables_and_no_real_values() -> None:
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            stripped = stripped.lstrip("# ").strip()
        if "=" not in stripped:
            continue
        name = stripped.split("=", 1)[0].strip()
        if name.startswith("PASSORCH_"):
            assert name in _KNOWN_ENV_VARS, name
    # Every variable stays commented out - no real value is committed.
    for name in _KNOWN_ENV_VARS:
        assert _populated_value(text, name) is None, name


def test_dockerignore_excludes_vcs_databases_and_secrets() -> None:
    entries = {
        line.strip()
        for line in _DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    for required in (".git", "*.db", ".env", "data", "tests", ".pytest_cache"):
        assert required in entries, required


def test_ci_workflow_uses_verified_invocations_and_requests_no_secrets() -> None:
    text = _CI_WORKFLOW.read_text(encoding="utf-8")
    assert "python -m pytest tests/test_frontend_js_execution.py" in text
    assert "python tests/test_frontend_js_execution.py" not in text
    assert "python -m ruff check ." in text
    assert "git diff --check" in text
    assert "contents: read" in text
    assert "secrets." not in text
    assert "-c constraints.txt" in text  # reproducible dependency resolution
    assert _populated_value(text, "PASSORCH_API_TOKEN") is None


def test_readme_documents_docker_and_ci_workflow() -> None:
    readme = _README.read_text(encoding="utf-8")
    assert "## Docker" in readme
    assert "docker compose up -d --build" in readme
    assert "docker compose down" in readme
    assert "## Continuous Integration" in readme
    assert ".env.example" in readme

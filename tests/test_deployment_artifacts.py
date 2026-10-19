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
    "PASSORCH_TRUSTED_PROXIES",
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


def test_dockerfile_healthcheck_is_the_read_only_readiness_endpoint() -> None:
    text = _DOCKERFILE.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith("HEALTHCHECK"))
    healthcheck = lines[start]
    if healthcheck.rstrip().endswith("\\"):
        healthcheck += lines[start + 1]
    # Phase 3 of the roadmap: the probe is readiness (stores listable),
    # not bare liveness, and it never calls an /api/ route.
    assert "/health/ready" in healthcheck
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
    assert "--require-hashes" in text and "requirements.lock" in text
    assert _populated_value(text, "PASSORCH_API_TOKEN") is None


# --------------------------------------------------------------------------- #
# supply-chain integrity (hashes, action SHAs, base-image digest)
# --------------------------------------------------------------------------- #

_REQLINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([0-9][A-Za-z0-9.]*) \\\r?$")
_HASHLINE = re.compile(r"^    --hash=sha256:([0-9a-f]{64})( \\\r?)?$")
_SHA_USES = re.compile(r"^(\s*)uses:\s*([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)@([0-9a-f]{40})\s*(#.*)?$")
_TAG_USES = re.compile(r"^(\s*)uses:\s*\S+@[A-Za-z0-9][A-Za-z0-9._-]*\s*$")
_FROM_DIGEST = re.compile(
    r"^FROM\s+python:3\.13-slim@sha256:([0-9a-f]{64})\s*$", re.MULTILINE
)


def test_requirements_lock_pins_every_dependency_with_hashes() -> None:
    lines = Path("requirements.lock").read_text(encoding="utf-8").splitlines()
    assert lines, "requirements.lock must not be empty"
    pinned: set[str] = set()
    i = 0
    while i < len(lines):
        match = _REQLINE.match(lines[i])
        assert match is not None, f"line {i + 1} is not a 'name==version \\' pin: {lines[i]!r}"
        name = match.group(1).lower().replace("_", "-")
        pinned.add(name)
        i += 1
        hashes = 0
        while i < len(lines) and lines[i].startswith("    --hash="):
            hash_match = _HASHLINE.match(lines[i])
            assert hash_match is not None, f"line {i + 1} is not a well-formed sha256 hash"
            hashes += 1
            i += 1
        assert hashes >= 1, f"{name} has no hashes"
    # The verified closure (runtime + test + typing toolchain) - direct
    # anchors, the two transitive packages that only a fresh environment
    # reveals, and mypy with its full closure.
    for required in (
        "fastapi",
        "starlette",
        "pydantic",
        "pydantic-core",
        "uvicorn",
        "httpx",
        "httpcore",
        "pytest",
        "ruff",
        "pygments",
        "colorama",
        "typing-extensions",
        "typing-inspection",
        "anyio",
        "certifi",
        "click",
        "h11",
        "idna",
        "iniconfig",
        "packaging",
        "pluggy",
        "sniffio",
        "annotated-types",
        "mypy",
        "mypy-extensions",
        "pathspec",
        "librt",
        "ast-serialize",
    ):
        assert required in pinned, required


def test_ci_actions_are_pinned_to_full_commit_shas() -> None:
    lines = _CI_WORKFLOW.read_text(encoding="utf-8").splitlines()
    uses = [line for line in lines if "uses:" in line]
    assert uses, "CI must use at least one action"
    for line in uses:
        sha_match = _SHA_USES.match(line)
        assert sha_match is not None, f"action not SHA-pinned: {line!r}"
    # No leftover tag-pinned uses lines.
    for line in lines:
        assert not _TAG_USES.match(line), f"tag-pinned action remains: {line!r}"


def test_dockerfile_base_image_is_digest_pinned_and_uses_the_lock() -> None:
    text = _DOCKERFILE.read_text(encoding="utf-8")
    digest = _FROM_DIGEST.search(text)
    assert digest is not None, "FROM must pin python:3.13-slim by sha256 digest"
    assert "--require-hashes" in text and "requirements.lock" in text
    # The project install must not re-resolve dependencies.
    assert "--no-deps ." in text
    # The old constraint-file flow is fully replaced by the lock.
    assert "-c constraints.txt" not in text


def test_readme_documents_docker_and_ci_workflow() -> None:
    readme = _README.read_text(encoding="utf-8")
    assert "## Docker" in readme
    assert "docker compose up -d --build" in readme
    assert "docker compose down" in readme
    assert "## Continuous Integration" in readme
    assert ".env.example" in readme


# ---------------------------------------------------------------------------
# incremental typing gate (Step 5)
# --------------------------------------------------------------------------- #

_MYPY_STEP = "python -m mypy app/api app/engines"


def test_ci_runs_the_scoped_mypy_gate_after_ruff() -> None:
    text = _CI_WORKFLOW.read_text(encoding="utf-8")
    assert _MYPY_STEP in text, "CI must run the scoped mypy gate"
    ruff_at = text.find("python -m ruff check")
    mypy_at = text.find(_MYPY_STEP)
    assert ruff_at != -1, "CI must keep the Ruff step"
    assert 0 < ruff_at < mypy_at, "the mypy step must come after Ruff"


def test_pyproject_declares_the_scoped_mypy_configuration() -> None:
    import tomllib

    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    mypy = config.get("tool", {}).get("mypy")
    assert mypy, "pyproject.toml must carry a [tool.mypy] section"
    assert mypy.get("check_untyped_defs") is True, "check_untyped_defs must be on"
    files = mypy.get("files")
    assert isinstance(files, list), "files must be a list"
    assert "app/api" in files and "app/engines" in files, files

    # Exactly one per-module override: the protected discovery service may
    # disable arg-type, and nothing else may be relaxed anywhere.
    overrides = mypy.get("overrides") or []
    assert len(overrides) == 1, f"unexpected mypy overrides: {overrides!r}"
    override = overrides[0]
    assert override.get("module") == "app.engines.discovery.service", override
    assert override.get("disable_error_code") == ["arg-type"], override

    # The gate itself must not be hollowed out by a global error disable.
    assert "disable_error_code" not in mypy, mypy

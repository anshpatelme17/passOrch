"""Phase 11A/Step 11 - documentation consistency regression tests.

Locks the release-audit findings so they cannot drift back:

* README's Testing commands must not append a second ``-q`` (pyproject's
  ``addopts`` already sets it, so an explicit ``-q`` becomes ``-qq`` and
  hides the pass-count summary);
* README must not claim zero subprocess execution anywhere - the single
  allowlisted discovery path exists - and must carry the accurate
  allowlist phrasing;
* every local document link in README resolves to a real file;
* ``.env.example`` documents exactly the environment variables the
  application reads (same set README documents);
* every structured-error example in README carries the outer ``detail``
  envelope (Step 11), and the guard/health bodies it shows equal the
  responses the API actually returns.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.app import create_app

_README = Path("README.md")

_DOCUMENTED_ENV_VARS = {
    "PASSORCH_API_TOKEN",
    "PASSORCH_JOB_DB",
    "PASSORCH_MAX_BODY_BYTES",
    "PASSORCH_RATE_LIMIT",
    "PASSORCH_RATE_WINDOW_SECONDS",
    "PASSORCH_TRUSTED_PROXIES",
}


def test_readme_testing_command_does_not_double_quiet_pytest() -> None:
    readme = _README.read_text(encoding="utf-8")
    assert "python -m pytest --tb=short -q" not in readme
    assert "python -m pytest --tb=short" in readme


def test_readme_does_not_claim_zero_subprocess_execution() -> None:
    readme = _README.read_text(encoding="utf-8")
    assert "no subprocess or engine execution anywhere" not in readme
    assert "allowlisted metadata commands" in readme
    assert "allowlisted discovery metadata commands" in readme


def test_readme_local_document_links_resolve() -> None:
    readme = _README.read_text(encoding="utf-8")
    targets = set(
        re.findall(r"\]\(((?:docs|THREAT_MODEL)[^)]+)\)", readme)
    )
    assert targets, "README must link the document set"
    for target in targets:
        assert Path(target).is_file(), target


def test_env_example_documents_exactly_the_app_environment_variables() -> None:
    example = Path(".env.example").read_text(encoding="utf-8")
    found = set(re.findall(r"PASSORCH_[A-Z0-9_]+", example))
    assert found == _DOCUMENTED_ENV_VARS


# --------------------------------------------------------------------------- #
# Step 11: documented error examples must show the real response envelope
# --------------------------------------------------------------------------- #


def test_readme_error_examples_carry_the_outer_detail_envelope() -> None:
    """Every ``{"error": ...}`` example in README sits inside a ``detail`` object.

    The API's wire shape is ``{"detail": {"error": code, "detail": message}}``
    (see ``app/api/errors.py`` and the request guards in ``app/api/app.py``);
    an example that starts at ``{"error":`` documents a body the server never
    sends.  The Error Contract section and every status bullet must agree.
    """

    readme = _README.read_text(encoding="utf-8")
    starts = [match.start() for match in re.finditer(r'\{"error":', readme)]
    assert starts, "README must document the structured error examples"
    for start in starts:
        prefix = readme[max(0, start - 14) : start]
        assert '"detail": ' in prefix, (
            "error example omits the outer detail envelope near: "
            + repr(readme[max(0, start - 40) : start + 48])
        )


def test_readme_guard_error_examples_match_the_live_responses() -> None:
    """The guard/health error bodies README shows equal real responses."""

    readme = _README.read_text(encoding="utf-8")

    # 401 - Authentication section.
    unauthorized = TestClient(create_app(api_token="readme-token")).post(
        "/api/jobs", json={"target": "pdf-document"}
    )
    assert unauthorized.status_code == 401
    assert unauthorized.json() == {
        "detail": {
            "error": "unauthorized",
            "detail": "a valid bearer token is required",
        }
    }
    assert (
        '{"detail": {"error": "unauthorized", "detail": "a valid bearer token is required"}}'
        in readme
    )

    # 429 - Rate Limiting section; the second mutation trips limit=1.
    # The README example uses the N placeholder, so the live body (retry_after
    # counted down) is compared field by field against the documented shape.
    rate_client = TestClient(create_app(rate_limit=1, rate_window_seconds=60))
    assert rate_client.post("/api/jobs", json={"target": "pdf-document"}).status_code == 200
    rate_limited = rate_client.post("/api/jobs", json={"target": "pdf-document"})
    assert rate_limited.status_code == 429
    assert rate_limited.json() == {
        "detail": {
            "error": "rate_limited",
            "detail": "too many requests",
            "retry_after": 60,
        }
    }
    assert (
        '{"detail": {"error": "rate_limited", "detail": "too many requests", "retry_after": N}}'
        in readme
    )

    # 413 - Rate Limiting section (body cap).
    too_large = TestClient(create_app(max_body_bytes=16)).post(
        "/api/target-instances",
        json={"name": "x" * 200, "target_type": "pdf-document"},
    )
    assert too_large.status_code == 413
    assert too_large.json() == {
        "detail": {
            "error": "payload_too_large",
            "detail": "request body exceeds the configured size limit",
        }
    }
    assert (
        '{"detail": {"error": "payload_too_large", "detail": "request body exceeds the configured size limit"}}'
        in readme
    )

    # 400 - Rate Limiting section (chunked framing), unguarded client so the
    # Content-Length cap cannot answer before the Transfer-Encoding check.
    chunked = TestClient(create_app()).post(
        "/api/jobs",
        json={"target": "pdf-document"},
        headers={"Transfer-Encoding": "chunked"},
    )
    assert chunked.status_code == 400
    assert chunked.json() == {
        "detail": {
            "error": "unsupported_transfer_encoding",
            "detail": "transfer-encoded request bodies are not supported",
        }
    }
    assert (
        '{"detail": {"error": "unsupported_transfer_encoding", "detail": "transfer-encoded request bodies are not supported"}}'
        in readme
    )

    # 503 - Health semantics section (readiness), store listing fails.
    ready_client = TestClient(create_app())

    def _broken_listing() -> list[object]:
        raise RuntimeError("store failure")

    ready_client.app.state.job_store.list = _broken_listing  # type: ignore[method-assign]
    not_ready = ready_client.get("/health/ready")
    assert not_ready.status_code == 503
    assert not_ready.json() == {
        "detail": {"error": "not_ready", "detail": "readiness probe failed"}
    }
    assert (
        '{"detail": {"error": "not_ready", "detail": "readiness probe failed"}}' in readme
    )

    # 503 - Persistence section: the full failing-store body is locked by
    # test_job_persistence; here the README wrapper is locked.
    assert '{"detail": {"error": "persistence_unavailable"' in readme

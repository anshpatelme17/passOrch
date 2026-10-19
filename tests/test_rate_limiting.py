"""Phase 8E - mutation rate limiting and body-size bounds tests.

Covers:
* over-limit mutations answer a structured HTTP 429 with ``Retry-After``
  and never leak internals; reads are never counted; the window resets;
* the client key is the socket peer address - ``X-Forwarded-For`` is
  ignored;
* oversized mutation bodies answer a structured HTTP 413, chunked
  (``Transfer-Encoding``) mutations are rejected with a structured HTTP
  400 before any body is read, and an invalid ``Content-Length`` fails
  closed;
* configuration: explicit ``create_app`` arguments win over environment
  variables, defaults stay generous, invalid values fail fast at
  construction;
* the address table stays bounded; and the guards never bypass route
  validation or the execution policy - a rejected request reaches no
  handler and records nothing;
* README documents behaviour and the process-local limitations.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.rate_limit import (
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_RATE_LIMIT,
    DEFAULT_RATE_WINDOW_SECONDS,
    MAX_TRACKED_ADDRESSES,
    RateLimiter,
    configured_limits,
    exceeds_body_limit,
)
from app.engines.discovery.models import (
    CapabilityStatus,
    EngineCapabilities,
    EngineSnapshot,
    JOHN,
)
from app.engines.discovery.registry import DiscoveryRegistry
from app.engines.jobs import JobStore
from app.engines.target_instances import TargetInstanceStore

_CONFIG_ERROR = "rate limit settings must be positive numbers"


def _verified_registry() -> DiscoveryRegistry:
    """John verified with the pdf format, so simulated jobs really simulate."""

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


def _job(client: TestClient, mode: str = "simulated", target: str = "pdf-document"):
    return client.post("/api/jobs", json={"target": target, "mode": mode})


def _fresh_app(**kwargs):
    """An app with private in-memory stores - tests never share the process-wide
    default stores, so assertions on totals are exact and isolated."""

    return create_app(
        discovery_registry=_verified_registry(),
        job_store=JobStore(),
        target_instance_store=TargetInstanceStore(),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# defaults and structured rejections
# --------------------------------------------------------------------------- #


def test_default_guards_allow_normal_traffic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PASSORCH_RATE_LIMIT", raising=False)
    monkeypatch.delenv("PASSORCH_RATE_WINDOW_SECONDS", raising=False)
    monkeypatch.delenv("PASSORCH_MAX_BODY_BYTES", raising=False)
    application = _fresh_app()
    client = TestClient(application)

    for index in range(20):
        assert _job(client).status_code == 200
        response = client.post(
            "/api/target-instances",
            json={"name": f"n{index}", "target_type": "pdf-document"},
        )
        assert response.status_code == 201

    assert client.get("/api/jobs").json()["total"] == 20
    assert client.get("/api/target-instances").json()["total"] == 20

    limiter = application.state.rate_limiter
    assert limiter.limit == DEFAULT_RATE_LIMIT
    assert limiter.window_seconds == DEFAULT_RATE_WINDOW_SECONDS
    assert application.state.max_body_bytes == DEFAULT_MAX_BODY_BYTES


def test_over_limit_mutation_gets_structured_429_with_retry_after() -> None:
    client = TestClient(_fresh_app(rate_limit=3, rate_window_seconds=60))

    for _ in range(3):
        assert _job(client).status_code == 200

    response = _job(client)
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
    assert response.json()["detail"] == {
        "error": "rate_limited",
        "detail": "too many requests",
        "retry_after": 60,
    }
    body = response.text.lower()
    assert "sqlite" not in body
    assert "traceback" not in body

    # Still over limit for the rest of the window.
    assert _job(client).status_code == 429


def test_reads_are_never_rate_limited() -> None:
    client = TestClient(_fresh_app(rate_limit=1, rate_window_seconds=60))

    assert _job(client).status_code == 200
    assert _job(client).status_code == 429

    for path in (
        "/health",
        "/api/targets",
        "/api/engines",
        "/api/jobs",
        "/api/jobs/stats",
        "/api/target-instances",
    ):
        assert client.get(path).status_code == 200


def test_window_resets_after_expiry() -> None:
    client = TestClient(_fresh_app(rate_limit=1, rate_window_seconds=0.15))

    assert _job(client).status_code == 200
    limited = _job(client)
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1

    import time

    time.sleep(0.25)
    assert _job(client).status_code == 200


def test_client_key_is_the_socket_address_not_forwarded_headers() -> None:
    client = TestClient(_fresh_app(rate_limit=1, rate_window_seconds=60))

    first = _job(client, target="pdf-document")
    assert first.status_code == 200

    # A different claimed origin, same socket: same bucket -> limited.
    spoofed = client.post(
        "/api/jobs",
        json={"target": "zip-archive"},
        headers={"X-Forwarded-For": "203.0.113.9"},
    )
    assert spoofed.status_code == 429


# --------------------------------------------------------------------------- #
# body size bound
# --------------------------------------------------------------------------- #


def test_oversized_body_gets_structured_413() -> None:
    client = TestClient(_fresh_app(max_body_bytes=100))

    # A small mutation passes the bound...
    assert _job(client).status_code == 200

    # ...and a large one is rejected before any handler runs.
    response = client.post(
        "/api/target-instances",
        json={"name": "x" * 300, "target_type": "pdf-document"},
    )
    assert response.status_code == 413
    assert response.json()["detail"] == {
        "error": "payload_too_large",
        "detail": "request body exceeds the configured size limit",
    }

    # The rejected body never became a record.
    assert client.get("/api/target-instances").json()["total"] == 0


def test_exceeds_body_limit_contract() -> None:
    assert exceeds_body_limit(None, 100) is False  # absent: no body assumed; TE rejected below
    assert exceeds_body_limit("", 100) is True  # present but invalid: fail closed
    assert exceeds_body_limit("abc", 100) is True
    assert exceeds_body_limit("-1", 100) is True
    assert exceeds_body_limit("100", 100) is False
    assert exceeds_body_limit(" 100 ", 100) is False
    assert exceeds_body_limit("101", 100) is True


def test_transfer_encoded_mutations_are_rejected_before_any_body_read() -> None:
    client = TestClient(_fresh_app())

    response = client.post(
        "/api/jobs",
        json={"target": "pdf-document"},
        headers={"Transfer-Encoding": "chunked"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == {
        "error": "unsupported_transfer_encoding",
        "detail": "transfer-encoded request bodies are not supported",
    }

    # The rejected framing never reached a handler: nothing was recorded.
    assert client.get("/api/jobs").json()["total"] == 0

    deleted = client.delete(
        "/api/target-instances/00000000-0000-4000-8000-000000000000",
        headers={"Transfer-Encoding": "chunked"},
    )
    assert deleted.status_code == 400

    # Reads are unaffected by the framing guard.
    assert client.get("/api/targets").status_code == 200


def test_empty_body_mutations_without_framing_still_work() -> None:
    """DELETE with neither Content-Length nor Transfer-Encoding is a no-body
    request and must keep working - the guard rejects framing, not absence."""

    client = TestClient(_fresh_app())
    created = client.post(
        "/api/target-instances",
        json={"name": "framing", "target_type": "pdf-document"},
    )
    assert created.status_code == 201
    assert client.delete(f"/api/target-instances/{created.json()['id']}").status_code == 204


# --------------------------------------------------------------------------- #
# configuration: precedence, defaults, fail-fast validation
# --------------------------------------------------------------------------- #


def test_explicit_arguments_win_over_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PASSORCH_RATE_LIMIT", "7")
    monkeypatch.setenv("PASSORCH_RATE_WINDOW_SECONDS", "30")
    monkeypatch.setenv("PASSORCH_MAX_BODY_BYTES", "500")

    from_environment = create_app()
    assert from_environment.state.rate_limiter.limit == 7
    assert from_environment.state.rate_limiter.window_seconds == 30.0
    assert from_environment.state.max_body_bytes == 500

    explicit = create_app(rate_limit=9, rate_window_seconds=5, max_body_bytes=64)
    assert explicit.state.rate_limiter.limit == 9
    assert explicit.state.rate_limiter.window_seconds == 5.0
    assert explicit.state.max_body_bytes == 64

    monkeypatch.setenv("PASSORCH_RATE_LIMIT", "   ")
    blank_env = configured_limits()
    assert blank_env.limit == DEFAULT_RATE_LIMIT


def test_invalid_configuration_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="positive"):
        create_app(rate_limit=0)
    with pytest.raises(ValueError, match="positive"):
        create_app(rate_limit=-5)
    with pytest.raises(ValueError, match="positive"):
        create_app(rate_window_seconds=0)
    with pytest.raises(ValueError, match="positive"):
        create_app(max_body_bytes=0)

    monkeypatch.setenv("PASSORCH_RATE_LIMIT", "lots")
    with pytest.raises(ValueError) as error:
        create_app()
    assert str(error.value) == _CONFIG_ERROR

    monkeypatch.setenv("PASSORCH_MAX_BODY_BYTES", "-1")
    with pytest.raises(ValueError, match="positive"):
        create_app()


# --------------------------------------------------------------------------- #
# bounded state and guard independence from policy
# --------------------------------------------------------------------------- #


def test_address_table_stays_bounded() -> None:
    limiter = RateLimiter(2, 60)
    for index in range(MAX_TRACKED_ADDRESSES + 500):
        limiter.check(f"198.51.100.{index}")
        assert limiter.tracked_addresses <= MAX_TRACKED_ADDRESSES
    assert limiter.tracked_addresses == MAX_TRACKED_ADDRESSES

    # Eviction trades history for bounded memory: an evicted address
    # starts a fresh window (documented limitation, not a bypass - it
    # still has to stay under its limit once counted again).
    assert limiter.check("198.51.100.0") is None


def test_rate_limit_never_bypasses_validation_or_policy() -> None:
    client = TestClient(_fresh_app(rate_limit=3, rate_window_seconds=60))

    # Route validation still rejects an unknown target (first counted hit).
    assert _job(client, target="bogus-target").status_code == 422

    # The execution policy still records a denial as a normal job while
    # the authorised simulation passes.
    denied = _job(client, mode="real")
    assert denied.status_code == 200

    simulated = _job(client, mode="simulated")
    assert simulated.status_code == 200
    assert simulated.json()["status"] == "simulated"

    # The next mutation is rejected outright - and reaches no handler.
    assert _job(client).status_code == 429
    listed = client.get("/api/jobs").json()
    assert listed["total"] == 2
    assert client.get("/api/jobs", params={"status": "denied"}).json()["total"] == 1


# --------------------------------------------------------------------------- #
# documentation
# --------------------------------------------------------------------------- #


def test_readme_documents_rate_limiting() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "## Rate Limiting" in readme
    assert "PASSORCH_RATE_LIMIT" in readme
    assert "Retry-After" in readme
    assert "X-Forwarded-For" in readme
    assert "payload_too_large" in readme
    assert "rate_limited" in readme
    assert "unsupported_transfer_encoding" in readme
    assert "Transfer-Encoding" in readme

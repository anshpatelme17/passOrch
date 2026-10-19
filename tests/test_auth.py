"""Phase 9A - opt-in bearer authentication and deployment-docs tests.

Covers:
* default: no token anywhere -> authentication off, every endpoint
  behaves exactly as before (frontend contract preserved);
* with a token: state-changing requests need a valid ``Bearer`` credential
  (generic identical 401 + ``WWW-Authenticate``), reads stay open;
* constant-time comparison via ``secrets.compare_digest``, explicit-vs-env
  precedence, fail-fast on an explicitly blank token, blank env = off;
* the token is never exposed (state, OpenAPI, responses) and never logged;
* guard order: rate limit -> authentication -> body bound; authentication
  never bypasses validation, the execution policy or persistence;
* README documents the authentication behaviour and deployment posture.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.engines.discovery.models import (
    CapabilityStatus,
    EngineCapabilities,
    EngineSnapshot,
    JOHN,
)
from app.engines.discovery.registry import DiscoveryRegistry
from app.engines.jobs import JobStore
from app.engines.target_instances import TargetInstanceStore

_TOKEN = "correct-horse-battery-staple"
_EMPTY_TOKEN_ERROR = "api token must be a non-empty string"


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
    """A private app: fresh stores, verified registry, exact assertions."""

    return create_app(
        discovery_registry=_verified_registry(),
        job_store=JobStore(),
        target_instance_store=TargetInstanceStore(),
        **kwargs,
    )


def _job(client: TestClient, headers: dict[str, str] | None = None, **payload):
    body = {"target": "pdf-document", "mode": "simulated"}
    body.update(payload)
    return client.post("/api/jobs", json=body, headers=headers)


def _auth(token: str = _TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# default: authentication off
# --------------------------------------------------------------------------- #


def test_auth_disabled_by_default_leaves_mutations_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PASSORCH_API_TOKEN", raising=False)
    application = _fresh_app()
    client = TestClient(application)

    assert application.state.auth_enabled is False
    assert _job(client).status_code == 200
    created = client.post(
        "/api/target-instances",
        json={"name": "plain", "target_type": "pdf-document"},
    )
    assert created.status_code == 201
    deleted = client.delete(f"/api/target-instances/{created.json()['id']}")
    assert deleted.status_code == 204


# --------------------------------------------------------------------------- #
# enabled: structured, uniform 401s
# --------------------------------------------------------------------------- #


def test_mutation_without_token_gets_structured_401() -> None:
    application = _fresh_app(api_token=_TOKEN)
    client = TestClient(application)
    assert application.state.auth_enabled is True

    response = _job(client)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["detail"] == {
        "error": "unauthorized",
        "detail": "a valid bearer token is required",
    }
    assert _TOKEN not in response.text

    # A rejected DELETE records nothing.
    other = TestClient(_fresh_app(api_token=_TOKEN))
    created = _job(other, headers=_auth())
    job_id = created.json()["job_id"]
    assert other.delete(f"/api/jobs/{job_id}").status_code == 401
    assert other.get(f"/api/jobs/{job_id}").status_code == 200


def test_every_credential_failure_returns_the_same_401() -> None:
    client = TestClient(_fresh_app(api_token=_TOKEN))
    headers = [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": f"Basic {_TOKEN}"},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer    "},
        {"Authorization": "Token whatever"},
    ]

    bodies = []
    for attempt in headers:
        response = _job(client, headers=attempt)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        bodies.append(response.json())

    assert all(body == bodies[0] for body in bodies)


def test_bearer_parsing_edge_cases_fail_closed() -> None:
    """Extra malformed Authorization shapes get the same generic 401."""

    client = TestClient(_fresh_app(api_token=_TOKEN))
    adversarial = [
        {"Authorization": "Bearer\t" + _TOKEN},  # tab separator, not space
        {"Authorization": "bearer"},  # scheme only
        {"Authorization": "Bearer  " + _TOKEN + " extra"},  # trailing junk
        {"Authorization": f"Bearer {_TOKEN[:-1]}"},  # one character short
        {"Authorization": f"Bearer {_TOKEN}x"},  # one character long
        {"Authorization": f"bearer {_TOKEN.upper()}"},  # case change in value
        {"Authorization": None},  # header absent (dict value None is dropped by client)
    ]
    for headers in adversarial:
        effective = {k: v for k, v in headers.items() if v is not None}
        response = _job(client, headers=effective)
        assert response.status_code == 401, headers
        assert response.json()["detail"]["error"] == "unauthorized"


def test_unicode_and_mixed_length_tokens_fail_closed_without_crashing() -> None:
    """compare_digest handles unequal lengths and near-misses safely.

    HTTP header values are ASCII on the wire (httpx rejects non-ASCII), so a
    non-ASCII configured token is unsendable as a credential - asserted here
    with an ASCII expected token plus unequal-length and single-character-off
    providers, which is the realistic adversarial surface.
    """

    long_token = "supercalifragilistic-token-42"
    client = TestClient(_fresh_app(api_token=long_token))

    for provided in (
        long_token + "x",  # one character long
        long_token[:-1],  # one character short
        long_token.upper(),  # case change
        long_token.replace("-", "_"),  # punctuation change
        "ascii-only",
        "",
    ):
        response = _job(client, headers={"Authorization": f"Bearer {provided}"})
        assert response.status_code == 401
        assert long_token not in response.text

    assert (
        _job(client, headers={"Authorization": f"Bearer {long_token}"}).status_code
        == 200
    )


def test_correct_token_authorizes_mutations() -> None:
    client = TestClient(_fresh_app(api_token=_TOKEN))

    assert _job(client, headers=_auth()).status_code == 200
    assert _job(client, headers={"authorization": _auth()["Authorization"]}).status_code == 200
    assert _job(client, headers={"Authorization": f"BEARER {_TOKEN}"}).status_code == 200
    assert _job(client, headers={"Authorization": f"Bearer   {_TOKEN}  "}).status_code == 200

    created = client.post(
        "/api/target-instances",
        json={"name": "authy", "target_type": "pdf-document"},
        headers=_auth(),
    )
    assert created.status_code == 201


def test_patch_and_delete_instance_mutations_respect_bearer_auth() -> None:
    """PATCH/DELETE on the instance routes require the token like POST/DELETE jobs."""

    client = TestClient(_fresh_app(api_token=_TOKEN))
    created = client.post(
        "/api/target-instances",
        json={"name": "authy", "target_type": "pdf-document"},
        headers=_auth(),
    )
    assert created.status_code == 201
    instance_id = created.json()["id"]
    url = f"/api/target-instances/{instance_id}"

    assert client.patch(url, json={"name": "renamed"}).status_code == 401
    assert client.patch(url, json={"name": "renamed"}, headers=_auth()).status_code == 200
    assert client.get(url).json()["name"] == "renamed"

    assert client.delete(url).status_code == 401
    assert client.get(url).status_code == 200
    assert client.delete(url, headers=_auth()).status_code == 204
    assert client.get(url).status_code == 404


def test_valid_token_does_not_bypass_patch_immutability() -> None:
    """Authentication never relaxes schema validation on PATCH."""

    client = TestClient(_fresh_app(api_token=_TOKEN))
    created = client.post(
        "/api/target-instances",
        json={"name": "authy", "target_type": "pdf-document"},
        headers=_auth(),
    )
    instance_id = created.json()["id"]

    response = client.patch(
        f"/api/target-instances/{instance_id}",
        json={"name": "x", "target_type": "zip-archive"},
        headers=_auth(),
    )
    assert response.status_code == 422
    assert client.get(f"/api/target-instances/{instance_id}").json() == created.json()


def test_reads_stay_unauthenticated_when_auth_enabled() -> None:
    client = TestClient(_fresh_app(api_token=_TOKEN))
    _job(client, headers=_auth())
    instance = client.post(
        "/api/target-instances",
        json={"name": "r", "target_type": "pdf-document"},
        headers=_auth(),
    ).json()

    for path in (
        "/health",
        "/api/targets",
        "/api/engines",
        "/api/jobs",
        "/api/jobs/stats",
        "/api/target-instances",
        f"/api/target-instances/{instance['id']}",
    ):
        assert client.get(path).status_code == 200


# --------------------------------------------------------------------------- #
# configuration and secrecy
# --------------------------------------------------------------------------- #


def test_explicit_token_wins_over_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PASSORCH_API_TOKEN", "environment-token")
    application = _fresh_app(api_token=_TOKEN)
    client = TestClient(application)

    assert application.state.auth_enabled is True
    assert _job(client, headers=_auth("environment-token")).status_code == 401
    assert _job(client, headers=_auth()).status_code == 200


def test_environment_token_enables_auth_and_blank_env_disables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PASSORCH_API_TOKEN", "  environment-token  ")
    from_env = _fresh_app()
    assert from_env.state.auth_enabled is True
    assert _job(TestClient(from_env)).status_code == 401
    assert _job(TestClient(from_env), headers=_auth("environment-token")).status_code == 200

    monkeypatch.setenv("PASSORCH_API_TOKEN", "   ")
    blank = _fresh_app()
    assert blank.state.auth_enabled is False
    assert _job(TestClient(blank)).status_code == 200

    monkeypatch.delenv("PASSORCH_API_TOKEN", raising=False)
    assert _fresh_app().state.auth_enabled is False


def test_blank_explicit_token_fails_fast() -> None:
    with pytest.raises(ValueError) as empty:
        create_app(api_token="")
    assert str(empty.value) == _EMPTY_TOKEN_ERROR

    with pytest.raises(ValueError, match="non-empty"):
        create_app(api_token="   ")


def test_token_is_never_exposed() -> None:
    application = _fresh_app(api_token=_TOKEN)
    client = TestClient(application)

    assert application.state.auth_enabled is True
    assert not hasattr(application.state, "api_token")

    document = json.dumps(application.openapi())
    assert _TOKEN not in document

    rejected = _job(client)
    assert rejected.status_code == 401
    assert _TOKEN not in rejected.text

    accepted = _job(client, headers=_auth())
    assert _TOKEN not in accepted.text


def test_auth_module_uses_constant_time_comparison() -> None:
    source = Path("app/api/auth.py").read_text(encoding="utf-8")
    assert "import secrets" in source
    assert "secrets.compare_digest" in source
    assert "provided == expected" not in source
    assert 'os.environ.get("PASSORCH_API_TOKEN"' in source


# --------------------------------------------------------------------------- #
# guard order and policy independence
# --------------------------------------------------------------------------- #


def test_rate_limit_runs_before_authentication() -> None:
    client = TestClient(_fresh_app(rate_limit=2, rate_window_seconds=60, api_token=_TOKEN))

    # Over budget without a token: rate limited, not a 401 leak.
    assert _job(client).status_code == 401
    assert _job(client).status_code == 401
    limited = _job(client)
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "60"


def test_authentication_runs_before_body_bound() -> None:
    client = TestClient(_fresh_app(max_body_bytes=100, api_token=_TOKEN))

    oversized = {"name": "x" * 300, "target_type": "pdf-document"}
    # Unauthenticated oversized mutation: 401 (no limit details leaked).
    assert (
        client.post("/api/target-instances", json=oversized).status_code == 401
    )
    # Authenticated oversized mutation: 413; small body passes.
    assert (
        client.post("/api/target-instances", json=oversized, headers=_auth()).status_code
        == 413
    )
    assert (
        client.post(
            "/api/target-instances",
            json={"name": "ok", "target_type": "pdf-document"},
            headers=_auth(),
        ).status_code
        == 201
    )


def test_auth_never_bypasses_validation_or_policy() -> None:
    client = TestClient(_fresh_app(api_token=_TOKEN))

    # Route validation still applies with a valid token.
    assert _job(client, headers=_auth(), target="bogus-target").status_code == 422

    # The execution policy still denies real mode as a normal job.
    denied = _job(client, headers=_auth(), mode="real")
    assert denied.status_code == 200
    assert denied.json()["status"] == "denied"


def test_auth_composes_with_persistence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("PASSORCH_JOB_DB", raising=False)
    db = tmp_path / "auth-jobs.db"
    client = TestClient(create_app(job_db_path=db, api_token=_TOKEN))

    assert _job(client).status_code == 401
    created = _job(client, headers=_auth())
    assert created.status_code == 200
    job_id = created.json()["job_id"]

    # Reads need no token and see the persisted job.
    assert client.get(f"/api/jobs/{job_id}").status_code == 200
    assert client.delete(f"/api/jobs/{job_id}").status_code == 401
    assert client.delete(f"/api/jobs/{job_id}", headers=_auth()).status_code == 204


# --------------------------------------------------------------------------- #
# documentation
# --------------------------------------------------------------------------- #


def test_readme_documents_authentication_and_deployment() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "## Authentication" in readme
    assert "## Deployment" in readme
    assert "PASSORCH_API_TOKEN" in readme
    assert "secrets.compare_digest" in readme
    assert "WWW-Authenticate" in readme
    assert "Authentication is not authorization" in readme
    assert "127.0.0.1" in readme
    assert "reverse proxy" in readme

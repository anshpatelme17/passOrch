"""Step 6 - one structured error envelope across every HTTP error family.

Locks the API error contract: every non-2xx body this application builds
(request-guard middleware and every route ``HTTPException``) is
``{"detail": {"error": "<code>", "detail": "<message>", ...}}`` with a
stable snake_case machine code and a short human sentence.  The single
documented exception - FastAPI's automatic request-body validation 422
array shape - is asserted here too so a future "helpful" rewrite cannot
silently change it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.auth import AUTHORIZATION_ERROR
from app.api.errors import (
    ENGINE_NOT_REGISTERED_ERROR,
    INVALID_REQUEST_ERROR,
    JOB_NOT_FOUND_ERROR,
    TARGET_INSTANCE_NOT_FOUND_ERROR,
    TARGET_NOT_MAPPED_ERROR,
    UNKNOWN_ENGINE_ERROR,
    api_error,
    engine_not_registered,
    error_body,
    instance_not_found,
    invalid_request,
    job_not_found,
    target_not_mapped,
    unknown_engine,
)
from app.api.rate_limit import (
    PAYLOAD_TOO_LARGE_ERROR,
    RATE_LIMIT_EXCEEDED_ERROR,
    UNSUPPORTED_TRANSFER_ENCODING_ERROR,
)
from app.engines.job_persistence import JobPersistenceError, PersistentJobStore
from app.engines.jobs import JobStore
from app.engines.target_instances import TargetInstanceStore

_ENVELOPE_CODE = re.compile(r"^[a-z][a-z0-9_]*$")


def _client(**kwargs: object) -> TestClient:
    return TestClient(create_app(**kwargs))  # type: ignore[arg-type]


def _assert_envelope(payload: object, *, code: str) -> dict[str, object]:
    """The payload is one error object with the standard envelope shape."""

    assert isinstance(payload, dict)
    assert set(payload) >= {"error", "detail"}
    assert payload["error"] == code
    assert _ENVELOPE_CODE.fullmatch(str(payload["error"]))
    assert isinstance(payload["detail"], str)
    assert payload["detail"]
    return payload


def _assert_clean(response: object) -> None:
    body = response.text.lower()  # type: ignore[attr-defined]
    assert "traceback" not in body
    assert "sqlite" not in body
    assert ".db" not in body


def test_error_body_helper_builds_the_envelope() -> None:
    body = error_body("some_code", "a message")
    assert body == {"error": "some_code", "detail": "a message"}


def test_error_body_extra_keys_never_overwrite_the_core_pair() -> None:
    body = error_body("some_code", "a message", error="evil", detail="evil", extra=1)
    assert body == {"error": "some_code", "detail": "a message", "extra": 1}


def test_api_error_helper_wraps_the_envelope_in_http_exception() -> None:
    exc = api_error(418, "teapot", "short and stout")
    assert exc.status_code == 418
    assert exc.detail == {"error": "teapot", "detail": "short and stout"}


def test_named_helpers_use_their_documented_codes_and_messages() -> None:
    assert job_not_found().detail == {
        "error": JOB_NOT_FOUND_ERROR,
        "detail": "job does not exist",
    }
    assert instance_not_found().detail == {
        "error": TARGET_INSTANCE_NOT_FOUND_ERROR,
        "detail": "target instance does not exist",
    }
    assert target_not_mapped().detail == {
        "error": TARGET_NOT_MAPPED_ERROR,
        "detail": "target is not mapped to an enabled format policy",
    }
    assert unknown_engine().detail == {
        "error": UNKNOWN_ENGINE_ERROR,
        "detail": "engine is not a known engine",
    }
    assert engine_not_registered().detail == {
        "error": ENGINE_NOT_REGISTERED_ERROR,
        "detail": "engine is not registered",
    }
    assert invalid_request("nope").detail == {
        "error": INVALID_REQUEST_ERROR,
        "detail": "nope",
    }


def test_guard_429_uses_the_envelope() -> None:
    client = _client(rate_limit=1, rate_window_seconds=60)
    assert client.post("/api/jobs", json={"target": "pdf-document"}).status_code == 200
    response = client.post("/api/jobs", json={"target": "pdf-document"})
    assert response.status_code == 429
    payload = _assert_envelope(
        response.json()["detail"], code=RATE_LIMIT_EXCEEDED_ERROR
    )
    assert payload["detail"] == "too many requests"
    assert payload["retry_after"] == 60
    assert response.headers["Retry-After"] == "60"
    _assert_clean(response)


def test_guard_401_uses_the_envelope() -> None:
    client = _client(api_token="s3cret-token")
    response = client.post("/api/jobs", json={"target": "pdf-document"})
    assert response.status_code == 401
    payload = _assert_envelope(response.json()["detail"], code=AUTHORIZATION_ERROR)
    assert payload["detail"] == "a valid bearer token is required"
    assert response.headers["WWW-Authenticate"] == "Bearer"
    _assert_clean(response)


def test_guard_413_uses_the_envelope() -> None:
    client = _client(max_body_bytes=16)
    response = client.post(
        "/api/target-instances",
        json={"name": "x" * 200, "target_type": "pdf-document"},
    )
    assert response.status_code == 413
    payload = _assert_envelope(response.json()["detail"], code=PAYLOAD_TOO_LARGE_ERROR)
    assert payload["detail"] == "request body exceeds the configured size limit"
    _assert_clean(response)


def test_guard_400_transfer_encoding_uses_the_envelope() -> None:
    client = _client()
    response = client.post(
        "/api/jobs",
        json={"target": "pdf-document"},
        headers={"Transfer-Encoding": "chunked"},
    )
    assert response.status_code == 400
    payload = _assert_envelope(
        response.json()["detail"], code=UNSUPPORTED_TRANSFER_ENCODING_ERROR
    )
    assert payload["detail"] == "transfer-encoded request bodies are not supported"
    _assert_clean(response)


def test_404_job_not_found_uses_the_envelope() -> None:
    client = _client(job_store=JobStore())
    for method, url in (
        ("get", "/api/jobs/not-a-uuid"),
        ("delete", "/api/jobs/not-a-uuid"),
    ):
        response = getattr(client, method)(url)
        assert response.status_code == 404
        _assert_envelope(response.json()["detail"], code=JOB_NOT_FOUND_ERROR)
        _assert_clean(response)


def test_404_instance_not_found_uses_the_envelope() -> None:
    client = _client(target_instance_store=TargetInstanceStore())
    for method, url in (
        ("get", "/api/target-instances/not-a-uuid"),
        ("delete", "/api/target-instances/not-a-uuid"),
        ("patch", "/api/target-instances/not-a-uuid"),
    ):
        kwargs: dict[str, object] = {"json": {"name": "x"}} if method == "patch" else {}
        response = getattr(client, method)(url, **kwargs)  # type: ignore[arg-type]
        assert response.status_code == 404
        _assert_envelope(
            response.json()["detail"], code=TARGET_INSTANCE_NOT_FOUND_ERROR
        )
        _assert_clean(response)


def test_404_target_not_mapped_uses_the_envelope() -> None:
    client = _client()
    response = client.post("/api/targets/nope/compatibility/plan", json={})
    assert response.status_code == 404
    _assert_envelope(response.json()["detail"], code=TARGET_NOT_MAPPED_ERROR)
    _assert_clean(response)


def test_404_engine_codes_use_the_envelope() -> None:
    client = _client()
    unknown = client.get("/api/engines/not-an-engine")
    assert unknown.status_code == 404
    _assert_envelope(unknown.json()["detail"], code=UNKNOWN_ENGINE_ERROR)
    _assert_clean(unknown)

    unregistered = client.get("/api/engines/john")
    assert unregistered.status_code == 404
    _assert_envelope(
        unregistered.json()["detail"], code=ENGINE_NOT_REGISTERED_ERROR
    )
    _assert_clean(unregistered)


def test_422_domain_and_query_rejections_use_the_envelope() -> None:
    client = _client(target_instance_store=TargetInstanceStore())
    created = client.post(
        "/api/target-instances", json={"name": "n", "target_type": "pdf-document"}
    ).json()

    empty_patch = client.patch(f"/api/target-instances/{created['id']}", json={})
    assert empty_patch.status_code == 422
    payload = _assert_envelope(empty_patch.json()["detail"], code=INVALID_REQUEST_ERROR)
    assert payload["detail"]

    bad_limit = client.get("/api/target-instances?limit=0")
    assert bad_limit.status_code == 422
    _assert_envelope(bad_limit.json()["detail"], code=INVALID_REQUEST_ERROR)

    bad_sort = client.get("/api/jobs?sort=banana")
    assert bad_sort.status_code == 422
    _assert_envelope(bad_sort.json()["detail"], code=INVALID_REQUEST_ERROR)


def test_422_unknown_target_keeps_its_structured_fields_in_the_envelope() -> None:
    client = _client(job_store=JobStore())
    response = client.post("/api/jobs", json={"target": "nope"})
    assert response.status_code == 422
    payload = _assert_envelope(response.json()["detail"], code="unknown_target")
    assert payload["target"] == "nope"
    assert "pdf-document" in payload["supported_targets"]
    _assert_clean(response)


def test_422_instance_association_errors_use_the_envelope() -> None:
    client = _client(
        job_store=JobStore(), target_instance_store=TargetInstanceStore()
    )
    created = client.post(
        "/api/target-instances", json={"name": "n", "target_type": "pdf-document"}
    ).json()

    absent = client.post(
        "/api/jobs",
        json={
            "target": "pdf-document",
            "instance_id": "00000000-0000-4000-8000-000000000000",
        },
    )
    assert absent.status_code == 422
    _assert_envelope(
        absent.json()["detail"], code=TARGET_INSTANCE_NOT_FOUND_ERROR
    )

    mismatch = client.post(
        "/api/jobs",
        json={"target": "zip-archive", "instance_id": created["id"]},
    )
    assert mismatch.status_code == 422
    payload = _assert_envelope(
        mismatch.json()["detail"], code="target_instance_type_mismatch"
    )
    assert payload["target"] == "zip-archive"
    assert payload["instance_target_type"] == "pdf-document"
    _assert_clean(mismatch)


def test_pydantic_422_keeps_the_default_array_shape() -> None:
    """The one documented exception: FastAPI body validation stays an array."""

    client = _client(job_store=JobStore())
    response = client.post("/api/jobs", json={"target": "pdf-document", "extra": 1})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    assert detail
    assert set(detail[0]) >= {"loc", "msg", "type"}


def test_503_persistence_unavailable_uses_the_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PersistentJobStore(tmp_path / "jobs.db")
    client = _client(job_store=store)

    def _explode(job: object) -> None:
        raise JobPersistenceError("failed to write a job record")

    monkeypatch.setattr(store._db, "insert_job", _explode)
    response = client.post("/api/jobs", json={"target": "pdf-document"})
    assert response.status_code == 503
    payload = _assert_envelope(
        response.json()["detail"], code="persistence_unavailable"
    )
    assert payload["detail"] == "job storage is temporarily unavailable"
    _assert_clean(response)
    assert str(tmp_path).lower() not in response.text.lower()


def test_503_readiness_uses_the_envelope() -> None:
    client = _client()

    def _boom() -> list[object]:
        raise RuntimeError("storage failure")

    client.app.state.job_store.list = _boom  # type: ignore[method-assign]
    response = client.get("/health/ready")
    assert response.status_code == 503
    payload = _assert_envelope(response.json()["detail"], code="not_ready")
    assert payload["detail"] == "readiness probe failed"
    _assert_clean(response)


def test_200_bodies_are_never_wrapped_in_the_error_envelope() -> None:
    client = _client(job_store=JobStore())
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "mode": "simulated"}

    created = client.post("/api/jobs", json={"target": "pdf-document"})
    assert created.status_code == 200
    assert "error" not in created.json()

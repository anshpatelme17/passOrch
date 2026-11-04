"""Steps 12/13B follow-up - the shared fail-closed store-resolution helpers.

Direct tests for ``app.api.store_resolution``.  The HTTP-facing fail-closed
behavior is asserted by the route regression suites (``tests.test_api_jobs``,
``tests.test_target_instances``, ``tests.test_operational_health``); these
tests pin the shared helper contract itself: correct store returned, absent
or ``None`` store fails closed with the same generic ``503
persistence_unavailable`` envelope as a wrong-typed store - never a silent
fallback to the process-wide default - and a wrong-typed store fails closed
with the one generic ``503 persistence_unavailable`` envelope - never a
details leak.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.api.app import create_app
from app.api.store_resolution import (
    PERSISTENCE_ERROR,
    job_store_from_state,
    persistence_unavailable_detail,
    resolve_job_store,
    resolve_target_instance_store,
    target_instance_store_from_state,
)
from app.engines.job_persistence import PersistentJobStore
from app.engines.jobs import JobStore, default_job_store
from app.engines.target_instances import (
    TargetInstanceStore,
    default_target_instance_store,
)


class _LeakyStore:
    """A wrong-type store: present, never a JobStore, never read or written."""

    def __repr__(self) -> str:
        return "LEAK-THIS"


class _ForeignInstanceStore:
    """A wrong-type store standing in for a miswired instance store."""

    def __repr__(self) -> str:
        return "LEAK-THIS"


class _EmptyState:
    """A state with no store attributes at all."""


class _NoneState:
    """A state where both store attributes exist but are ``None``."""

    job_store: Any = None
    target_instance_store: Any = None


def _request_for(app: object) -> Request:
    return Request({"type": "http", "app": app})


# --------------------------------------------------------------------------- #
# correctly typed stores
# --------------------------------------------------------------------------- #


def test_correctly_typed_job_store_is_returned() -> None:
    app = create_app(job_store=JobStore(), target_instance_store=TargetInstanceStore())
    assert resolve_job_store(_request_for(app)) is app.state.job_store


def test_correctly_typed_target_instance_store_is_returned() -> None:
    app = create_app(job_store=JobStore(), target_instance_store=TargetInstanceStore())
    assert resolve_target_instance_store(_request_for(app)) is app.state.target_instance_store


def test_persistent_job_store_is_returned_for_a_configured_path(tmp_path: Any) -> None:
    app = create_app(job_db_path=tmp_path / "x.db")
    assert isinstance(resolve_job_store(_request_for(app)), PersistentJobStore)


# --------------------------------------------------------------------------- #
# absent / None stores fail closed (the process-wide default is never used)
# --------------------------------------------------------------------------- #


def test_missing_store_fails_closed_with_the_shared_503() -> None:
    for helper, detail in (
        (job_store_from_state, persistence_unavailable_detail()),
        (target_instance_store_from_state, persistence_unavailable_detail("target instance")),
    ):
        with pytest.raises(HTTPException) as excinfo:
            helper(_EmptyState())
        assert excinfo.value.status_code == 503
        assert excinfo.value.detail == detail


def test_none_store_fails_closed_the_same_way() -> None:
    for helper, detail in (
        (job_store_from_state, persistence_unavailable_detail()),
        (target_instance_store_from_state, persistence_unavailable_detail("target instance")),
    ):
        with pytest.raises(HTTPException) as excinfo:
            helper(_NoneState())
        assert excinfo.value.status_code == 503
        assert excinfo.value.detail == detail


def test_missing_store_never_falls_back_to_the_process_default() -> None:
    jobs_before = len(default_job_store())
    instances_before = len(default_target_instance_store())
    for helper in (job_store_from_state, target_instance_store_from_state):
        for state in (_EmptyState(), _NoneState()):
            with pytest.raises(HTTPException):
                helper(state)
    # The process-wide singletons were not substituted and nothing was written.
    assert len(default_job_store()) == jobs_before
    assert len(default_target_instance_store()) == instances_before


def test_resolve_helpers_fail_closed_when_state_is_miswired() -> None:
    app = create_app(job_store=JobStore(), target_instance_store=TargetInstanceStore())
    app.state.job_store = None
    with pytest.raises(HTTPException) as excinfo:
        resolve_job_store(_request_for(app))
    assert excinfo.value.status_code == 503
    assert excinfo.value.detail == persistence_unavailable_detail()

    app.state.target_instance_store = 123
    with pytest.raises(HTTPException) as excinfo:
        resolve_target_instance_store(_request_for(app))
    assert excinfo.value.status_code == 503


# --------------------------------------------------------------------------- #
# independent application instances
# --------------------------------------------------------------------------- #


def test_independent_apps_use_their_own_stores() -> None:
    first = create_app(job_store=JobStore(), target_instance_store=TargetInstanceStore())
    second = create_app(job_store=JobStore(), target_instance_store=TargetInstanceStore())
    first_client = TestClient(first)
    second_client = TestClient(second)

    res = first_client.post(
        "/api/jobs", json={"target": "pdf-document", "reference": "app-a"}
    )
    assert res.status_code == 200
    job_id = res.json()["job_id"]

    # The other instance cannot see the job; neither leaks into the default.
    assert second_client.get(f"/api/jobs/{job_id}").status_code == 404
    assert first_client.get(f"/api/jobs/{job_id}").status_code == 200
    assert default_job_store().get(job_id) is None


def test_miswired_app_answers_generic_503_and_never_uses_default() -> None:
    app = create_app(job_store=JobStore(), target_instance_store=TargetInstanceStore())
    app.state.job_store = None
    client = TestClient(app)

    res = client.post("/api/jobs", json={"target": "pdf-document"})
    assert res.status_code == 503
    assert res.json() == {
        "detail": {
            "error": PERSISTENCE_ERROR,
            "detail": "job storage is temporarily unavailable",
        }
    }

    app.state.target_instance_store = None
    res = client.post(
        "/api/target-instances",
        json={"name": "x", "target_type": "pdf-document"},
    )
    assert res.status_code == 503
    assert res.json() == {
        "detail": {
            "error": PERSISTENCE_ERROR,
            "detail": "target instance storage is temporarily unavailable",
        }
    }


def test_readiness_answers_generic_503_when_a_store_is_missing() -> None:
    app = create_app(job_store=JobStore(), target_instance_store=TargetInstanceStore())
    client = TestClient(app)

    app.state.job_store = None
    assert client.get("/health/ready").json() == {
        "detail": {"error": "not_ready", "detail": "readiness probe failed"}
    }

    app.state.job_store = JobStore()
    app.state.target_instance_store = None
    assert client.get("/health/ready").status_code == 503


# --------------------------------------------------------------------------- #
# wrong-typed stores fail closed
# --------------------------------------------------------------------------- #


def test_wrong_type_job_store_is_rejected_with_the_shared_503() -> None:
    app = create_app(job_store=JobStore(), target_instance_store=TargetInstanceStore())
    app.state.job_store = _LeakyStore()
    with pytest.raises(HTTPException) as excinfo:
        resolve_job_store(_request_for(app))
    assert excinfo.value.status_code == 503
    assert excinfo.value.detail == persistence_unavailable_detail()
    assert excinfo.value.detail == {
        "error": PERSISTENCE_ERROR,
        "detail": "job storage is temporarily unavailable",
    }
    # the offending object must not be named, repr'd or leaked
    assert "LEAK-THIS" not in str(excinfo.value.detail)


def test_wrong_type_job_store_is_rejected_on_the_state_helper_too() -> None:
    state = _EmptyState()
    state.job_store = _LeakyStore()  # type: ignore[attr-defined]
    with pytest.raises(HTTPException) as excinfo:
        job_store_from_state(state)
    assert excinfo.value.status_code == 503
    assert "LEAK-THIS" not in str(excinfo.value.detail)


def test_wrong_type_target_instance_store_uses_the_target_subject() -> None:
    app = create_app(job_store=JobStore(), target_instance_store=TargetInstanceStore())
    app.state.target_instance_store = _ForeignInstanceStore()
    with pytest.raises(HTTPException) as excinfo:
        resolve_target_instance_store(_request_for(app))
    assert excinfo.value.status_code == 503
    assert excinfo.value.detail == persistence_unavailable_detail("target instance")
    assert excinfo.value.detail == {
        "error": PERSISTENCE_ERROR,
        "detail": "target instance storage is temporarily unavailable",
    }
    assert "LEAK-THIS" not in str(excinfo.value.detail)


def test_wrong_type_target_instance_store_fails_closed_on_the_state_helper() -> None:
    state = _EmptyState()
    state.target_instance_store = _ForeignInstanceStore()  # type: ignore[attr-defined]
    with pytest.raises(HTTPException) as excinfo:
        target_instance_store_from_state(state)
    assert excinfo.value.status_code == 503
    assert "LEAK-THIS" not in str(excinfo.value.detail)


# --------------------------------------------------------------------------- #
# readiness keeps its contract through the shared helpers
# --------------------------------------------------------------------------- #


def test_readiness_answers_200_for_validly_typed_stores() -> None:
    app = create_app(job_store=JobStore(), target_instance_store=TargetInstanceStore())
    client = TestClient(app)
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mode": "simulated"}


def test_readiness_answers_generic_503_for_a_miswired_job_store() -> None:
    app = create_app(job_store=JobStore(), target_instance_store=TargetInstanceStore())
    app.state.job_store = _LeakyStore()
    client = TestClient(app)
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {
        "detail": {"error": "not_ready", "detail": "readiness probe failed"}
    }

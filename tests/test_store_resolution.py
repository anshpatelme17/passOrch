"""Steps 12/13B follow-up - the shared fail-closed store-resolution helpers.

Direct tests for ``app.api.store_resolution``.  The HTTP-facing fail-closed
behavior is asserted by the route regression suites (``tests.test_api_jobs``,
``tests.test_target_instances``, ``tests.test_operational_health``); these
tests pin the shared helper contract itself: correct store returned, absent or
``None`` store falls back to the process-wide default, and a wrong-typed store
fails closed with the one generic ``503 persistence_unavailable`` envelope -
never a silent fallback, never a details leak.
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
# absent / None stores keep the historical fallback
# --------------------------------------------------------------------------- #


def test_missing_store_falls_back_to_the_process_default() -> None:
    assert job_store_from_state(_EmptyState()) is default_job_store()
    assert target_instance_store_from_state(_EmptyState()) is default_target_instance_store()


def test_none_store_falls_back_the_same_way() -> None:
    assert job_store_from_state(_NoneState()) is default_job_store()
    assert target_instance_store_from_state(_NoneState()) is default_target_instance_store()


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

"""Phase 9D/3 - operational health, logging, and recovery contracts.

Locks in the operational audit:

* ``GET /health`` is a minimal liveness probe - exactly ``{status, mode}``,
  nothing path/secret/traceback shaped;
* ``GET /health/ready`` is a read-only readiness probe: both stores must
  answer an in-memory listing; a store failure is one generic ``503
  not_ready`` with no exception text, SQL, path or traceback, and the
  probe never writes or opens SQLite;
* persistence runs in SQLite's default rollback-journal mode: no WAL/SHM
  sidecar files appear next to a store that has accepted writes (the
  operations documentation must not claim otherwise);
* README's Operations section documents health semantics, the
  no-application-logging contract, shutdown, and backup/restore.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.engines.instance_persistence import PersistentTargetInstanceStore
from app.engines.job_persistence import PersistentJobStore
from app.engines.jobs import AuditJob, JobStore, JobStatus
from app.engines.target_instances import TargetInstanceStore

_READY_BODY = {"status": "ok", "mode": "simulated"}


class _ExplodingJobStore(JobStore):
    """A job store whose listing fails - readiness must fail closed."""

    def list(self):
        raise RuntimeError("sqlite3.OperationalError: no such table: jobs /data/secret.db")


class _ExplodingInstanceStore(TargetInstanceStore):
    def list(self):
        raise RuntimeError("boom internals must never reach the client")


# -- wrongly typed stores (readiness hardening) ------------------------------- #


class _ForeignJobStore:
    """A wrong-typed object with a working ``list()`` that must not pass."""

    def list(self):
        return []


class _ForeignInstanceStore:
    """A wrong-typed object with a working ``list()`` that must not pass."""

    def list(self):
        return []


def test_health_response_is_exactly_the_documented_liveness_body() -> None:
    client = TestClient(
        create_app(job_store=JobStore(), target_instance_store=TargetInstanceStore())
    )
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == _READY_BODY
    text = response.text.lower()
    for leaked in (".db", "sqlite", "traceback", "token", "secret", "/data"):
        assert leaked not in text


def test_readiness_answers_ready_for_healthy_stores_with_hardened_headers() -> None:
    client = TestClient(
        create_app(job_store=JobStore(), target_instance_store=TargetInstanceStore())
    )

    for _ in range(3):
        response = client.get("/health/ready")
        assert response.status_code == 200
        assert response.json() == _READY_BODY
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        text = response.text.lower()
        for leaked in (".db", "sqlite", "traceback", "token", "secret", "/data"):
            assert leaked not in text


def test_readiness_answers_generic_503_when_a_store_fails() -> None:
    for jobs, instances in (
        (_ExplodingJobStore(), TargetInstanceStore()),
        (JobStore(), _ExplodingInstanceStore()),
    ):
        client = create_app(job_store=jobs, target_instance_store=instances)
        response = TestClient(client).get("/health/ready")

        assert response.status_code == 503
        assert response.json() == {
            "detail": {"error": "not_ready", "detail": "readiness probe failed"}
        }
        text = response.text.lower()
        for leaked in ("sqlite", "traceback", "secret.db", "no such table", "boom internals"):
            assert leaked not in text


def test_readiness_answers_generic_503_for_wrongly_typed_stores() -> None:
    """A foreign object with a working list() must not make readiness healthy.

    Readiness must fail closed on a type mismatch, not call into the foreign
    object (which could be a miswired store, a different subsystem, or an
    attacker-controlled object placed on application state).
    """

    # Mutate state post-construction to simulate a miswired application.
    client = TestClient(
        create_app(job_store=JobStore(), target_instance_store=TargetInstanceStore())
    )
    client.app.state.target_instance_store = _ForeignInstanceStore()
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {
        "detail": {"error": "not_ready", "detail": "readiness probe failed"}
    }
    text = response.text.lower()
    for leaked in ("foreign", "traceback", "typeerror", "target_instance_store"):
        assert leaked not in text

    client = TestClient(
        create_app(job_store=JobStore(), target_instance_store=TargetInstanceStore())
    )
    client.app.state.job_store = _ForeignJobStore()
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {
        "detail": {"error": "not_ready", "detail": "readiness probe failed"}
    }
    text = response.text.lower()
    for leaked in ("foreign", "traceback", "typeerror", "job_store"):
        assert leaked not in text

    # Both wrong at once.
    client = TestClient(
        create_app(job_store=JobStore(), target_instance_store=TargetInstanceStore())
    )
    client.app.state.job_store = _ForeignJobStore()
    client.app.state.target_instance_store = _ForeignInstanceStore()
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {
        "detail": {"error": "not_ready", "detail": "readiness probe failed"}
    }


def test_readiness_probe_never_writes_or_opens_the_database(tmp_path: Path) -> None:
    db = tmp_path / "ready.db"
    jobs = PersistentJobStore(db, capacity=4)
    instances = PersistentTargetInstanceStore(db, capacity=4)
    client = TestClient(
        create_app(job_store=jobs, target_instance_store=instances)
    )

    for _ in range(5):
        assert client.get("/health/ready").status_code == 200
        assert client.get("/health").status_code == 200

    snapshot = db.read_bytes()
    stat = db.stat()
    assert snapshot  # the database file exists after construction
    assert db.read_bytes() == snapshot
    assert db.stat().st_mtime_ns == stat.st_mtime_ns
    assert not Path(str(db) + "-wal").exists()
    assert not Path(str(db) + "-shm").exists()


def test_readme_and_limitations_document_readiness_and_retention() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "/health/ready" in readme
    assert "not_ready" in readme
    assert "readiness" in readme

    limitations = Path("docs/LIMITATIONS.md").read_text(encoding="utf-8")
    assert "Capacity-bounded retention" in limitations
    assert "default 100" in limitations
    assert "one-way" in limitations


def test_writes_leave_no_wal_or_shm_sidecar_files(tmp_path: Path) -> None:
    db = tmp_path / "ops.db"
    store = PersistentJobStore(db, capacity=4)
    store.put(
        AuditJob(
            job_id=str(uuid.uuid4()),
            status=JobStatus.FAILED,
            created_at=1_700_000_000,
            target="pdf-document",
            failure_reason="no_preview",
        )
    )

    assert db.is_file()
    assert not Path(str(db) + "-wal").exists()
    assert not Path(str(db) + "-shm").exists()
    connection = sqlite3.connect(db)
    try:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()
    assert journal_mode == "delete"


def test_readme_operations_section_documents_verified_behavior() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "## Operations" in readme
    assert "liveness" in readme
    assert "never log" in readme
    assert "backup_database" in readme
    # The earlier false claim (WAL sidecars do not exist here) stays fixed.
    assert "including WAL/SHM sidecars" not in readme
    assert "default rollback-journal" in readme or "rollback-journal mode" in readme

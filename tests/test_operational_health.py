"""Phase 9D - operational health, logging, and recovery contracts.

Locks in the operational audit:

* ``GET /health`` is a minimal liveness probe - exactly ``{status, mode}``,
  nothing path/secret/traceback shaped;
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
from app.engines.job_persistence import PersistentJobStore
from app.engines.jobs import AuditJob, JobStore, JobStatus
from app.engines.target_instances import TargetInstanceStore


def test_health_response_is_exactly_the_documented_liveness_body() -> None:
    client = TestClient(
        create_app(job_store=JobStore(), target_instance_store=TargetInstanceStore())
    )
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mode": "simulated"}
    text = response.text.lower()
    for leaked in (".db", "sqlite", "traceback", "token", "secret", "/data"):
        assert leaked not in text


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

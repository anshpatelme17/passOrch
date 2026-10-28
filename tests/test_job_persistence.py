"""Phase 8A - persistent job history (SQLite) tests.

Covers:
* durability: jobs written through one application reopen with identical
  API output and identical typed records; filters, sorting, pagination and
  instance-association filters work after a restart;
* lifecycle: shared and isolated databases, PASSORCH_JOB_DB wiring,
  default in-memory behaviour, mutual exclusion, capacity hydration prune,
  delete/reset durability, connections released between operations;
* failure: unusable paths, corrupt files and invalid or forbidden rows
  raise JobPersistenceError (never a silent memory-only fallback); write
  failures answer HTTP 503 with a generic structured detail and leave the
  in-memory view truthful;
* safety: persisted payloads carry no forbidden command/argv/path keys,
  reads never touch the database, and README documents the feature.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
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
from app.engines.job_persistence import (
    JobPersistenceError,
    PersistentJobStore,
    configured_job_store,
)
from app.engines.jobs import (
    FORBIDDEN_JOB_FIELDS,
    AuditJob,
    DuplicateJobError,
    JobStatus,
    JobStore,
)
from app.engines.target_instances import TargetInstanceStore


def _registry() -> DiscoveryRegistry:
    registry = DiscoveryRegistry()
    registry.register(EngineSnapshot(engine=JOHN, discovered=True, binary_found=True))
    registry.set_capabilities(
        JOHN,
        EngineCapabilities(
            engine=JOHN,
            capability_status=CapabilityStatus.VERIFIED,
            formats=("pdf", "zip"),
        ),
    )
    return registry


def _client(db_path: Path) -> TestClient:
    return TestClient(
        create_app(
            discovery_registry=_registry(),
            job_db_path=db_path,
        )
    )


def _store_client(store: JobStore) -> TestClient:
    return TestClient(
        create_app(
            discovery_registry=_registry(),
            job_store=store,
            target_instance_store=TargetInstanceStore(),
        )
    )


def _create_job(
    client: TestClient,
    *,
    target: str = "pdf-document",
    mode: str = "simulated",
    instance_id: str | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {"target": target, "mode": mode}
    if instance_id is not None:
        body["instance_id"] = instance_id
    response = client.post("/api/jobs", json=body)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert isinstance(payload, dict)
    return payload


def _job_id(created: dict[str, object]) -> str:
    identifier = created["job_id"]
    assert isinstance(identifier, str)
    return identifier


def _failed_job(target: str = "pdf-document", created_at: int = 1_700_000_000) -> AuditJob:
    return AuditJob(
        job_id=str(uuid.uuid4()),
        status=JobStatus.FAILED,
        created_at=created_at,
        target=target,
        failure_reason="no_preview",
    )


def _execute(db_path: Path, sql: str, parameters: tuple[object, ...] = ()) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(sql, parameters)
        connection.commit()
    finally:
        connection.close()


def _fetch_payload(db_path: Path, job_id: str) -> str:
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            "SELECT payload FROM passorch_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return str(row[0])


def _row_count(db_path: Path) -> int:
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute("SELECT COUNT(*) FROM passorch_jobs").fetchone()
    finally:
        connection.close()
    assert row is not None
    return int(row[0])


def _forbidden_keys(payload: object) -> set[str]:
    found: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in FORBIDDEN_JOB_FIELDS:
                found.add(key)
            found |= _forbidden_keys(value)
    elif isinstance(payload, list):
        for item in payload:
            found |= _forbidden_keys(item)
    return found


# --------------------------------------------------------------------------- #
# durability across restarts
# --------------------------------------------------------------------------- #


def test_persisted_jobs_survive_reopen_with_identical_api_output(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    first = _client(db)
    created_a = _create_job(first)
    created_b = _create_job(first, mode="real")
    created_c = _create_job(first, target="zip-archive")

    before = {
        "list": first.get("/api/jobs").json(),
        "detail": first.get(f"/api/jobs/{_job_id(created_a)}").json(),
        "stats": first.get("/api/jobs/stats").json(),
    }

    second = _client(db)
    after = {
        "list": second.get("/api/jobs").json(),
        "detail": second.get(f"/api/jobs/{_job_id(created_a)}").json(),
        "stats": second.get("/api/jobs/stats").json(),
    }

    assert after == before
    assert [job["job_id"] for job in after["list"]["jobs"]] == [
        _job_id(created_c),
        _job_id(created_b),
        _job_id(created_a),
    ]


def test_reopened_store_returns_identical_typed_records(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    store = PersistentJobStore(db)
    client = _store_client(store)
    simulated = _create_job(client)
    denied = _create_job(client, mode="real")
    failed = _failed_job()
    store.put(failed)

    original_simulated = store.get(_job_id(simulated))
    original_denied = store.get(_job_id(denied))
    original_failed = store.get(failed.job_id)
    assert original_simulated is not None
    assert original_denied is not None
    assert original_failed is not None
    assert original_simulated.preview is not None

    reopened = PersistentJobStore(db)
    restored_simulated = reopened.get(_job_id(simulated))
    restored_denied = reopened.get(_job_id(denied))
    restored_failed = reopened.get(failed.job_id)
    assert restored_simulated == original_simulated
    assert restored_denied == original_denied
    assert restored_failed == original_failed
    assert restored_simulated is not None
    assert restored_simulated.preview is not None
    assert restored_simulated.result() == original_simulated.result()
    assert len(reopened) == 3


def test_filters_sort_and_pagination_work_after_reopen(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    first = _client(db)
    instance = first.post(
        "/api/target-instances",
        json={"name": "quarterly", "target_type": "pdf-document"},
    )
    assert instance.status_code == 201, instance.text
    instance_id = instance.json()["id"]

    oldest = _create_job(first)
    associated = _create_job(first, mode="real", instance_id=instance_id)
    newest = _create_job(first, target="zip-archive")

    second = _client(db)

    by_status = second.get("/api/jobs", params={"status": "denied"}).json()
    assert by_status["total"] == 1
    assert by_status["jobs"][0]["job_id"] == _job_id(associated)

    by_target = second.get("/api/jobs", params={"target": "zip-archive"}).json()
    assert by_target["total"] == 1
    assert by_target["jobs"][0]["job_id"] == _job_id(newest)

    by_instance = second.get("/api/jobs", params={"instance_id": instance_id}).json()
    assert by_instance["total"] == 1
    assert by_instance["jobs"][0]["job_id"] == _job_id(associated)

    unassociated = second.get("/api/jobs", params={"instance_id": ""}).json()
    assert unassociated["total"] == 2

    ascending = second.get("/api/jobs", params={"sort": "created_asc", "limit": 1}).json()
    assert ascending["total"] == 3
    assert ascending["count"] == 1
    assert ascending["jobs"][0]["job_id"] == _job_id(oldest)

    second_page = second.get(
        "/api/jobs", params={"sort": "created_asc", "limit": 1, "offset": 1}
    ).json()
    assert second_page["jobs"][0]["job_id"] == _job_id(associated)


def test_persisted_payloads_contain_no_forbidden_fields(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    client = _client(db)
    created = _create_job(client)

    payload = json.loads(_fetch_payload(db, _job_id(created)))
    assert not _forbidden_keys(payload)
    assert not _forbidden_keys(client.get(f"/api/jobs/{_job_id(created)}").json())
    assert not _forbidden_keys(client.get("/api/jobs").json())


# --------------------------------------------------------------------------- #
# configuration and isolation
# --------------------------------------------------------------------------- #


def test_default_create_app_stays_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PASSORCH_JOB_DB", raising=False)
    application = create_app()
    assert type(application.state.job_store) is JobStore
    assert not isinstance(application.state.job_store, PersistentJobStore)


def test_configured_job_store_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PASSORCH_JOB_DB", raising=False)
    assert configured_job_store(None) is None

    explicit = configured_job_store(tmp_path / "explicit.db")
    assert isinstance(explicit, PersistentJobStore)
    assert explicit.db_path == tmp_path / "explicit.db"

    env_path = tmp_path / "env.db"
    monkeypatch.setenv("PASSORCH_JOB_DB", str(env_path))
    chosen = configured_job_store(tmp_path / "wins.db")
    assert isinstance(chosen, PersistentJobStore)
    assert chosen.db_path == tmp_path / "wins.db"
    assert not env_path.exists()

    from_env = configured_job_store(None)
    assert isinstance(from_env, PersistentJobStore)
    assert env_path.exists()

    monkeypatch.setenv("PASSORCH_JOB_DB", "   ")
    assert configured_job_store(None) is None

    with pytest.raises(JobPersistenceError):
        configured_job_store("   ")


def test_environment_variable_enables_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PASSORCH_JOB_DB", str(tmp_path / "env.db"))
    first = TestClient(
        create_app(
            discovery_registry=_registry(),
            target_instance_store=TargetInstanceStore(),
        )
    )
    assert isinstance(first.app.state.job_store, PersistentJobStore)
    created = _create_job(first)

    second = TestClient(
        create_app(
            discovery_registry=_registry(),
            target_instance_store=TargetInstanceStore(),
        )
    )
    detail = second.get(f"/api/jobs/{_job_id(created)}")
    assert detail.status_code == 200
    assert detail.json() == created


def test_job_store_and_job_db_path_are_mutually_exclusive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        create_app(job_store=JobStore(), job_db_path=tmp_path / "jobs.db")


def test_two_application_instances_share_one_database(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    writer = _client(db)
    created = _create_job(writer)

    reader = _client(db)
    detail = reader.get(f"/api/jobs/{_job_id(created)}")
    assert detail.status_code == 200
    assert detail.json() == created
    assert reader.get("/api/jobs").json()["total"] == 1


def test_separate_databases_are_isolated(tmp_path: Path) -> None:
    one = _client(tmp_path / "one.db")
    two = _client(tmp_path / "two.db")
    created = _create_job(one)

    assert two.get(f"/api/jobs/{_job_id(created)}").status_code == 404
    assert two.get("/api/jobs").json()["total"] == 0
    assert one.get("/api/jobs").json()["total"] == 1


# --------------------------------------------------------------------------- #
# lifecycle: capacity, deletion, reset, released connections
# --------------------------------------------------------------------------- #


def test_capacity_hydration_keeps_newest_and_prunes_database(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    store = PersistentJobStore(db, capacity=2)
    jobs = [_failed_job(created_at=index) for index in range(3)]
    for job in jobs:
        store.put(job)

    assert [item.job_id for item in store.list()] == [jobs[2].job_id, jobs[1].job_id]
    assert _row_count(db) == 2

    reopened = PersistentJobStore(db, capacity=2)
    assert [item.job_id for item in reopened.list()] == [jobs[2].job_id, jobs[1].job_id]
    assert _row_count(db) == 2

    shrunk = PersistentJobStore(db, capacity=1)
    assert [item.job_id for item in shrunk.list()] == [jobs[2].job_id]
    assert _row_count(db) == 1


def test_deletion_survives_restart(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    first = _client(db)
    created = _create_job(first)
    assert first.delete(f"/api/jobs/{_job_id(created)}").status_code == 204

    second = _client(db)
    assert second.get(f"/api/jobs/{_job_id(created)}").status_code == 404
    assert second.get("/api/jobs").json()["total"] == 0


def test_reset_clears_persisted_rows(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    store = PersistentJobStore(db)
    store.put(_failed_job())
    assert len(store) == 1

    store.reset()
    assert len(store) == 0
    assert _row_count(db) == 0

    assert len(PersistentJobStore(db)) == 0


def test_connections_are_released_between_operations(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    client = _client(db)
    created = _create_job(client)
    assert client.get("/api/jobs").status_code == 200
    assert client.delete(f"/api/jobs/{_job_id(created)}").status_code == 204

    # A second writer must be able to take the file's write lock immediately:
    # no operation may hold a connection open past its own call.
    concurrent = PersistentJobStore(db)
    concurrent.put(_failed_job())
    assert len(concurrent) == 1


# --------------------------------------------------------------------------- #
# failure behaviour: fail closed, structured 503, no fabricated state
# --------------------------------------------------------------------------- #


def test_create_app_fails_fast_on_unusable_or_blank_path(tmp_path: Path) -> None:
    directory = tmp_path / "not-a-file"
    directory.mkdir()
    with pytest.raises(JobPersistenceError):
        create_app(job_db_path=directory)
    with pytest.raises(JobPersistenceError):
        create_app(job_db_path="   ")


def test_opening_a_directory_path_fails_closed(tmp_path: Path) -> None:
    directory = tmp_path / "not-a-file"
    directory.mkdir()
    with pytest.raises(JobPersistenceError):
        PersistentJobStore(directory)


def test_corrupt_database_file_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    db.write_bytes(b"this is definitely not sqlite" * 16)
    with pytest.raises(JobPersistenceError) as error:
        PersistentJobStore(db)
    assert str(tmp_path) not in str(error.value)


def test_unreadable_persisted_row_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    store = PersistentJobStore(db)
    job = _failed_job()
    store.put(job)

    _execute(
        db,
        "UPDATE passorch_jobs SET payload = ? WHERE job_id = ?",
        ("{ this is not json", job.job_id),
    )

    with pytest.raises(JobPersistenceError):
        PersistentJobStore(db)


def test_row_with_forbidden_keys_anywhere_is_rejected(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    store = PersistentJobStore(db)
    client = _store_client(store)
    created = _create_job(client)

    record = json.loads(_fetch_payload(db, _job_id(created)))
    record["preview"]["request"]["command"] = "id"
    _execute(
        db,
        "UPDATE passorch_jobs SET payload = ? WHERE job_id = ?",
        (json.dumps(record), _job_id(created)),
    )

    with pytest.raises(JobPersistenceError):
        PersistentJobStore(db)


def test_write_failure_returns_structured_503_without_leaking_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "jobs.db"
    store = PersistentJobStore(db)
    client = _store_client(store)

    def _explode(job: AuditJob) -> None:
        raise JobPersistenceError("failed to write a job record")

    monkeypatch.setattr(store._db, "insert_job", _explode)
    response = client.post("/api/jobs", json={"target": "pdf-document"})
    assert response.status_code == 503
    assert response.json()["detail"] == {
        "error": "persistence_unavailable",
        "detail": "job storage is temporarily unavailable",
    }
    body = response.text.lower()
    assert "sqlite" not in body
    assert "traceback" not in body
    assert str(tmp_path).lower() not in body

    # a failed write is never fabricated as a stored job
    assert len(store) == 0
    assert client.get("/api/jobs").json()["total"] == 0


def test_delete_failure_returns_structured_503_and_keeps_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PersistentJobStore(tmp_path / "jobs.db")
    client = _store_client(store)
    created = _create_job(client)

    def _explode(job_id: str) -> bool:
        raise JobPersistenceError("failed to delete a job record")

    monkeypatch.setattr(store._db, "delete_job", _explode)
    response = client.delete(f"/api/jobs/{_job_id(created)}")
    assert response.status_code == 503
    assert response.json()["detail"] == {
        "error": "persistence_unavailable",
        "detail": "job storage is temporarily unavailable",
    }
    assert store.get(_job_id(created)) is not None
    assert client.get("/api/jobs").json()["total"] == 1


def test_database_level_duplicate_job_id_raises_duplicate_error(tmp_path: Path) -> None:
    store = PersistentJobStore(tmp_path / "jobs.db")
    job = _failed_job()
    store.put(job)

    with pytest.raises(DuplicateJobError):
        store._db.insert_job(job)


# --------------------------------------------------------------------------- #
# atomic eviction mirror: insert + FIFO-row delete in ONE transaction
# --------------------------------------------------------------------------- #


def test_eviction_delete_failure_rolls_the_insert_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed victim-row delete must not leave the new row committed.

    The fault is injected at the SQL layer (a statement against a
    nonexistent table) so the real sqlite driver fails during the
    eviction stage of the combined transaction, exercising its actual
    rollback behaviour instead of mocking the whole operation.
    """

    db = tmp_path / "jobs.db"
    store = PersistentJobStore(db, capacity=2)
    older = _failed_job(created_at=1)
    newer = _failed_job(created_at=2)
    store.put(older)
    store.put(newer)

    monkeypatch.setattr(
        "app.engines.job_persistence._DELETE_JOB_SQL",
        "DELETE FROM no_such_table WHERE job_id = ?",
    )
    fresh = _failed_job(created_at=3)
    with pytest.raises(JobPersistenceError):
        store.put(fresh)

    # memory: exactly the original two jobs, FIFO order intact, no new job
    assert [item.job_id for item in store.list()] == [newer.job_id, older.job_id]
    assert store.get(fresh.job_id) is None
    # sqlite: exactly the original two rows - the rolled-back insert left
    # no new row and removed no victim row
    assert _row_count(db) == 2
    connection = sqlite3.connect(db)
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM passorch_jobs WHERE job_id = ?", (fresh.job_id,)
        ).fetchone()
    finally:
        connection.close()
    assert row is not None and int(row[0]) == 0

    monkeypatch.undo()
    # the store remains fully usable after the failed attempt
    store.put(fresh)
    assert [item.job_id for item in store.list()] == [fresh.job_id, newer.job_id]
    assert _row_count(db) == 2


def test_eviction_insert_failure_inside_the_combined_operation_is_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing insert inside the combined operation changes nothing."""

    db = tmp_path / "jobs.db"
    store = PersistentJobStore(db, capacity=2)
    older = _failed_job(created_at=1)
    newer = _failed_job(created_at=2)
    store.put(older)
    store.put(newer)

    monkeypatch.setattr(
        "app.engines.job_persistence._INSERT_JOB_SQL",
        "INSERT INTO no_such_table (job_id, payload) VALUES (?, ?)",
    )
    fresh = _failed_job(created_at=3)
    with pytest.raises(JobPersistenceError):
        store.put(fresh)

    assert [item.job_id for item in store.list()] == [newer.job_id, older.job_id]
    assert store.get(fresh.job_id) is None
    assert _row_count(db) == 2


def test_successful_eviction_put_removes_the_oldest_row_in_both_views(
    tmp_path: Path,
) -> None:
    """A successful third put evicts the oldest record in memory and SQLite."""

    db = tmp_path / "jobs.db"
    store = PersistentJobStore(db, capacity=2)
    first = _failed_job(created_at=1)
    second = _failed_job(created_at=2)
    third = _failed_job(created_at=3)
    store.put(first)
    store.put(second)
    store.put(third)

    assert [item.job_id for item in store.list()] == [third.job_id, second.job_id]
    assert store.get(first.job_id) is None
    assert _row_count(db) == 2

    reopened = PersistentJobStore(db, capacity=2)
    assert [item.job_id for item in reopened.list()] == [third.job_id, second.job_id]
    assert reopened.get(first.job_id) is None


def test_out_of_band_row_removal_reads_as_missing(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    first = _client(db)
    created = _create_job(first)

    _execute(db, "DELETE FROM passorch_jobs")

    second = _client(db)
    assert second.get(f"/api/jobs/{_job_id(created)}").status_code == 404
    assert second.get("/api/jobs").json()["total"] == 0


def test_read_endpoints_never_touch_the_database(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    client = _client(db)
    created = _create_job(client)
    payload_before = db.read_bytes()
    stamp_before = db.stat().st_mtime_ns

    for _ in range(3):
        assert client.get("/api/jobs").status_code == 200
        assert client.get("/api/jobs/stats").status_code == 200
        assert client.get(f"/api/jobs/{_job_id(created)}").status_code == 200

    assert db.read_bytes() == payload_before
    assert db.stat().st_mtime_ns == stamp_before


# --------------------------------------------------------------------------- #
# documentation
# --------------------------------------------------------------------------- #


def test_readme_documents_persistence() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "## Persistence" in readme
    assert "PASSORCH_JOB_DB" in readme
    assert "job_db_path" in readme
    assert "SQLite" in readme
    assert "persistence_unavailable" in readme

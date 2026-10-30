"""Phase 8B - persistent target instance storage (SQLite) tests.

Covers:
* durability: instances written through one application reopen with
  identical API output and identical typed records; filters, pagination,
  totals and insertion order work after a restart; deletions and resets
  survive restarts; recorded jobs keep their immutable instance snapshot
  when the instance is deleted;
* configuration consistency: one ``job_db_path`` configures both stores
  over one database file, shared/isolated files, PASSORCH_JOB_DB wiring,
  mutual exclusion with store injections, default in-memory behaviour;
* lifecycle: capacity hydration prune, unknown-id deletes, connections
  released between operations, reads never touching the database;
* failure: unusable paths, corrupt files, invalid or forbidden rows raise
  InstancePersistenceError (never a silent memory-only fallback); write
  failures answer HTTP 503 with a generic structured detail and leave the
  in-memory view truthful; put compensates memory including FIFO victims;
* safety: persisted payloads carry exactly the five record keys and no
  forbidden command/argv/path keys anywhere, and README documents the
  feature.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.core.orchestrator.target_instances import create_target_instance
from app.engines.discovery.models import (
    CapabilityStatus,
    EngineCapabilities,
    EngineSnapshot,
    JOHN,
)
from app.engines.discovery.registry import DiscoveryRegistry
from app.engines.instance_persistence import (
    InstancePersistenceError,
    PersistentTargetInstanceStore,
    configured_instance_store,
)
from app.engines.job_persistence import PersistentJobStore
from app.engines.jobs import FORBIDDEN_JOB_FIELDS, JobStore
from app.engines.target_instances import (
    DuplicateTargetInstanceError,
    TargetInstanceRecord,
    TargetInstanceStore,
)

_ALLOWED_PAYLOAD_KEYS = {"id", "name", "target_type", "description", "created_at"}


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
    """A client whose job and instance stores are both persistent."""

    return TestClient(create_app(discovery_registry=_registry(), job_db_path=db_path))


def _store_client(store: TargetInstanceStore) -> TestClient:
    return TestClient(create_app(discovery_registry=_registry(), target_instance_store=store))


def _create_instance(
    client: TestClient,
    *,
    name: str = "instance",
    target_type: str = "pdf-document",
    description: str = "recorded",
) -> dict[str, object]:
    response = client.post(
        "/api/target-instances",
        json={"name": name, "target_type": target_type, "description": description},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert isinstance(payload, dict)
    return payload


def _instance_id(created: dict[str, object]) -> str:
    identifier = created["id"]
    assert isinstance(identifier, str)
    return identifier


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


def _execute(db_path: Path, sql: str, parameters: tuple[object, ...] = ()) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(sql, parameters)
        connection.commit()
    finally:
        connection.close()


def _fetch_instance_payload(db_path: Path, instance_id: str) -> str:
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            "SELECT payload FROM passorch_instances WHERE instance_id = ?", (instance_id,)
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return str(row[0])


def _instance_row_count(db_path: Path) -> int:
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute("SELECT COUNT(*) FROM passorch_instances").fetchone()
    finally:
        connection.close()
    assert row is not None
    return int(row[0])


def _table_names(db_path: Path) -> set[str]:
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    finally:
        connection.close()
    return {str(row[0]) for row in rows}


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


def test_persisted_instances_survive_reopen_with_identical_api_output(tmp_path: Path) -> None:
    db = tmp_path / "instances.db"
    first = _client(db)
    created = [
        _create_instance(first, name="alpha"),
        _create_instance(first, name="beta", target_type="zip-archive"),
        _create_instance(first, name="gamma", description="newest"),
    ]
    ids = [_instance_id(item) for item in created]

    before = {
        "list": first.get("/api/target-instances").json(),
        "detail": first.get(f"/api/target-instances/{ids[0]}").json(),
    }

    second = _client(db)
    after = {
        "list": second.get("/api/target-instances").json(),
        "detail": second.get(f"/api/target-instances/{ids[0]}").json(),
    }

    assert after == before
    assert after["list"]["total"] == 3
    assert [item["id"] for item in after["list"]["instances"]] == ids
    assert after["detail"] == created[0]


def test_reopened_store_returns_identical_typed_records(tmp_path: Path) -> None:
    db = tmp_path / "instances.db"
    store = PersistentTargetInstanceStore(db)
    records = [
        create_target_instance("first", "pdf-document", "one", store=store),
        create_target_instance("second", "zip-archive", "two", store=store),
    ]

    reopened = PersistentTargetInstanceStore(db)
    assert len(reopened) == 2
    for record in records:
        restored = reopened.get(record.id)
        assert restored == record
        assert restored is not None
        assert restored.created_at == record.created_at
        assert restored.description == record.description
    assert [item.id for item in reopened.list()] == [record.id for record in records]


def test_filters_pagination_and_totals_work_after_reopen(tmp_path: Path) -> None:
    db = tmp_path / "instances.db"
    first = _client(db)
    alpha = _create_instance(first, name="alpha")
    alphabet = _create_instance(first, name="Alphabet", target_type="zip-archive")
    beta = _create_instance(first, name="beta")

    second = _client(db)

    by_type = second.get("/api/target-instances", params={"target_type": "pdf-document"}).json()
    assert by_type["total"] == 2
    assert by_type["count"] == 2
    assert {item["id"] for item in by_type["instances"]} == {
        _instance_id(alpha),
        _instance_id(beta),
    }

    by_name = second.get("/api/target-instances", params={"name": "alpha"}).json()
    assert by_name["total"] == 2
    assert {item["id"] for item in by_name["instances"]} == {
        _instance_id(alpha),
        _instance_id(alphabet),
    }
    assert {item["name"] for item in by_name["instances"]} == {"alpha", "Alphabet"}

    unknown = second.get("/api/target-instances", params={"target_type": "unknown-type"}).json()
    assert unknown == {
        "instances": [],
        "count": 0,
        "total": 0,
        "limit": 50,
        "offset": 0,
    }

    page = second.get("/api/target-instances", params={"limit": 1, "offset": 1}).json()
    assert page["count"] == 1
    assert page["total"] == 3
    assert page["instances"][0]["name"] == "Alphabet"

    assert second.get("/api/target-instances", params={"limit": 0}).status_code == 422
    assert second.get("/api/target-instances", params={"limit": 101}).status_code == 422
    assert second.get("/api/target-instances", params={"offset": -1}).status_code == 422


def test_deletion_survives_restart(tmp_path: Path) -> None:
    db = tmp_path / "instances.db"
    first = _client(db)
    kept = _create_instance(first, name="kept")
    doomed = _create_instance(first, name="doomed")

    response = first.delete(f"/api/target-instances/{_instance_id(doomed)}")
    assert response.status_code == 204
    assert response.content == b""

    second = _client(db)
    assert (
        second.get(f"/api/target-instances/{_instance_id(doomed)}").status_code == 404
    )
    listing = second.get("/api/target-instances").json()
    assert listing["total"] == 1
    assert listing["instances"][0]["id"] == _instance_id(kept)


def test_reset_clears_persisted_rows(tmp_path: Path) -> None:
    db = tmp_path / "instances.db"
    store = PersistentTargetInstanceStore(db)
    create_target_instance("one", "pdf-document", "", store=store)
    create_target_instance("two", "zip-archive", "", store=store)
    assert len(store) == 2

    store.reset()
    assert len(store) == 0
    assert _instance_row_count(db) == 0
    assert len(PersistentTargetInstanceStore(db)) == 0


def test_jobs_survive_instance_deletion_and_keep_their_snapshot(tmp_path: Path) -> None:
    db = tmp_path / "instances.db"
    client = _client(db)
    instance = _create_instance(client, name="victim", target_type="pdf-document")
    instance_id = _instance_id(instance)
    job = _create_job(client, instance_id=instance_id)

    assert client.delete(f"/api/target-instances/{instance_id}").status_code == 204
    assert client.get(f"/api/jobs/{_job_id(job)}").json() == job

    second = _client(db)
    assert second.get(f"/api/target-instances/{instance_id}").status_code == 404
    associated = second.get("/api/jobs", params={"instance_id": instance_id}).json()
    assert associated["total"] == 1
    assert associated["jobs"][0]["job_id"] == _job_id(job)
    assert second.get(f"/api/jobs/{_job_id(job)}").json() == job


# --------------------------------------------------------------------------- #
# configuration: one path, both stores, mutual exclusion, environment
# --------------------------------------------------------------------------- #


def test_one_path_configures_both_stores_over_one_database_file(tmp_path: Path) -> None:
    db = tmp_path / "shared.db"
    application = create_app(discovery_registry=_registry(), job_db_path=db)
    assert isinstance(application.state.job_store, PersistentJobStore)
    assert isinstance(application.state.target_instance_store, PersistentTargetInstanceStore)
    assert application.state.target_instance_store.db_path == db
    assert application.state.job_store.db_path == db

    client = TestClient(application)
    _create_instance(client)
    _create_job(client)

    tables = _table_names(db)
    assert {"passorch_jobs", "passorch_instances"} <= tables
    connection = sqlite3.connect(db)
    try:
        job_rows = connection.execute("SELECT COUNT(*) FROM passorch_jobs").fetchone()
        instance_rows = connection.execute(
            "SELECT COUNT(*) FROM passorch_instances"
        ).fetchone()
    finally:
        connection.close()
    assert job_rows is not None and int(job_rows[0]) == 1
    assert instance_rows is not None and int(instance_rows[0]) == 1


def test_configured_instance_store_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PASSORCH_JOB_DB", raising=False)
    assert configured_instance_store(None) is None

    explicit = configured_instance_store(tmp_path / "explicit.db")
    assert isinstance(explicit, PersistentTargetInstanceStore)
    assert explicit.db_path == tmp_path / "explicit.db"

    env_path = tmp_path / "env.db"
    monkeypatch.setenv("PASSORCH_JOB_DB", str(env_path))
    chosen = configured_instance_store(tmp_path / "wins.db")
    assert isinstance(chosen, PersistentTargetInstanceStore)
    assert chosen.db_path == tmp_path / "wins.db"
    assert not env_path.exists()

    from_env = configured_instance_store(None)
    assert isinstance(from_env, PersistentTargetInstanceStore)
    assert env_path.exists()

    monkeypatch.setenv("PASSORCH_JOB_DB", "   ")
    assert configured_instance_store(None) is None

    with pytest.raises(InstancePersistenceError):
        configured_instance_store("   ")


def test_mutual_exclusion_covers_both_store_injections(tmp_path: Path) -> None:
    with pytest.raises(
        ValueError, match="target_instance_store and job_db_path are mutually exclusive"
    ):
        create_app(target_instance_store=TargetInstanceStore(), job_db_path=tmp_path / "x.db")

    with pytest.raises(ValueError, match="job_store and job_db_path are mutually exclusive"):
        create_app(job_store=JobStore(), job_db_path=tmp_path / "x.db")


def test_environment_variable_enables_persistence_for_both_stores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PASSORCH_JOB_DB", str(tmp_path / "env.db"))
    first = TestClient(create_app(discovery_registry=_registry()))
    assert isinstance(first.app.state.job_store, PersistentJobStore)
    assert isinstance(first.app.state.target_instance_store, PersistentTargetInstanceStore)

    created = _create_instance(first)

    second = TestClient(create_app(discovery_registry=_registry()))
    detail = second.get(f"/api/target-instances/{_instance_id(created)}")
    assert detail.status_code == 200
    assert detail.json() == created


def test_two_application_instances_share_one_database(tmp_path: Path) -> None:
    db = tmp_path / "instances.db"
    writer = _client(db)
    created = _create_instance(writer)

    reader = _client(db)
    detail = reader.get(f"/api/target-instances/{_instance_id(created)}")
    assert detail.status_code == 200
    assert detail.json() == created
    assert reader.get("/api/target-instances").json()["total"] == 1


def test_separate_databases_are_isolated(tmp_path: Path) -> None:
    one = _client(tmp_path / "one.db")
    two = _client(tmp_path / "two.db")
    created = _create_instance(one)

    assert two.get(f"/api/target-instances/{_instance_id(created)}").status_code == 404
    assert two.get("/api/target-instances").json()["total"] == 0
    assert one.get("/api/target-instances").json()["total"] == 1


def test_default_create_app_stays_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PASSORCH_JOB_DB", raising=False)
    application = create_app()
    assert type(application.state.target_instance_store) is TargetInstanceStore
    assert not isinstance(application.state.target_instance_store, PersistentTargetInstanceStore)


# --------------------------------------------------------------------------- #
# lifecycle: capacity, deletion, released connections, read-only reads
# --------------------------------------------------------------------------- #


def test_capacity_hydration_keeps_newest_and_prunes_database(tmp_path: Path) -> None:
    db = tmp_path / "instances.db"
    store = PersistentTargetInstanceStore(db, capacity=2)
    records = [
        create_target_instance("one", "pdf-document", "", store=store),
        create_target_instance("two", "pdf-document", "", store=store),
        create_target_instance("three", "zip-archive", "", store=store),
    ]

    assert [item.id for item in store.list()] == [records[1].id, records[2].id]
    assert _instance_row_count(db) == 2

    reopened = PersistentTargetInstanceStore(db, capacity=2)
    assert [item.id for item in reopened.list()] == [records[1].id, records[2].id]
    assert _instance_row_count(db) == 2

    shrunk = PersistentTargetInstanceStore(db, capacity=1)
    assert [item.id for item in shrunk.list()] == [records[2].id]
    assert _instance_row_count(db) == 1


def test_validation_failure_during_hydration_never_prunes_rows(
    tmp_path: Path,
) -> None:
    """A construction that fails validation must not have deleted rows.

    The newest retained row is tampered with so it still *parses* (plain
    string fields) but fails the base class's target-type validation.
    Opening with a smaller capacity must raise without pruning the older
    rows - the destructive prune runs only after every hydrated record
    has validated.
    """

    db = tmp_path / "instances.db"
    seeding = PersistentTargetInstanceStore(db, capacity=5)
    originals = [
        create_target_instance(f"record-{index}", "pdf-document", "", store=seeding)
        for index in range(1, 5)
    ]

    # tamper the NEWEST row at the SQL layer: the unmapped target type
    # parses fine but super().put() rejects it
    newest = originals[-1]
    tampered = json.loads(_fetch_instance_payload(db, newest.id))
    tampered["target_type"] = "unknown-format"
    _execute(
        db,
        "UPDATE passorch_instances SET payload = ? WHERE instance_id = ?",
        (json.dumps(tampered), newest.id),
    )

    with pytest.raises(InstancePersistenceError):
        PersistentTargetInstanceStore(db, capacity=2)

    # every original row is still present - the would-be-pruned rows
    # were never deleted, and the tampered payload is untouched
    assert _instance_row_count(db) == 4
    connection = sqlite3.connect(db)
    try:
        ids = {
            str(row[0])
            for row in connection.execute("SELECT instance_id FROM passorch_instances")
        }
    finally:
        connection.close()
    assert ids == {record.id for record in originals}
    assert json.loads(_fetch_instance_payload(db, newest.id))["target_type"] == (
        "unknown-format"
    )

    # and once the bad row is repaired, the pruned reopen succeeds
    repaired = json.loads(_fetch_instance_payload(db, newest.id))
    repaired["target_type"] = "pdf-document"
    _execute(
        db,
        "UPDATE passorch_instances SET payload = ? WHERE instance_id = ?",
        (json.dumps(repaired), newest.id),
    )
    reopened = PersistentTargetInstanceStore(db, capacity=2)
    assert [item.id for item in reopened.list()] == [originals[2].id, newest.id]
    assert _instance_row_count(db) == 2


def test_delete_of_unknown_ids_returns_false_and_leaves_rows(tmp_path: Path) -> None:
    db = tmp_path / "instances.db"
    store = PersistentTargetInstanceStore(db)
    record = create_target_instance("keep", "pdf-document", "", store=store)

    assert store.delete(123) is False
    assert store.delete("not-a-uuid") is False
    assert store.delete(str(uuid.uuid4())) is False

    assert _instance_row_count(db) == 1
    assert store.get(record.id) is not None
    assert store.delete(record.id) is True
    assert _instance_row_count(db) == 0


def test_connections_are_released_between_operations(tmp_path: Path) -> None:
    db = tmp_path / "instances.db"
    client = _client(db)
    _create_instance(client)
    assert client.get("/api/target-instances").status_code == 200

    # A second writer must be able to take the file's write lock immediately:
    # no operation may hold a connection open past its own call.
    concurrent = PersistentTargetInstanceStore(db)
    record = create_target_instance("later", "zip-archive", "", store=concurrent)
    assert concurrent.get(record.id) is not None


def test_read_endpoints_never_touch_the_database(tmp_path: Path) -> None:
    db = tmp_path / "instances.db"
    client = _client(db)
    created = _create_instance(client)
    payload_before = db.read_bytes()
    stamp_before = db.stat().st_mtime_ns

    for _ in range(3):
        assert client.get("/api/target-instances").status_code == 200
        assert client.get(f"/api/target-instances/{_instance_id(created)}").status_code == 200

    assert db.read_bytes() == payload_before
    assert db.stat().st_mtime_ns == stamp_before


# --------------------------------------------------------------------------- #
# failure behaviour: fail closed, structured 503, no fabricated state
# --------------------------------------------------------------------------- #


def test_instance_store_fails_closed_on_unusable_or_blank_path(tmp_path: Path) -> None:
    directory = tmp_path / "not-a-file"
    directory.mkdir()
    with pytest.raises(InstancePersistenceError) as error:
        PersistentTargetInstanceStore(directory)
    assert str(tmp_path) not in str(error.value)

    with pytest.raises(InstancePersistenceError):
        PersistentTargetInstanceStore("   ")

    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"this is definitely not sqlite" * 16)
    with pytest.raises(InstancePersistenceError) as corrupt_error:
        PersistentTargetInstanceStore(corrupt)
    assert str(tmp_path) not in str(corrupt_error.value)


def test_create_app_fails_fast_when_instance_rows_are_invalid(tmp_path: Path) -> None:
    db = tmp_path / "instances.db"
    store = PersistentTargetInstanceStore(db)
    record = create_target_instance("poisoned", "pdf-document", "", store=store)
    _execute(
        db,
        "UPDATE passorch_instances SET payload = ? WHERE instance_id = ?",
        ('{"name": 5}', record.id),
    )

    with pytest.raises(InstancePersistenceError) as error:
        create_app(discovery_registry=_registry(), job_db_path=db)
    assert str(tmp_path) not in str(error.value)


def test_unreadable_persisted_row_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "instances.db"
    store = PersistentTargetInstanceStore(db)
    record = create_target_instance("broken", "pdf-document", "", store=store)

    _execute(
        db,
        "UPDATE passorch_instances SET payload = ? WHERE instance_id = ?",
        ("{ this is not json", record.id),
    )

    with pytest.raises(InstancePersistenceError):
        PersistentTargetInstanceStore(db)


def test_row_with_forbidden_keys_anywhere_is_rejected(tmp_path: Path) -> None:
    db = tmp_path / "instances.db"
    store = PersistentTargetInstanceStore(db)
    record = create_target_instance("scanned", "pdf-document", "", store=store)

    payload = json.loads(_fetch_instance_payload(db, record.id))
    payload["command"] = "id"
    _execute(
        db,
        "UPDATE passorch_instances SET payload = ? WHERE instance_id = ?",
        (json.dumps(payload), record.id),
    )

    with pytest.raises(InstancePersistenceError):
        PersistentTargetInstanceStore(db)


def test_invalid_typed_or_unmapped_rows_fail_closed(tmp_path: Path) -> None:
    db = tmp_path / "instances.db"
    store = PersistentTargetInstanceStore(db)
    record = create_target_instance("typed", "pdf-document", "", store=store)
    original = json.loads(_fetch_instance_payload(db, record.id))

    wrong_type = dict(original, created_at="soon")
    _execute(
        db,
        "UPDATE passorch_instances SET payload = ? WHERE instance_id = ?",
        (json.dumps(wrong_type), record.id),
    )
    with pytest.raises(InstancePersistenceError):
        PersistentTargetInstanceStore(db)

    unmapped = dict(original, target_type="unknown-format")
    _execute(
        db,
        "UPDATE passorch_instances SET payload = ? WHERE instance_id = ?",
        (json.dumps(unmapped), record.id),
    )
    with pytest.raises(InstancePersistenceError):
        PersistentTargetInstanceStore(db)


def test_write_failure_returns_structured_503_without_leaking_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "instances.db"
    store = PersistentTargetInstanceStore(db)
    client = _store_client(store)

    def _explode(record: TargetInstanceRecord) -> None:
        raise InstancePersistenceError("failed to write an instance record")

    monkeypatch.setattr(store._db, "insert_instance", _explode)
    response = client.post(
        "/api/target-instances",
        json={"name": "doomed", "target_type": "pdf-document"},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == {
        "error": "persistence_unavailable",
        "detail": "target instance storage is temporarily unavailable",
    }
    body = response.text.lower()
    assert "sqlite" not in body
    assert "traceback" not in body
    assert str(tmp_path).lower() not in body

    # a failed write is never fabricated as a stored instance
    assert len(store) == 0
    assert client.get("/api/target-instances").json()["total"] == 0


def test_delete_failure_returns_structured_503_and_keeps_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PersistentTargetInstanceStore(tmp_path / "instances.db")
    client = _store_client(store)
    created = _create_instance(client)

    def _explode(instance_id: str) -> bool:
        raise InstancePersistenceError("failed to delete an instance record")

    monkeypatch.setattr(store._db, "delete_instance", _explode)
    response = client.delete(f"/api/target-instances/{_instance_id(created)}")
    assert response.status_code == 503
    assert response.json()["detail"] == {
        "error": "persistence_unavailable",
        "detail": "target instance storage is temporarily unavailable",
    }
    assert store.get(_instance_id(created)) is not None
    assert client.get("/api/target-instances").json()["total"] == 1


def test_put_failure_compensates_memory_including_the_fifo_victim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "instances.db"
    store = PersistentTargetInstanceStore(db, capacity=1)
    first = create_target_instance("first", "pdf-document", "", store=store)

    def _explode(record: TargetInstanceRecord, victim_id: str) -> None:
        raise InstancePersistenceError("failed to write an instance record")

    monkeypatch.setattr(store._db, "insert_instance_with_eviction", _explode)
    second = TargetInstanceRecord(
        id=str(uuid.uuid4()),
        name="second",
        target_type="zip-archive",
        description="",
        created_at=1,
    )
    with pytest.raises(InstancePersistenceError):
        store.put(second)

    assert store.get(second.id) is None
    assert [item.id for item in store.list()] == [first.id]
    assert _instance_row_count(db) == 1


def test_database_level_duplicate_raises_duplicate_error(tmp_path: Path) -> None:
    store = PersistentTargetInstanceStore(tmp_path / "instances.db")
    record = create_target_instance("original", "pdf-document", "", store=store)

    with pytest.raises(DuplicateTargetInstanceError):
        store._db.insert_instance(record)


def test_duplicate_row_compensates_the_memory_copy_on_put(tmp_path: Path) -> None:
    db = tmp_path / "instances.db"
    store = PersistentTargetInstanceStore(db)
    record = create_target_instance("original", "pdf-document", "", store=store)

    # Simulate memory/DB divergence: the row exists only in the database.
    with store._lock:
        store._instances.pop(record.id, None)

    with pytest.raises(DuplicateTargetInstanceError):
        store.put(record)
    assert store.get(record.id) is None


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

    db = tmp_path / "instances.db"
    store = PersistentTargetInstanceStore(db, capacity=2)
    older = create_target_instance("older", "pdf-document", "", store=store)
    newer = create_target_instance("newer", "zip-archive", "", store=store)

    monkeypatch.setattr(
        "app.engines.instance_persistence._DELETE_INSTANCE_SQL",
        "DELETE FROM no_such_table WHERE instance_id = ?",
    )
    fresh = TargetInstanceRecord(
        id=str(uuid.uuid4()),
        name="fresh",
        target_type="zip-archive",
        description="",
        created_at=1,
    )
    with pytest.raises(InstancePersistenceError):
        store.put(fresh)

    # memory: exactly the original two instances, FIFO order intact
    assert [item.id for item in store.list()] == [older.id, newer.id]
    assert store.get(fresh.id) is None
    # sqlite: exactly the original two rows - the rolled-back insert left
    # no new row and removed no victim row
    assert _instance_row_count(db) == 2
    connection = sqlite3.connect(db)
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM passorch_instances WHERE instance_id = ?",
            (fresh.id,),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None and int(row[0]) == 0

    monkeypatch.undo()
    # the store remains fully usable after the failed attempt
    store.put(fresh)
    assert [item.id for item in store.list()] == [newer.id, fresh.id]
    assert _instance_row_count(db) == 2


def test_eviction_insert_failure_inside_the_combined_operation_is_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing insert inside the combined operation changes nothing."""

    db = tmp_path / "instances.db"
    store = PersistentTargetInstanceStore(db, capacity=2)
    older = create_target_instance("older", "pdf-document", "", store=store)
    newer = create_target_instance("newer", "zip-archive", "", store=store)

    monkeypatch.setattr(
        "app.engines.instance_persistence._INSERT_INSTANCE_SQL",
        "INSERT INTO no_such_table (instance_id, payload) VALUES (?, ?)",
    )
    fresh = TargetInstanceRecord(
        id=str(uuid.uuid4()),
        name="fresh",
        target_type="zip-archive",
        description="",
        created_at=1,
    )
    with pytest.raises(InstancePersistenceError):
        store.put(fresh)

    assert [item.id for item in store.list()] == [older.id, newer.id]
    assert store.get(fresh.id) is None
    assert _instance_row_count(db) == 2


def test_successful_eviction_put_removes_the_oldest_row_in_both_views(
    tmp_path: Path,
) -> None:
    """A successful third put evicts the oldest record in memory and SQLite."""

    db = tmp_path / "instances.db"
    store = PersistentTargetInstanceStore(db, capacity=2)
    first = create_target_instance("first", "pdf-document", "", store=store)
    second = create_target_instance("second", "zip-archive", "", store=store)
    third = TargetInstanceRecord(
        id=str(uuid.uuid4()),
        name="third",
        target_type="zip-archive",
        description="",
        created_at=1,
    )
    store.put(third)

    assert [item.id for item in store.list()] == [second.id, third.id]
    assert store.get(first.id) is None
    assert _instance_row_count(db) == 2

    reopened = PersistentTargetInstanceStore(db, capacity=2)
    assert [item.id for item in reopened.list()] == [second.id, third.id]
    assert reopened.get(first.id) is None


def test_out_of_band_row_removal_reads_as_missing(tmp_path: Path) -> None:
    db = tmp_path / "instances.db"
    first = _client(db)
    created = _create_instance(first)

    _execute(db, "DELETE FROM passorch_instances")

    second = _client(db)
    assert second.get(f"/api/target-instances/{_instance_id(created)}").status_code == 404
    assert second.get("/api/target-instances").json()["total"] == 0


# --------------------------------------------------------------------------- #
# security and documentation
# --------------------------------------------------------------------------- #


def test_persisted_payloads_contain_exactly_the_record_keys(tmp_path: Path) -> None:
    db = tmp_path / "instances.db"
    client = _client(db)
    created = _create_instance(client)

    payload = json.loads(_fetch_instance_payload(db, _instance_id(created)))
    assert set(payload) == _ALLOWED_PAYLOAD_KEYS
    assert not _forbidden_keys(payload)
    assert not _forbidden_keys(client.get("/api/target-instances").json())
    assert not _forbidden_keys(client.get(f"/api/target-instances/{_instance_id(created)}").json())


def test_readme_documents_persistence_for_instances() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "## Persistence" in readme
    assert "target instances" in readme
    assert "target_instance_store" in readme
    assert "InstancePersistenceError" in readme
    assert "same database file" in readme


# -- Phase 4: the PATCH update mirror --------------------------------------- #


def test_update_failure_returns_structured_503_and_keeps_stored_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PersistentTargetInstanceStore(tmp_path / "instances.db")
    client = _store_client(store)
    created = _create_instance(client)

    def _explode(record: TargetInstanceRecord) -> bool:
        raise InstancePersistenceError("failed to update an instance record /data/secret.db")

    monkeypatch.setattr(store._db, "update_instance", _explode)
    response = client.patch(
        f"/api/target-instances/{_instance_id(created)}", json={"name": "doomed"}
    )
    assert response.status_code == 503
    assert response.json()["detail"] == {
        "error": "persistence_unavailable",
        "detail": "target instance storage is temporarily unavailable",
    }
    body = response.text.lower()
    assert "sqlite" not in body
    assert "traceback" not in body
    assert "secret.db" not in body
    assert str(tmp_path).lower() not in body

    # the in-memory view stays truthful: the failed update was never applied
    stored = store.get(_instance_id(created))
    assert stored is not None
    assert stored.name == "instance"
    assert stored.description == "recorded"
    # and the update stays retryable once the database recovers
    monkeypatch.undo()
    retry = client.patch(
        f"/api/target-instances/{_instance_id(created)}", json={"name": "doomed"}
    )
    assert retry.status_code == 200
    assert store.get(_instance_id(created)).name == "doomed"


def test_update_is_mirrored_and_survives_reopen(tmp_path: Path) -> None:
    db = tmp_path / "instances.db"
    store = PersistentTargetInstanceStore(db)
    client = _store_client(store)
    first = _create_instance(client, name="first")
    second = _create_instance(client, name="second")

    response = client.patch(
        f"/api/target-instances/{_instance_id(first)}",
        json={"name": "first-renamed", "description": "changed"},
    )
    assert response.status_code == 200

    reopened = PersistentTargetInstanceStore(db)
    hydrated_first = reopened.get(_instance_id(first))
    hydrated_second = reopened.get(_instance_id(second))
    assert hydrated_first is not None
    assert hydrated_first.name == "first-renamed"
    assert hydrated_first.description == "changed"
    assert hydrated_first.target_type == hydrated_second.target_type == "pdf-document"
    # insertion order is preserved across the update and the reopen
    assert [r.id for r in reopened.list()] == [_instance_id(first), _instance_id(second)]


def test_update_rewrites_only_the_payload_row(tmp_path: Path) -> None:
    db = tmp_path / "instances.db"
    store = PersistentTargetInstanceStore(db)
    client = _store_client(store)
    created = _create_instance(client)
    instance_id = _instance_id(created)
    seq_before = sqlite3.connect(db).execute(
        "SELECT seq FROM passorch_instances WHERE instance_id = ?", (instance_id,)
    ).fetchone()[0]

    assert (
        client.patch(f"/api/target-instances/{instance_id}", json={"name": "rewritten"}).status_code
        == 200
    )

    connection = sqlite3.connect(db)
    try:
        row = connection.execute(
            "SELECT seq, payload FROM passorch_instances WHERE instance_id = ?", (instance_id,)
        ).fetchone()
    finally:
        connection.close()
    assert row[0] == seq_before, "an update must not change the row's seq/position"
    payload = json.loads(row[1])
    assert payload["name"] == "rewritten"
    assert set(payload) == {"id", "name", "target_type", "description", "created_at"}

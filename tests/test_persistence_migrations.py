"""Phase 8C - schema versioning, migrations, backup and restore tests.

Covers:
* stamping: every store construction stamps ``PRAGMA user_version`` with
  the current ``SCHEMA_VERSION`` over one uniform schema (both tables),
  idempotently; pending migrations run **only** at construction - never
  on a read path;
* migration: legacy unversioned files upgrade in place without losing
  rows; migrations are ordered, DDL-only and non-destructive; a failed
  migration rolls back with the version and file intact; a file stamped
  by a newer schema is rejected fail-closed with no leak of paths or SQL;
* backup: ``backup_database`` copies a live database consistently,
  restore is the same call with reversed arguments, and unusable
  arguments fail closed without leaving a broken destination behind;
* documentation: README explains schema versions and backup/restore.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.core.orchestrator.target_instances import create_target_instance
from app.engines import persistence as persistence_module
from app.engines.instance_persistence import (
    InstancePersistenceError,
    PersistentTargetInstanceStore,
)
from app.engines.job_persistence import JobPersistenceError, PersistentJobStore
from app.engines.jobs import AuditJob, JobStatus
from app.engines.persistence import (
    SCHEMA_VERSION,
    PersistenceError,
    backup_database,
)

_JOB_TABLE = "passorch_jobs"
_INSTANCE_TABLE = "passorch_instances"


def _failed_job(created_at: int = 1_700_000_000) -> AuditJob:
    return AuditJob(
        job_id="3f1d3b0e-6f6a-4e4e-8a53-9d5a04e5d001",
        status=JobStatus.FAILED,
        created_at=created_at,
        target="pdf-document",
        failure_reason="no_preview",
    )


def _user_version(db_path: Path) -> int:
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute("PRAGMA user_version").fetchone()
    finally:
        connection.close()
    assert row is not None
    return int(row[0])


def _set_user_version(db_path: Path, version: int) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(f"PRAGMA user_version = {int(version)}")
        connection.commit()
    finally:
        connection.close()


def _table_names(db_path: Path) -> set[str]:
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    finally:
        connection.close()
    return {str(row[0]) for row in rows}


# --------------------------------------------------------------------------- #
# stamping and idempotence
# --------------------------------------------------------------------------- #


def test_fresh_databases_are_stamped_with_one_uniform_schema(tmp_path: Path) -> None:
    jobs_db = tmp_path / "jobs.db"
    instances_db = tmp_path / "instances.db"

    job_store = PersistentJobStore(jobs_db)
    instance_store = PersistentTargetInstanceStore(instances_db)

    assert _user_version(jobs_db) == SCHEMA_VERSION
    assert _user_version(instances_db) == SCHEMA_VERSION
    # One file carries one schema: both tables exist whichever store opens first.
    assert {_JOB_TABLE, _INSTANCE_TABLE} <= _table_names(jobs_db)
    assert {_JOB_TABLE, _INSTANCE_TABLE} <= _table_names(instances_db)

    job_store.put(job := _failed_job())
    record = create_target_instance("stamped", "pdf-document", "", store=instance_store)
    assert job_store.get(job.job_id) == job
    assert instance_store.get(record.id) is not None


def test_reopening_is_idempotent_and_keeps_records(tmp_path: Path) -> None:
    db = tmp_path / "records.db"
    store = PersistentTargetInstanceStore(db)
    record = create_target_instance("kept", "pdf-document", "", store=store)

    for _ in range(3):
        reopened = PersistentTargetInstanceStore(db)
        assert reopened.get(record.id) == record
        assert _user_version(db) == SCHEMA_VERSION
        assert _instance_row_count(db) == 1


def _instance_row_count(db_path: Path) -> int:
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(f"SELECT COUNT(*) FROM {_INSTANCE_TABLE}").fetchone()
    finally:
        connection.close()
    assert row is not None
    return int(row[0])


# --------------------------------------------------------------------------- #
# migrations: legacy upgrades, rollback, fail-closed future versions
# --------------------------------------------------------------------------- #


def test_legacy_unversioned_file_migrates_without_losing_job_rows(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    store = PersistentJobStore(db)
    job = _failed_job()
    store.put(job)

    # Emulate a pre-versioning file: no stamp, and only the jobs table.
    _set_user_version(db, 0)
    connection = sqlite3.connect(db)
    try:
        connection.execute(f"DROP TABLE {_INSTANCE_TABLE}")
        connection.commit()
    finally:
        connection.close()

    reopened = PersistentJobStore(db)
    assert reopened.get(job.job_id) == job
    assert _user_version(db) == SCHEMA_VERSION
    assert {_JOB_TABLE, _INSTANCE_TABLE} <= _table_names(db)


def test_legacy_unversioned_file_migrates_without_losing_instance_rows(
    tmp_path: Path,
) -> None:
    db = tmp_path / "legacy.db"
    store = PersistentTargetInstanceStore(db)
    record = create_target_instance("legacy", "pdf-document", "", store=store)

    _set_user_version(db, 0)
    connection = sqlite3.connect(db)
    try:
        connection.execute(f"DROP TABLE {_JOB_TABLE}")
        connection.commit()
    finally:
        connection.close()

    reopened = PersistentTargetInstanceStore(db)
    assert reopened.get(record.id) == record
    assert _user_version(db) == SCHEMA_VERSION
    assert {_JOB_TABLE, _INSTANCE_TABLE} <= _table_names(db)


def test_future_schema_version_is_rejected_fail_closed(tmp_path: Path) -> None:
    db = tmp_path / "future.db"
    store = PersistentTargetInstanceStore(db)
    record = create_target_instance("older", "pdf-document", "", store=store)
    _set_user_version(db, SCHEMA_VERSION + 98)

    with pytest.raises(JobPersistenceError) as job_error:
        PersistentJobStore(db)
    with pytest.raises(InstancePersistenceError) as instance_error:
        PersistentTargetInstanceStore(db)

    assert str(tmp_path) not in str(job_error.value)
    assert str(tmp_path) not in str(instance_error.value)
    # The newer file was not modified in any way: version and rows intact.
    assert _user_version(db) == SCHEMA_VERSION + 98
    connection = sqlite3.connect(db)
    try:
        row = connection.execute(
            f"SELECT payload FROM {_INSTANCE_TABLE} WHERE instance_id = ?", (record.id,)
        ).fetchone()
    finally:
        connection.close()
    assert row is not None


def test_failed_migration_rolls_back_and_leaves_database_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "rollback.db"
    patched = (
        persistence_module._Migration(
            1,
            (
                "CREATE TABLE half_done (id INTEGER PRIMARY KEY)",
                "INSERT INTO does_not_exist DEFAULT VALUES",
            ),
        ),
    )
    with monkeypatch.context() as patch:
        patch.setattr(persistence_module, "_MIGRATIONS", patched)

        with pytest.raises(JobPersistenceError):
            PersistentJobStore(db)

        assert _user_version(db) == 0
        assert "half_done" not in _table_names(db)

    # With the patch removed the real migration applies and the store works.
    store = PersistentJobStore(db)
    assert _user_version(db) == SCHEMA_VERSION
    assert {_JOB_TABLE, _INSTANCE_TABLE} <= _table_names(db)
    job = _failed_job()
    store.put(job)
    assert store.get(job.job_id) == job


def test_migrations_are_ordered_and_ddl_only() -> None:
    targets = [migration.target for migration in persistence_module._MIGRATIONS]
    assert targets == sorted(set(targets))
    assert targets[0] >= 1
    assert targets[-1] == SCHEMA_VERSION

    for migration in persistence_module._MIGRATIONS:
        for statement in migration.statements:
            assert statement.startswith("CREATE TABLE IF NOT EXISTS")
            upper = statement.upper()
            assert "DROP " not in upper
            assert "DELETE " not in upper
            assert "INSERT " not in upper


# --------------------------------------------------------------------------- #
# construction-only migration timing
# --------------------------------------------------------------------------- #


def test_migrations_run_at_construction_and_never_on_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PASSORCH_JOB_DB", raising=False)
    db = tmp_path / "timing.db"
    store = PersistentTargetInstanceStore(db)
    record = create_target_instance("timed", "pdf-document", "", store=store)
    _set_user_version(db, 0)

    # An application bound to the already-open store reads without opening
    # any new persistent store, so the pending migration must not run.
    client = TestClient(create_app(target_instance_store=store))
    assert client.get("/api/target-instances").status_code == 200
    assert client.get(f"/api/target-instances/{record.id}").status_code == 200
    assert _user_version(db) == 0

    # Construction migrates.
    reopened = PersistentTargetInstanceStore(db)
    assert _user_version(db) == SCHEMA_VERSION
    assert reopened.get(record.id) == record


# --------------------------------------------------------------------------- #
# backup and restore
# --------------------------------------------------------------------------- #


def test_backup_creates_a_standalone_consistent_copy(tmp_path: Path) -> None:
    db = tmp_path / "live.db"
    copy = tmp_path / "copy.db"
    store = PersistentTargetInstanceStore(db)
    records = [
        create_target_instance("one", "pdf-document", "", store=store),
        create_target_instance("two", "zip-archive", "", store=store),
    ]

    backup_database(db, copy)

    assert _user_version(copy) == SCHEMA_VERSION
    assert _instance_row_count(copy) == 2

    # Mutating the live database afterwards never reaches the backup.
    store.reset()
    assert _instance_row_count(db) == 0
    restored_copy = PersistentTargetInstanceStore(copy)
    assert [item.id for item in restored_copy.list()] == [item.id for item in records]


def test_restore_from_backup_recovers_records(tmp_path: Path) -> None:
    db = tmp_path / "live.db"
    copy = tmp_path / "copy.db"
    store = PersistentTargetInstanceStore(db)
    records = [
        create_target_instance("alpha", "pdf-document", "", store=store),
        create_target_instance("beta", "zip-archive", "", store=store),
    ]

    backup_database(db, copy)
    store.reset()
    assert len(store) == 0

    backup_database(copy, db)

    reopened = PersistentTargetInstanceStore(db)
    assert [item.id for item in reopened.list()] == [item.id for item in records]
    assert _user_version(db) == SCHEMA_VERSION


def test_backup_fails_closed_on_unusable_arguments(tmp_path: Path) -> None:
    db = tmp_path / "live.db"
    PersistentTargetInstanceStore(db)

    with pytest.raises(PersistenceError) as missing:
        backup_database(tmp_path / "missing.db", tmp_path / "copy.db")
    assert str(tmp_path) not in str(missing.value)
    assert not (tmp_path / "copy.db").exists()

    with pytest.raises(PersistenceError):
        backup_database("   ", tmp_path / "copy.db")
    with pytest.raises(PersistenceError):
        backup_database(db, "   ")

    with pytest.raises(PersistenceError):
        backup_database(db, db)
    alias = db.parent / ".." / db.parent.name / db.name
    with pytest.raises(PersistenceError):
        backup_database(db, alias)

    with pytest.raises(PersistenceError):
        backup_database(db, tmp_path / "no-such-directory" / "copy.db")
    assert not (tmp_path / "no-such-directory").exists()


def test_backup_of_a_live_store_is_available_for_both_stores(tmp_path: Path) -> None:
    db = tmp_path / "shared.db"
    copy = tmp_path / "shared-backup.db"
    store = PersistentTargetInstanceStore(db)
    job_store = PersistentJobStore(db)
    job_store.put(_failed_job())
    create_target_instance("live", "pdf-document", "", store=store)

    # No connection is held between operations, so backup works while
    # both stores are open.
    backup_database(db, copy)
    assert _table_names(copy) == _table_names(db)
    reopened_jobs = PersistentJobStore(copy)
    assert _failed_job().job_id in {job.job_id for job in reopened_jobs.list()}


# --------------------------------------------------------------------------- #
# documentation
# --------------------------------------------------------------------------- #


def test_readme_documents_schema_versioning_and_backup() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "user_version" in readme
    assert "backup_database" in readme
    assert "migrat" in readme.lower()

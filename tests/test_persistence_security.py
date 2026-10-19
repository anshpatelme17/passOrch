"""Phase 8D - persistence security audit and regression tests.

Locks in the audit findings as executable contracts:

* SQL stays static and parameterised - every statement handed to
  ``execute`` / ``write`` is a module constant (the single documented
  exception is the ``PRAGMA user_version`` stamp, whose value is an
  internal integer); ``executescript`` (raw multi-statement SQL) is
  banned outright;
* the persistence layer never logs or prints - failures surface only as
  typed exceptions with generic messages, so no SQL text, path or driver
  string can reach stdout, a log or an API response;
* every GET endpoint is read-only - a persistent database's bytes and
  mtime do not change across repeated reads of health, targets, engines,
  jobs, job stats, job detail, instances, instance detail, the frontend
  shell or the OpenAPI document;
* concurrency fails closed - a write that cannot take the database lock
  raises a PersistenceError (never silent loss, never corruption), and a
  shared store stays thread-safe under parallel writers;
* documented limits stay honest - README states that stores hydrate once
  at construction and do not offer live cross-process visibility.
"""

from __future__ import annotations

import ast
import sqlite3
import threading
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
)
from app.engines.job_persistence import JobPersistenceError, PersistentJobStore
from app.engines.persistence import PersistenceError, backup_database
from app.engines.target_instances import TargetInstanceRecord

_PERSISTENCE_MODULES = (
    Path("app/engines/persistence.py"),
    Path("app/engines/job_persistence.py"),
    Path("app/engines/instance_persistence.py"),
)

#: The only f-string SQL permitted anywhere: PRAGMA values cannot be
#: bound parameters, so the internal integer schema version is formatted
#: in (documented in the module contract).
_PRAGMA_HEAD = "PRAGMA user_version = "


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


def _call_names(tree: ast.AST) -> set[str]:
    return {
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Attribute, ast.Name))
    }


def _imported_names(tree: ast.AST) -> set[str]:
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }


def _sql_arguments(tree: ast.AST) -> list[ast.expr]:
    """The SQL-position argument of every execute / write call."""

    arguments: list[ast.expr] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr in {"execute", "write"} and node.args:
            arguments.append(node.args[0])
    return arguments


def _top_level_constants(tree: ast.Module) -> dict[str, ast.expr]:
    """Top-level assignments: ``name -> value AST`` (SQL lives in these)."""

    constants: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                constants[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                constants[node.target.id] = node.value
    return constants


def _migration_statements(tree: ast.Module) -> list[ast.expr]:
    """The statement expressions declared in the module's ``_MIGRATIONS``."""

    migrations = _top_level_constants(tree).get("_MIGRATIONS")
    if not isinstance(migrations, ast.Tuple):
        raise AssertionError("_MIGRATIONS must be a literal tuple")
    statements: list[ast.expr] = []
    for entry in migrations.elts:
        if (
            isinstance(entry, ast.Call)
            and isinstance(entry.func, ast.Name)
            and entry.func.id == "_Migration"
            and len(entry.args) == 2
            and isinstance(entry.args[1], ast.Tuple)
        ):
            statements.extend(entry.args[1].elts)
        else:
            raise AssertionError(f"unexpected _MIGRATIONS entry: {ast.dump(entry)}")
    return statements


def _failures(tmp_path: Path) -> list[str]:
    """Messages from every documented failure mode, for leak checking."""

    messages: list[str] = []
    with pytest.raises(PersistenceError) as blank:
        PersistentTargetInstanceStore("   ")
    messages.append(str(blank.value))

    directory = tmp_path / "not-a-file"
    directory.mkdir()
    with pytest.raises(PersistenceError) as directory_error:
        PersistentTargetInstanceStore(directory)
    messages.append(str(directory_error.value))

    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"definitely not sqlite" * 8)
    with pytest.raises(JobPersistenceError) as corrupt_error:
        PersistentJobStore(corrupt)
    messages.append(str(corrupt_error.value))

    with pytest.raises(PersistenceError) as unsupported:
        future = tmp_path / "future.db"
        PersistentTargetInstanceStore(future)
        connection = sqlite3.connect(future)
        try:
            connection.execute("PRAGMA user_version = 99")
            connection.commit()
        finally:
            connection.close()
        PersistentTargetInstanceStore(future)
    messages.append(str(unsupported.value))

    with pytest.raises(PersistenceError) as same_file:
        never = tmp_path / "never-created.db"
        backup_database(never, never)
    messages.append(str(same_file.value))
    assert not (tmp_path / "never-created.db").exists()
    return messages


# --------------------------------------------------------------------------- #
# static safety: parameterised SQL, no raw scripts, no logging
# --------------------------------------------------------------------------- #


def test_sql_arguments_are_constants_except_the_documented_pragma() -> None:
    for module in _PERSISTENCE_MODULES:
        tree = ast.parse(module.read_text(encoding="utf-8"))
        constants = _top_level_constants(tree)
        for argument in _sql_arguments(tree):
            if isinstance(argument, ast.Constant):
                assert isinstance(argument.value, str)
                continue
            if isinstance(argument, ast.JoinedStr):
                head = "".join(
                    piece.value
                    for piece in argument.values
                    if isinstance(piece, ast.Constant)
                )
                assert head == _PRAGMA_HEAD, f"unexpected f-string SQL in {module}"
                continue
            if isinstance(argument, ast.Name):
                # Either a module-level SQL constant (a string literal) or a
                # pass-through of this method's own parameters - whose callers
                # are checked at their own call sites - or the migration loop
                # variable, whose statements are checked against the literal
                # _MIGRATIONS declaration below.
                if argument.id in {"statement", "sql"}:
                    continue
                declared = constants.get(argument.id)
                assert declared is not None, f"unresolvable SQL name in {module}: {argument.id}"
                assert (
                    isinstance(declared, ast.Constant) and isinstance(declared.value, str)
                ), f"non-literal SQL constant in {module}: {argument.id}"
                continue
            raise AssertionError(f"non-static SQL argument in {module}: {ast.dump(argument)}")


def test_migration_statements_are_string_literals() -> None:
    module = Path("app/engines/persistence.py")
    tree = ast.parse(module.read_text(encoding="utf-8"))
    constants = _top_level_constants(tree)
    for statement in _migration_statements(tree):
        if isinstance(statement, ast.Name):
            statement = constants[statement.id]
        assert isinstance(statement, ast.Constant) and isinstance(statement.value, str)


def test_multi_statement_raw_sql_scripts_are_banned() -> None:
    for module in _PERSISTENCE_MODULES:
        tree = ast.parse(module.read_text(encoding="utf-8"))
        assert "executescript" not in _call_names(tree)


def test_persistence_layer_never_logs_or_prints() -> None:
    for module in _PERSISTENCE_MODULES:
        tree = ast.parse(module.read_text(encoding="utf-8"))
        imported = _imported_names(tree)
        assert "logging" not in imported
        assert "sys" not in imported
        called = _call_names(tree)
        assert "print" not in called
        assert "pprint" not in called


def test_routes_never_log_or_print() -> None:
    for module in (Path("app/api/routes/jobs.py"), Path("app/api/routes/target_instances.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        assert "logging" not in _imported_names(tree)
        assert "print" not in _call_names(tree)


# --------------------------------------------------------------------------- #
# failure messages stay generic
# --------------------------------------------------------------------------- #


def test_failure_messages_never_contain_paths_sql_or_driver_text(tmp_path: Path) -> None:
    for message in _failures(tmp_path):
        lowered = message.lower()
        assert "sqlite" not in lowered
        assert ".db" not in lowered
        assert "select" not in lowered
        assert "create" not in lowered
        assert "pragma" not in lowered
        assert "traceback" not in lowered


# --------------------------------------------------------------------------- #
# every GET endpoint is side-effect free on a persistent database
# --------------------------------------------------------------------------- #


def test_all_get_endpoints_leave_the_database_untouched(tmp_path: Path) -> None:
    db = tmp_path / "readonly.db"
    client = TestClient(create_app(discovery_registry=_registry(), job_db_path=db))

    instance = client.post(
        "/api/target-instances",
        json={"name": "read-only", "target_type": "pdf-document"},
    )
    assert instance.status_code == 201, instance.text
    instance_id = instance.json()["id"]
    job = client.post("/api/jobs", json={"target": "pdf-document"})
    assert job.status_code == 200, job.text
    job_id = job.json()["job_id"]

    payload_before = db.read_bytes()
    stamp_before = db.stat().st_mtime_ns

    paths = (
        "/health",
        "/api/targets",
        "/api/engines",
        "/api/jobs",
        "/api/jobs/stats",
        f"/api/jobs/{job_id}",
        "/api/target-instances",
        f"/api/target-instances/{instance_id}",
        "/",
        "/openapi.json",
    )
    for _ in range(3):
        for path in paths:
            response = client.get(path)
            assert response.status_code == 200, f"{path} -> {response.status_code}"

    assert db.read_bytes() == payload_before
    assert db.stat().st_mtime_ns == stamp_before


# --------------------------------------------------------------------------- #
# concurrency: fail closed on lock timeout, stay consistent under threads
# --------------------------------------------------------------------------- #


def test_lock_timeout_raises_and_never_loses_or_corrupts_state(tmp_path: Path) -> None:
    db = tmp_path / "locked.db"
    store = PersistentTargetInstanceStore(db)
    baseline = create_target_instance("baseline", "pdf-document", "", store=store)

    # Hold the database's write lock from outside the store, and shrink the
    # store's busy-timeout so the test observes the failure immediately.
    blocker = sqlite3.connect(db)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        store._db._db._timeout = 0.05

        record = TargetInstanceRecord(
            id=str(uuid.uuid4()),
            name="blocked",
            target_type="pdf-document",
            description="",
            created_at=1,
        )
        with pytest.raises(InstancePersistenceError):
            store.put(record)

        # The mirrored insert failed: memory must not show a write the
        # database never accepted, and the baseline stays intact.
        assert store.get(record.id) is None
        assert [item.id for item in store.list()] == [baseline.id]
    finally:
        blocker.rollback()
        blocker.close()

    # Once the lock is released the very same write succeeds.
    store._db._db._timeout = 5.0
    record_after = TargetInstanceRecord(
        id=str(uuid.uuid4()),
        name="after-lock",
        target_type="pdf-document",
        description="",
        created_at=2,
    )
    store.put(record_after)

    reopened = PersistentTargetInstanceStore(db)
    assert len(reopened) == 2
    assert reopened.get(record_after.id) == record_after
    assert reopened.get(baseline.id) == baseline


def test_shared_store_stays_thread_safe_under_parallel_writers(tmp_path: Path) -> None:
    db = tmp_path / "parallel.db"
    store = PersistentTargetInstanceStore(db, capacity=1000)
    failures: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            for item in range(10):
                create_target_instance(f"thread-{index}-{item}", "pdf-document", "", store=store)
        except BaseException as exc:  # noqa: BLE001 - collected for the assertion below
            failures.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert len(store) == 40

    reopened = PersistentTargetInstanceStore(db)
    assert len(reopened) == 40
    assert {item.id for item in reopened.list()} == {item.id for item in store.list()}


# --------------------------------------------------------------------------- #
# documented limits stay honest
# --------------------------------------------------------------------------- #


def test_readme_states_the_hydrate_once_and_process_limits() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "hydrates once at construction" in readme
    assert "do not see each other's writes live" in readme
    assert "never touch the database" in readme

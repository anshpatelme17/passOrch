"""Deterministic concurrency tests for the SQLite-mirrored stores.

The persistent stores keep two views in agreement - the bounded in-memory
container and the mirrored rows in SQLite - behind one RLock. These tests
exercise the interleavings that require under real thread contention:

* concurrent creates land exactly one whole record per request, never a
  torn record or a capacity violation;
* capacity-bearing evictions agree across memory and SQLite - both views
  see the same surviving set;
* a database failure during a mutation restores both views to the exact
  prior state (compensation is deterministic);
* readers, which share the same lock, never observe a record whose write
  fails and is rolled back.

Nothing here uses sleeps or timing assertions: every assertion is either
an aggregate over joined threads, a forced-failure outcome, or a monotonic
"never observed" sample collected while a sibling thread runs.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.engines.instance_persistence import (
    InstancePersistenceError,
    PersistentTargetInstanceStore,
)
from app.engines.job_persistence import (
    JobPersistenceError,
    PersistentJobStore,
)
from app.engines.jobs import AuditJob, JobStatus
from app.engines.target_instances import TargetInstanceRecord

_JOB_UUID_TEMPLATE = "00000000-0000-4000-8000-{index:012x}"


def _job(index: int) -> AuditJob:
    return AuditJob(
        job_id=_JOB_UUID_TEMPLATE.format(index=index),
        status=JobStatus.FAILED,
        created_at=1_700_000_000 + index,
        target="pdf-document",
        failure_reason="no_preview",
    )


def _instance(index: int) -> TargetInstanceRecord:
    return TargetInstanceRecord(
        id=str(uuid.uuid4()),
        name=f"concurrent-{index:02d}",
        target_type="zip-archive",
        description=f"description-{index:02d}",
        created_at=1_700_000_000 + index,
    )


def _db_ids(db_path: Path, table: str, column: str) -> set[str]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(f"SELECT {column} FROM {table}").fetchall()
    return {str(row[0]) for row in rows}


# ---------------------------------------------------------------------------
# concurrent creates: whole records, one row each, no capacity violation
# ---------------------------------------------------------------------------


def test_concurrent_job_creates_land_whole_records_and_mirror_exactly(tmp_path: Path) -> None:
    db = tmp_path / "records.db"
    total = 24
    store = PersistentJobStore(db)  # one shared store, as in the real app

    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = list(pool.map(store.put, [_job(index) for index in range(total)]))

    assert len({job.job_id for job in jobs}) == total
    memory_ids = {job.job_id for job in store.list()}
    assert len(memory_ids) == total
    assert memory_ids == _db_ids(db, "passorch_jobs", "job_id")


def test_concurrent_instance_creates_land_whole_records_and_mirror_exactly(
    tmp_path: Path,
) -> None:
    db = tmp_path / "instances.db"
    total = 24
    store = PersistentTargetInstanceStore(db)  # one shared store, as in the real app

    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(store.put, [_instance(index) for index in range(total)]))

    assert len({record.id for record in records}) == total
    memory_ids = {record.id for record in store.list()}
    assert len(memory_ids) == total
    assert memory_ids == _db_ids(db, "passorch_instances", "instance_id")
    by_id = {record.id: record for record in store.list()}
    for record in records:
        assert by_id[record.id].name == record.name
        assert by_id[record.id].description == record.description


# ---------------------------------------------------------------------------
# capacity: eviction under contention keeps both views on the same survivors
# ---------------------------------------------------------------------------


def test_concurrent_job_puts_do_not_exceed_capacity_and_views_agree(tmp_path: Path) -> None:
    db = tmp_path / "bounded.db"
    capacity = 7
    total = 20
    store = PersistentJobStore(db, capacity=capacity)  # shared, as in the real app

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(store.put, [_job(index) for index in range(total)]))

    assert len(store) == capacity
    memory_ids = {job.job_id for job in store.list()}
    # Reopen: hydration prunes to capacity, and both views must agree.
    fresh = PersistentJobStore(db, capacity=capacity)
    assert len(fresh) == capacity
    assert _db_ids(db, "passorch_jobs", "job_id") == {job.job_id for job in fresh.list()}
    assert memory_ids == _db_ids(db, "passorch_jobs", "job_id")


def test_concurrent_instance_puts_do_not_exceed_capacity_and_views_agree(
    tmp_path: Path,
) -> None:
    db = tmp_path / "bounded-instances.db"
    capacity = 7
    total = 20
    store = PersistentTargetInstanceStore(db, capacity=capacity)  # shared, as in the real app

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(store.put, [_instance(index) for index in range(total)]))

    assert len(store) == capacity
    memory_ids = {record.id for record in store.list()}
    fresh = PersistentTargetInstanceStore(db, capacity=capacity)
    assert len(fresh) == capacity
    assert _db_ids(db, "passorch_instances", "instance_id") == {
        record.id for record in fresh.list()
    }
    assert memory_ids == _db_ids(db, "passorch_instances", "instance_id")


# ---------------------------------------------------------------------------
# duplicate races: exactly one winner, and both views carry it
# ---------------------------------------------------------------------------


def test_concurrent_identical_job_puts_exactly_one_wins(tmp_path: Path) -> None:
    db = tmp_path / "duplicate.db"
    job = _job(99)
    store = PersistentJobStore(db)
    errors: list[Exception] = []
    lock = threading.Lock()

    def put() -> None:
        try:
            store.put(job)
        except Exception as exc:  # noqa: BLE001 - aggregate, asserted below
            with lock:
                errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: put(), range(8)))

    assert len(errors) == 7
    assert _db_ids(db, "passorch_jobs", "job_id") == {job.job_id}
    assert store.get(job.job_id) is not None


def test_concurrent_identical_instance_puts_exactly_one_wins(tmp_path: Path) -> None:
    db = tmp_path / "instance-duplicate.db"
    record = _instance(98)
    store = PersistentTargetInstanceStore(db)
    outcomes: list[str] = []
    lock = threading.Lock()

    def put() -> None:
        try:
            store.put(record)
            with lock:
                outcomes.append("stored")
        except Exception:
            with lock:
                outcomes.append("rejected")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: put(), range(8)))

    assert sorted(outcomes) == ["rejected"] * 7 + ["stored"]
    assert _db_ids(db, "passorch_instances", "instance_id") == {record.id}
    assert store.get(record.id) is not None


# ---------------------------------------------------------------------------
# forced failure under contention: both views roll back deterministically
# ---------------------------------------------------------------------------


def test_eviction_failure_under_contention_restores_both_views(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "eviction-failure.db"
    store = PersistentJobStore(db, capacity=3)
    seeded = [store.put(_job(index)) for index in range(3)]

    def _explode(job: AuditJob, victim_job_id: str) -> None:
        raise JobPersistenceError("failed to write a job record")

    monkeypatch.setattr(store._db, "insert_job_with_eviction", _explode)

    def put(index: int) -> None:
        with pytest.raises(JobPersistenceError):
            store.put(_job(100 + index))

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(put, range(4)))

    # list() is newest-first: reversed seeded insertion order
    assert [job.job_id for job in store.list()] == [job.job_id for job in reversed(seeded)]
    assert _db_ids(db, "passorch_jobs", "job_id") == {job.job_id for job in seeded}

    monkeypatch.undo()
    recovery = store.put(_job(200))
    survivors = _db_ids(db, "passorch_jobs", "job_id")
    assert len(survivors) == 3
    assert recovery.job_id in survivors
    assert survivors >= {job.job_id for job in seeded[1:]} | {recovery.job_id}


def test_instance_eviction_failure_under_contention_restores_both_views(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "instance-eviction-failure.db"
    store = PersistentTargetInstanceStore(db, capacity=3)
    seeded = [store.put(_instance(index)) for index in range(3)]

    def _explode(record: TargetInstanceRecord, victim_id: str) -> None:
        raise InstancePersistenceError("failed to write an instance record")

    monkeypatch.setattr(store._db, "insert_instance_with_eviction", _explode)

    def put(index: int) -> None:
        with pytest.raises(InstancePersistenceError):
            store.put(_instance(100 + index))

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(put, range(4)))

    # instance list() is oldest-first (insertion order); job list() is reversed
    assert [record.id for record in store.list()] == [record.id for record in seeded]
    assert _db_ids(db, "passorch_instances", "instance_id") == {
        record.id for record in seeded
    }


def test_plain_insert_failure_rolls_back_memory_truthfully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "plain-failure.db"
    store = PersistentJobStore(db)

    def _explode(job: AuditJob) -> None:
        raise JobPersistenceError("failed to write a job record")

    monkeypatch.setattr(store._db, "insert_job", _explode)
    with pytest.raises(JobPersistenceError):
        store.put(_job(300))

    assert len(store) == 0
    assert _db_ids(db, "passorch_jobs", "job_id") == set()

    monkeypatch.undo()
    store.put(_job(301))
    assert len(store) == 1
    assert _db_ids(db, "passorch_jobs", "job_id") == {_JOB_UUID_TEMPLATE.format(index=301)}


# ---------------------------------------------------------------------------
# readers during a failing write never see a phantom, both stores
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("store_kind", ["job", "instance"])
def test_readers_during_a_failing_write_never_observe_the_phantom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store_kind: str
) -> None:
    if store_kind == "job":
        db = tmp_path / "reader.db"
        store = PersistentJobStore(db)
        seed = store.put(_job(400))

        def _explode(job: AuditJob) -> None:
            raise JobPersistenceError("failed to write a job record")

        monkeypatch.setattr(store._db, "insert_job", _explode)
        phantom = _job(401)
    else:
        db = tmp_path / "reader-instance.db"
        store = PersistentTargetInstanceStore(db)
        seed = store.put(_instance(400))

        def _explode(record: TargetInstanceRecord) -> None:
            raise InstancePersistenceError("failed to write an instance record")

        monkeypatch.setattr(store._db, "insert_instance", _explode)
        phantom = _instance(401)

    saw_phantom = threading.Event()
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            if store.get(phantom.job_id if store_kind == "job" else phantom.id):
                saw_phantom.set()

    thread = threading.Thread(target=reader)
    thread.start()
    for _ in range(8):
        with pytest.raises((JobPersistenceError, InstancePersistenceError)):
            store.put(phantom)
    stop.set()
    thread.join(timeout=10)

    assert not saw_phantom.is_set()
    # the seeded record is still stored, and the memory and DB views agree
    if store_kind == "job":
        assert len(store) == 1
        assert _db_ids(db, "passorch_jobs", "job_id") == {seed.job_id}
    else:
        assert len(store) == 1
        assert _db_ids(db, "passorch_instances", "instance_id") == {seed.id}


# ---------------------------------------------------------------------------
# put/delete churn: threads that only put-then-delete their own ids
# ---------------------------------------------------------------------------


def test_concurrent_put_then_delete_leaves_store_empty_and_db_empty(tmp_path: Path) -> None:
    db = tmp_path / "churn.db"
    store = PersistentJobStore(db)  # shared, as in the real app

    def churn(index: int) -> None:
        job = _job(index)
        store.put(job)
        assert store.delete(job.job_id) is True

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(churn, range(16)))

    assert len(store) == 0
    assert _db_ids(db, "passorch_jobs", "job_id") == set()
    assert len(PersistentJobStore(db)) == 0

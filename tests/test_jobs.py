"""Phase 3B.1 - job data layer tests.

Coverage for the pure, self-contained job layer (``app/engines/jobs.py``):

* ``JobStatus`` lifecycle values and terminal-state predicate;
* ``AuditJob`` - frozen, slotted, validated, JSON-safe, composes
  ``PlanPreview.as_dict()`` verbatim and never re-serialises it;
* ``JobStore`` - bounded, thread-safe, FIFO-by-insertion-order (never
  ``created_at``), canonical-UUID keys, duplicate rejection, forbidden-field
  scan over the shared constants, ``None``-leak-free reads;
* concurrency - writers/readers under contention never exceed capacity and a
  duplicate race resolves to exactly one stored job;
* static security boundary - no process execution imports or calls, no shell,
  and only the three shared forbidden-field constants are borrowed from the
  pipeline modules.

Previews come from the unchanged ``preview_plan`` pipeline (deterministic
mock simulation) so the job store is exercised with *real* pipeline output
instead of hand-built fakes.
"""

from __future__ import annotations

import ast
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path

import pytest

from app.core.orchestrator.plan import PlanPreview, preview_plan
from app.engines.adapter import FORBIDDEN_REQUEST_FIELDS, ExecutionStatus
from app.engines.discovery.models import (
    CapabilityStatus,
    EngineCapabilities,
    EngineSnapshot,
    JOHN,
)
from app.engines.discovery.registry import FORBIDDEN_PAYLOAD_KEYS, DiscoveryRegistry
from app.engines.discovery.service import FORBIDDEN_REFRESH_FIELDS
from app.engines.jobs import (
    DEFAULT_JOB_STORE_CAPACITY,
    FORBIDDEN_JOB_FIELDS,
    TERMINAL_JOB_STATUSES,
    AuditJob,
    DuplicateJobError,
    InvalidJobError,
    JobError,
    JobStatus,
    JobStore,
    default_job_store,
    is_terminal,
)

JOBS_MODULE = Path("app/engines/jobs.py")

VERIFIED = CapabilityStatus.VERIFIED
UNKNOWN = CapabilityStatus.UNKNOWN

#: Representative forbidden keys drawn from all three shared constant sets.
FORBIDDEN_KEY_SAMPLE = (
    "command",
    "argv",
    "executable",
    "path",
    "shell",
    "subprocess",
    "run",
    "raw_output",
    "probe_output",
    "stdout",
    "stderr",
    "output",
)

MALFORMED_UUIDS = (
    "",
    "not-a-uuid",
    "00000000-0000-4000-8000-0000000000AB",
    "00000000 0000 4000 8000 000000000001",
    "00000000000040008000000000000001",
    "0123456789abcdef0123456789abcdef",
    "00000000-0000-4000-8000-00000000001",
)


def _uuid(i: int) -> str:
    """A canonical lowercase UUID whose tail is a zero-padded hex index."""

    return f"00000000-0000-4000-8000-{i:012x}"


def _registry(status: CapabilityStatus) -> DiscoveryRegistry:
    registry = DiscoveryRegistry()
    registry.register(
        EngineSnapshot(engine=JOHN, discovered=True, binary_found=True)
    )
    registry.set_capabilities(
        JOHN,
        EngineCapabilities(
            engine=JOHN, capability_status=status, formats=("pdf",)
        ),
    )
    return registry


def _authorized_preview() -> PlanPreview:
    """A fully simulated preview (authorized + mock result)."""

    return preview_plan("pdf-document", discovery_registry=_registry(VERIFIED))


def _denied_preview() -> PlanPreview:
    """A structured denial: no plan, no simulation result."""

    return preview_plan("pdf-document", discovery_registry=_registry(UNKNOWN))


def _job(**overrides: object) -> AuditJob:
    fields: dict[str, object] = {
        "job_id": _uuid(1),
        "status": JobStatus.SIMULATED,
        "created_at": 1,
        "target": "pdf-document",
        "preview": _authorized_preview(),
        "failure_reason": "",
    }
    fields.update(overrides)
    return AuditJob(**fields)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class _PoisonedJob(AuditJob):
    """AuditJob whose serialisation smears a forbidden key at the top level."""

    hidden_key: str = "command"
    hidden_value: str = "tainted"

    def as_dict(self) -> dict[str, object]:
        payload = AuditJob.as_dict(self)
        payload[self.hidden_key] = self.hidden_value
        return payload


@dataclass(frozen=True, slots=True)
class _NestedPoisonedJob(AuditJob):
    """AuditJob whose serialisation hides a forbidden key deep inside preview."""

    hidden_key: str = "argv"

    def as_dict(self) -> dict[str, object]:
        payload = AuditJob.as_dict(self)
        payload["preview"] = {"inner": {"deeper": {self.hidden_key: "tainted"}}}
        return payload


def _poisoned(**overrides: object) -> _PoisonedJob:
    fields: dict[str, object] = {
        "job_id": _uuid(7),
        "status": JobStatus.SIMULATED,
        "created_at": 1,
        "target": "pdf-document",
        "preview": _authorized_preview(),
        "failure_reason": "",
        "hidden_key": "command",
    }
    fields.update(overrides)
    return _PoisonedJob(**fields)  # type: ignore[arg-type]


def _nested_poisoned(**overrides: object) -> _NestedPoisonedJob:
    fields: dict[str, object] = {
        "job_id": _uuid(8),
        "status": JobStatus.SIMULATED,
        "created_at": 1,
        "target": "pdf-document",
        "preview": _authorized_preview(),
        "failure_reason": "",
        "hidden_key": "argv",
    }
    fields.update(overrides)
    return _NestedPoisonedJob(**fields)  # type: ignore[arg-type]


def _join(threads: list[threading.Thread]) -> None:
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()


# --------------------------------------------------------------------------- #
# JobStatus
# --------------------------------------------------------------------------- #


def test_job_status_values_are_explicit() -> None:
    assert JobStatus.PENDING.value == "pending"
    assert JobStatus.SIMULATED.value == "simulated"
    assert JobStatus.DENIED.value == "denied"
    assert JobStatus.FAILED.value == "failed"


def test_job_status_is_a_str_enum_and_exhaustive() -> None:
    assert set(JobStatus) == {
        JobStatus.PENDING,
        JobStatus.SIMULATED,
        JobStatus.DENIED,
        JobStatus.FAILED,
    }
    assert isinstance(JobStatus.PENDING, str)


def test_terminal_job_statuses_are_the_terminal_states() -> None:
    assert set(TERMINAL_JOB_STATUSES) == {
        JobStatus.SIMULATED,
        JobStatus.DENIED,
        JobStatus.FAILED,
    }
    assert JobStatus.PENDING not in TERMINAL_JOB_STATUSES


def test_is_terminal_is_terminal_only_and_tolerant() -> None:
    assert is_terminal(JobStatus.SIMULATED) is True
    assert is_terminal(JobStatus.DENIED) is True
    assert is_terminal(JobStatus.FAILED) is True
    assert is_terminal(JobStatus.PENDING) is False
    # ``JobStatus`` mixes ``str``: an identical plain string value matches a
    # terminal member, while non-matching and non-string objects never do.
    assert is_terminal("simulated") is True
    assert is_terminal("denied") is True
    assert is_terminal("pending") is False
    assert is_terminal("bogus") is False
    assert is_terminal(None) is False
    assert is_terminal(object()) is False


def test_job_errors_are_a_closed_hierarchy() -> None:
    assert issubclass(InvalidJobError, JobError)
    assert issubclass(DuplicateJobError, JobError)


# --------------------------------------------------------------------------- #
# AuditJob
# --------------------------------------------------------------------------- #


def test_audit_job_field_defaults() -> None:
    job = AuditJob(
        job_id=_uuid(1), status=JobStatus.PENDING, created_at=0, target="x"
    )
    assert job.preview is None
    assert job.failure_reason == ""


def test_audit_job_is_frozen() -> None:
    job = _job()
    with pytest.raises(FrozenInstanceError):
        job.job_id = _uuid(2)  # type: ignore[misc]


def test_audit_job_uses_slots() -> None:
    assert not hasattr(_job(), "__dict__")


def test_audit_job_rejects_non_string_job_id() -> None:
    with pytest.raises(InvalidJobError):
        AuditJob(
            job_id=42,  # type: ignore[arg-type]
            status=JobStatus.SIMULATED,
            created_at=1,
            target="pdf-document",
        )


def test_audit_job_rejects_non_jobstatus_status() -> None:
    with pytest.raises(InvalidJobError):
        AuditJob(
            job_id=_uuid(1),
            status="simulated",  # type: ignore[arg-type]
            created_at=1,
            target="pdf-document",
        )


def test_audit_job_accepts_created_at_zero() -> None:
    assert AuditJob(
        job_id=_uuid(1), status=JobStatus.SIMULATED, created_at=0, target="x"
    ).created_at == 0


def test_audit_job_rejects_non_int_created_at() -> None:
    for bad in (True, 1.5, "1", None):
        with pytest.raises(InvalidJobError):
            AuditJob(
                job_id=_uuid(1),
                status=JobStatus.SIMULATED,
                created_at=bad,  # type: ignore[arg-type]
                target="pdf-document",
            )


def test_audit_job_rejects_non_string_target() -> None:
    with pytest.raises(InvalidJobError):
        AuditJob(
            job_id=_uuid(1),
            status=JobStatus.SIMULATED,
            created_at=1,
            target=42,  # type: ignore[arg-type]
        )


def test_audit_job_rejects_foreign_preview_types() -> None:
    for foreign in (object(), {}, "preview"):
        with pytest.raises(InvalidJobError):
            AuditJob(
                job_id=_uuid(1),
                status=JobStatus.SIMULATED,
                created_at=1,
                target="pdf-document",
                preview=foreign,  # type: ignore[arg-type]
            )


def test_audit_job_rejects_non_string_failure_reason() -> None:
    with pytest.raises(InvalidJobError):
        AuditJob(
            job_id=_uuid(1),
            status=JobStatus.FAILED,
            created_at=1,
            target="pdf-document",
            failure_reason=None,  # type: ignore[arg-type]
        )


def test_audit_job_status_properties() -> None:
    simulated = _job()
    denied = AuditJob(
        job_id=_uuid(2),
        status=JobStatus.DENIED,
        created_at=1,
        target="pdf-document",
        preview=_denied_preview(),
    )
    failed = AuditJob(
        job_id=_uuid(3),
        status=JobStatus.FAILED,
        created_at=1,
        target="pdf-document",
        preview=None,
        failure_reason="boom",
    )
    assert simulated.is_simulated is True
    assert simulated.is_denied is False
    assert simulated.is_failed is False
    assert denied.is_denied is True
    assert denied.is_simulated is False
    assert failed.is_failed is True


# --------------------------------------------------------------------------- #
# AuditJob.as_dict / result
# --------------------------------------------------------------------------- #


def test_as_dict_top_level_is_the_closed_job_shape() -> None:
    assert set(_job().as_dict()) == {
        "job_id",
        "status",
        "created_at",
        "target",
        "failure_reason",
        "preview",
    }


def test_as_dict_composes_the_preview_verbatim() -> None:
    job = _job()
    prev = job.preview
    assert prev is not None
    assert job.as_dict()["preview"] == prev.as_dict()


def test_as_dict_marks_simulated_status_and_emits_simulation() -> None:
    job = _job()
    payload = job.as_dict()
    assert payload["status"] == "simulated"
    assert payload["preview"]["simulation"]["status"] == "simulated"
    assert payload["preview"]["simulation"]["simulation_id"]


def test_as_dict_omits_preview_key_when_there_is_no_preview() -> None:
    job = AuditJob(
        job_id=_uuid(5),
        status=JobStatus.FAILED,
        created_at=1,
        target="pdf-document",
        failure_reason="boom",
    )
    assert job.as_dict() == {
        "job_id": _uuid(5),
        "status": "failed",
        "created_at": 1,
        "target": "pdf-document",
        "failure_reason": "boom",
    }


def test_result_returns_the_simulation_for_a_simulated_job() -> None:
    job = _job()
    prev = job.preview
    assert prev is not None and prev.simulation is not None
    assert job.result() is prev.simulation
    assert job.result() is not None
    assert job.result().status is ExecutionStatus.SIMULATED
    assert len(job.result().simulation_id) == 16


def test_result_is_none_for_a_denied_job() -> None:
    job = AuditJob(
        job_id=_uuid(6),
        status=JobStatus.DENIED,
        created_at=2,
        target="pdf-document",
        preview=_denied_preview(),
    )
    assert job.preview is not None and job.preview.simulation is None
    assert job.result() is None


def test_result_is_none_when_there_is_no_preview() -> None:
    job = AuditJob(
        job_id=_uuid(6),
        status=JobStatus.FAILED,
        created_at=2,
        target="pdf-document",
        failure_reason="boom",
    )
    assert job.result() is None


# --------------------------------------------------------------------------- #
# JobStore: capacity
# --------------------------------------------------------------------------- #


def test_store_default_capacity_is_100() -> None:
    assert JobStore().capacity == DEFAULT_JOB_STORE_CAPACITY == 100


def test_store_accepts_an_explicit_capacity() -> None:
    assert JobStore(capacity=7).capacity == 7


@pytest.mark.parametrize("bad", [0, -1, 1.5, "5", True, None])
def test_store_rejects_invalid_capacities(bad: object) -> None:
    with pytest.raises(InvalidJobError):
        JobStore(capacity=bad)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "other,expected",
    [
        (JobStore(capacity=1).capacity, 1),
        (JobStore().capacity, DEFAULT_JOB_STORE_CAPACITY),
    ],
)
def test_store_capacity_is_a_plain_property(other: int, expected: int) -> None:
    assert other == expected


# --------------------------------------------------------------------------- #
# JobStore: put
# --------------------------------------------------------------------------- #


def test_put_accepts_all_terminal_statuses() -> None:
    store = JobStore()
    store.put(
        AuditJob(
            job_id=_uuid(1),
            status=JobStatus.SIMULATED,
            created_at=1,
            target="pdf-document",
            preview=_authorized_preview(),
        )
    )
    store.put(
        AuditJob(
            job_id=_uuid(2),
            status=JobStatus.DENIED,
            created_at=2,
            target="pdf-document",
            preview=_denied_preview(),
        )
    )
    store.put(
        AuditJob(
            job_id=_uuid(3),
            status=JobStatus.FAILED,
            created_at=3,
            target="pdf-document",
            preview=None,
            failure_reason="boom",
        )
    )
    assert len(store) == 3


def test_put_returns_the_stored_instance() -> None:
    store = JobStore()
    job = _job()
    assert store.put(job) is job
    assert store.get(job.job_id) is job


def test_put_rejects_non_auditjob_values() -> None:
    store = JobStore()
    for probe in ({}, [], None, "job", 42):
        with pytest.raises(InvalidJobError):
            store.put(probe)  # type: ignore[arg-type]
    assert len(store) == 0


def test_put_rejects_pending_jobs() -> None:
    store = JobStore()
    pending = AuditJob(
        job_id=_uuid(9),
        status=JobStatus.PENDING,
        created_at=1,
        target="pdf-document",
        preview=None,
    )
    with pytest.raises(InvalidJobError, match="terminal"):
        store.put(pending)
    assert len(store) == 0


@pytest.mark.parametrize("bad_uuid", MALFORMED_UUIDS)
def test_put_rejects_non_canonical_uuids(bad_uuid: str) -> None:
    store = JobStore()
    with pytest.raises(InvalidJobError, match="UUID"):
        store.put(_job(job_id=bad_uuid))
    assert len(store) == 0


def test_put_rejects_duplicates_and_leaves_the_store_unchanged() -> None:
    store = JobStore()
    store.put(_job(job_id=_uuid(1)))
    with pytest.raises(DuplicateJobError):
        store.put(_job(job_id=_uuid(1)))
    assert len(store) == 1
    assert store.get(_uuid(1)) is not None


def test_put_allows_reuse_of_a_job_id_once_the_record_is_evicted() -> None:
    store = JobStore(capacity=1)
    store.put(_job(job_id=_uuid(1)))
    store.put(_job(job_id=_uuid(2)))
    assert store.get(_uuid(1)) is None
    store.put(_job(job_id=_uuid(1)))
    assert len(store) == 1
    assert store.get(_uuid(1)) is not None


@pytest.mark.parametrize("key", FORBIDDEN_KEY_SAMPLE)
def test_put_rejects_jobs_serialising_forbidden_field_keys(key: str) -> None:
    store = JobStore()
    job = _poisoned(hidden_key=key)
    assert isinstance(job, AuditJob)
    with pytest.raises(InvalidJobError, match="forbidden"):
        store.put(job)
    assert len(store) == 0


@pytest.mark.parametrize("key", ("command", "argv", "raw_output"))
def test_put_forbidden_scan_reaches_nested_preview_objects(key: str) -> None:
    store = JobStore()
    with pytest.raises(InvalidJobError, match="forbidden"):
        store.put(_nested_poisoned(hidden_key=key))
    assert len(store) == 0


# --------------------------------------------------------------------------- #
# JobStore: get / exists / list
# --------------------------------------------------------------------------- #


def test_get_returns_the_stored_job() -> None:
    store = JobStore()
    job = _job()
    store.put(job)
    assert store.get(job.job_id) is job


def test_get_is_none_for_an_unknown_canonical_uuid() -> None:
    store = JobStore()
    store.put(_job(job_id=_uuid(1)))
    assert store.get(_uuid(999)) is None


@pytest.mark.parametrize(
    "probe",
    ["", "not-a-uuid", 42, None, ["x"], _uuid(3), "00000000-0000-4000-8000-0000000000ab"],
)
def test_get_is_none_for_malformed_or_unknown_identifiers(probe: object) -> None:
    assert JobStore().get(probe) is None


def test_get_cannot_distinguish_unknown_from_malformed() -> None:
    store = JobStore()
    store.put(_job(job_id=_uuid(1)))
    assert store.get(_uuid(2)) is None
    assert store.get("junk") is None


def test_exists_reflects_presence() -> None:
    store = JobStore()
    store.put(_job(job_id=_uuid(1)))
    assert store.exists(_uuid(1)) is True
    assert store.exists(_uuid(2)) is False
    assert store.exists("junk") is False


def test_get_returns_none_after_fifo_eviction() -> None:
    store = JobStore(capacity=1)
    first = _job(job_id=_uuid(1))
    store.put(first)
    store.put(_job(job_id=_uuid(2)))
    assert store.get(first.job_id) is None


def test_list_is_newest_first() -> None:
    store = JobStore()
    store.put(_job(job_id=_uuid(1), created_at=1))
    store.put(_job(job_id=_uuid(2), created_at=2))
    store.put(_job(job_id=_uuid(3), created_at=3))
    listed = store.list()
    assert [job.job_id for job in listed] == [_uuid(3), _uuid(2), _uuid(1)]


def test_list_is_deterministic() -> None:
    store = JobStore()
    for i in range(1, 6):
        store.put(_job(job_id=_uuid(i), created_at=i))
    assert store.list() == store.list()


def test_list_is_bounded_by_capacity() -> None:
    store = JobStore(capacity=2)
    for i in range(1, 6):
        store.put(_job(job_id=_uuid(i), created_at=i))
    assert len(store.list()) == 2
    assert len(store) == 2


# --------------------------------------------------------------------------- #
# JobStore: FIFO eviction
# --------------------------------------------------------------------------- #


def test_fifo_eviction_removes_the_oldest_inserted_job() -> None:
    store = JobStore(capacity=2)
    store.put(_job(job_id=_uuid(1), created_at=1))
    store.put(_job(job_id=_uuid(2), created_at=2))
    store.put(_job(job_id=_uuid(3), created_at=3))
    assert store.get(_uuid(1)) is None
    assert store.get(_uuid(2)) is not None
    assert store.get(_uuid(3)) is not None
    assert [job.job_id for job in store.list()] == [_uuid(3), _uuid(2)]


def test_fifo_eviction_ignores_created_at() -> None:
    store = JobStore(capacity=2)
    first = _job(job_id=_uuid(1), created_at=10)
    second = _job(job_id=_uuid(2), created_at=1)
    store.put(first)
    store.put(second)
    store.put(_job(job_id=_uuid(3), created_at=2))
    assert store.get(first.job_id) is None
    assert store.get(second.job_id) is second
    assert [job.job_id for job in store.list()] == [_uuid(3), _uuid(2)]


# --------------------------------------------------------------------------- #
# JobStore: length / reset / singleton
# --------------------------------------------------------------------------- #


def test_len_tracks_the_store_contents() -> None:
    store = JobStore()
    assert len(store) == 0
    store.put(_job(job_id=_uuid(1)))
    store.put(_job(job_id=_uuid(2)))
    assert len(store) == 2


def test_reset_clears_contents_and_preserves_capacity() -> None:
    store = JobStore(capacity=3)
    for i in range(1, 4):
        store.put(_job(job_id=_uuid(i)))
    store.reset()
    assert len(store) == 0
    assert store.capacity == 3


def test_store_is_usable_after_reset() -> None:
    store = JobStore(capacity=3)
    for i in range(1, 7):
        store.put(_job(job_id=_uuid(i)))
    assert len(store) == 3
    store.reset()
    store.put(_job(job_id=_uuid(100)))
    assert len(store) == 1
    assert store.get(_uuid(100)) is not None


def test_default_job_store_is_a_singleton() -> None:
    assert default_job_store() is default_job_store()


def test_default_job_store_starts_empty_at_default_capacity() -> None:
    assert len(default_job_store()) == 0
    assert default_job_store().capacity == DEFAULT_JOB_STORE_CAPACITY


def test_default_job_store_identity_is_stable_across_threads() -> None:
    barrier = threading.Barrier(8)
    seen: list[JobStore] = []

    def _probe() -> None:
        barrier.wait(timeout=30)
        seen.append(default_job_store())

    threads = [threading.Thread(target=_probe) for _ in range(8)]
    for thread in threads:
        thread.start()
    _join(threads)
    assert len(seen) == 8
    assert all(store is seen[0] for store in seen)


# --------------------------------------------------------------------------- #
# concurrency
# --------------------------------------------------------------------------- #


def test_concurrent_writers_never_exceed_capacity() -> None:
    store = JobStore(capacity=5)
    jobs = [_job(job_id=_uuid(i)) for i in range(1, 33)]
    barrier = threading.Barrier(len(jobs) + 1)

    def _write(job: AuditJob) -> None:
        barrier.wait(timeout=30)
        store.put(job)

    threads = [threading.Thread(target=_write, args=(job,)) for job in jobs]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=30)
    _join(threads)

    assert len(store) == 5
    assert len(store.list()) == 5


def test_concurrent_duplicate_puts_exactly_one_wins() -> None:
    store = JobStore()
    job = _job()
    barrier = threading.Barrier(20)
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def _attempt() -> None:
        try:
            barrier.wait(timeout=30)
            store.put(job)
        except BaseException as exc:  # pragma: no cover - collected below
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=_attempt) for _ in range(20)]
    for thread in threads:
        thread.start()
    _join(threads)

    assert len(store) == 1
    assert store.get(job.job_id) is job
    assert len(errors) == 19
    assert all(isinstance(exc, DuplicateJobError) for exc in errors)


def test_concurrent_readers_and_writers_keep_the_store_consistent() -> None:
    store = JobStore(capacity=8)
    jobs = [_job(job_id=_uuid(i)) for i in range(1, 17)]
    barrier = threading.Barrier(len(jobs) + 8 + 1)
    stop = threading.Event()
    observed: list[int] = []
    observed_lock = threading.Lock()

    def _writer(job: AuditJob) -> None:
        barrier.wait(timeout=30)
        store.put(job)

    def _reader() -> None:
        barrier.wait(timeout=30)
        while not stop.is_set():
            snapshot = store.list()
            with observed_lock:
                observed.append(len(snapshot))
            store.get(_uuid(1))
            store.exists(_uuid(2))
            time.sleep(0)

    writers = [threading.Thread(target=_writer, args=(job,)) for job in jobs]
    readers = [threading.Thread(target=_reader) for _ in range(8)]
    for thread in writers + readers:
        thread.start()
    barrier.wait(timeout=30)
    _join(writers)
    stop.set()
    _join(readers)

    assert len(store) <= 8
    assert all(0 <= size <= 8 for size in observed)


def test_parallel_unique_puts_land_at_expected_fifo_boundary() -> None:
    store = JobStore(capacity=4)
    jobs = [_job(job_id=_uuid(i)) for i in range(1, 9)]

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(store.put, jobs))

    surviving = [job.job_id for job in store.list()]
    assert len(surviving) == len(store) == 4
    assert set(surviving) <= {_uuid(i) for i in range(1, 9)}


# --------------------------------------------------------------------------- #
# static safety boundary (app/engines/jobs.py)
# --------------------------------------------------------------------------- #


def test_jobs_module_never_imports_process_execution() -> None:
    tree = ast.parse(JOBS_MODULE.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not (imported & {"subprocess", "os", "shlex", "pty", "shutil"})


def test_jobs_module_has_no_process_shell_or_eval_calls() -> None:
    tree = ast.parse(JOBS_MODULE.read_text(encoding="utf-8"))
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Attribute, ast.Name))
    }
    for forbidden in (
        "system",
        "popen",
        "Popen",
        "exec",
        "eval",
        "spawnv",
        "check_output",
        "run",
        "subprocess",
    ):
        assert forbidden not in called


def test_jobs_module_declares_no_execution_keyword_field_defaults() -> None:
    tree = ast.parse(JOBS_MODULE.read_text(encoding="utf-8"))
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert not (
        literals
        & {"shell", "exec", "argv", "executable", "command", "path", "run", "subprocess"}
    )


def test_jobs_module_reuses_the_shared_forbidden_constants() -> None:
    assert FORBIDDEN_JOB_FIELDS == frozenset(
        FORBIDDEN_REQUEST_FIELDS
        | FORBIDDEN_REFRESH_FIELDS
        | FORBIDDEN_PAYLOAD_KEYS
    )


def test_jobs_module_imports_only_allowed_names_from_pipeline_modules() -> None:
    tree = ast.parse(JOBS_MODULE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in (
            "app.core.orchestrator.plan",
            "app.engines.adapter",
            "app.engines.discovery.registry",
            "app.engines.discovery.service",
        ):
            imported.update(alias.name for alias in node.names)
    assert imported == {
        "FORBIDDEN_REQUEST_FIELDS",
        "FORBIDDEN_REFRESH_FIELDS",
        "FORBIDDEN_PAYLOAD_KEYS",
        "PlanPreview",
        "SimulationResult",
    }


def test_jobs_module_never_imports_runtime_discovery_or_policy_modules() -> None:
    tree = ast.parse(JOBS_MODULE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    for banned in (
        "app.engines.discovery.john",
        "app.engines.discovery.executor",
        "app.engines.discovery.allowlist",
        "app.engines.discovery.capability_map",
        "app.engines.discovery.models",
        "app.engines.adapter_registry",
        "app.engines.execution_policy",
        "app.engines.selection",
        "app.api",
    ):
        assert banned not in imported


def test_jobs_module_never_calls_pipeline_or_discovery_functions() -> None:
    tree = ast.parse(JOBS_MODULE.read_text(encoding="utf-8"))
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Attribute, ast.Name))
    }
    for forbidden in (
        "run_safe_command",
        "locate_engine_binary",
        "capabilities_from_probes",
        "refresh",
        "authorize",
        "simulate",
        "select",
        "preview_plan",
        "build_execution_plan",
    ):
        assert forbidden not in called
"""Phase 8A - durable job persistence: a SQLite mirror of the job store.

Adds an *opt-in* durable layer on top of the Phase 3B.1 in-memory
:class:`~app.engines.jobs.JobStore` so terminal job records survive a
restart without changing any existing default:

* :class:`JobPersistenceError` - the single, uniform failure this module
  raises (initialisation, read, corruption and write failures alike);
* :class:`PersistentJobStore`  - a ``JobStore`` subclass: the bounded,
  lock-guarded in-memory container stays authoritative for every read while
  ``put`` / ``delete`` / ``reset`` are mirrored to a SQLite database file;
* :func:`configured_job_store` - path resolution (an explicit
  ``create_app(job_db_path=...)`` first, then ``PASSORCH_JOB_DB``).

Contract:

* side-effect free until used - nothing here runs at import time, and a
  database file is only ever created for an explicitly configured path;
* one connection per operation - opened, committed or rolled back and
  closed inside a single call: no lifespan hook, no long-lived handle, no
  leaked lock;
* fail-closed - any database failure raises ``JobPersistenceError``; the
  store never silently degrades to memory-only, never discards an
  unreadable or forbidden record, and ``put`` compensates the in-memory
  copy when the mirrored insert fails so the two views never diverge
  silently;
* metadata only - rows carry the JSON form of ``AuditJob`` (including the
  structured plan-preview graph); no credential or secret is ever part of a
  job record, and every hydrated payload is re-scanned for forbidden
  command / argv / path style keys before it re-enters the store;
* parameterised SQL only, stdlib ``sqlite3`` only - no subprocess, no
  shell, no new dependency.

Ordering and bounds: rows are keyed by an autoincrement ``seq`` so
hydration restores the exact insertion order (the base class documents
newest-first listing and FIFO-by-insertion eviction).  The database
mirrors the bounded store - deleted and evicted rows are removed - and
opening prunes any row beyond ``capacity``, which also self-heals the rare
case of a partially failed write.  Two processes may share one file, but
each store hydrates once at construction: live cross-instance visibility
is not offered.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.core.orchestrator.plan import PlanPreview
from app.engines.adapter import (
    ExecutionMode,
    ExecutionPlan,
    ExecutionRequest,
    ExecutionStatus,
    SimulationResult,
)
from app.engines.discovery.models import CapabilityStatus
from app.engines.execution_policy import AuthorizationStatus, PolicyDecision, mode_label
from app.engines.jobs import (
    DEFAULT_JOB_STORE_CAPACITY,
    AuditJob,
    DuplicateJobError,
    JobError,
    JobStatus,
    JobStore,
)
from app.engines.persistence import (
    PersistenceDatabase,
    PersistenceError,
    as_boolean,
    as_integer,
    as_mapping,
    as_string_tuple,
    as_text,
    collect_forbidden_keys,
    configured_database_path,
    validate_storage_path,
)
from app.engines.selection import (
    SelectionCandidate,
    SelectionDecision,
    SelectionStatus,
)

__all__ = ["JobPersistenceError", "PersistentJobStore", "configured_job_store"]

#: Generic failure messages - never interpolated with SQL text, file paths
#: or driver messages, so nothing sensitive can reach an API response.
_INIT_FAILED = "failed to initialize job database"
_READ_FAILED = "failed to read persisted jobs"
_WRITE_FAILED = "failed to write a job record"
_DELETE_FAILED = "failed to delete a job record"
_CLEAR_FAILED = "failed to clear persisted jobs"
_INVALID_RECORD = "persisted job record is invalid"

_INSERT_JOB_SQL = "INSERT INTO passorch_jobs (job_id, payload) VALUES (?, ?)"
_SELECT_JOBS_SQL = "SELECT seq, payload FROM passorch_jobs ORDER BY seq ASC"
_PRUNE_JOBS_SQL = "DELETE FROM passorch_jobs WHERE seq < ?"
_DELETE_JOB_SQL = "DELETE FROM passorch_jobs WHERE job_id = ?"
_CLEAR_JOBS_SQL = "DELETE FROM passorch_jobs"


class JobPersistenceError(PersistenceError):
    """A database operation (or its configuration) failed.

    The only failure this module surfaces (a subclass of the shared
    :class:`~app.engines.persistence.PersistenceError`).  Routes translate
    it into a structured HTTP 503; the message stays generic so no SQL
    text, path or driver string can leak to an API client.
    """


# --------------------------------------------------------------------------- #
# serialisation: typed records -> JSON (full fidelity, no as_dict shortcuts)
# --------------------------------------------------------------------------- #


def _candidate_to_record(candidate: SelectionCandidate) -> dict[str, object]:
    return {
        "engine": candidate.engine,
        "phase1_name": candidate.phase1_name,
        "priority": candidate.priority,
        "capability_status": candidate.capability_status.value,
        "formats": list(candidate.formats),
        "targets": list(candidate.targets),
        "discovered": candidate.discovered,
    }


def _selection_to_record(selection: SelectionDecision) -> dict[str, object]:
    selected = selection.selected
    return {
        "target": selection.target,
        "status": selection.status.value,
        "selected": None if selected is None else _candidate_to_record(selected),
        "candidates": [_candidate_to_record(item) for item in selection.candidates],
        "reason": selection.reason,
    }


def _request_to_record(request: ExecutionRequest) -> dict[str, object]:
    return {
        "target": request.target,
        "engine": request.engine,
        "mode": mode_label(request.mode),
        "selection_status": request.selection_status,
        "capability_status": request.capability_status,
        "selected_engine": request.selected_engine,
        "matched_formats": list(request.matched_formats),
        "reference": request.reference,
    }


def _authorization_to_record(decision: PolicyDecision) -> dict[str, object]:
    return {
        "status": decision.status.value,
        "reason": decision.reason,
        "detail": decision.detail,
        "problems": list(decision.problems),
        "adapter": decision.adapter,
        "mode": decision.mode,
    }


def _plan_to_record(plan: ExecutionPlan) -> dict[str, object]:
    return {
        "request": _request_to_record(plan.request),
        "mode": plan.mode.value,
        "adapter": plan.adapter,
        "steps": list(plan.steps),
        "simulated": plan.simulated,
    }


def _simulation_to_record(simulation: SimulationResult) -> dict[str, object]:
    return {
        "status": simulation.status.value,
        "adapter": simulation.adapter,
        "engine": simulation.engine,
        "target": simulation.target,
        "mode": simulation.mode.value,
        "steps": list(simulation.steps),
        "message": simulation.message,
        "simulation_id": simulation.simulation_id,
        "simulated": simulation.simulated,
        "metadata": dict(simulation.metadata),
    }


def _preview_to_record(preview: PlanPreview) -> dict[str, object]:
    plan = preview.plan
    simulation = preview.simulation
    return {
        "target": preview.target,
        "selection": _selection_to_record(preview.selection),
        "request": _request_to_record(preview.request),
        "authorization": _authorization_to_record(preview.authorization),
        "plan": None if plan is None else _plan_to_record(plan),
        "simulation": None if simulation is None else _simulation_to_record(simulation),
    }


def _job_to_payload(job: AuditJob) -> str:
    """The JSON row payload for one job record (explicit fields only)."""

    record = {
        "job_id": job.job_id,
        "status": job.status.value,
        "created_at": job.created_at,
        "target": job.target,
        "failure_reason": job.failure_reason,
        "instance_id": job.instance_id,
        "instance_target_type": job.instance_target_type,
        "preview": None if job.preview is None else _preview_to_record(job.preview),
    }
    try:
        return json.dumps(record, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise JobPersistenceError(_WRITE_FAILED) from exc


# --------------------------------------------------------------------------- #
# deserialisation: JSON -> typed records, failing closed on anything odd
# --------------------------------------------------------------------------- #


def _mode_from_label(label: str) -> ExecutionMode | str:
    try:
        return ExecutionMode(label)
    except ValueError:
        return label


def _candidate_from_record(data: object) -> SelectionCandidate:
    record = as_mapping(data, "candidate")
    return SelectionCandidate(
        engine=as_text(record["engine"], "candidate.engine"),
        phase1_name=as_text(record["phase1_name"], "candidate.phase1_name"),
        priority=as_integer(record["priority"], "candidate.priority"),
        capability_status=CapabilityStatus(
            as_text(record["capability_status"], "candidate.capability_status")
        ),
        formats=as_string_tuple(record["formats"], "candidate.formats"),
        targets=as_string_tuple(record["targets"], "candidate.targets"),
        discovered=as_boolean(record["discovered"], "candidate.discovered"),
    )


def _selection_from_record(data: object) -> SelectionDecision:
    record = as_mapping(data, "selection")
    selected = record["selected"]
    candidates = record["candidates"]
    if not isinstance(candidates, list):
        raise TypeError("selection.candidates must be an array")
    return SelectionDecision(
        target=as_text(record["target"], "selection.target"),
        status=SelectionStatus(as_text(record["status"], "selection.status")),
        selected=None if selected is None else _candidate_from_record(selected),
        candidates=tuple(_candidate_from_record(item) for item in candidates),
        reason=as_text(record["reason"], "selection.reason"),
    )


def _request_from_record(data: object) -> ExecutionRequest:
    record = as_mapping(data, "request")
    return ExecutionRequest(
        target=as_text(record["target"], "request.target"),
        engine=as_text(record["engine"], "request.engine"),
        mode=_mode_from_label(as_text(record["mode"], "request.mode")),
        selection_status=as_text(record["selection_status"], "request.selection_status"),
        capability_status=as_text(record["capability_status"], "request.capability_status"),
        selected_engine=as_text(record["selected_engine"], "request.selected_engine"),
        matched_formats=as_string_tuple(record["matched_formats"], "request.matched_formats"),
        reference=as_text(record["reference"], "request.reference"),
    )


def _authorization_from_record(data: object) -> PolicyDecision:
    record = as_mapping(data, "authorization")
    problems = record["problems"]
    if not isinstance(problems, list):
        raise TypeError("authorization.problems must be an array")
    return PolicyDecision(
        status=AuthorizationStatus(as_text(record["status"], "authorization.status")),
        reason=as_text(record["reason"], "authorization.reason"),
        detail=as_text(record["detail"], "authorization.detail"),
        problems=tuple(as_text(item, "authorization.problems[]") for item in problems),
        adapter=as_text(record["adapter"], "authorization.adapter"),
        mode=as_text(record["mode"], "authorization.mode"),
    )


def _plan_from_record(data: object) -> ExecutionPlan:
    record = as_mapping(data, "plan")
    steps = record["steps"]
    if not isinstance(steps, list):
        raise TypeError("plan.steps must be an array")
    return ExecutionPlan(
        request=_request_from_record(record["request"]),
        mode=ExecutionMode(as_text(record["mode"], "plan.mode")),
        adapter=as_text(record["adapter"], "plan.adapter"),
        steps=tuple(as_text(step, "plan.steps[]") for step in steps),
        simulated=as_boolean(record["simulated"], "plan.simulated"),
    )


def _simulation_from_record(data: object) -> SimulationResult:
    record = as_mapping(data, "simulation")
    steps = record["steps"]
    metadata = record["metadata"]
    if not isinstance(steps, list):
        raise TypeError("simulation.steps must be an array")
    if not isinstance(metadata, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in metadata.items()
    ):
        raise TypeError("simulation.metadata must be a string mapping")
    return SimulationResult(
        status=ExecutionStatus(as_text(record["status"], "simulation.status")),
        adapter=as_text(record["adapter"], "simulation.adapter"),
        engine=as_text(record["engine"], "simulation.engine"),
        target=as_text(record["target"], "simulation.target"),
        mode=ExecutionMode(as_text(record["mode"], "simulation.mode")),
        steps=tuple(as_text(step, "simulation.steps[]") for step in steps),
        message=as_text(record["message"], "simulation.message"),
        simulation_id=as_text(record["simulation_id"], "simulation.simulation_id"),
        simulated=as_boolean(record["simulated"], "simulation.simulated"),
        metadata=dict(metadata),
    )


def _preview_from_record(data: object) -> PlanPreview:
    record = as_mapping(data, "preview")
    plan = record["plan"]
    simulation = record["simulation"]
    return PlanPreview(
        target=as_text(record["target"], "preview.target"),
        selection=_selection_from_record(record["selection"]),
        request=_request_from_record(record["request"]),
        authorization=_authorization_from_record(record["authorization"]),
        plan=None if plan is None else _plan_from_record(plan),
        simulation=None if simulation is None else _simulation_from_record(simulation),
    )


def _job_from_record(data: object) -> AuditJob:
    record = as_mapping(data, "job record")
    preview = record["preview"]
    return AuditJob(
        job_id=as_text(record["job_id"], "job_id"),
        status=JobStatus(as_text(record["status"], "status")),
        created_at=as_integer(record["created_at"], "created_at"),
        target=as_text(record["target"], "target"),
        preview=None if preview is None else _preview_from_record(preview),
        failure_reason=as_text(record["failure_reason"], "failure_reason"),
        instance_id=as_text(record["instance_id"], "instance_id"),
        instance_target_type=as_text(record["instance_target_type"], "instance_target_type"),
    )


def _job_from_payload(payload: object) -> AuditJob:
    """Parse one stored payload - fail closed on anything unexpected."""

    try:
        if not isinstance(payload, str):
            raise TypeError("payload must be a string")
        data = json.loads(payload)
        forbidden = collect_forbidden_keys(data)
        if forbidden:
            raise ValueError(f"payload contains forbidden fields: {', '.join(forbidden)}")
        return _job_from_record(data)
    except Exception as exc:
        raise JobPersistenceError(_INVALID_RECORD) from exc


# --------------------------------------------------------------------------- #
# SQLite facade: one connection per operation, parameterised SQL only
# --------------------------------------------------------------------------- #


class _JobDatabase:
    """The ``passorch_jobs`` table behind :class:`PersistentJobStore`.

    A thin domain layer over the shared
    :class:`~app.engines.persistence.PersistenceDatabase`; all driver
    handling, transactions and generic failure messages live there.
    """

    def __init__(self, path: Path) -> None:
        self._db = PersistenceDatabase(path, error_type=JobPersistenceError)

    @property
    def path(self) -> Path:
        return self._db.path

    def open(self, failure: str) -> None:
        """Create the schema and run pending migrations (construction only)."""

        self._db.open(failure)

    def insert_job(self, job: AuditJob) -> None:
        payload = _job_to_payload(job)
        self._db.write(
            _INSERT_JOB_SQL,
            (job.job_id, payload),
            failure=_WRITE_FAILED,
            on_integrity=DuplicateJobError(job.job_id),
        )

    def insert_job_with_eviction(self, job: AuditJob, victim_job_id: str) -> None:
        """Insert ``job`` and delete the FIFO victim row in ONE transaction.

        Both statements run on the same connection so the mirror change is
        all-or-nothing: a failure at either stage (especially the
        victim-row delete) rolls the insert back too, leaving the database
        with neither the new row nor the stale victim row.  The put
        compensation path then restores the in-memory view to match.

        The transaction commits only after both statements succeed.  The
        driver raises for the insert's uniqueness conflict is mapped here
        to the domain duplicate error (``transaction`` itself maps any
        other driver failure onto the generic ``failure`` message); the
        exception still trips the rollback before it propagates.
        """

        payload = _job_to_payload(job)
        with self._db.transaction(_WRITE_FAILED) as connection:
            try:
                connection.execute(_INSERT_JOB_SQL, (job.job_id, payload))
            except sqlite3.IntegrityError as exc:
                raise DuplicateJobError(job.job_id) from exc
            connection.execute(_DELETE_JOB_SQL, (victim_job_id,))

    def delete_job(self, job_id: str) -> bool:
        return self._db.write(_DELETE_JOB_SQL, (job_id,), failure=_DELETE_FAILED) > 0

    def load_jobs(self, capacity: int) -> tuple[AuditJob, ...]:
        with self._db.transaction(_READ_FAILED) as connection:
            rows = connection.execute(_SELECT_JOBS_SQL).fetchall()
            if len(rows) > capacity:
                cutoff_seq = rows[len(rows) - capacity][0]
                connection.execute(_PRUNE_JOBS_SQL, (cutoff_seq,))
                rows = rows[len(rows) - capacity :]
            return tuple(_job_from_payload(payload) for _, payload in rows)

    def clear(self) -> None:
        self._db.write(_CLEAR_JOBS_SQL, (), failure=_CLEAR_FAILED)


# --------------------------------------------------------------------------- #
# public store
# --------------------------------------------------------------------------- #


class PersistentJobStore(JobStore):
    """Bounded in-memory job store with a durable SQLite mirror.

    Reads are served from memory exactly like the base class.  ``put``,
    ``delete`` and ``reset`` additionally mirror the change to the database
    at ``db_path``; a failed mirror write raises :class:`JobPersistenceError`
    and leaves the in-memory view truthful (a failed ``put`` rolls back its
    own insert, a failed ``delete`` removes nothing).  An eviction-bearing
    ``put`` mirrors its insert and FIFO-row delete as one atomic
    transaction, so neither view can report a row the other never saw.

    Opening happens in the constructor: the schema is brought to the
    current version (running any pending Phase 8C migrations, stamping
    ``PRAGMA user_version``) and the newest ``capacity`` rows are
    hydrated in insertion order.  An unusable, corrupt, newer-schema or
    invalid database raises ``JobPersistenceError`` instead of silently
    starting empty.
    """

    def __init__(
        self,
        db_path: str | Path,
        capacity: int = DEFAULT_JOB_STORE_CAPACITY,
    ) -> None:
        super().__init__(capacity)
        validated = validate_storage_path(db_path, error_type=JobPersistenceError)
        self._db = _JobDatabase(validated)
        self._db.open(_INIT_FAILED)
        with self._lock:
            for job in self._db.load_jobs(self._capacity):
                try:
                    super().put(job)
                except JobError as exc:
                    raise JobPersistenceError(_INVALID_RECORD) from exc

    @property
    def db_path(self) -> Path:
        """The configured database file (created on first use)."""

        return self._db.path

    def put(self, job: AuditJob) -> AuditJob:
        """Store a terminal job in memory and in the database.

        Validation is the base class contract (identical exceptions for
        malformed or duplicate jobs).  If the mirrored write fails after
        the in-memory insert, memory is rolled back - including restoring
        any FIFO victim the insert evicted - so neither view reports a
        write that the other side never saw.  When the insert also evicts
        a FIFO victim, the mirror (new row insert + victim row delete) is
        one atomic SQLite transaction: a failure at either stage rolls the
        insert back, so the stale victim row can never linger committed.
        """

        with self._lock:
            victim: tuple[str, AuditJob] | None = None
            if (
                isinstance(job, AuditJob)
                and job.job_id not in self._jobs
                and len(self._jobs) >= self._capacity
            ):
                first_id, first_job = next(iter(self._jobs.items()))
                victim = (first_id, first_job)
            stored = super().put(job)
            try:
                if victim is not None:
                    self._db.insert_job_with_eviction(stored, victim[0])
                else:
                    self._db.insert_job(stored)
            except (JobPersistenceError, DuplicateJobError):
                self._jobs.pop(stored.job_id, None)
                if victim is not None and victim[0] not in self._jobs:
                    self._jobs[victim[0]] = victim[1]
                    self._jobs.move_to_end(victim[0], last=False)
                raise
            return stored

    def delete(self, job_id: object) -> bool:
        """Remove a stored job from the database first, then from memory.

        A database failure raises before memory changes, so the job stays
        deletable; malformed or unknown identifiers answer ``False``
        identically, mirroring the base class contract.
        """

        with self._lock:
            existing = self.get(job_id)
            if existing is None:
                return False
            self._db.delete_job(existing.job_id)
            return super().delete(job_id)

    def reset(self) -> None:
        """Clear the database first, then memory (all-or-nothing on failure)."""

        with self._lock:
            self._db.clear()
            super().reset()


def configured_job_store(
    job_db_path: str | Path | None = None,
) -> PersistentJobStore | None:
    """The persistent store for the configured database path, else ``None``.

    Resolution order: an explicit ``job_db_path`` always wins over the
    ``PASSORCH_JOB_DB`` environment variable.  An unset or blank environment
    variable means "stay in-memory" (``None``), while an explicit blank path
    is a configuration error - never a silent fallback.  Opening failures
    raise :class:`JobPersistenceError`.
    """

    path = configured_database_path(job_db_path, error_type=JobPersistenceError)
    if path is None:
        return None
    return PersistentJobStore(path)

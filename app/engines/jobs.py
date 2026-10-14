"""Phase 3B.1 - job data layer: lifecycle, record, bounded thread-safe store.

This module is a pure, self-contained data layer.  It knows nothing about
discovery, capability refresh, engine selection, authorisation or execution:

* ``JobStatus``  - the explicit lifecycle values;
* ``AuditJob``   - an immutable job record that *optionally* reuses an
                  existing :class:`~app.core.orchestrator.plan.PlanPreview`
                  as its payload (it never re-serialises it);
* ``JobStore``   - a bounded, thread-safe in-memory container.

Security boundary:

* no process execution, no subprocess, no shell;
* no discovery / capability-refresh / policy / adapter invocation;
* the only non-``__future__`` imports are pure data definitions plus the
  existing forbidden-field constants (reused, never duplicated);
* ``put()`` rejects any job whose serialised form contains a forbidden
  command / argv / executable / path / shell / raw-output key.

Lifecycle: the store accepts **only terminal** jobs (``SIMULATED``,
``DENIED``, ``FAILED``).  ``PENDING`` records cannot be stored.

Concurrency: every read and write is guarded by a single ``threading.RLock``;
insertion + FIFO capacity eviction happen under the same lock, so the store
never observes a torn or over-capacity state.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum

from app.core.orchestrator.plan import PlanPreview
from app.engines.adapter import FORBIDDEN_REQUEST_FIELDS, SimulationResult
from app.engines.discovery.registry import FORBIDDEN_PAYLOAD_KEYS
from app.engines.discovery.service import FORBIDDEN_REFRESH_FIELDS

__all__ = [
    "DEFAULT_JOB_STORE_CAPACITY",
    "FORBIDDEN_JOB_FIELDS",
    "TERMINAL_JOB_STATUSES",
    "AuditJob",
    "DuplicateJobError",
    "InvalidJobError",
    "JobError",
    "JobStatus",
    "JobStore",
    "default_job_store",
    "is_terminal",
]

#: Default maximum number of jobs a store retains.
DEFAULT_JOB_STORE_CAPACITY = 100

#: Canonical lowercase UUID (8-4-4-4-12).  *Not* a simulation token.
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

#: Reused forbidden-field constants, combined (never re-implemented).
FORBIDDEN_JOB_FIELDS: frozenset[str] = frozenset(
    FORBIDDEN_REQUEST_FIELDS | FORBIDDEN_REFRESH_FIELDS | FORBIDDEN_PAYLOAD_KEYS
)


class JobStatus(str, Enum):
    """Explicit job lifecycle.

    ``PENDING`` is the transient pre-evaluation state; the store only ever
    contains the terminal states ``SIMULATED`` / ``DENIED`` / ``FAILED``.
    """

    PENDING = "pending"
    SIMULATED = "simulated"
    DENIED = "denied"
    FAILED = "failed"


#: Terminal lifecycle states - the only ones a JobStore may contain.
TERMINAL_JOB_STATUSES: tuple[JobStatus, ...] = (
    JobStatus.SIMULATED,
    JobStatus.DENIED,
    JobStatus.FAILED,
)


def is_terminal(status: object) -> bool:
    """True when ``status`` is one of the terminal job lifecycle states."""

    return status in TERMINAL_JOB_STATUSES


class JobError(Exception):
    """Base class for job-layer failures."""


class InvalidJobError(JobError):
    """Raised when a job (or the store) is structurally invalid."""


class DuplicateJobError(JobError):
    """Raised when a ``job_id`` is already present in the store."""


@dataclass(frozen=True, slots=True)
class AuditJob:
    """Immutable job record.

    ``preview`` reuses the existing :class:`PlanPreview` - the full
    selection / request / authorisation / plan / simulation outcome - and is
    ``None`` only for ``FAILED`` jobs (no preview was produced).  The preview
    fields are never duplicated on the job itself.

    ``instance_id`` / ``instance_target_type`` (Phase 3E) are an optional
    association snapshot: both default to ``""`` for an unassociated job.
    ``instance_target_type`` must equal ``target`` whenever it is set, and the
    pair is validated together.  Neither field is ever re-resolved, so a job
    stays readable after the instance is deleted or evicted.
    """

    job_id: str
    status: JobStatus
    created_at: int
    target: str
    preview: PlanPreview | None = None
    failure_reason: str = ""
    instance_id: str = ""
    instance_target_type: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, str):
            raise InvalidJobError("job_id must be a string")
        if not isinstance(self.status, JobStatus):
            raise InvalidJobError("status must be a JobStatus")
        if not isinstance(self.created_at, int) or isinstance(self.created_at, bool):
            raise InvalidJobError("created_at must be an integer")
        if not isinstance(self.target, str):
            raise InvalidJobError("target must be a string")
        if self.preview is not None and not isinstance(self.preview, PlanPreview):
            raise InvalidJobError("preview must be a PlanPreview or None")
        if not isinstance(self.failure_reason, str):
            raise InvalidJobError("failure_reason must be a string")
        if not isinstance(self.instance_id, str):
            raise InvalidJobError("instance_id must be a string")
        if not isinstance(self.instance_target_type, str):
            raise InvalidJobError("instance_target_type must be a string")
        if self.instance_id and not self.instance_id.strip():
            raise InvalidJobError("instance_id must not be blank when set")
        if self.instance_target_type and self.instance_target_type != self.target:
            raise InvalidJobError(
                "instance_target_type must equal the job target when set"
            )
        if bool(self.instance_id) != bool(self.instance_target_type):
            # the association is a pair: an id without its type snapshot, or a
            # type snapshot without an id, is a malformed record.
            raise InvalidJobError(
                "instance_id and instance_target_type must be set together"
            )

    # -- lifecycle ---------------------------------------------------------- #

    @property
    def is_simulated(self) -> bool:
        return self.status is JobStatus.SIMULATED

    @property
    def is_denied(self) -> bool:
        return self.status is JobStatus.DENIED

    @property
    def is_failed(self) -> bool:
        return self.status is JobStatus.FAILED

    # -- derived accessors -------------------------------------------------- #

    def result(self) -> SimulationResult | None:
        """The audit result (deterministic simulation) when one exists."""

        if self.preview is None:
            return None
        return self.preview.simulation

    # -- serialisation ------------------------------------------------------ #

    def as_dict(self) -> dict[str, object]:
        """JSON-safe view - composes ``preview.as_dict()`` verbatim."""

        payload: dict[str, object] = {
            "job_id": self.job_id,
            "status": self.status.value,
            "created_at": self.created_at,
            "target": self.target,
            "failure_reason": self.failure_reason,
        }
        if self.preview is not None:
            payload["preview"] = self.preview.as_dict()
        payload["instance_id"] = self.instance_id
        payload["instance_target_type"] = self.instance_target_type
        return payload


class JobStore:
    """Bounded, thread-safe, in-memory container for terminal jobs.

    FIFO by *insertion order*: when storage exceeds ``capacity`` the oldest
    inserted job is evicted.  ``created_at`` never drives eviction.
    """

    def __init__(self, capacity: int = DEFAULT_JOB_STORE_CAPACITY) -> None:
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1:
            raise InvalidJobError(f"job store capacity must be an integer >= 1 (got {capacity!r})")
        self._capacity = capacity
        self._jobs: OrderedDict[str, AuditJob] = OrderedDict()
        self._lock = threading.RLock()

    @property
    def capacity(self) -> int:
        return self._capacity

    # -- writes ------------------------------------------------------------- #

    def put(self, job: AuditJob) -> AuditJob:
        """Insert a terminal job, evicting the oldest past capacity.

        Raises ``InvalidJobError`` for structural problems and
        ``DuplicateJobError`` when the ``job_id`` is already stored.
        """

        if not isinstance(job, AuditJob):
            raise InvalidJobError("an AuditJob is required to store a job")
        if not _UUID_RE.fullmatch(job.job_id):
            raise InvalidJobError(f"job_id is not a canonical UUID: {job.job_id!r}")
        if not is_terminal(job.status):
            raise InvalidJobError(
                f"only terminal jobs may be stored (got status {job.status.value!r})"
            )
        if not isinstance(job.target, str):
            raise InvalidJobError("job target must be a string")
        if not isinstance(job.instance_id, str) or (
            job.instance_id and not job.instance_id.strip()
        ):
            raise InvalidJobError("job instance_id must be a non-blank string or \"\"")
        if not isinstance(job.instance_target_type, str):
            raise InvalidJobError("job instance_target_type must be a string")
        if job.instance_target_type and job.instance_target_type != job.target:
            raise InvalidJobError("job instance_target_type must equal the job target")
        if bool(job.instance_id) != bool(job.instance_target_type):
            raise InvalidJobError(
                "job instance_id and instance_target_type must be set together"
            )

        forbidden = self._forbidden_keys(job.as_dict())
        if forbidden:
            raise InvalidJobError(f"job serialises forbidden fields: {', '.join(forbidden)}")

        with self._lock:
            if job.job_id in self._jobs:
                raise DuplicateJobError(job.job_id)
            self._jobs[job.job_id] = job
            if len(self._jobs) > self._capacity:
                self._jobs.popitem(last=False)
        return job

    @staticmethod
    def _forbidden_keys(value: object) -> tuple[str, ...]:
        """Recursively collect forbidden keys anywhere in ``value``."""

        found: list[str] = []
        if isinstance(value, dict):
            for key, item in value.items():
                if key in FORBIDDEN_JOB_FIELDS:
                    found.append(key)
                found.extend(JobStore._forbidden_keys(item))
        elif isinstance(value, (list, tuple)):
            for item in value:
                found.extend(JobStore._forbidden_keys(item))
        return tuple(sorted(set(found)))

    # -- reads -------------------------------------------------------------- #

    def get(self, job_id: object) -> AuditJob | None:
        """The stored job for a canonical UUID, else ``None``.

        Malformed and unknown identifiers return ``None`` identically - no
        caller can distinguish them.
        """

        if not isinstance(job_id, str) or not _UUID_RE.fullmatch(job_id):
            return None
        with self._lock:
            return self._jobs.get(job_id)

    def exists(self, job_id: object) -> bool:
        return self.get(job_id) is not None

    def delete(self, job_id: object) -> bool:
        """Remove the job for a canonical UUID.

        Returns ``True`` when a job was removed, ``False`` when the identifier
        was malformed or no such job existed - mirroring the indistinguishability
        contract of :meth:`get`.

        The deletion does not affect capacity or FIFO eviction of other jobs.
        """

        if not isinstance(job_id, str) or not _UUID_RE.fullmatch(job_id):
            return False
        with self._lock:
            if job_id in self._jobs:
                del self._jobs[job_id]
                return True
            return False

    def list(self) -> tuple[AuditJob, ...]:
        """Stored jobs, newest-first, deterministic, bounded by capacity."""

        with self._lock:
            return tuple(reversed(self._jobs.values()))

    # -- lifecycle ---------------------------------------------------------- #

    def reset(self) -> None:
        with self._lock:
            self._jobs.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._jobs)


def default_job_store() -> JobStore:
    """Process-wide store used when no store is injected."""

    return _DEFAULT_JOB_STORE


_DEFAULT_JOB_STORE = JobStore()

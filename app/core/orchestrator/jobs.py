"""Phase 3B.2 - job orchestration: create terminal audit jobs.

``create_job`` composes the existing, unchanged ``preview_plan`` pipeline
(selection -> request -> policy -> plan -> mock simulation) with the Phase 3B.1
data layer (``AuditJob`` / ``JobStore``).  It is the *only* authorised way to
produce and persist a job record:

* ``job_status_for`` maps a plan preview to an explicit terminal status
  (``SIMULATED`` / ``DENIED`` / ``FAILED``) - never ``PENDING``;
* ``failure_reason_for`` records the policy reason for a denial or an
  internal ``authorized_without_simulation`` outcome;
* ``create_job`` mints a canonical UUID ``job_id`` (disjoint from ``simulation_id``),
  stamps an epoch ``created_at`` via an injectable clock and stores the record
  in an injectable :class:`~app.engines.jobs.JobStore` (default: the process-wide
  singleton from Phase 3B.1).

Security boundary (mirrors the rest of the reconstruction):

* no process execution, no subprocess, no shell;
* the only pipeline function called is ``preview_plan`` - discovery refresh,
  policy internals and adapters are never invoked directly here;
* no borrowing of discovery / policy / selection internals beyond type hints.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

from app.core.orchestrator.plan import PlanPreview, preview_plan
from app.engines.adapter import ExecutionMode
from app.engines.adapter_registry import EngineAdapterRegistry
from app.engines.discovery.registry import DiscoveryRegistry
from app.engines.execution_policy import ExecutionPolicy
from app.engines.jobs import AuditJob, JobStatus, JobStore, default_job_store

__all__ = ["create_job", "failure_reason_for", "job_status_for"]


def job_status_for(preview: PlanPreview | None) -> JobStatus:
    """Map a plan preview to the explicit terminal status to record.

    ``DENIED`` when the policy did not authorise, ``SIMULATED`` when a mock
    simulation result exists, ``FAILED`` when the pipeline claimed success but
    produced no simulation.  A missing preview is ``FAILED`` too.
    """

    if preview is None:
        return JobStatus.FAILED
    if not preview.authorized:
        return JobStatus.DENIED
    if preview.simulation is None:
        return JobStatus.FAILED
    return JobStatus.SIMULATED


def failure_reason_for(preview: PlanPreview | None) -> str:
    """The recorded failure/denial reason - or ``""`` for a simulated job."""

    if preview is None:
        return "no_preview"
    if not preview.authorization.authorized:
        return preview.authorization.reason or "denied"
    if preview.simulation is None:
        return "authorized_without_simulation"
    return ""


def create_job(
    target: str,
    *,
    discovery_registry: DiscoveryRegistry | None = None,
    adapter_registry: EngineAdapterRegistry | None = None,
    policy: ExecutionPolicy | None = None,
    mode: ExecutionMode | str = ExecutionMode.SIMULATED,
    reference: str = "",
    instance_id: str = "",
    instance_target_type: str = "",
    job_id: str | None = None,
    clock: Callable[[], int] | None = None,
    store: JobStore | None = None,
) -> AuditJob:
    """Run the preview pipeline and persist a terminal, bounded audit job.

    ``job_id`` is a canonical lowercase UUID (freshly minted unless supplied);
    ``created_at`` is the epoch second from ``clock`` (real time by default).
    ``instance_id`` / ``instance_target_type`` (Phase 3E) are an optional
    association snapshot recorded as-is and never re-resolved; both default to
    ``""``.  The record is stored through ``store.put`` (Phase 3B.1 - bounded,
    FIFO, thread-safe) so structural and duplicate failures raise
    :class:`~app.engines.jobs.InvalidJobError` /
    :class:`~app.engines.jobs.DuplicateJobError`.
    """

    now = clock if clock is not None else lambda: int(time.time())
    identifier = job_id if isinstance(job_id, str) and job_id else str(uuid.uuid4())

    preview = preview_plan(
        target,
        discovery_registry=discovery_registry,
        adapter_registry=adapter_registry,
        policy=policy,
        mode=mode,
        reference=reference,
    )

    status = job_status_for(preview)
    reason = failure_reason_for(preview)
    record = AuditJob(
        job_id=identifier,
        status=status,
        created_at=now(),
        target=preview.target if preview is not None else "",
        preview=None if status is JobStatus.FAILED else preview,
        failure_reason=reason,
        instance_id=instance_id,
        instance_target_type=instance_target_type,
    )

    job_store = store if isinstance(store, JobStore) else default_job_store()
    return job_store.put(record)
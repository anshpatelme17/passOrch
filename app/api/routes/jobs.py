"""Phase 2D/3B.3/3D - jobs API routes (read-only + create).

Endpoints:
* ``POST /api/jobs`` - create and record a terminal job (or denial/failure)
* ``GET  /api/jobs`` - list stored jobs in newest-first order
* ``GET  /api/jobs/{job_id}`` - fetch one recorded job (404 if not found)

All routes remain simulation-only, reuse the existing orchestrator and
Phase 3B.1 store, never trigger discovery/refresh, and never leak raw
discovery output or command fields. Malformed and unknown IDs return the
same ``job_not_found`` contract.

Phase 3D: ``POST /api/jobs`` requires an explicit ``target`` selected by the
caller and validated against the read-only ``TARGET_FORMAT_POLICY`` map. An
unmapped target is a transport-level 422 with a structured detail; it is never
guessed from ``reference`` and never defaulted.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from app.api.schemas import JobCreateRequest, JobInfo, JobListResponse, JobSummary
from app.core.orchestrator.jobs import create_job
from app.engines.adapter_registry import EngineAdapterRegistry, default_adapter_registry
from app.engines.discovery.capability_map import TARGET_FORMAT_POLICY
from app.engines.discovery.registry import DiscoveryRegistry
from app.engines.execution_policy import ExecutionPolicy
from app.engines.jobs import AuditJob, JobStore, default_job_store
from app.engines.target_instances import (
    TargetInstanceStore,
    default_target_instance_store,
)

__all__ = ["create", "detail", "list_jobs", "router", "unknown_target_detail"]

#: Stable machine-readable code for a target missing from the format policy.
UNKNOWN_TARGET_ERROR = "unknown_target"

#: Structured detail for an ``instance_id`` that is malformed or absent.
#: ``target_instance_id`` is echoed verbatim (or ``""``); malformed and
#: unknown identifiers are deliberately indistinguishable.
INSTANCE_NOT_FOUND_ERROR = "target_instance_not_found"

#: Structured detail for an instance whose ``target_type`` differs from the
#: requested ``target``.  Both values are echoed so a client can show which
#: of the two is wrong without a second round-trip.
INSTANCE_TYPE_MISMATCH_ERROR = "target_instance_type_mismatch"

#: Stable, structured detail for a target that is not in ``TARGET_FORMAT_POLICY``.
#: ``supported_targets`` mirrors the read-only catalog exposed by
#: ``GET /api/targets`` so a caller can build a picker without a second request.
UNKNOWN_TARGET_DETAIL = {
    "error": UNKNOWN_TARGET_ERROR,
    "detail": "target is not mapped to an enabled format policy",
    "supported_targets": sorted(TARGET_FORMAT_POLICY),
}

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def unknown_target_detail(target: object) -> dict[str, object]:
    """Structured 422 detail naming the rejected target and the valid ones.

    Both the outer mapping and the ``supported_targets`` list are rebuilt per
    call, so a caller mutating the result can never reach the shared constant
    (or a detail already handed to another caller).
    """

    detail = dict(UNKNOWN_TARGET_DETAIL)
    detail["supported_targets"] = list(UNKNOWN_TARGET_DETAIL["supported_targets"])
    detail["target"] = target if isinstance(target, str) else ""
    return detail


def instance_not_found_detail(instance_id: object) -> dict[str, object]:
    """Structured 422 detail for a malformed or absent instance ID.

    The shape is identical for malformed and unknown identifiers so the
    response cannot be used to probe which instance IDs exist.
    """

    return {
        "error": INSTANCE_NOT_FOUND_ERROR,
        "detail": "target instance does not exist",
        "target_instance_id": instance_id if isinstance(instance_id, str) else "",
    }


def instance_type_mismatch_detail(target: str, instance_target_type: str) -> dict[str, object]:
    """Structured 422 detail for an instance whose type is not the request target."""

    return {
        "error": INSTANCE_TYPE_MISMATCH_ERROR,
        "detail": "target instance target_type does not match the requested target",
        "target": target,
        "instance_target_type": instance_target_type,
    }


def _resolve_job_store(request: Request) -> JobStore:
    state = request.app.state
    store = getattr(state, "job_store", None)
    if isinstance(store, JobStore):
        return store
    return default_job_store()


def _resolve_target_instance_store(request: Request) -> TargetInstanceStore:
    """The target-instance store **belonging to this application**.

    An injected store always wins over the process-wide default, so an
    instance registered against another app can never be resolved here.
    """

    store = getattr(request.app.state, "target_instance_store", None)
    if isinstance(store, TargetInstanceStore):
        return store
    return default_target_instance_store()


def _resolve_discovery_registry(request: Request) -> DiscoveryRegistry | None:
    registry = getattr(request.app.state, "discovery_registry", None)
    if isinstance(registry, DiscoveryRegistry):
        return registry
    return None


def _resolve_adapter_registry(request: Request) -> EngineAdapterRegistry:
    registry = getattr(request.app.state, "adapter_registry", None)
    if isinstance(registry, EngineAdapterRegistry):
        return registry
    return default_adapter_registry()


def _resolve_policy(request: Request) -> ExecutionPolicy:
    policy = getattr(request.app.state, "execution_policy", None)
    if isinstance(policy, ExecutionPolicy):
        return policy
    return ExecutionPolicy(_resolve_adapter_registry(request))


@router.post("", response_model=JobInfo)
def create(request: Request, body: JobCreateRequest) -> JobInfo:
    """Create and persist a terminal audit job (simulated/denied/failed).

    The target must be an entry of the read-only ``TARGET_FORMAT_POLICY`` map;
    anything else is a structured 422 and never reaches job creation. ``mode``
    stays a free string so an unsupported value is answered by the policy with
    a structured denial instead of a transport error.

    When ``instance_id`` is supplied it must name an existing instance in *this*
    application's store whose ``target_type`` equals ``target``. The resolved
    pair is snapshotted onto the job and never re-resolved, so a failure here
    stores nothing and a later instance deletion cannot unread a job.
    """

    if body.target not in TARGET_FORMAT_POLICY:
        raise HTTPException(
            status_code=422,
            detail=unknown_target_detail(body.target),
        )

    instance_id = body.instance_id if body.instance_id is not None else ""
    instance_target_type = ""
    if body.instance_id is not None:
        # An explicitly supplied (even empty) identifier is validated, never
        # silently downgraded to "no association": that would hide client bugs
        # the way the old implicit target default did.
        instance = _resolve_target_instance_store(request).get(instance_id)
        if instance is None:
            raise HTTPException(
                status_code=422,
                detail=instance_not_found_detail(instance_id),
            )
        if instance.target_type != body.target:
            raise HTTPException(
                status_code=422,
                detail=instance_type_mismatch_detail(body.target, instance.target_type),
            )
        instance_target_type = instance.target_type

    job = create_job(
        body.target,
        discovery_registry=_resolve_discovery_registry(request),
        adapter_registry=_resolve_adapter_registry(request),
        policy=_resolve_policy(request),
        mode=body.mode,
        reference=body.reference,
        instance_id=instance_id,
        instance_target_type=instance_target_type,
        store=_resolve_job_store(request),
    )
    return JobInfo(**job.as_dict())


@router.get("", response_model=JobListResponse)
def list_jobs(request: Request) -> JobListResponse:
    """List stored jobs, newest-first, bounded by capacity."""

    store = _resolve_job_store(request)
    jobs: list[AuditJob] = list(store.list())
    summaries = [
        JobSummary(
            job_id=j.job_id,
            status=j.status.value,
            created_at=j.created_at,
            target=j.target,
        )
        for j in jobs
    ]
    return JobListResponse(jobs=summaries, count=len(summaries))


@router.get("/{job_id}", response_model=JobInfo)
def detail(job_id: str, request: Request) -> JobInfo:
    """Return one recorded job; 404 for malformed or unknown IDs."""

    store = _resolve_job_store(request)
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job_not_found")
    return JobInfo(**job.as_dict())


@router.delete("/{job_id}")
def delete_job(job_id: str, request: Request) -> Response:
    """Delete a recorded job; 204 on success, 404 for malformed or unknown IDs."""

    store = _resolve_job_store(request)
    if not store.delete(job_id):
        raise HTTPException(status_code=404, detail="job_not_found")
    return Response(status_code=204)

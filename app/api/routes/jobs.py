"""Phase 2D/3B.3/3D/7B/7C/8A - jobs API routes (read-only + create).

Endpoints:
* ``POST /api/jobs`` - create and record a terminal job (or denial/failure);
  answers 503 with a generic structured detail when persistent job storage
  is configured but the write fails (Phase 8A)
* ``GET  /api/jobs`` - list stored jobs with optional ``status`` / ``target`` /
  ``instance_id`` filters, ``sort`` (``created_desc`` default, ``created_asc``)
  and ``limit`` / ``offset`` pagination; newest-first by default
* ``GET  /api/jobs/{job_id}`` - fetch one recorded job (404 if not found)
* ``DELETE /api/jobs/{job_id}`` - delete a recorded job (204/404; 503 on a
  failed persistence write)

All routes remain simulation-only, reuse the existing orchestrator and
Phase 3B.1 store, never trigger discovery/refresh, and never leak raw
discovery output or command fields. Malformed and unknown IDs return the
same ``job_not_found`` contract. Reads are served from the in-memory store
and never touch the database; a ``JobPersistenceError`` from a write is
translated here into HTTP 503 with no SQL text, path or driver message.

Phase 3D: ``POST /api/jobs`` requires an explicit ``target`` selected by the
caller and validated against the read-only ``TARGET_FORMAT_POLICY`` map. An
unmapped target is a transport-level 422 with a structured detail; it is never
guessed from ``reference`` and never defaulted.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from app.api.errors import invalid_request, job_not_found
from app.api.schemas import JobCreateRequest, JobInfo, JobPageResponse, JobStatsResponse, JobSummary
from app.api.store_resolution import (
    persistence_unavailable_detail,
    resolve_job_store,
    resolve_target_instance_store,
)
from app.core.orchestrator.jobs import create_job
from app.engines.adapter_registry import EngineAdapterRegistry, default_adapter_registry
from app.engines.discovery.capability_map import TARGET_FORMAT_POLICY
from app.engines.discovery.registry import DiscoveryRegistry
from app.engines.execution_policy import ExecutionPolicy
from app.engines.job_persistence import JobPersistenceError
from app.engines.jobs import AuditJob

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

#: Stable machine-readable code for a target that is not in ``TARGET_FORMAT_POLICY``.
#: ``supported_targets`` mirrors the read-only catalog exposed by
#: ``GET /api/targets`` so a caller can build a picker without a second request.
UNKNOWN_TARGET_DETAIL: dict[str, object] = {
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

    detail: dict[str, object] = dict(UNKNOWN_TARGET_DETAIL)
    detail["supported_targets"] = sorted(TARGET_FORMAT_POLICY)
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

    When a persistent job database is configured and the mirrored write
    fails, the job was not stored: the response is a structured 503 with no
    database detail (Phase 8A).
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
        instance = resolve_target_instance_store(request).get(instance_id)
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

    try:
        job = create_job(
            body.target,
            discovery_registry=_resolve_discovery_registry(request),
            adapter_registry=_resolve_adapter_registry(request),
            policy=_resolve_policy(request),
            mode=body.mode,
            reference=body.reference,
            instance_id=instance_id,
            instance_target_type=instance_target_type,
            store=resolve_job_store(request),
        )
    except JobPersistenceError as exc:
        raise HTTPException(
            status_code=503,
            detail=persistence_unavailable_detail(),
        ) from exc
    return JobInfo.model_validate(job.as_dict())


#: Accepted ``sort`` modes for ``GET /api/jobs``.  ``created_desc`` (the
#: default) is exactly the store's documented newest-first order;
#: ``created_asc`` is its exact reversal, so both directions are deterministic
#: even when several jobs share a ``created_at`` second.
JOB_SORT_MODES: tuple[str, ...] = ("created_desc", "created_asc")


@router.get("", response_model=JobPageResponse)
def list_jobs(
    request: Request,
    status: str | None = None,
    target: str | None = None,
    instance_id: str | None = None,
    sort: str = "created_desc",
    limit: int = 50,
    offset: int = 0,
) -> JobPageResponse:
    """List stored jobs with optional filtering, sorting and pagination.

    Filters (``status``, ``target``, ``instance_id``) are plain value matches
    applied **before** pagination: ``total`` always counts the filtered result
    set, and an unknown value simply yields an empty page (HTTP 200). An empty
    ``instance_id=`` matches jobs recorded with no instance association.
    ``sort`` is a mode, not a filter, so an unrecognised value is a 422.
    """

    if limit < 1 or limit > 100:
        raise invalid_request("limit must be between 1 and 100")
    if offset < 0:
        raise invalid_request("offset must be non-negative")
    if sort not in JOB_SORT_MODES:
        raise invalid_request("sort must be one of: " + ", ".join(JOB_SORT_MODES))

    store = resolve_job_store(request)
    jobs: list[AuditJob] = list(store.list())

    if status is not None:
        jobs = [j for j in jobs if j.status.value == status]
    if target is not None:
        jobs = [j for j in jobs if j.target == target]
    if instance_id is not None:
        jobs = [j for j in jobs if j.instance_id == instance_id]

    if sort == "created_asc":
        jobs.reverse()

    total = len(jobs)
    paginated = jobs[offset : offset + limit]
    summaries = [
        JobSummary(
            job_id=j.job_id,
            status=j.status.value,
            created_at=j.created_at,
            target=j.target,
        )
        for j in paginated
    ]
    return JobPageResponse(
        jobs=summaries,
        count=len(summaries),
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/stats", response_model=JobStatsResponse)
def job_stats(request: Request) -> JobStatsResponse:
    """Read-only aggregation over the job store."""

    store = resolve_job_store(request)
    jobs: list[AuditJob] = list(store.list())

    by_status: dict[str, int] = {}
    by_target: dict[str, int] = {}
    simulated = denied = failed = 0

    for job in jobs:
        status = job.status.value
        by_status[status] = by_status.get(status, 0) + 1
        target = job.target or ""
        by_target[target] = by_target.get(target, 0) + 1
        if status == "simulated":
            simulated += 1
        elif status == "denied":
            denied += 1
        elif status == "failed":
            failed += 1

    return JobStatsResponse(
        total=len(jobs),
        simulated=simulated,
        denied=denied,
        failed=failed,
        by_status=by_status,
        by_target=by_target,
    )


@router.get("/{job_id}", response_model=JobInfo)
def detail(job_id: str, request: Request) -> JobInfo:
    """Return one recorded job; 404 for malformed or unknown IDs."""

    store = resolve_job_store(request)
    job = store.get(job_id)
    if job is None:
        raise job_not_found()
    return JobInfo.model_validate(job.as_dict())


@router.delete("/{job_id}")
def delete_job(job_id: str, request: Request) -> Response:
    """Delete a recorded job; 204 on success, 404 for malformed or unknown IDs.

    With persistent storage configured, a failed database delete answers 503
    and the job stays stored (memory is only cleared after the database
    write succeeds).
    """

    store = resolve_job_store(request)
    try:
        deleted = store.delete(job_id)
    except JobPersistenceError as exc:
        raise HTTPException(
            status_code=503,
            detail=persistence_unavailable_detail(),
        ) from exc
    if not deleted:
        raise job_not_found()
    return Response(status_code=204)

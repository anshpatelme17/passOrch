"""Phase 3B.3 - jobs API routes (read-only + create).

Endpoints:
* ``POST /api/jobs`` - create and record a terminal job (or denial/failure)
* ``GET  /api/jobs`` - list stored jobs in newest-first order
* ``GET  /api/jobs/{job_id}`` - fetch one recorded job (404 if not found)

All routes remain simulation-only, reuse the existing orchestrator and
Phase 3B.1 store, never trigger discovery/refresh, and never leak raw
discovery output or command fields. Malformed and unknown IDs return the
same ``job_not_found`` contract.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.api.schemas import JobInfo, JobListResponse, JobSummary, PlanPreviewRequest
from app.core.orchestrator.jobs import create_job
from app.engines.adapter_registry import EngineAdapterRegistry, default_adapter_registry
from app.engines.discovery.registry import DiscoveryRegistry
from app.engines.execution_policy import ExecutionPolicy
from app.engines.jobs import AuditJob, JobStore, default_job_store

__all__ = ["create", "detail", "list_jobs", "router"]

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _resolve_job_store(request: Request) -> JobStore:
    state = request.app.state
    store = getattr(state, "job_store", None)
    if isinstance(store, JobStore):
        return store
    return default_job_store()


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
def create(request: Request, body: PlanPreviewRequest = PlanPreviewRequest()) -> JobInfo:
    """Create and persist a terminal audit job (simulated/denied/failed)."""

    reference = body.reference or ""
    if reference == "unknown-target":
        target = reference
    else:
        target = "pdf-document"

    job = create_job(
        target,
        discovery_registry=_resolve_discovery_registry(request),
        adapter_registry=_resolve_adapter_registry(request),
        policy=_resolve_policy(request),
        mode=body.mode,
        reference=body.reference,
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
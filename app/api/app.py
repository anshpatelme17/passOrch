"""Phase 2D/3A/3B.3 - FastAPI application factory.

Simulation-only surface: the app exposes a read-only target list, a read-only
engine capability list, a structured plan-preview endpoint, and job endpoints
(`POST/GET /api/jobs` and `GET /api/jobs/{job_id}`). Nothing in this module
(or anywhere it wires up) accepts a command, argv, executable path or real
execution mode, and no endpoint can trigger engine probing.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.api.routes.engines import router as engines_router
from app.api.routes.jobs import router as jobs_router
from app.api.routes.target_instances import router as target_instances_router
from app.api.routes.targets import router as targets_router
from app.engines.adapter_registry import EngineAdapterRegistry, default_adapter_registry
from app.engines.discovery.registry import DiscoveryRegistry, default_discovery_registry
from app.engines.execution_policy import ExecutionPolicy
from app.engines.jobs import JobStore, default_job_store
from app.engines.target_instances import TargetInstanceStore, default_target_instance_store

__all__ = ["create_app", "app"]


def create_app(
    *,
    discovery_registry: DiscoveryRegistry | None = None,
    adapter_registry: EngineAdapterRegistry | None = None,
    execution_policy: ExecutionPolicy | None = None,
    job_store: JobStore | None = None,
    target_instance_store: TargetInstanceStore | None = None,
) -> FastAPI:
    """Build the PassOrch API with injectable read-only registries and store."""

    application = FastAPI(
        title="PassOrch (simulated)",
        version="2d.0",
        description=(
            "Capability-aware password-auditing orchestration platform - "
            "simulation-only plan preview. No engine is ever executed."
        ),
    )
    application.state.discovery_registry = (
        discovery_registry if discovery_registry is not None else default_discovery_registry()
    )
    effective_adapters = (
        adapter_registry if adapter_registry is not None else default_adapter_registry()
    )
    application.state.adapter_registry = effective_adapters
    # A default policy is bound to the injected adapter registry so an
    # injected registry is never bypassed by the process-wide default.
    application.state.execution_policy = (
        execution_policy if execution_policy is not None else ExecutionPolicy(effective_adapters)
    )
    application.state.job_store = (
        job_store if isinstance(job_store, JobStore) else default_job_store()
    )
    application.state.target_instance_store = (
        target_instance_store
        if isinstance(target_instance_store, TargetInstanceStore)
        else default_target_instance_store()
    )
    application.include_router(targets_router)
    application.include_router(engines_router)
    application.include_router(jobs_router)
    application.include_router(target_instances_router)

    @application.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok", "mode": "simulated"}

    return application


app = create_app()

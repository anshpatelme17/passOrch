"""Phase 2D - target API routes (read-only list + plan preview).

* ``GET  /api/targets`` - list the read-only target format policy.
* ``POST /api/targets/{target_id}/compatibility/plan`` - structured plan
  preview for one mapped target.

The preview endpoint runs selection -> request -> policy -> plan -> mock
simulation.  It never executes anything, never accepts a command and returns
structured denial information instead of a plan when the policy says no.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.api.schemas import (
    PlanPreviewRequest,
    PlanPreviewResponse,
    TargetListResponse,
    TargetSummary,
)
from app.core.orchestrator.plan import preview_plan
from app.engines.adapter_registry import EngineAdapterRegistry, default_adapter_registry
from app.engines.discovery.capability_map import TARGET_FORMAT_POLICY, target_policy
from app.engines.discovery.registry import DiscoveryRegistry
from app.engines.execution_policy import ExecutionPolicy

__all__ = ["list_targets", "plan_preview", "router"]

router = APIRouter(prefix="/api/targets", tags=["targets"])


@router.get("", response_model=TargetListResponse)
def list_targets() -> TargetListResponse:
    """Every mapped target from the read-only capability map."""

    summaries = [
        TargetSummary(
            target=policy.target,
            description=policy.description,
            formats=list(policy.formats),
            enabled=policy.enabled,
            usable=policy.usable,
        )
        for policy in TARGET_FORMAT_POLICY.values()
    ]
    return TargetListResponse(targets=summaries, count=len(summaries))


@router.post("/{target_id}/compatibility/plan", response_model=PlanPreviewResponse)
def plan_preview(
    target_id: str,
    request: Request,
    body: PlanPreviewRequest = PlanPreviewRequest(),
) -> PlanPreviewResponse:
    """Structured, simulation-only plan preview for one mapped target."""

    if target_policy(target_id) is None:
        raise HTTPException(status_code=404, detail="target_not_mapped")

    state = request.app.state
    discovery_registry = getattr(state, "discovery_registry", None)
    if not isinstance(discovery_registry, DiscoveryRegistry):
        discovery_registry = None

    adapter_registry = getattr(state, "adapter_registry", None)
    if not isinstance(adapter_registry, EngineAdapterRegistry):
        adapter_registry = default_adapter_registry()

    policy = getattr(state, "execution_policy", None)
    if not isinstance(policy, ExecutionPolicy):
        policy = ExecutionPolicy(adapter_registry)

    preview = preview_plan(
        target_id,
        discovery_registry=discovery_registry,
        policy=policy,
        mode=body.mode,
        reference=body.reference,
    )
    return PlanPreviewResponse(**preview.as_dict())

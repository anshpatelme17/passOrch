"""Phase 2D - execution orchestration (simulation only).

Pipeline implemented here::

    Target
      -> discovery registry + capability-aware selection   (Phase 2C)
      -> ExecutionRequest                                   (app.engines.adapter)
      -> ExecutionPolicy                                    (app.engines.execution_policy)
      -> ExecutionPlan                                      (app.engines.adapter)
      -> registered mock adapter .simulate()                (app.engines.adapter)
      -> SimulationResult

The orchestrator never selects an adapter by itself, never builds a command
and never invokes a process: an adapter is looked up from the closed registry
and only the deterministic ``MockEngineAdapter.simulate()`` is called.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from app.core.router.candidates import discovered_decision
from app.engines.adapter import (
    MOCK_HASHCAT,
    MOCK_JOHN,
    ExecutionMode,
    ExecutionPlan,
    ExecutionRequest,
    ExecutionStatus,
    SimulationResult,
    build_execution_plan,
)
from app.engines.adapter_registry import EngineAdapterRegistry
from app.engines.discovery.models import HASHCAT, JOHN
from app.engines.discovery.registry import DiscoveryRegistry
from app.engines.execution_policy import (
    ExecutionPolicy,
    PolicyDecision,
    default_execution_policy,
    mode_label,
)
from app.engines.selection import CapabilityMatcher, SelectionDecision

__all__ = [
    "ADAPTER_FOR_ENGINE",
    "PlanPreview",
    "adapter_for_engine",
    "build_request",
    "preview_plan",
]

#: Discovery engine identity -> registered mock adapter identity.
ADAPTER_FOR_ENGINE: Mapping[str, str] = MappingProxyType(
    {JOHN: MOCK_JOHN, HASHCAT: MOCK_HASHCAT}
)


def adapter_for_engine(engine: str) -> str:
    """Closed mapping from a selected discovery engine to a mock adapter.

    Returns an empty string for identities outside the closed set; the policy
    then denies the request with ``unknown_engine``.
    """

    if not isinstance(engine, str):
        return ""
    return ADAPTER_FOR_ENGINE.get(engine, "")


def build_request(
    target: str,
    decision: SelectionDecision,
    *,
    mode: ExecutionMode | str = ExecutionMode.SIMULATED,
    reference: str = "",
) -> ExecutionRequest:
    """Turn a selection decision into a structured, command-free request."""

    candidate = decision.selected if isinstance(decision, SelectionDecision) else None
    selection_status = (
        decision.status.value if isinstance(decision, SelectionDecision) else ""
    )
    engine = adapter_for_engine(candidate.engine) if candidate is not None else ""
    capability_status = candidate.capability_status.value if candidate else ""
    matched = (
        CapabilityMatcher.matching_formats(target, candidate.formats)
        if candidate is not None
        else ()
    )
    return ExecutionRequest(
        target=target,
        engine=engine,
        mode=mode,
        selection_status=selection_status,
        capability_status=capability_status,
        selected_engine=candidate.engine if candidate is not None else "",
        matched_formats=matched,
        reference=reference,
    )


@dataclass(frozen=True, slots=True)
class PlanPreview:
    """Full, structured outcome of one plan-preview call."""

    target: str
    selection: SelectionDecision
    request: ExecutionRequest
    authorization: PolicyDecision
    plan: ExecutionPlan | None = None
    simulation: SimulationResult | None = None

    @property
    def authorized(self) -> bool:
        return self.authorization.authorized

    @property
    def simulated(self) -> bool:
        return (
            self.simulation is not None
            and self.simulation.status is ExecutionStatus.SIMULATED
        )

    @property
    def simulated_only(self) -> bool:
        """True when nothing but a simulation outcome exists."""

        return (self.plan is None or self.plan.simulated) and (
            self.simulation is None or self.simulation.simulated
        )

    def as_dict(self) -> dict[str, object]:
        """JSON-safe view - structured fields only."""

        return {
            "target": self.target,
            "selection": {
                "status": self.selection.status.value,
                "reason": self.selection.reason,
                "selected_engine": self.selection.selected_engine or "",
                "candidates": [
                    candidate.engine for candidate in self.selection.candidates
                ],
            },
            "request": {
                "target": self.request.target,
                "engine": self.request.engine,
                "mode": mode_label(self.request.mode),
                "selection_status": self.request.selection_status,
                "capability_status": self.request.capability_status,
                "selected_engine": self.request.selected_engine,
                "matched_formats": list(self.request.matched_formats),
                "reference": self.request.reference,
            },
            "authorization": self.authorization.as_dict(),
            "plan": self.plan.as_dict() if self.plan is not None else None,
            "simulation": self.simulation.as_dict() if self.simulation is not None else None,
        }


def preview_plan(
    target: str,
    *,
    discovery_registry: DiscoveryRegistry | None = None,
    adapter_registry: EngineAdapterRegistry | None = None,
    policy: ExecutionPolicy | None = None,
    mode: ExecutionMode | str = ExecutionMode.SIMULATED,
    reference: str = "",
) -> PlanPreview:
    """Run selection -> request -> policy -> plan -> mock simulation.

    A denied request stops at the policy: no plan and no simulation result is
    produced for it.
    """

    safe_target = target if isinstance(target, str) else ""

    selection = discovered_decision(safe_target, registry=discovery_registry)
    request = build_request(safe_target, selection, mode=mode, reference=reference)

    if policy is None:
        policy = (
            ExecutionPolicy(adapter_registry)
            if adapter_registry is not None
            else default_execution_policy()
        )

    authorization = policy.authorize(request)
    if not authorization.authorized:
        return PlanPreview(
            target=safe_target,
            selection=selection,
            request=request,
            authorization=authorization,
        )

    plan = build_execution_plan(request)
    adapter = policy.registry.require(plan.adapter)
    simulation = adapter.simulate(plan)
    return PlanPreview(
        target=safe_target,
        selection=selection,
        request=request,
        authorization=authorization,
        plan=plan,
        simulation=simulation,
    )

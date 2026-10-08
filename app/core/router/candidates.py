"""Router candidate resolution.

``discovered_candidates()`` consults the **Phase 2 discovery registry** through
the capability-aware selector.

``candidates_or_route_hints()`` prefers verified discovery candidates and only
falls back to the legacy Phase 1 ``RouteSpec.priority_hints`` when discovery
produced no verified candidate.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.core.router.table import route_for
from app.engines.discovery.registry import (
    DiscoveryRegistry,
    default_discovery_registry,
)
from app.engines.selection import (
    CapabilityAwareEngineSelector,
    SelectionCandidate,
    SelectionDecision,
)

__all__ = [
    "CandidateSource",
    "CandidatesOrHints",
    "candidates_or_route_hints",
    "discovered_candidates",
    "discovered_decision",
    "route_hints_for",
]


class CandidateSource(str, Enum):
    """Where the winning candidate or hint came from."""

    DISCOVERY = "discovery"
    ROUTE_TABLE = "route_table"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class CandidatesOrHints:
    """Outcome of resolving candidates for one target."""

    target: str
    source: CandidateSource
    candidates: tuple[SelectionCandidate, ...] = ()
    decision: SelectionDecision | None = None
    route: str | None = None
    priority_hints: tuple[str, ...] = ()

    @property
    def has_verified_candidates(self) -> bool:
        return self.source is CandidateSource.DISCOVERY and bool(self.candidates)

    @property
    def uses_legacy_hints(self) -> bool:
        return self.source is CandidateSource.ROUTE_TABLE

    @property
    def selected_engine(self) -> str | None:
        if self.candidates:
            return self.candidates[0].engine
        return None


def _selector(registry: DiscoveryRegistry | None) -> CapabilityAwareEngineSelector:
    if registry is not None:
        return CapabilityAwareEngineSelector(registry)
    return CapabilityAwareEngineSelector(default_discovery_registry())


def discovered_decision(
    target: str, *, registry: DiscoveryRegistry | None = None
) -> SelectionDecision:
    """Capability-aware decision drawn from the discovery registry."""

    return _selector(registry).select(target)


def discovered_candidates(
    target: str, *, registry: DiscoveryRegistry | None = None
) -> tuple[SelectionCandidate, ...]:
    """Verified discovery candidates for ``target`` (empty when none)."""

    return _selector(registry).candidates(target)


def route_hints_for(target: str) -> tuple[str, ...]:
    """Legacy Phase 1 priority hints from the static route table."""

    spec = route_for(target)
    return spec.priority_hints if spec is not None else ()


def candidates_or_route_hints(
    target: str, *, registry: DiscoveryRegistry | None = None
) -> CandidatesOrHints:
    """Prefer verified discovery candidates; fall back to Phase 1 hints."""

    decision = discovered_decision(target, registry=registry)
    candidates = tuple(decision.candidates)
    if candidates:
        return CandidatesOrHints(
            target=target,
            source=CandidateSource.DISCOVERY,
            candidates=candidates,
            decision=decision,
        )

    spec = route_for(target)
    if spec is not None:
        return CandidatesOrHints(
            target=target,
            source=CandidateSource.ROUTE_TABLE,
            decision=decision,
            route=spec.route,
            priority_hints=spec.priority_hints,
        )

    return CandidatesOrHints(
        target=target,
        source=CandidateSource.NONE,
        decision=decision,
    )

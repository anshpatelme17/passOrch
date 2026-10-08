"""Phase 2C - capability-aware engine selection.

Selection is **pure decision-making**:

* only ``VERIFIED`` capability evidence can produce a real-engine candidate;
* ``UNKNOWN`` / ``ERROR`` / missing capability produces no candidate;
* filenames, extensions and any other user input are not capability evidence
  - the selector consults the read-only target policy and the discovery
  registry, nothing else;
* no subprocess is ever executed here.

Discovery engine identities are mapped to Phase 1 registry names so the two
registries stay conceptually separate.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from app.engines.discovery.capability_map import formats_for_target, is_usable
from app.engines.discovery.errors import SelectionError
from app.engines.discovery.models import (
    HASHCAT,
    JOHN,
    CapabilityStatus,
)
from app.engines.discovery.registry import DiscoveryRegistry, RegistryEntry, default_discovery_registry

__all__ = [
    "DISCOVERY_PRIORITIES",
    "DISCOVERY_TO_PHASE1",
    "CapabilityAwareEngineSelector",
    "CapabilityMatcher",
    "EngineSelectionPolicy",
    "SelectionCandidate",
    "SelectionDecision",
    "SelectionStatus",
    "phase1_name",
]

#: Discovery engine identity -> Phase 1 registry name.
DISCOVERY_TO_PHASE1: Mapping[str, str] = MappingProxyType(
    {JOHN: "john-the-ripper", HASHCAT: "hashcat"}
)

#: Discovery priorities - higher wins.  Alphabetical order breaks ties.
DISCOVERY_PRIORITIES: Mapping[str, int] = MappingProxyType({JOHN: 10, HASHCAT: 5})


def phase1_name(engine: str) -> str:
    """Map a discovery engine identity onto its Phase 1 registry name."""

    return DISCOVERY_TO_PHASE1.get(engine, engine)


class SelectionStatus(str, Enum):
    """Outcome of a capability-aware selection."""

    SELECTED = "selected"
    NO_CANDIDATE = "no_candidate"
    NO_VERIFIED_CAPABILITY = "no_verified_capability"
    UNSUPPORTED_TARGET = "unsupported_target"


@dataclass(frozen=True, slots=True)
class SelectionCandidate:
    """A real-engine candidate backed by VERIFIED capability evidence."""

    engine: str
    phase1_name: str
    priority: int
    capability_status: CapabilityStatus
    formats: tuple[str, ...]
    targets: tuple[str, ...]
    discovered: bool

    def __post_init__(self) -> None:
        if self.capability_status is not CapabilityStatus.VERIFIED:
            raise SelectionError(
                f"only VERIFIED capability evidence may produce a candidate "
                f"(got {self.capability_status.value!r} for {self.engine!r})"
            )
        if not self.discovered:
            raise SelectionError(
                f"candidate {self.engine!r} must come from a discovered engine"
            )
        object.__setattr__(self, "formats", tuple(self.formats))
        object.__setattr__(self, "targets", tuple(self.targets))


@dataclass(frozen=True, slots=True)
class SelectionDecision:
    """The result of selecting an engine for one target."""

    target: str
    status: SelectionStatus
    selected: SelectionCandidate | None = None
    candidates: tuple[SelectionCandidate, ...] = ()
    reason: str = ""

    @property
    def has_candidate(self) -> bool:
        return self.selected is not None

    @property
    def is_verified(self) -> bool:
        return self.selected is not None

    @property
    def selected_engine(self) -> str | None:
        return self.selected.engine if self.selected else None

    @property
    def selected_phase1_name(self) -> str | None:
        return self.selected.phase1_name if self.selected else None


class CapabilityMatcher:
    """Matches an audit target against VERIFIED capability evidence."""

    @staticmethod
    def target_supported(target: str) -> bool:
        return is_usable(target)

    @staticmethod
    def matching_formats(target: str, formats: tuple[str, ...]) -> tuple[str, ...]:
        relevant = formats_for_target(target)
        verified = set(formats)
        return tuple(name for name in relevant if name in verified)

    @staticmethod
    def matches(target: str, formats: tuple[str, ...]) -> bool:
        if not CapabilityMatcher.target_supported(target):
            return False
        return bool(CapabilityMatcher.matching_formats(target, formats))


class EngineSelectionPolicy:
    """Eligibility and ordering policy for discovery registry entries."""

    def __init__(self, priorities: Mapping[str, int] | None = None) -> None:
        self._priorities = MappingProxyType(dict(priorities or DISCOVERY_PRIORITIES))

    @property
    def priorities(self) -> Mapping[str, int]:
        return self._priorities

    def priority(self, engine: str) -> int:
        return int(self._priorities.get(engine, 0))

    def phase1_name(self, engine: str) -> str:
        return phase1_name(engine)

    def is_eligible(self, entry: RegistryEntry) -> bool:
        """Only discovered engines with VERIFIED evidence are eligible."""

        return entry.snapshot.discovered and entry.capability_status is (
            CapabilityStatus.VERIFIED
        )

    def candidate_for(self, entry: RegistryEntry) -> SelectionCandidate | None:
        if not self.is_eligible(entry):
            return None
        return SelectionCandidate(
            engine=entry.engine,
            phase1_name=self.phase1_name(entry.engine),
            priority=self.priority(entry.engine),
            capability_status=entry.capability_status,
            formats=entry.formats,
            targets=entry.targets,
            discovered=entry.snapshot.discovered,
        )

    @staticmethod
    def sort_key(candidate: SelectionCandidate) -> tuple[int, str]:
        """Highest priority first; alphabetical engine name breaks ties."""

        return (-candidate.priority, candidate.engine)

    def order(
        self, candidates: tuple[SelectionCandidate, ...] | list[SelectionCandidate]
    ) -> tuple[SelectionCandidate, ...]:
        return tuple(sorted(candidates, key=self.sort_key))

    def has_any_eligible(self, registry: DiscoveryRegistry) -> bool:
        return any(self.is_eligible(entry) for entry in registry.entries())


class CapabilityAwareEngineSelector:
    """Pure, capability-aware selector over the Phase 2 discovery registry."""

    def __init__(
        self,
        registry: DiscoveryRegistry | None = None,
        *,
        policy: EngineSelectionPolicy | None = None,
        matcher: CapabilityMatcher | None = None,
    ) -> None:
        self._registry = registry if registry is not None else default_discovery_registry()
        self._policy = policy if policy is not None else EngineSelectionPolicy()
        self._matcher = matcher if matcher is not None else CapabilityMatcher()

    @property
    def registry(self) -> DiscoveryRegistry:
        return self._registry

    @property
    def policy(self) -> EngineSelectionPolicy:
        return self._policy

    def candidates(self, target: str) -> tuple[SelectionCandidate, ...]:
        """Verified, target-matching candidates ordered by policy."""

        if not isinstance(target, str) or not self._matcher.target_supported(target):
            return ()
        matched: list[SelectionCandidate] = []
        for entry in self._registry.entries():
            candidate = self._policy.candidate_for(entry)
            if candidate is None:
                continue
            if not self._matcher.matches(target, candidate.formats):
                continue
            matched.append(candidate)
        return self._policy.order(matched)

    def select(self, target: str) -> SelectionDecision:
        """Decide which engine to use for ``target`` - no side effects."""

        if not isinstance(target, str) or not self._matcher.target_supported(target):
            return SelectionDecision(
                target=target if isinstance(target, str) else "",
                status=SelectionStatus.UNSUPPORTED_TARGET,
                reason="target_not_mapped",
            )

        candidates = self.candidates(target)
        if candidates:
            return SelectionDecision(
                target=target,
                status=SelectionStatus.SELECTED,
                selected=candidates[0],
                candidates=candidates,
                reason="verified_capability",
            )

        if not self._policy.has_any_eligible(self._registry):
            return SelectionDecision(
                target=target,
                status=SelectionStatus.NO_VERIFIED_CAPABILITY,
                reason="capability_missing",
            )

        return SelectionDecision(
            target=target,
            status=SelectionStatus.NO_CANDIDATE,
            reason="no_capability_match",
        )

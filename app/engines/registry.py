"""Phase 1 simulated engine registry.

This module is intentionally *simulated*: it answers the question "which
engine would PassOrch prefer for a target if nothing had been discovered
yet?" using a static, in-memory table.

It is kept conceptually separate from the Phase 2 discovery registry
(``app.engines.discovery.registry``):

* Phase 1 knows nothing about probe output, capability status or allowlisted
  discovery operations.
* Phase 2 knows nothing about Phase 1 selection ordering beyond an explicit,
  well defined name mapping.

No subprocess execution, no probing and no capability evidence originates
here.  Everything in this module is a *simulation*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

__all__ = [
    "ENGINE_HASHCAT",
    "ENGINE_JOHN",
    "EngineRegistry",
    "EngineSelector",
    "PHASE1_ENGINE_NAMES",
    "Phase1Priority",
    "SimulatedEngine",
    "default_registry",
]

ENGINE_JOHN = "john-the-ripper"
ENGINE_HASHCAT = "hashcat"

PHASE1_ENGINE_NAMES: tuple[str, ...] = (ENGINE_HASHCAT, ENGINE_JOHN)


class Phase1Priority:
    """Static Phase 1 preference values (higher wins)."""

    JOHN = 10
    HASHCAT = 5


def _simulated_targets() -> tuple[str, ...]:
    # Imported lazily-free: Phase 1 must not depend on discovery either.
    return (
        "office-document",
        "pdf-document",
        "raw-password-hash",
        "unix-password-hash",
        "windows-password-hash",
        "zip-archive",
    )


@dataclass(frozen=True, slots=True)
class SimulatedEngine:
    """A statically declared, simulated engine entry."""

    name: str
    display_name: str
    priority: int
    targets: tuple[str, ...]
    simulated: bool = True
    notes: str = field(default="phase-1 simulated entry")

    def supports(self, target: str) -> bool:
        return target in self.targets


def _default_engines() -> tuple[SimulatedEngine, ...]:
    targets = _simulated_targets()
    return (
        SimulatedEngine(
            name=ENGINE_JOHN,
            display_name="John the Ripper (simulated)",
            priority=Phase1Priority.JOHN,
            targets=targets,
        ),
        SimulatedEngine(
            name=ENGINE_HASHCAT,
            display_name="Hashcat (simulated)",
            priority=Phase1Priority.HASHCAT,
            targets=targets,
        ),
    )


class EngineRegistry:
    """Static Phase 1 registry of simulated engines."""

    def __init__(self, engines: Iterable[SimulatedEngine] | None = None) -> None:
        entries: dict[str, SimulatedEngine] = {}
        for engine in engines if engines is not None else _default_engines():
            if engine.name in entries:
                raise ValueError(f"duplicate simulated engine: {engine.name}")
            entries[engine.name] = engine
        self._engines: Mapping[str, SimulatedEngine] = dict(entries)

    @property
    def engines(self) -> tuple[SimulatedEngine, ...]:
        return tuple(sorted(self._engines.values(), key=lambda e: e.name))

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._engines))

    def get(self, name: str) -> SimulatedEngine | None:
        return self._engines.get(name)

    def supports_target(self, target: str) -> bool:
        return any(engine.supports(target) for engine in self._engines.values())

    def engines_for_target(self, target: str) -> tuple[SimulatedEngine, ...]:
        matched = [engine for engine in self._engines.values() if engine.supports(target)]
        return tuple(sorted(matched, key=lambda e: (-e.priority, e.name)))

    def as_dict(self) -> dict[str, SimulatedEngine]:
        """A defensive copy; callers cannot mutate the registry through it."""
        return dict(self._engines)


class EngineSelector:
    """Pure Phase 1 selector: highest static priority wins, name breaks ties."""

    def __init__(self, registry: EngineRegistry | None = None) -> None:
        self._registry = registry if registry is not None else EngineRegistry()

    @property
    def registry(self) -> EngineRegistry:
        return self._registry

    def rank(self, target: str) -> tuple[SimulatedEngine, ...]:
        return self._registry.engines_for_target(target)

    def select(self, target: str) -> SimulatedEngine | None:
        ranked = self.rank(target)
        return ranked[0] if ranked else None


def default_registry() -> EngineRegistry:
    """A fresh default Phase 1 registry (never mutated globally)."""

    return EngineRegistry()

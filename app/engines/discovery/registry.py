"""Phase 2B discovery registry.

Stores *engine snapshots* (what was discovered) and *capability evidence*
(what the verified parsing flow proved), and exposes a JSON-safe payload.

Hard rules:

* only known engine identities may be registered;
* ``EngineCapabilities.capability_status`` is always surfaced in the payload;
* raw probe output is never stored, never returned and never serialised;
* real-engine compatibility requires a **VERIFIED** capability status - an
  ``UNKNOWN``, ``ERROR`` or missing capability is not compatible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from app.engines.discovery.errors import RegistryError, UnknownEngineError
from app.engines.discovery.models import (
    KNOWN_ENGINES,
    CapabilityStatus,
    EngineCapabilities,
    EngineSnapshot,
)

__all__ = [
    "DiscoveryRegistry",
    "RegistryEntry",
    "default_discovery_registry",
]

FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {"stdout", "stderr", "raw_output", "probe_output", "output", "command", "argv"}
)


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    """A registered engine: discovery facts plus capability evidence."""

    engine: str
    snapshot: EngineSnapshot
    capabilities: EngineCapabilities | None = None

    @property
    def capability_status(self) -> CapabilityStatus:
        if self.capabilities is None:
            return CapabilityStatus.UNKNOWN
        return self.capabilities.capability_status

    @property
    def has_capability_evidence(self) -> bool:
        return self.capabilities is not None

    @property
    def is_verified(self) -> bool:
        return self.capability_status is CapabilityStatus.VERIFIED

    @property
    def real_engine_compatible(self) -> bool:
        """True only when the binary was discovered *and* proven VERIFIED."""

        return self.snapshot.discovered and self.is_verified

    @property
    def formats(self) -> tuple[str, ...]:
        if self.capabilities is None:
            return ()
        return self.capabilities.listed_formats

    @property
    def targets(self) -> tuple[str, ...]:
        if self.capabilities is None:
            return ()
        return self.capabilities.targets


class DiscoveryRegistry:
    """In-memory registry of discovery snapshots and capability evidence."""

    def __init__(self, entries: Iterable[RegistryEntry] = ()) -> None:
        self._entries: dict[str, RegistryEntry] = {}
        for entry in entries:
            self._store(entry)

    # -- registration ------------------------------------------------------ #

    def register(self, snapshot: EngineSnapshot) -> RegistryEntry:
        """Register (or refresh) an engine snapshot."""

        if snapshot.engine not in KNOWN_ENGINES:
            raise UnknownEngineError(snapshot.engine)
        existing = self._entries.get(snapshot.engine)
        entry = RegistryEntry(
            engine=snapshot.engine,
            snapshot=snapshot,
            capabilities=existing.capabilities if existing else None,
        )
        self._entries[snapshot.engine] = entry
        return entry

    def set_capabilities(self, engine: str, capabilities: EngineCapabilities) -> RegistryEntry:
        """Attach capability evidence to a registered (or new) engine."""

        if engine not in KNOWN_ENGINES:
            raise UnknownEngineError(engine)
        if capabilities.engine != engine:
            raise RegistryError(
                f"capability evidence for {capabilities.engine!r} cannot be attached to {engine!r}"
            )
        existing = self._entries.get(engine)
        snapshot = existing.snapshot if existing else EngineSnapshot(engine=engine)
        entry = RegistryEntry(engine=engine, snapshot=snapshot, capabilities=capabilities)
        self._entries[engine] = entry
        return entry

    def _store(self, entry: RegistryEntry) -> None:
        if entry.engine not in KNOWN_ENGINES:
            raise UnknownEngineError(entry.engine)
        self._entries[entry.engine] = entry

    def reset(self) -> None:
        self._entries.clear()

    # -- lookup ------------------------------------------------------------ #

    def get(self, engine: str) -> RegistryEntry | None:
        return self._entries.get(engine)

    def require(self, engine: str) -> RegistryEntry:
        entry = self.get(engine)
        if entry is None:
            raise RegistryError(f"engine {engine!r} is not registered")
        return entry

    def engines(self) -> tuple[str, ...]:
        return tuple(sorted(self._entries))

    def entries(self) -> tuple[RegistryEntry, ...]:
        return tuple(self._entries[name] for name in self.engines())

    def capability_status(self, engine: str) -> CapabilityStatus:
        entry = self.get(engine)
        if entry is None:
            return CapabilityStatus.UNKNOWN
        return entry.capability_status

    def verified_engines(self) -> tuple[str, ...]:
        return tuple(e.engine for e in self.entries() if e.is_verified)

    def compatible_engines(self) -> tuple[str, ...]:
        return tuple(e.engine for e in self.entries() if e.real_engine_compatible)

    def entry_payload(self, entry: RegistryEntry) -> dict[str, Any]:
        capabilities = entry.capabilities
        payload: dict[str, Any] = {
            "engine": entry.engine,
            "discovered": entry.snapshot.discovered,
            "binary_found": entry.snapshot.binary_found,
            "version": entry.snapshot.version,
            "operations": list(entry.snapshot.operations),
            "snapshot_error": entry.snapshot.error,
            "capability_status": entry.capability_status.value,
            "formats": list(entry.formats),
            "targets": list(entry.targets),
            "source_operation": capabilities.source_operation if capabilities else None,
            "reason": capabilities.reason if capabilities else "capability_missing",
            "parser": capabilities.parser if capabilities else None,
            "real_engine_compatible": entry.real_engine_compatible,
        }
        return payload

    def payload(self) -> dict[str, Any]:
        """JSON-safe registry payload.

        Contains ``capability_status`` for every engine and never any raw
        probe output.
        """

        return {"engines": [self.entry_payload(entry) for entry in self.entries()]}

    def validate_payload(self, payload: Mapping[str, Any] | None = None) -> None:
        """Raise if any raw probe output key ever appears in the payload."""

        data = payload if payload is not None else self.payload()
        for engine_payload in data.get("engines", []):
            forbidden = FORBIDDEN_PAYLOAD_KEYS & set(engine_payload)
            if forbidden:
                raise RegistryError(f"payload leaks raw output keys: {sorted(forbidden)}")
            if "capability_status" not in engine_payload:
                raise RegistryError("payload must expose capability_status")


def _build_default_registry() -> DiscoveryRegistry:
    return DiscoveryRegistry()


def default_discovery_registry() -> DiscoveryRegistry:
    """Process-wide discovery registry used when no registry is injected."""

    return _DEFAULT_DISCOVERY_REGISTRY


_DEFAULT_DISCOVERY_REGISTRY = _build_default_registry()

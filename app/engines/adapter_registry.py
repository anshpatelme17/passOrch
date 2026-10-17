"""2D.1 - engine adapter registry.

A closed registry of adapter engines.  It:

* registers only adapters bound to a known mock engine name
  (``mock_john`` / ``mock_hashcat``),
* rejects duplicate registrations,
* rejects unknown engine identities,
* rejects adapters that expose a command / argv / executable surface.

It performs **no** discovery, **no** engine selection and **no** execution.
"""

from __future__ import annotations

from typing import Iterable

from app.engines.adapter import (
    FORBIDDEN_REQUEST_FIELDS,
    MOCK_ENGINE_NAMES,
    MOCK_HASHCAT,
    MOCK_JOHN,
    AdapterError,
    DuplicateAdapterError,
    EngineAdapter,
    MockEngineAdapter,
    UnknownAdapterError,
)

__all__ = [
    "EngineAdapterRegistry",
    "KNOWN_ADAPTER_ENGINES",
    "build_default_registry",
    "default_adapter_registry",
]

#: Closed set of engine identities the registry accepts.
KNOWN_ADAPTER_ENGINES: frozenset[str] = frozenset(MOCK_ENGINE_NAMES)


class EngineAdapterRegistry:
    """Immutable-by-policy registry of simulation adapters."""

    def __init__(self, adapters: Iterable[EngineAdapter] = ()) -> None:
        self._adapters: dict[str, EngineAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    # -- registration ------------------------------------------------------ #

    def register(self, adapter: EngineAdapter) -> None:
        """Register one adapter.

        Raises ``UnknownAdapterError`` for an engine identity outside
        :data:`KNOWN_ADAPTER_ENGINES` and ``DuplicateAdapterError`` when the
        engine is already registered.
        """

        if adapter is None:
            raise AdapterError("an adapter instance is required")
        if not isinstance(adapter, EngineAdapter):
            raise AdapterError("an adapter must expose a name and a simulate(plan) method")

        name = getattr(adapter, "name", None)
        if not isinstance(name, str) or not name.strip():
            raise AdapterError("an adapter requires a non-empty engine name")

        self._reject_forbidden_surface(adapter, name)

        if name not in KNOWN_ADAPTER_ENGINES:
            raise UnknownAdapterError(name)
        if name in self._adapters:
            raise DuplicateAdapterError(name)

        self._adapters[name] = adapter

    @staticmethod
    def _reject_forbidden_surface(adapter: EngineAdapter, name: str) -> None:
        exposed = sorted(field for field in FORBIDDEN_REQUEST_FIELDS if hasattr(adapter, field))
        if exposed:
            raise AdapterError(
                f"adapter {name!r} exposes a forbidden execution surface: {', '.join(exposed)}"
            )

    def reset(self) -> None:
        self._adapters.clear()

    # -- lookup ------------------------------------------------------------ #

    def get(self, name: str) -> EngineAdapter | None:
        if not isinstance(name, str):
            return None
        return self._adapters.get(name)

    def require(self, name: str) -> EngineAdapter:
        adapter = self.get(name)
        if adapter is None:
            raise UnknownAdapterError(name)
        return adapter

    def list(self) -> tuple[str, ...]:
        """Registered engine names, alphabetical (deterministic)."""

        return tuple(sorted(self._adapters))

    def adapters(self) -> tuple[EngineAdapter, ...]:
        return tuple(self._adapters[name] for name in self.list())

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._adapters

    def __len__(self) -> int:
        return len(self._adapters)


def build_default_registry() -> EngineAdapterRegistry:
    """A fresh registry pre-populated with the Phase 1 mock engine names."""

    registry = EngineAdapterRegistry()
    registry.register(MockEngineAdapter(MOCK_JOHN))
    registry.register(MockEngineAdapter(MOCK_HASHCAT))
    return registry


def default_adapter_registry() -> EngineAdapterRegistry:
    """Process-wide registry used when no registry is injected."""

    return _DEFAULT_ADAPTER_REGISTRY


_DEFAULT_ADAPTER_REGISTRY = build_default_registry()

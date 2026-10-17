"""Discovery error hierarchy.

Every discovery-layer failure is expressed with one of these types so callers
can fail closed without ever inspecting raw probe output.
"""

from __future__ import annotations

__all__ = [
    "CapabilityParseError",
    "DiscoveryError",
    "EngineBinaryMissingError",
    "ForeignOutputError",
    "OperationNotAllowedError",
    "RegistryError",
    "SelectionError",
    "UnknownEngineError",
]


class DiscoveryError(Exception):
    """Base class for every PassOrch discovery failure."""


class UnknownEngineError(DiscoveryError):
    """Raised when an engine identity is not part of the known engine set."""

    def __init__(self, engine: object) -> None:
        super().__init__(f"unknown engine identity: {engine!r}")
        self.engine = engine


class OperationNotAllowedError(DiscoveryError):
    """Raised when an operation is not on the immutable discovery allowlist."""

    def __init__(self, engine: object, operation: object) -> None:
        super().__init__(f"operation {operation!r} is not allowlisted for engine {engine!r}")
        self.engine = engine
        self.operation = operation


class EngineBinaryMissingError(DiscoveryError):
    """Raised when a known engine binary cannot be located."""

    def __init__(self, engine: str) -> None:
        super().__init__(f"engine binary not found for {engine!r}")
        self.engine = engine


class CapabilityParseError(DiscoveryError):
    """Base class for capability parsing failures."""


class ForeignOutputError(CapabilityParseError):
    """Raised when probe output is presented to the wrong parser."""

    def __init__(self, engine: object, operation: object, parser: str) -> None:
        super().__init__(
            f"output provenance mismatch: parser {parser!r} cannot accept "
            f"engine={engine!r} operation={operation!r}"
        )
        self.engine = engine
        self.operation = operation
        self.parser = parser


class RegistryError(DiscoveryError):
    """Base class for discovery registry failures."""


class SelectionError(DiscoveryError):
    """Base class for capability-aware selection failures."""

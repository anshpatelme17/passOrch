"""Phase 2A/2B shared discovery models.

Hard rules enforced here:

* ``EngineCapabilities.capability_status`` defaults to ``UNKNOWN``.
* Capability records never carry raw probe output; only sanitized,
  structured facts are retained.
* Engine identity is a closed set (``KNOWN_ENGINES``); binary names come from
  a fixed table and are never user supplied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping

__all__ = [
    "DISCOVERY_ENGINE_ORDER",
    "ENGINE_BINARIES",
    "HASHCAT",
    "JOHN",
    "KNOWN_ENGINES",
    "CapabilityStatus",
    "EngineCapabilities",
    "EngineSnapshot",
    "ProbeResult",
    "sanitize_single_line",
]

JOHN = "john"
HASHCAT = "hashcat"

KNOWN_ENGINES: frozenset[str] = frozenset({JOHN, HASHCAT})

#: Deterministic discovery order (alphabetical); selection applies priorities.
DISCOVERY_ENGINE_ORDER: tuple[str, ...] = (HASHCAT, JOHN)

#: Fixed engine -> binary name table.  Never user supplied, never a path.
ENGINE_BINARIES: Mapping[str, str] = MappingProxyType({JOHN: "john", HASHCAT: "hashcat"})

MAX_VERSION_LENGTH = 64


class CapabilityStatus(str, Enum):
    """Trust level of an engine's capability evidence."""

    VERIFIED = "verified"
    UNKNOWN = "unknown"
    ERROR = "error"


def sanitize_single_line(value: str, *, max_length: int = MAX_VERSION_LENGTH) -> str:
    """Collapse a probe-derived string into one short, printable line."""

    cleaned = " ".join(value.split())
    printable = "".join(ch for ch in cleaned if ch.isprintable())
    if len(printable) > max_length:
        return printable[: max_length - 1] + "…"
    return printable


@dataclass(frozen=True, slots=True)
class EngineSnapshot:
    """What discovery knows about one engine (never raw probe output)."""

    engine: str
    discovered: bool = False
    binary_found: bool = False
    version: str | None = None
    operations: tuple[str, ...] = ()
    error: str | None = None

    def __post_init__(self) -> None:
        if self.version is not None:
            object.__setattr__(self, "version", sanitize_single_line(self.version))


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Transient result of one allowlisted discovery operation.

    ``stdout``/``stderr`` are raw and MUST NOT be persisted in the discovery
    registry or in any API payload.
    """

    engine: str
    operation: str
    ok: bool
    returncode: int
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    error: str | None = None

    @property
    def has_output(self) -> bool:
        return bool(self.stdout.strip())


@dataclass(frozen=True, slots=True)
class EngineCapabilities:
    """Structured capability evidence for one engine.

    The default is always ``UNKNOWN``: evidence must be produced by the
    verified capability parsing flow, never asserted by hand.
    """

    engine: str
    capability_status: CapabilityStatus = CapabilityStatus.UNKNOWN
    formats: tuple[str, ...] = ()
    targets: tuple[str, ...] = ()
    source_operation: str | None = None
    reason: str | None = None
    parser: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "formats", tuple(self.formats))
        object.__setattr__(self, "targets", tuple(self.targets))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def is_verified(self) -> bool:
        return self.capability_status is CapabilityStatus.VERIFIED

    @property
    def listed_formats(self) -> tuple[str, ...]:
        return self.formats if self.capability_status is CapabilityStatus.VERIFIED else ()

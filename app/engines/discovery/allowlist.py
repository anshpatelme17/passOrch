"""Immutable, fail-closed discovery command allowlist.

Only these operations may ever be considered for discovery:

``john``
    ``--version``, ``-v``, ``--help``, ``--list=formats``, ``--list=help``

``hashcat``
    ``--version``, ``-V``, ``--help``, ``--devices``, ``--backend-info``

There is deliberately **no** Hashcat mode-list operation, therefore no
Hashcat capability probe exists.  Hashcat capabilities stay ``UNKNOWN`` until
such an operation is (deliberately, explicitly) allowlisted.

Properties enforced by this module:

* the table is immutable (frozen dataclass + read-only mapping),
* unknown engine identities fail closed,
* unknown / non-string / whitespace-containing operations fail closed,
* matching is exact: no prefixes, no globbing, no case folding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from app.engines.discovery.errors import OperationNotAllowedError, UnknownEngineError
from app.engines.discovery.models import HASHCAT, JOHN, KNOWN_ENGINES

__all__ = [
    "DISCOVERY_ALLOWLIST",
    "HASHCAT_OPERATIONS",
    "JOHN_OPERATIONS",
    "DiscoveryAllowlist",
    "iter_allowlisted_operations",
]

JOHN_OPERATIONS: tuple[str, ...] = (
    "--version",
    "-v",
    "--help",
    "--list=formats",
    "--list=help",
)

HASHCAT_OPERATIONS: tuple[str, ...] = (
    "--version",
    "-V",
    "--help",
    "--devices",
    "--backend-info",
)


@dataclass(frozen=True, slots=True)
class DiscoveryAllowlist:
    """A closed, immutable map of engine -> allowlisted discovery operations."""

    _table: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized: dict[str, tuple[str, ...]] = {}
        for engine, operations in dict(self._table).items():
            if engine not in KNOWN_ENGINES:
                raise UnknownEngineError(engine)
            ops = tuple(operations)
            if not ops:
                raise OperationNotAllowedError(engine, "<empty allowlist>")
            seen: set[str] = set()
            for op in ops:
                if (
                    not isinstance(op, str)
                    or not op
                    or op != op.strip()
                    or any(ch.isspace() for ch in op)
                ):
                    raise OperationNotAllowedError(engine, op)
                if op in seen:
                    raise ValueError(f"duplicate allowlisted operation: {engine} {op}")
                seen.add(op)
            normalized[engine] = ops
        for engine in KNOWN_ENGINES:
            normalized.setdefault(engine, ())
        object.__setattr__(self, "_table", MappingProxyType(normalized))

    # -- introspection ---------------------------------------------------- #

    def engines(self) -> tuple[str, ...]:
        return tuple(sorted(self._table))

    def operations(self, engine: str) -> tuple[str, ...]:
        """Allowlisted operations for a known engine (empty tuple if unknown)."""

        return self._table.get(engine, ())

    def table(self) -> Mapping[str, tuple[str, ...]]:
        return self._table

    # -- fail-closed checks ----------------------------------------------- #

    def allows(self, engine: object, operation: object) -> bool:
        if not isinstance(engine, str) or engine not in KNOWN_ENGINES:
            return False
        if not isinstance(operation, str) or not operation:
            return False
        if operation != operation.strip() or any(ch.isspace() for ch in operation):
            return False
        return operation in self._table.get(engine, ())

    def require(self, engine: object, operation: object) -> None:
        if not isinstance(engine, str) or engine not in KNOWN_ENGINES:
            raise UnknownEngineError(engine)
        if not self.allows(engine, operation):
            raise OperationNotAllowedError(engine, operation)

    # -- deliberately absent ---------------------------------------------- #
    # There is intentionally no ``add``, ``allow``, ``update``, ``extend``,
    # ``register`` or ``remove`` API.  The allowlist is a constant.


def iter_allowlisted_operations() -> tuple[tuple[str, str], ...]:
    """Every (engine, operation) pair, in deterministic order."""

    pairs: list[tuple[str, str]] = []
    for engine in DISCOVERY_ALLOWLIST.engines():
        for operation in DISCOVERY_ALLOWLIST.operations(engine):
            pairs.append((engine, operation))
    return tuple(pairs)


def _build_default() -> DiscoveryAllowlist:
    return DiscoveryAllowlist({JOHN: JOHN_OPERATIONS, HASHCAT: HASHCAT_OPERATIONS})


DISCOVERY_ALLOWLIST: DiscoveryAllowlist = _build_default()

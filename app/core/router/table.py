"""Phase 1 routing table.

This module is deliberately independent from discovery: it contains no import
of ``app.engines`` (or anything else from the application), only static
strings describing how a target would be routed when discovery has produced
no verified candidate.

``priority_hints`` are **Phase 1 registry names**, never discovery identities
and never executable names.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

__all__ = [
    "ROUTE_TABLE",
    "RouteSpec",
    "all_routes",
    "priority_hints",
    "route_for",
    "routed_targets",
]


@dataclass(frozen=True, slots=True)
class RouteSpec:
    """A static, simulated routing hint for one audit target."""

    target: str
    route: str
    priority_hints: tuple[str, ...]
    description: str = ""


def _spec(target: str, route: str, hints: tuple[str, ...], description: str) -> RouteSpec:
    return RouteSpec(target=target, route=route, priority_hints=hints,
                     description=description)


_JOHN_FIRST = ("john-the-ripper", "hashcat")


def _build_table() -> Mapping[str, RouteSpec]:
    specs = (
        _spec("office-document", "document-audit", _JOHN_FIRST,
              "Route Office document encryption audits"),
        _spec("pdf-document", "document-audit", _JOHN_FIRST,
              "Route PDF document encryption audits"),
        _spec("raw-password-hash", "hash-audit", _JOHN_FIRST,
              "Route stored password hash audits"),
        _spec("unix-password-hash", "unix-account-audit", _JOHN_FIRST,
              "Route Unix account password hash audits"),
        _spec("windows-password-hash", "windows-account-audit", _JOHN_FIRST,
              "Route Windows account password hash audits"),
        _spec("zip-archive", "archive-audit", _JOHN_FIRST,
              "Route archive encryption audits"),
    )
    return MappingProxyType({spec.target: spec for spec in specs})


#: Immutable Phase 1 route table (no discovery dependency).
ROUTE_TABLE: Mapping[str, RouteSpec] = _build_table()


def route_for(target: str) -> RouteSpec | None:
    if not isinstance(target, str):
        return None
    return ROUTE_TABLE.get(target)


def priority_hints(target: str) -> tuple[str, ...]:
    spec = route_for(target)
    return spec.priority_hints if spec is not None else ()


def routed_targets() -> tuple[str, ...]:
    return tuple(sorted(ROUTE_TABLE))


def all_routes() -> tuple[RouteSpec, ...]:
    return tuple(ROUTE_TABLE[target] for target in routed_targets())

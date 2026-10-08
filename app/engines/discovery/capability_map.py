"""Phase 2B.5 target -> format capability map.

The map is a **read-only policy** describing which engine format names are
relevant for each audit target.  It carries no capability evidence on its own:
a target being mapped here never makes any engine support it.

Nothing in this module reads filenames, extensions or other user input, and
nothing here mutates at runtime.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

__all__ = [
    "TARGET_FORMAT_POLICY",
    "TargetPolicy",
    "assert_read_only",
    "formats_for_target",
    "is_usable",
    "mapped_targets",
    "policy_as_dict",
    "supports_target",
    "target_policy",
]


@dataclass(frozen=True, slots=True)
class TargetPolicy:
    """Read-only policy describing the formats relevant for one target."""

    target: str
    formats: tuple[str, ...]
    description: str = ""
    enabled: bool = True

    @property
    def usable(self) -> bool:
        return self.enabled and bool(self.formats)


def _policy(target: str, formats: tuple[str, ...], description: str) -> TargetPolicy:
    return TargetPolicy(target=target, formats=formats, description=description)


def _build_policy_map() -> Mapping[str, TargetPolicy]:
    entries = (
        _policy(
            "office-document",
            ("office2007", "office2010", "office2013"),
            "Office document encryption audit",
        ),
        _policy("pdf-document", ("pdf",), "PDF document encryption audit"),
        _policy(
            "raw-password-hash",
            ("raw-md5", "raw-sha1", "raw-sha256"),
            "Stored password hash audit",
        ),
        _policy(
            "unix-password-hash",
            ("bcrypt", "md5crypt", "sha256crypt", "sha512crypt"),
            "Unix account password hash audit",
        ),
        _policy("windows-password-hash", ("ntlm",), "Windows account password hash audit"),
        _policy("zip-archive", ("pkzip", "zip"), "Archive encryption audit"),
    )
    table = {entry.target: entry for entry in entries}
    return MappingProxyType(table)


#: Immutable target -> format policy table.
TARGET_FORMAT_POLICY: Mapping[str, TargetPolicy] = _build_policy_map()


def target_policy(target: str) -> TargetPolicy | None:
    if not isinstance(target, str):
        return None
    return TARGET_FORMAT_POLICY.get(target)


def formats_for_target(target: str) -> tuple[str, ...]:
    """Formats relevant for ``target`` (empty tuple for unknown targets)."""

    policy = target_policy(target)
    return policy.formats if policy is not None else ()


def supports_target(target: str) -> bool:
    """True when the target has a mapped format policy."""

    policy = target_policy(target)
    return policy is not None and bool(policy.formats)


def mapped_targets() -> tuple[str, ...]:
    """Every mapped target, alphabetically."""

    return tuple(sorted(TARGET_FORMAT_POLICY))


def is_usable(target: str) -> bool:
    """True when the target is mapped, enabled and has at least one format."""

    policy = target_policy(target)
    return policy is not None and policy.usable


def policy_as_dict() -> dict[str, TargetPolicy]:
    """A defensive copy for introspection; mutating it cannot alter the map."""

    return dict(TARGET_FORMAT_POLICY)


def assert_read_only() -> None:
    """Raise if the policy table is ever mutated at runtime (test helper)."""

    if not isinstance(TARGET_FORMAT_POLICY, MappingProxyType):  # pragma: no cover
        raise AssertionError("target policy table must be a read-only mapping")
    for entry in TARGET_FORMAT_POLICY.values():
        if not dataclasses.is_dataclass(entry):  # pragma: no cover
            raise AssertionError("target policy entries must be frozen dataclasses")

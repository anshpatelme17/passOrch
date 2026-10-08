"""Discovery command allowlist - immutability and fail-closed behaviour."""

from __future__ import annotations

import dataclasses

import pytest

from app.engines.discovery.allowlist import (
    DISCOVERY_ALLOWLIST,
    HASHCAT_OPERATIONS,
    JOHN_OPERATIONS,
    DiscoveryAllowlist,
    iter_allowlisted_operations,
)
from app.engines.discovery.errors import OperationNotAllowedError, UnknownEngineError
from app.engines.discovery.models import HASHCAT, JOHN

# --------------------------------------------------------------------------- #
# content of the allowlist
# --------------------------------------------------------------------------- #


def test_john_allowlist_content_and_order() -> None:
    assert JOHN_OPERATIONS == (
        "--version",
        "-v",
        "--help",
        "--list=formats",
        "--list=help",
    )


def test_hashcat_allowlist_content_and_order() -> None:
    assert HASHCAT_OPERATIONS == (
        "--version",
        "-V",
        "--help",
        "--devices",
        "--backend-info",
    )


def test_allowlist_covers_exactly_the_known_engines() -> None:
    assert DISCOVERY_ALLOWLIST.engines() == (HASHCAT, JOHN)


def test_no_hashcat_mode_list_operation_exists() -> None:
    for operation in DISCOVERY_ALLOWLIST.operations(HASHCAT):
        assert not operation.startswith("--list")
        assert "mode" not in operation
        assert "format" not in operation
        assert "hash-info" not in operation
        assert "stdout" not in operation


def test_john_capability_operation_is_allowlisted() -> None:
    assert DISCOVERY_ALLOWLIST.allows(JOHN, "--list=formats") is True


def test_hashcat_has_no_capability_operation() -> None:
    for operation in ("--list=formats", "--list=modes", "--hash-info", "--backend-info"):
        if operation == "--backend-info":
            continue
        assert DISCOVERY_ALLOWLIST.allows(HASHCAT, operation) is False


def test_iter_allowlisted_operations_is_deterministic() -> None:
    first = iter_allowlisted_operations()
    second = iter_allowlisted_operations()
    assert first == second
    assert len(first) == len(JOHN_OPERATIONS) + len(HASHCAT_OPERATIONS)
    assert (JOHN, "--list=formats") in first
    assert all(pair[0] in (JOHN, HASHCAT) for pair in first)


# --------------------------------------------------------------------------- #
# fail-closed matching
# --------------------------------------------------------------------------- #


def test_unknown_engine_is_rejected() -> None:
    assert DISCOVERY_ALLOWLIST.allows("custom", "--version") is False
    with pytest.raises(UnknownEngineError):
        DISCOVERY_ALLOWLIST.require("custom", "--version")


def test_unknown_engine_fails_closed_even_for_allowlisted_operation() -> None:
    with pytest.raises(UnknownEngineError):
        DISCOVERY_ALLOWLIST.require("custom", "--version")


def test_unknown_operation_is_rejected() -> None:
    assert DISCOVERY_ALLOWLIST.allows(JOHN, "--rules") is False
    with pytest.raises(OperationNotAllowedError):
        DISCOVERY_ALLOWLIST.require(JOHN, "--rules")


def test_matching_is_exact_not_prefix_based() -> None:
    assert DISCOVERY_ALLOWLIST.allows(JOHN, "--list=formats=extra") is False
    assert DISCOVERY_ALLOWLIST.allows(JOHN, "--list") is False
    assert DISCOVERY_ALLOWLIST.allows(JOHN, "--vers") is False


def test_matching_is_case_sensitive() -> None:
    assert DISCOVERY_ALLOWLIST.allows(JOHN, "--VERSION") is False
    assert DISCOVERY_ALLOWLIST.allows(HASHCAT, "-v") is False


def test_whitespace_padded_operation_is_rejected() -> None:
    assert DISCOVERY_ALLOWLIST.allows(JOHN, " --version") is False
    assert DISCOVERY_ALLOWLIST.allows(JOHN, "--version ") is False
    assert DISCOVERY_ALLOWLIST.allows(JOHN, "--version\t") is False


def test_multitoken_operation_is_rejected() -> None:
    assert DISCOVERY_ALLOWLIST.allows(JOHN, "--list=formats --format=raw-md5") is False


def test_non_string_operation_is_rejected() -> None:
    assert DISCOVERY_ALLOWLIST.allows(JOHN, None) is False
    assert DISCOVERY_ALLOWLIST.allows(JOHN, 7) is False
    assert DISCOVERY_ALLOWLIST.allows(JOHN, ["--version"]) is False
    assert DISCOVERY_ALLOWLIST.allows(None, "--version") is False


def test_empty_string_operation_is_rejected() -> None:
    assert DISCOVERY_ALLOWLIST.allows(JOHN, "") is False
    with pytest.raises(OperationNotAllowedError):
        DISCOVERY_ALLOWLIST.require(JOHN, "")


def test_require_validates_engine_before_operation() -> None:
    with pytest.raises(UnknownEngineError):
        DISCOVERY_ALLOWLIST.require("nope", "not-allowlisted")


# --------------------------------------------------------------------------- #
# immutability
# --------------------------------------------------------------------------- #

FORBIDDEN_MUTATORS = (
    "add",
    "allow",
    "allow_operation",
    "update",
    "extend",
    "register",
    "remove",
    "clear",
    "discard",
    "pop",
)


@pytest.mark.parametrize("name", FORBIDDEN_MUTATORS)
def test_allowlist_exposes_no_mutation_api(name: str) -> None:
    assert not hasattr(DISCOVERY_ALLOWLIST, name)


def test_allowlist_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        DISCOVERY_ALLOWLIST._table = {}  # type: ignore[misc]


def test_allowlist_table_mapping_is_read_only() -> None:
    with pytest.raises(TypeError):
        DISCOVERY_ALLOWLIST.table()["john"] = ("--rules",)  # type: ignore[index]


def test_operations_returns_immutable_tuple() -> None:
    operations = DISCOVERY_ALLOWLIST.operations(JOHN)
    assert isinstance(operations, tuple)
    with pytest.raises((AttributeError, TypeError)):
        operations.append("--rules")  # type: ignore[attr-defined]


def test_mutating_module_level_tuple_is_impossible() -> None:
    with pytest.raises(TypeError):
        JOHN_OPERATIONS[0] = "--rules"  # type: ignore[index]


def test_allowlist_module_constants_cannot_be_rebound_by_consumers() -> None:
    import app.engines.discovery.allowlist as module

    assert isinstance(module.DISCOVERY_ALLOWLIST, DiscoveryAllowlist)


# --------------------------------------------------------------------------- #
# construction rules
# --------------------------------------------------------------------------- #


def test_construction_rejects_unknown_engine() -> None:
    with pytest.raises(UnknownEngineError):
        DiscoveryAllowlist({"custom": ("--version",)})


def test_construction_rejects_empty_operation_list() -> None:
    with pytest.raises(OperationNotAllowedError):
        DiscoveryAllowlist({JOHN: ()})


def test_construction_rejects_duplicate_operations() -> None:
    with pytest.raises(ValueError):
        DiscoveryAllowlist({JOHN: ("--version", "--version")})


def test_construction_rejects_whitespace_operations() -> None:
    with pytest.raises(OperationNotAllowedError):
        DiscoveryAllowlist({JOHN: (" --version",)})


def test_missing_engine_entry_fails_closed() -> None:
    allowlist = DiscoveryAllowlist({JOHN: JOHN_OPERATIONS})
    assert allowlist.operations(HASHCAT) == ()
    assert allowlist.allows(HASHCAT, "--version") is False
    with pytest.raises(OperationNotAllowedError):
        allowlist.require(HASHCAT, "--version")


def test_allowlist_dataclass_has_no_default_operation_grant() -> None:
    empty = DiscoveryAllowlist()
    assert empty.allows(JOHN, "--version") is False
    assert empty.allows(HASHCAT, "--version") is False

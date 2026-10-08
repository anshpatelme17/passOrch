"""Phase 2B.5 - read-only target -> format policy map."""

from __future__ import annotations

import dataclasses
from types import MappingProxyType

import pytest

from app.engines.discovery.capability_map import (
    TARGET_FORMAT_POLICY,
    TargetPolicy,
    assert_read_only,
    formats_for_target,
    is_usable,
    mapped_targets,
    policy_as_dict,
    supports_target,
    target_policy,
)

EXPECTED_TARGETS = (
    "office-document",
    "pdf-document",
    "raw-password-hash",
    "unix-password-hash",
    "windows-password-hash",
    "zip-archive",
)


def test_mapped_targets_are_sorted_and_complete() -> None:
    assert mapped_targets() == EXPECTED_TARGETS


def test_policy_table_is_read_only() -> None:
    with pytest.raises(TypeError):
        TARGET_FORMAT_POLICY["new-target"] = TargetPolicy(target="new-target",  # type: ignore[index]
                                                           formats=("x",))


def test_policy_table_is_a_mapping_proxy() -> None:
    assert isinstance(TARGET_FORMAT_POLICY, MappingProxyType)


def test_policy_entries_are_frozen() -> None:
    policy = target_policy("pdf-document")
    assert policy is not None
    with pytest.raises(Exception):
        policy.target = "other"  # type: ignore[misc]


def test_formats_for_target_known() -> None:
    assert formats_for_target("pdf-document") == ("pdf",)
    assert "ntlm" in formats_for_target("windows-password-hash")


def test_formats_for_target_returns_immutable_tuple() -> None:
    formats = formats_for_target("zip-archive")
    assert isinstance(formats, tuple)
    with pytest.raises((AttributeError, TypeError)):
        formats.append("evil")  # type: ignore[attr-defined]


def test_formats_for_target_unknown_returns_empty() -> None:
    assert formats_for_target("unknown-target") == ()


def test_formats_for_target_filename_returns_empty() -> None:
    assert formats_for_target("report.pdf") == ()
    assert formats_for_target(".zip") == ()
    assert formats_for_target("archive.zip") == ()


def test_formats_for_target_non_string_returns_empty() -> None:
    assert formats_for_target(None) == ()  # type: ignore[arg-type]
    assert formats_for_target(42) == ()  # type: ignore[arg-type]


def test_supports_target_positive() -> None:
    for target in EXPECTED_TARGETS:
        assert supports_target(target) is True


def test_supports_target_negative() -> None:
    assert supports_target("unknown-target") is False
    assert supports_target("") is False
    assert supports_target("payload.exe") is False


def test_target_policy_lookup() -> None:
    assert target_policy("pdf-document") is not None
    assert target_policy("nope") is None
    assert target_policy(None) is None  # type: ignore[arg-type]


def test_every_mapped_policy_has_formats() -> None:
    for target in mapped_targets():
        policy = target_policy(target)
        assert policy is not None
        assert policy.formats
        assert policy.enabled is True


def test_every_format_name_is_normalised() -> None:
    for policy in TARGET_FORMAT_POLICY.values():
        for name in policy.formats:
            assert name == name.strip()
            assert name == name.lower()
            assert " " not in name


def test_is_usable_for_mapped_targets() -> None:
    for target in EXPECTED_TARGETS:
        assert is_usable(target) is True


def test_is_usable_for_unmapped_targets() -> None:
    assert is_usable("unknown-target") is False
    assert is_usable("") is False


def test_disabled_policy_is_not_usable() -> None:
    disabled = TargetPolicy(target="t", formats=("a",), enabled=False)
    assert disabled.usable is False


def test_policy_without_formats_is_not_usable() -> None:
    empty = TargetPolicy(target="t", formats=())
    assert empty.usable is False
    assert empty.formats == ()


def test_policy_as_dict_is_a_copy() -> None:
    snapshot = policy_as_dict()
    snapshot.pop("pdf-document", None)
    assert "pdf-document" in TARGET_FORMAT_POLICY


def test_assert_read_only_passes() -> None:
    assert_read_only()


def test_target_policy_is_frozen_dataclass() -> None:
    assert dataclasses.is_dataclass(TargetPolicy)
    assert TargetPolicy.__dataclass_params__.frozen  # type: ignore[attr-defined]


def test_descriptions_present_for_all_targets() -> None:
    for policy in TARGET_FORMAT_POLICY.values():
        assert policy.description

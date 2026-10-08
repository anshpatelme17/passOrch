"""Phase 2B.5 - engine capability flow (John verified, Hashcat unknown)."""

from __future__ import annotations

import pytest

from app.engines.discovery import hashcat as hashcat_mod
from app.engines.discovery import john as john_mod
from app.engines.discovery.allowlist import DISCOVERY_ALLOWLIST, HASHCAT_OPERATIONS
from app.engines.discovery.capabilities import (
    JOHN_FORMATS_HEADER,
    MODE_LIST_OPERATION,
)
from app.engines.discovery.errors import CapabilityParseError
from app.engines.discovery.models import (
    HASHCAT,
    JOHN,
    CapabilityStatus,
    EngineCapabilities,
    ProbeResult,
)

VERIFIED = CapabilityStatus.VERIFIED
UNKNOWN = CapabilityStatus.UNKNOWN
ERROR = CapabilityStatus.ERROR

FULL_JOHN_OUTPUT = (
    f"{JOHN_FORMATS_HEADER}\n"
    "  md5crypt, bcrypt, sha512crypt, sha256crypt,\n"
    "  ntlm, raw-md5, raw-sha1, raw-sha256,\n"
    "  zip, pkzip, pdf, office2007, office2010, office2013\n"
)

PARTIAL_JOHN_OUTPUT = (
    f"{JOHN_FORMATS_HEADER}\n"
    "  md5crypt, bcrypt, sha512crypt, sha256crypt\n"
)


def _probe(
    operation: str,
    *,
    engine: str = JOHN,
    stdout: str = "",
    ok: bool = True,
    returncode: int = 0,
    truncated: bool = False,
) -> ProbeResult:
    return ProbeResult(
        engine=engine,
        operation=operation,
        ok=ok,
        returncode=returncode,
        stdout=stdout,
        truncated=truncated,
    )


# --------------------------------------------------------------------------- #
# John - missing / error / verified
# --------------------------------------------------------------------------- #


def test_john_without_probe_is_unknown_missing_capability() -> None:
    caps = john_mod.capabilities_from_probe(None)
    assert caps.capability_status is UNKNOWN
    assert caps.reason == "capability_probe_missing"
    assert caps.formats == ()
    assert caps.targets == ()


def test_john_empty_probe_mapping_is_unknown() -> None:
    caps = john_mod.capabilities_from_probes({})
    assert caps.capability_status is UNKNOWN
    assert caps.reason == "capability_probe_missing"


def test_john_failed_probe_is_error() -> None:
    probe = _probe("--list=formats", ok=False, returncode=1)
    caps = john_mod.capabilities_from_probe(probe)
    assert caps.capability_status is ERROR
    assert caps.reason == "capability_probe_failed"
    assert caps.formats == ()


def test_john_truncated_probe_is_unknown() -> None:
    probe = _probe("--list=formats", stdout=FULL_JOHN_OUTPUT, truncated=True)
    caps = john_mod.capabilities_from_probe(probe)
    assert caps.capability_status is UNKNOWN
    assert caps.reason == "truncated_probe"
    assert caps.formats == ()


def test_john_non_allowlisted_probe_operation_is_error() -> None:
    probe = _probe("--rules", stdout="anything")
    caps = john_mod.capabilities_from_probe(probe)
    assert caps.capability_status is ERROR
    assert caps.reason == "probe_operation_not_allowlisted"
    assert DISCOVERY_ALLOWLIST.allows(JOHN, "--rules") is False


def test_john_foreign_probe_engine_is_error() -> None:
    probe = _probe("--list=formats", engine=HASHCAT, stdout=FULL_JOHN_OUTPUT)
    caps = john_mod.capabilities_from_probe(probe)
    assert caps.capability_status is ERROR
    assert caps.reason == "foreign_probe"


def test_john_allowlisted_non_capability_probe_is_unknown() -> None:
    probe = _probe("--version", stdout="john 1.9.0")
    caps = john_mod.capabilities_from_probe(probe)
    assert caps.capability_status is UNKNOWN
    assert caps.reason == "capability_probe_missing"
    assert caps.formats == ()


def test_john_probes_mapping_with_foreign_operation_is_error() -> None:
    caps = john_mod.capabilities_from_probes({"--rules": _probe("--rules")})
    assert caps.capability_status is ERROR
    assert caps.reason == "probe_operation_not_allowlisted"


def test_john_full_probe_is_verified() -> None:
    probe = _probe("--list=formats", stdout=FULL_JOHN_OUTPUT)
    caps = john_mod.capabilities_from_probe(probe)
    assert caps.capability_status is VERIFIED
    assert caps.is_verified is True
    assert caps.parser == "john-format-parser"
    assert caps.source_operation == "--list=formats"
    assert caps.reason is None
    assert "md5crypt" in caps.formats


def test_john_verified_targets_cover_every_mapped_target() -> None:
    probe = _probe("--list=formats", stdout=FULL_JOHN_OUTPUT)
    caps = john_mod.capabilities_from_probe(probe)
    assert set(caps.targets) == {
        "office-document",
        "pdf-document",
        "raw-password-hash",
        "unix-password-hash",
        "windows-password-hash",
        "zip-archive",
    }


def test_john_supported_targets_derived_from_verified_formats_only() -> None:
    probe = _probe("--list=formats", stdout=PARTIAL_JOHN_OUTPUT)
    caps = john_mod.capabilities_from_probe(probe)
    assert caps.capability_status is VERIFIED
    assert caps.targets == ("unix-password-hash",)


def test_john_unknown_capability_yields_no_targets() -> None:
    caps = EngineCapabilities(engine=JOHN, formats=("md5crypt", "pdf"))
    assert john_mod.supported_targets(caps) == ()


def test_john_error_capability_yields_no_targets() -> None:
    caps = EngineCapabilities(
        engine=JOHN, capability_status=ERROR, formats=("md5crypt",)
    )
    assert john_mod.supported_targets(caps) == ()


def test_john_verified_but_empty_formats_yields_no_targets() -> None:
    caps = EngineCapabilities(engine=JOHN, capability_status=VERIFIED, formats=())
    assert john_mod.supported_targets(caps) == ()


def test_john_verified_formats_give_subset_of_targets() -> None:
    caps = EngineCapabilities(
        engine=JOHN, capability_status=VERIFIED, formats=("pdf", "ntlm")
    )
    assert john_mod.supported_targets(caps) == (
        "pdf-document",
        "windows-password-hash",
    )


def test_john_unmapped_format_gives_no_targets() -> None:
    caps = EngineCapabilities(
        engine=JOHN, capability_status=VERIFIED, formats=("not-a-real-format",)
    )
    assert john_mod.supported_targets(caps) == ()


def test_john_parse_formats_rejects_unbound_operation() -> None:
    with pytest.raises(CapabilityParseError):
        john_mod.parse_formats("x", operation="--version")


def test_john_format_operation_is_allowlisted() -> None:
    assert DISCOVERY_ALLOWLIST.allows(JOHN, john_mod.JOHN_FORMAT_OPERATION) is True


def test_john_capability_operation_constant() -> None:
    assert john_mod.JOHN_CAPABILITY_OPERATIONS == ("--list=formats",)
    assert john_mod.JOHN_ENGINE == JOHN


# --------------------------------------------------------------------------- #
# Hashcat - always UNKNOWN (no allowlisted mode-list operation)
# --------------------------------------------------------------------------- #


def test_hashcat_without_probe_is_unknown_formats_not_listed() -> None:
    caps = hashcat_mod.capabilities_from_probe(None)
    assert caps.capability_status is UNKNOWN
    assert caps.reason == "formats_not_listed"
    assert caps.formats == ()
    assert caps.targets == ()


def test_hashcat_empty_probe_mapping_is_unknown() -> None:
    caps = hashcat_mod.capabilities_from_probes({})
    assert caps.capability_status is UNKNOWN
    assert caps.reason == "formats_not_listed"


def test_hashcat_version_probe_still_unknown() -> None:
    probe = _probe("--version", engine=HASHCAT, stdout="v6.2.6")
    caps = hashcat_mod.capabilities_from_probe(probe)
    assert caps.capability_status is UNKNOWN
    assert caps.reason == "formats_not_listed"
    assert caps.formats == ()


def test_hashcat_failed_allowlisted_probe_is_error() -> None:
    probe = _probe("--devices", engine=HASHCAT, ok=False, returncode=2)
    caps = hashcat_mod.capabilities_from_probe(probe)
    assert caps.capability_status is ERROR
    assert caps.reason == "capability_probe_failed"


def test_hashcat_mode_list_probe_is_rejected_by_allowlist() -> None:
    probe = _probe(MODE_LIST_OPERATION, engine=HASHCAT, stdout="Supported hash modes:\n")
    caps = hashcat_mod.capabilities_from_probe(probe)
    assert caps.capability_status is ERROR
    assert caps.reason == "probe_operation_not_allowlisted"
    assert DISCOVERY_ALLOWLIST.allows(HASHCAT, MODE_LIST_OPERATION) is False


def test_hashcat_foreign_probe_engine_is_error() -> None:
    probe = _probe("--devices", engine=JOHN, ok=True)
    caps = hashcat_mod.capabilities_from_probe(probe)
    assert caps.capability_status is ERROR
    assert caps.reason == "foreign_probe"


def test_hashcat_non_allowlisted_probe_is_error() -> None:
    probe = _probe("--stdout", engine=HASHCAT)
    caps = hashcat_mod.capabilities_from_probe(probe)
    assert caps.capability_status is ERROR
    assert caps.reason == "probe_operation_not_allowlisted"


@pytest.mark.parametrize("operation", HASHCAT_OPERATIONS)
def test_hashcat_never_becomes_verified_for_any_allowlisted_operation(
    operation: str,
) -> None:
    probe = _probe(operation, engine=HASHCAT, ok=True, stdout="some output")
    caps = hashcat_mod.capabilities_from_probe(probe)
    assert caps.capability_status is not VERIFIED
    assert caps.formats == ()
    assert caps.targets == ()


def test_hashcat_supported_targets_requires_verified_formats() -> None:
    unknown = EngineCapabilities(engine=HASHCAT, formats=("pdf",))
    assert hashcat_mod.supported_targets(unknown) == ()
    assert hashcat_mod.supported_targets(
        EngineCapabilities(engine=HASHCAT, capability_status=ERROR)
    ) == ()


def test_hashcat_pipeline_never_produces_verified_targets() -> None:
    """Even with allowlisted probes in hand, Hashcat reports no targets."""

    probes = {
        operation: _probe(operation, engine=HASHCAT, ok=True, stdout="v6.2.6")
        for operation in HASHCAT_OPERATIONS
    }
    caps = hashcat_mod.capabilities_from_probes(probes)
    assert caps.capability_status is not VERIFIED
    assert hashcat_mod.supported_targets(caps) == ()


def test_hashcat_mode_list_operation_constant_is_not_allowlisted() -> None:
    assert hashcat_mod.HASHCAT_MODE_LIST_OPERATION == MODE_LIST_OPERATION
    assert DISCOVERY_ALLOWLIST.allows(HASHCAT, hashcat_mod.HASHCAT_MODE_LIST_OPERATION) is False


def test_hashcat_parse_modes_rejects_unbound_operation() -> None:
    with pytest.raises(CapabilityParseError):
        hashcat_mod.parse_modes("x", operation="--version")


def test_hashcat_format_reason_constant() -> None:
    assert hashcat_mod.FORMATS_NOT_LISTED == "formats_not_listed"


# --------------------------------------------------------------------------- #
# cross-engine safety
# --------------------------------------------------------------------------- #


def test_capability_records_carry_no_raw_output() -> None:
    probe = _probe("--list=formats", stdout=FULL_JOHN_OUTPUT)
    caps = john_mod.capabilities_from_probe(probe)
    fields = set(EngineCapabilities.__dataclass_fields__)  # type: ignore[attr-defined]
    assert not fields & {"stdout", "stderr", "raw_output", "output"}
    for value in (caps.reason, caps.source_operation, caps.parser):
        assert "Supported hash algorithms" not in (value or "")


def test_john_and_hashcat_capability_engines_match_their_identity() -> None:
    john_caps = john_mod.capabilities_from_probe(
        _probe("--list=formats", stdout=FULL_JOHN_OUTPUT)
    )
    hashcat_caps = hashcat_mod.capabilities_from_probe(None)
    assert john_caps.engine == JOHN
    assert hashcat_caps.engine == HASHCAT

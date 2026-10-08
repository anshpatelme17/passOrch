"""Phase 2B discovery registry - storage, compatibility, payload safety."""

from __future__ import annotations

import json

import pytest

from app.engines.discovery.errors import RegistryError, UnknownEngineError
from app.engines.discovery.models import (
    HASHCAT,
    JOHN,
    CapabilityStatus,
    EngineCapabilities,
    EngineSnapshot,
)
from app.engines.discovery.registry import (
    DiscoveryRegistry,
    RegistryEntry,
    default_discovery_registry,
)

VERIFIED = CapabilityStatus.VERIFIED
UNKNOWN = CapabilityStatus.UNKNOWN
ERROR = CapabilityStatus.ERROR


def _verified(engine: str = JOHN, formats: tuple[str, ...] = ("md5crypt",)) -> EngineCapabilities:
    return EngineCapabilities(
        engine=engine,
        capability_status=VERIFIED,
        formats=formats,
        source_operation="--list=formats",
        parser="john-format-parser",
    )


def _snapshot(engine: str = JOHN, discovered: bool = True) -> EngineSnapshot:
    return EngineSnapshot(engine=engine, discovered=discovered, binary_found=discovered)


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #


def test_register_known_engine() -> None:
    registry = DiscoveryRegistry()
    entry = registry.register(_snapshot())
    assert entry.engine == JOHN
    assert registry.get(JOHN) is entry


def test_register_unknown_engine_rejected() -> None:
    registry = DiscoveryRegistry()
    with pytest.raises(UnknownEngineError):
        registry.register(EngineSnapshot(engine="custom", discovered=True))


def test_register_refresh_preserves_capability_evidence() -> None:
    registry = DiscoveryRegistry()
    registry.register(_snapshot())
    registry.set_capabilities(JOHN, _verified())
    registry.register(_snapshot(discovered=True))
    assert registry.get(JOHN).capability_status is VERIFIED


def test_set_capabilities_unknown_engine_rejected() -> None:
    registry = DiscoveryRegistry()
    with pytest.raises(UnknownEngineError):
        registry.set_capabilities("custom", EngineCapabilities(engine="custom"))


def test_set_capabilities_engine_mismatch_rejected() -> None:
    registry = DiscoveryRegistry()
    registry.register(_snapshot(engine=JOHN))
    with pytest.raises(RegistryError):
        registry.set_capabilities(JOHN, _verified(engine=HASHCAT))


def test_set_capabilities_without_snapshot_is_allowed_but_not_discovered() -> None:
    registry = DiscoveryRegistry()
    registry.set_capabilities(JOHN, _verified())
    entry = registry.require(JOHN)
    assert entry.snapshot.discovered is False
    assert entry.real_engine_compatible is False


def test_reset_clears_registry() -> None:
    registry = DiscoveryRegistry()
    registry.register(_snapshot())
    registry.reset()
    assert registry.get(JOHN) is None
    assert registry.engines() == ()


def test_get_unknown_returns_none() -> None:
    assert DiscoveryRegistry().get(HASHCAT) is None


def test_require_unknown_raises() -> None:
    with pytest.raises(RegistryError):
        DiscoveryRegistry().require(HASHCAT)


def test_entries_and_engines_are_sorted() -> None:
    registry = DiscoveryRegistry()
    registry.register(_snapshot(engine=JOHN))
    registry.register(_snapshot(engine=HASHCAT))
    assert registry.engines() == (HASHCAT, JOHN)
    assert [e.engine for e in registry.entries()] == [HASHCAT, JOHN]


def test_registry_entry_is_frozen() -> None:
    entry = RegistryEntry(engine=JOHN, snapshot=_snapshot())
    with pytest.raises(Exception):
        entry.engine = HASHCAT  # type: ignore[misc]


def test_registry_never_stores_probe_results() -> None:
    registry = DiscoveryRegistry()
    registry.register(_snapshot())
    source_params = set(DiscoveryRegistry.set_capabilities.__annotations__)
    assert "stdout" not in source_params
    assert "ProbeResult" not in source_params


# --------------------------------------------------------------------------- #
# capability status / compatibility
# --------------------------------------------------------------------------- #


def test_missing_capability_reports_unknown() -> None:
    registry = DiscoveryRegistry()
    registry.register(_snapshot())
    entry = registry.require(JOHN)
    assert entry.capability_status is UNKNOWN
    assert entry.has_capability_evidence is False
    assert registry.capability_status(JOHN) is UNKNOWN


def test_unregistered_engine_reports_unknown() -> None:
    assert DiscoveryRegistry().capability_status(JOHN) is UNKNOWN


def test_unknown_capability_is_not_real_engine_compatible() -> None:
    registry = DiscoveryRegistry()
    registry.register(_snapshot())
    registry.set_capabilities(
        JOHN, EngineCapabilities(engine=JOHN, capability_status=UNKNOWN)
    )
    assert registry.require(JOHN).real_engine_compatible is False


def test_error_capability_is_not_real_engine_compatible() -> None:
    registry = DiscoveryRegistry()
    registry.register(_snapshot())
    registry.set_capabilities(
        JOHN, EngineCapabilities(engine=JOHN, capability_status=ERROR, reason="boom")
    )
    assert registry.require(JOHN).real_engine_compatible is False


def test_missing_capability_is_not_real_engine_compatible() -> None:
    registry = DiscoveryRegistry()
    registry.register(_snapshot())
    assert registry.require(JOHN).real_engine_compatible is False


def test_verified_capability_is_real_engine_compatible() -> None:
    registry = DiscoveryRegistry()
    registry.register(_snapshot())
    registry.set_capabilities(JOHN, _verified())
    entry = registry.require(JOHN)
    assert entry.is_verified is True
    assert entry.real_engine_compatible is True


def test_verified_but_undiscovered_is_not_compatible() -> None:
    registry = DiscoveryRegistry()
    registry.register(_snapshot(discovered=False))
    registry.set_capabilities(JOHN, _verified())
    assert registry.require(JOHN).real_engine_compatible is False


def test_formats_hidden_unless_verified() -> None:
    registry = DiscoveryRegistry()
    registry.register(_snapshot())
    registry.set_capabilities(
        JOHN, EngineCapabilities(engine=JOHN, formats=("md5crypt",))
    )
    assert registry.require(JOHN).formats == ()


def test_verified_formats_are_exposed() -> None:
    registry = DiscoveryRegistry()
    registry.register(_snapshot())
    registry.set_capabilities(JOHN, _verified(formats=("md5crypt", "bcrypt")))
    assert registry.require(JOHN).formats == ("md5crypt", "bcrypt")


def test_verified_engines_listing() -> None:
    registry = DiscoveryRegistry()
    registry.register(_snapshot(engine=JOHN))
    registry.register(_snapshot(engine=HASHCAT))
    registry.set_capabilities(JOHN, _verified())
    registry.set_capabilities(
        HASHCAT, EngineCapabilities(engine=HASHCAT, capability_status=UNKNOWN)
    )
    assert registry.verified_engines() == (JOHN,)
    assert registry.compatible_engines() == (JOHN,)


def test_default_registry_is_a_registry() -> None:
    assert isinstance(default_discovery_registry(), DiscoveryRegistry)


# --------------------------------------------------------------------------- #
# payload safety
# --------------------------------------------------------------------------- #


def test_payload_exposes_capability_status_for_every_engine() -> None:
    registry = DiscoveryRegistry()
    registry.register(_snapshot(engine=JOHN))
    registry.register(_snapshot(engine=HASHCAT))
    payload = registry.payload()
    statuses = {e["engine"]: e["capability_status"] for e in payload["engines"]}
    assert statuses == {JOHN: "unknown", HASHCAT: "unknown"}


def test_payload_is_json_serialisable() -> None:
    registry = DiscoveryRegistry()
    registry.register(_snapshot())
    registry.set_capabilities(JOHN, _verified())
    encoded = json.dumps(registry.payload())
    assert "verified" in encoded


def test_payload_never_contains_raw_probe_output() -> None:
    registry = DiscoveryRegistry()
    registry.register(
        EngineSnapshot(engine=JOHN, discovered=True, version="john 1.9.0-jumbo")
    )
    registry.set_capabilities(JOHN, _verified())
    encoded = json.dumps(registry.payload())
    assert "Supported hash algorithms" not in encoded
    registry.validate_payload()
    registry.validate_payload(registry.payload())


def test_payload_has_no_raw_output_keys() -> None:
    registry = DiscoveryRegistry()
    registry.register(_snapshot())
    registry.set_capabilities(JOHN, _verified())
    for engine_payload in registry.payload()["engines"]:
        assert not set(engine_payload) & {
            "stdout",
            "stderr",
            "raw_output",
            "probe_output",
            "output",
            "command",
            "argv",
        }


def test_validate_payload_rejects_leaking_payload() -> None:
    registry = DiscoveryRegistry()
    with pytest.raises(RegistryError):
        registry.validate_payload(
            {"engines": [{"engine": JOHN, "capability_status": "verified",
                          "stdout": "raw"}]}
        )


def test_validate_payload_requires_capability_status() -> None:
    registry = DiscoveryRegistry()
    with pytest.raises(RegistryError):
        registry.validate_payload({"engines": [{"engine": JOHN}]})


def test_payload_reports_reason_for_unknown_hashcat() -> None:
    registry = DiscoveryRegistry()
    registry.register(_snapshot(engine=HASHCAT))
    registry.set_capabilities(
        HASHCAT,
        EngineCapabilities(
            engine=HASHCAT,
            capability_status=UNKNOWN,
            reason="formats_not_listed",
        ),
    )
    payload = registry.payload()["engines"][0]
    assert payload["capability_status"] == "unknown"
    assert payload["reason"] == "formats_not_listed"
    assert payload["formats"] == []


def test_payload_flags_real_engine_compatibility() -> None:
    registry = DiscoveryRegistry()
    registry.register(_snapshot())
    registry.set_capabilities(JOHN, _verified())
    assert registry.payload()["engines"][0]["real_engine_compatible"] is True

"""Hashcat discovery integration.

Hashcat currently reports ``UNKNOWN`` with reason ``formats_not_listed``
because **no allowlisted mode-list operation exists**.  The
:class:`~app.engines.discovery.capabilities.HashcatFormatParser` is bound to
``MODE_LIST_OPERATION``, which the immutable allowlist refuses, so no Hashcat
capability can ever become ``VERIFIED`` through the discovery pipeline.
"""

from __future__ import annotations

from typing import Mapping

from app.engines.discovery.allowlist import DISCOVERY_ALLOWLIST
from app.engines.discovery.capabilities import (
    MODE_LIST_OPERATION,
    ParsedCapabilities,
    get_parser,
)
from app.engines.discovery.capability_map import formats_for_target, mapped_targets
from app.engines.discovery.errors import CapabilityParseError
from app.engines.discovery.models import (
    HASHCAT,
    CapabilityStatus,
    EngineCapabilities,
    ProbeResult,
)

__all__ = [
    "FORMATS_NOT_LISTED",
    "HASHCAT_CAPABILITY_OPERATIONS",
    "HASHCAT_ENGINE",
    "HASHCAT_MODE_LIST_OPERATION",
    "capabilities_from_probe",
    "capabilities_from_probes",
    "parse_modes",
    "supported_targets",
]

HASHCAT_ENGINE = HASHCAT
HASHCAT_MODE_LIST_OPERATION = MODE_LIST_OPERATION
FORMATS_NOT_LISTED = "formats_not_listed"
HASHCAT_CAPABILITY_OPERATIONS: tuple[str, ...] = (HASHCAT_MODE_LIST_OPERATION,)


def parse_modes(output: str, *, operation: str = HASHCAT_MODE_LIST_OPERATION) -> ParsedCapabilities:
    """Parse hashcat mode output with the parser bound to that operation."""

    parser = get_parser(HASHCAT_ENGINE, operation)
    if parser is None:
        raise CapabilityParseError(
            f"no Hashcat capability parser is bound to operation {operation!r}"
        )
    return parser.parse(output, engine=HASHCAT_ENGINE, operation=operation)


def supported_targets(capabilities: EngineCapabilities) -> tuple[str, ...]:
    """Hashcat targets, derived only from VERIFIED capabilities.

    Today this is always empty: Hashcat capability status never reaches
    ``VERIFIED`` without an allowlisted mode-list operation.
    """

    if capabilities.capability_status is not CapabilityStatus.VERIFIED:
        return ()
    verified = set(capabilities.formats)
    if not verified:
        return ()
    return tuple(
        target
        for target in mapped_targets()
        if any(name in verified for name in formats_for_target(target))
    )


def capabilities_from_probe(probe: ProbeResult | None) -> EngineCapabilities:
    """Build Hashcat capability evidence from one probe result."""

    if probe is None:
        return EngineCapabilities(
            engine=HASHCAT_ENGINE,
            capability_status=CapabilityStatus.UNKNOWN,
            reason=FORMATS_NOT_LISTED,
        )

    if probe.engine != HASHCAT_ENGINE:
        return EngineCapabilities(
            engine=HASHCAT_ENGINE,
            capability_status=CapabilityStatus.ERROR,
            source_operation=probe.operation,
            reason="foreign_probe",
        )

    if not DISCOVERY_ALLOWLIST.allows(HASHCAT_ENGINE, probe.operation):
        return EngineCapabilities(
            engine=HASHCAT_ENGINE,
            capability_status=CapabilityStatus.ERROR,
            source_operation=probe.operation,
            reason="probe_operation_not_allowlisted",
        )

    if probe.operation not in HASHCAT_CAPABILITY_OPERATIONS:
        # Allowlisted, but it never lists modes/formats.
        if not probe.ok:
            return EngineCapabilities(
                engine=HASHCAT_ENGINE,
                capability_status=CapabilityStatus.ERROR,
                source_operation=probe.operation,
                reason="capability_probe_failed",
            )
        return EngineCapabilities(
            engine=HASHCAT_ENGINE,
            capability_status=CapabilityStatus.UNKNOWN,
            source_operation=probe.operation,
            reason=FORMATS_NOT_LISTED,
        )

    # A mode-list probe can never be produced by the allowlisted runner.
    return EngineCapabilities(
        engine=HASHCAT_ENGINE,
        capability_status=CapabilityStatus.ERROR,
        source_operation=probe.operation,
        reason="probe_operation_not_allowlisted",
    )


def capabilities_from_probes(probes: Mapping[str, ProbeResult]) -> EngineCapabilities:
    """Build Hashcat capability evidence from a probe mapping."""

    for operation, probe in probes.items():
        if probe.engine != HASHCAT_ENGINE or not DISCOVERY_ALLOWLIST.allows(
            HASHCAT_ENGINE, operation
        ):
            return EngineCapabilities(
                engine=HASHCAT_ENGINE,
                capability_status=CapabilityStatus.ERROR,
                source_operation=operation,
                reason="probe_operation_not_allowlisted",
            )
        if not probe.ok:
            return EngineCapabilities(
                engine=HASHCAT_ENGINE,
                capability_status=CapabilityStatus.ERROR,
                source_operation=operation,
                reason="capability_probe_failed",
            )

    # No allowlisted operation lists modes, so evidence stays UNKNOWN.
    return EngineCapabilities(
        engine=HASHCAT_ENGINE,
        capability_status=CapabilityStatus.UNKNOWN,
        reason=FORMATS_NOT_LISTED,
    )

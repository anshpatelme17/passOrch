"""John the Ripper discovery integration (verified parsing only).

``supported_targets`` is derived exclusively from **VERIFIED** capability
evidence: an ``UNKNOWN`` / ``ERROR`` / missing capability yields no targets.

No subprocess is executed here; this module only interprets
:class:`~app.engines.discovery.models.ProbeResult` objects produced by the
allowlisted runner.
"""

from __future__ import annotations

import dataclasses
from typing import Mapping

from app.engines.discovery.allowlist import DISCOVERY_ALLOWLIST
from app.engines.discovery.capabilities import (
    JohnFormatParser,
    ParsedCapabilities,
    get_parser,
)
from app.engines.discovery.capability_map import formats_for_target, mapped_targets
from app.engines.discovery.errors import CapabilityParseError
from app.engines.discovery.models import (
    JOHN,
    CapabilityStatus,
    EngineCapabilities,
    ProbeResult,
)

__all__ = [
    "JOHN_CAPABILITY_OPERATIONS",
    "JOHN_ENGINE",
    "JOHN_FORMAT_OPERATION",
    "capabilities_from_probe",
    "capabilities_from_probes",
    "parse_formats",
    "supported_targets",
]

JOHN_ENGINE = JOHN
JOHN_FORMAT_OPERATION = JohnFormatParser.operation
JOHN_VERSION_OPERATION = "--version"
JOHN_CAPABILITY_OPERATIONS: tuple[str, ...] = (JOHN_FORMAT_OPERATION,)


def parse_formats(output: str, *, operation: str = JOHN_FORMAT_OPERATION) -> ParsedCapabilities:
    """Parse John format output with the parser bound to that operation."""

    parser = get_parser(JOHN_ENGINE, operation)
    if parser is None:
        raise CapabilityParseError(
            f"no John capability parser is bound to operation {operation!r}"
        )
    return parser.parse(output, engine=JOHN_ENGINE, operation=operation)


def supported_targets(capabilities: EngineCapabilities) -> tuple[str, ...]:
    """Targets supported by John, derived only from VERIFIED formats."""

    if capabilities.capability_status is not CapabilityStatus.VERIFIED:
        return ()
    verified = set(capabilities.formats)
    if not verified:
        return ()
    supported = [
        target
        for target in mapped_targets()
        if any(name in verified for name in formats_for_target(target))
    ]
    return tuple(supported)


def capabilities_from_probe(probe: ProbeResult | None) -> EngineCapabilities:
    """Build John capability evidence from one probe result."""

    if probe is None:
        return EngineCapabilities(engine=JOHN_ENGINE, reason="capability_probe_missing")

    if probe.engine != JOHN_ENGINE:
        return EngineCapabilities(
            engine=JOHN_ENGINE,
            capability_status=CapabilityStatus.ERROR,
            source_operation=probe.operation,
            reason="foreign_probe",
        )

    if not DISCOVERY_ALLOWLIST.allows(JOHN_ENGINE, probe.operation):
        return EngineCapabilities(
            engine=JOHN_ENGINE,
            capability_status=CapabilityStatus.ERROR,
            source_operation=probe.operation,
            reason="probe_operation_not_allowlisted",
        )

    if probe.operation not in JOHN_CAPABILITY_OPERATIONS:
        return EngineCapabilities(
            engine=JOHN_ENGINE,
            source_operation=probe.operation,
            reason="capability_probe_missing",
        )

    if not probe.ok:
        return EngineCapabilities(
            engine=JOHN_ENGINE,
            capability_status=CapabilityStatus.ERROR,
            source_operation=probe.operation,
            reason="capability_probe_failed",
        )

    if probe.truncated:
        return EngineCapabilities(
            engine=JOHN_ENGINE,
            source_operation=probe.operation,
            reason="truncated_probe",
        )

    try:
        parsed = parse_formats(probe.stdout, operation=probe.operation)
    except CapabilityParseError:
        return EngineCapabilities(
            engine=JOHN_ENGINE,
            capability_status=CapabilityStatus.ERROR,
            source_operation=probe.operation,
            reason="foreign_output",
        )

    capabilities = EngineCapabilities(
        engine=JOHN_ENGINE,
        capability_status=parsed.status,
        formats=parsed.formats,
        source_operation=probe.operation,
        reason=parsed.reason,
        parser=parsed.parser,
    )
    return dataclasses.replace(capabilities, targets=supported_targets(capabilities))


def capabilities_from_probes(probes: Mapping[str, ProbeResult]) -> EngineCapabilities:
    """Build John capability evidence from a probe mapping."""

    for operation, probe in probes.items():
        if probe.engine != JOHN_ENGINE or not DISCOVERY_ALLOWLIST.allows(
            JOHN_ENGINE, operation
        ):
            return EngineCapabilities(
                engine=JOHN_ENGINE,
                capability_status=CapabilityStatus.ERROR,
                source_operation=operation,
                reason="probe_operation_not_allowlisted",
            )

    capability_probe = probes.get(JOHN_FORMAT_OPERATION)
    return capabilities_from_probe(capability_probe)

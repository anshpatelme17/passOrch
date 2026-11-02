"""M3.1 - structured audit-event core: closed schema, one JSON line to stdout.

This module is the **sole approved structured application event output**.
General application logging stays prohibited everywhere else (a tested
security contract); this service is the narrow, deliberate exception, and it
emits only the closed schema defined here:

* exactly the approved fields (``schema_version``, ``event``, ``ts``,
  ``job_id``, ``target``, ``status``, ``reason``, ``engine``,
  ``discovered``, ``capability_status``, ``truncated``, ``instance_id``,
  ``simulation_only``) - no free-text field exists, so operator references,
  execution modes, ``PolicyDecision.detail``/``problems``, exception
  messages, headers, cookies, request bodies, client addresses, subprocess
  argv, executable paths and raw probe output have nowhere to go;
* closed value sets sourced from the project's own constants
  (``REASON_*``, ``JobStatus``, ``KNOWN_ENGINES``, ``CapabilityStatus``,
  ``TARGET_FORMAT_POLICY``) plus transport codes pinned by tests to the
  authoritative API error codes - never invented strings;
* ``schema_version`` is the fixed literal ``"1"`` and ``simulation_only`` is
  the fixed literal ``true``: neither is a constructor field, so no caller
  can override them.

**Delivery design (why a direct stdout sink, not the ``logging`` module).**
The production sink writes one complete JSON line with a single
``sys.stdout.write`` plus ``flush``, resolving ``sys.stdout`` at call time.
The module imports no ``logging`` at all: there is no root-logger
reconfiguration, no handler that could be duplicated or silently swapped for
a ``NullHandler``, no propagation path that could double-emit, and no
interaction with uvicorn's own logging configuration.  Correction of the
M3.1 review finding F3: uvicorn's default access-log handler is
``ext://sys.stdout``, so uvicorn's access logs - not only this service's
audit lines - appear on **stdout** as well.  A consumer's stdout therefore
never holds audit records alone: consumers must identify audit records by
their JSON schema (``schema_version: "1"`` plus the closed field set), not
by line order or by assuming anything else about stdout.  The sink and
clock are instance-scoped and injectable, so tests capture or fail
emissions without touching any global state.  This is also the only sink
compatible with the read-only container filesystem: stdout, never a file,
so retention and rotation belong to the runtime.

**Failure boundary (narrow and deliberate).**  ``AuditLogger.emit`` wraps
construction, serialization and the sink call in one ``except Exception``
that returns silently: a logging failure never raises into the caller,
never prints or logs the exception text (the application never logs), and
never changes a job status or an API response.  Audit output is best-effort
forensics - the persisted job record remains the authoritative record.  A
line longer than ``MAX_LINE_BYTES`` is dropped rather than written, keeping
output bounded even if the schema is ever widened carelessly.

**Scope.**  ``create_app`` exposes the service as
``application.state.audit``.  M3.2 wires the job-create route to it:
``job_created`` exactly once after a successful persist (simulated, denied
or failed) and ``job_create_rejected`` on the approved pre-creation and
persistence rejection paths.  M3.3 wires the discovery-refresh route:
``refresh_requested`` once before the service call, ``refresh_rejected``
on the two understood rejections (lock busy, unknown engine) with closed
reason codes, and one ``refresh_completed`` per returned report.  The
read routes (list, detail, export) emit nothing.  Emission is best-effort
forensics: the persisted job record - never this service - is the
authoritative record, and there is no persistent audit-event store.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from app.engines.discovery.capability_map import TARGET_FORMAT_POLICY
from app.engines.discovery.models import KNOWN_ENGINES, CapabilityStatus
from app.engines.execution_policy import (
    REASON_AUTHORIZED,
    REASON_CAPABILITY_NOT_VERIFIED,
    REASON_INVALID_REQUEST,
    REASON_NON_SIMULATION,
    REASON_SELECTION_NOT_SELECTED,
    REASON_UNKNOWN_ENGINE,
    REASON_UNSUPPORTED_MODE,
)
from app.engines.jobs import JobStatus

__all__ = [
    "APPROVED_FIELDS",
    "AUDIT_SCHEMA_VERSION",
    "EVENT_JOB_CREATE_REJECTED",
    "EVENT_JOB_CREATED",
    "EVENT_REFRESH_COMPLETED",
    "EVENT_REFRESH_REJECTED",
    "EVENT_REFRESH_REQUESTED",
    "JOB_REASON_VALUES",
    "MAX_EVENT_TS",
    "MAX_LINE_BYTES",
    "REFRESH_REASON_VALUES",
    "REASON_DISCOVERY_REFRESH_FAILED",
    "REASON_INSTANCE_NOT_FOUND",
    "REASON_INSTANCE_TYPE_MISMATCH",
    "REASON_PERSISTENCE_UNAVAILABLE",
    "REASON_REFRESH_IN_PROGRESS",
    "REASON_UNKNOWN_TARGET",
    "TERMINAL_STATUS_VALUES",
    "AuditEvent",
    "AuditLogger",
]

#: Fixed envelope version - a literal, not a field: callers cannot override it.
AUDIT_SCHEMA_VERSION = "1"

#: Fixed simulation marker - a literal, not a field: every event states it.
SIMULATION_ONLY = True

#: Closed event catalogue (M3.0 review, section 9).
EVENT_JOB_CREATED = "job_created"
EVENT_JOB_CREATE_REJECTED = "job_create_rejected"
EVENT_REFRESH_REQUESTED = "refresh_requested"
EVENT_REFRESH_COMPLETED = "refresh_completed"
EVENT_REFRESH_REJECTED = "refresh_rejected"

EVENT_NAMES: frozenset[str] = frozenset(
    {
        EVENT_JOB_CREATED,
        EVENT_JOB_CREATE_REJECTED,
        EVENT_REFRESH_REQUESTED,
        EVENT_REFRESH_COMPLETED,
        EVENT_REFRESH_REJECTED,
    }
)

#: Exactly the approved output field names - no key outside this set may ever
#: appear in an emitted line.
APPROVED_FIELDS: frozenset[str] = frozenset(
    {
        "schema_version",
        "event",
        "ts",
        "job_id",
        "target",
        "status",
        "reason",
        "engine",
        "discovered",
        "capability_status",
        "truncated",
        "instance_id",
        "simulation_only",
    }
)

#: Payload fields in fixed emission order (the envelope fields are prepended).
_PAYLOAD_FIELDS: tuple[str, ...] = (
    "job_id",
    "target",
    "status",
    "reason",
    "engine",
    "discovered",
    "capability_status",
    "truncated",
    "instance_id",
)

#: Closed status values: the three terminal ``JobStatus`` outcomes.  Audit
#: events only ever describe terminal jobs - ``PENDING`` is not an auditable
#: outcome, and no rejection event carries a status at all.
TERMINAL_STATUS_VALUES: frozenset[str] = frozenset(
    {
        JobStatus.SIMULATED.value,
        JobStatus.DENIED.value,
        JobStatus.FAILED.value,
    }
)

#: Transport reason codes.  These literals are pinned by tests to the
#: authoritative API error constants (routes and store resolution).  They
#: are duplicated here rather than imported because this library module
#: must never depend on the routers: routes import only this module's
#: event-name constants (``EVENT_JOB_*`` and ``EVENT_REFRESH_*``), so the
#: import direction stays routes -> audit -> engines with no cycle.
REASON_UNKNOWN_TARGET = "unknown_target"
REASON_INSTANCE_NOT_FOUND = "target_instance_not_found"
REASON_INSTANCE_TYPE_MISMATCH = "target_instance_type_mismatch"
REASON_PERSISTENCE_UNAVAILABLE = "persistence_unavailable"
REASON_REFRESH_IN_PROGRESS = "refresh_in_progress"
REASON_DISCOVERY_REFRESH_FAILED = "discovery_refresh_failed"

#: Reason codes allowed on job events: the execution-policy reasons, the
#: recorded job failure reasons (``failure_reason_for``), and the job-create
#: transport codes.
JOB_REASON_VALUES: frozenset[str] = frozenset(
    {
        REASON_AUTHORIZED,
        REASON_CAPABILITY_NOT_VERIFIED,
        REASON_INVALID_REQUEST,
        REASON_NON_SIMULATION,
        REASON_SELECTION_NOT_SELECTED,
        REASON_UNKNOWN_ENGINE,
        REASON_UNSUPPORTED_MODE,
        "no_preview",
        "denied",
        "authorized_without_simulation",
        REASON_UNKNOWN_TARGET,
        REASON_INSTANCE_NOT_FOUND,
        REASON_INSTANCE_TYPE_MISMATCH,
        REASON_PERSISTENCE_UNAVAILABLE,
    }
)

#: Reason codes the discovery domain can actually produce - every literal
#: found in ``app/engines/discovery`` (refresh reports, probe outcomes and
#: capability-parser outcomes), asserted by an AST coverage test.
DISCOVERY_REASON_VALUES: frozenset[str] = frozenset(
    {
        "binary_not_found",
        "binary_missing",
        "foreign_output",
        "capability_missing",
        "formats_not_listed",
        "foreign_probe",
        "probe_operation_not_allowlisted",
        "capability_probe_failed",
        "capability_probe_missing",
        "truncated_probe",
        "invalid_output",
        "oversized_output",
        "missing_header",
        "no_formats",
        "no_modes",
        "truncated_output",
        "invalid_tokens",
    }
)

#: Reason codes allowed on refresh events: discovery outcomes plus the
#: refresh transport codes (``unknown_engine`` is shared with the policy set
#: - the 404 code and the policy reason are the same stable string).
REFRESH_REASON_VALUES: frozenset[str] = DISCOVERY_REASON_VALUES | {
    REASON_REFRESH_IN_PROGRESS,
    REASON_DISCOVERY_REFRESH_FAILED,
    REASON_UNKNOWN_ENGINE,
}

#: Per-event payload policy: which fields may appear (``_EVENT_FIELDS``) and
#: which must appear (``_EVENT_REQUIRED``).  Together with ``APPROVED_FIELDS``
#: this makes the schema strict in both directions - nothing missing where it
#: matters, nothing extra anywhere.
_EVENT_FIELDS: dict[str, frozenset[str]] = {
    EVENT_JOB_CREATED: frozenset({"job_id", "target", "status", "reason", "instance_id"}),
    EVENT_JOB_CREATE_REJECTED: frozenset({"reason", "target", "instance_id"}),
    EVENT_REFRESH_REQUESTED: frozenset({"engine"}),
    EVENT_REFRESH_COMPLETED: frozenset(
        {"engine", "discovered", "capability_status", "reason", "truncated"}
    ),
    EVENT_REFRESH_REJECTED: frozenset({"reason", "engine"}),
}

_EVENT_REQUIRED: dict[str, frozenset[str]] = {
    EVENT_JOB_CREATED: frozenset({"job_id", "target", "status"}),
    EVENT_JOB_CREATE_REJECTED: frozenset({"reason"}),
    EVENT_REFRESH_REQUESTED: frozenset(),
    EVENT_REFRESH_COMPLETED: frozenset(),
    EVENT_REFRESH_REJECTED: frozenset({"reason"}),
}

#: Bounded timestamp range: epoch seconds from the Unix epoch up to
#: 2100-01-01T00:00:00Z, so a broken clock can never serialize an unbounded
#: integer.
MAX_EVENT_TS = 4_102_444_800

#: Hard ceiling on one emitted line (envelope + payload), defence in depth
#: behind the closed sets: a line that somehow grew past this is dropped.
MAX_LINE_BYTES = 1024

_TARGET_VALUES: frozenset[str] = frozenset(TARGET_FORMAT_POLICY)
_CAPABILITY_STATUS_VALUES: frozenset[str] = frozenset(
    member.value for member in CapabilityStatus
)

#: Sink contract: receives exactly one JSON line, newline included.
Sink = Callable[[str], None]


def _write_stdout(line: str) -> None:
    """The production sink: one write, one flush, stdout resolved at call time."""

    sys.stdout.write(line)
    sys.stdout.flush()


def _epoch_seconds() -> int:
    """UTC epoch seconds (POSIX time) - the default injectable clock."""

    return int(time.time())


def _closed(name: str, value: object, allowed: frozenset[str]) -> str:
    """Coerce an enum member (or plain string) into ``allowed`` or raise.

    Enum members are normalised to their ``value`` so an emitted payload
    always carries plain strings, and the normalised value must then be an
    **exact** ``str`` (M3.1 review finding F1): a hostile ``str`` subclass
    with an overridden ``__hash__`` / ``__eq__`` can lie about closed-set
    membership (it hashes to an allowed member and claims equality with
    everything), so the exact-type gate runs *before* the membership test
    and rejects it outright.  The offending value is never echoed into the
    message: validation errors are developer-facing and must not grow with
    hostile input.
    """

    normalised = value.value if isinstance(value, Enum) else value
    if type(normalised) is not str or normalised not in allowed:
        raise ValueError(f"{name} is outside the closed set")
    return normalised


def _canonical_uuid(name: str, value: object) -> str:
    """Require a canonical lowercase UUID string (what this app mints).

    Exact ``str`` type only (M3.1 review finding F1): a ``str`` subclass
    never reaches ``uuid.UUID``, whose internal ``list.count(None)``
    comparison an overridden ``__eq__`` can fool into raising ``TypeError``
    instead of the ``ValueError`` this validator promises.
    """

    if type(value) is not str:
        raise ValueError(f"{name} must be a canonical UUID string")
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise ValueError(f"{name} must be a canonical UUID string") from None
    if str(parsed) != value:
        raise ValueError(f"{name} must be a canonical lowercase UUID")
    return value


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One audit event: frozen, validated, bounded, JSON-serialisable.

    ``event`` and ``ts`` are required; every other field is optional and is
    **omitted from the output when absent** (``None``), never filled with a
    placeholder.  ``schema_version`` and ``simulation_only`` are not fields -
    they are fixed constants written by :meth:`to_json`.
    """

    event: str
    ts: int
    job_id: str | None = None
    target: str | None = None
    status: str | None = None
    reason: str | None = None
    engine: str | None = None
    discovered: bool | None = None
    capability_status: str | None = None
    truncated: bool | None = None
    instance_id: str | None = None

    def __post_init__(self) -> None:
        event = _closed("event", self.event, EVENT_NAMES)
        object.__setattr__(self, "event", event)

        # Exact int only: rejects ``bool`` and hostile ``int`` subclasses
        # whose overridden ``__eq__`` could lie about the range comparison.
        if type(self.ts) is not int:
            raise ValueError("ts must be an integer epoch second")
        if not 0 <= self.ts <= MAX_EVENT_TS:
            raise ValueError("ts is outside the bounded epoch range")

        if self.job_id is not None:
            object.__setattr__(self, "job_id", _canonical_uuid("job_id", self.job_id))
        if self.instance_id is not None:
            object.__setattr__(
                self, "instance_id", _canonical_uuid("instance_id", self.instance_id)
            )
        if self.target is not None:
            object.__setattr__(self, "target", _closed("target", self.target, _TARGET_VALUES))
        if self.status is not None:
            object.__setattr__(
                self, "status", _closed("status", self.status, TERMINAL_STATUS_VALUES)
            )
        if self.engine is not None:
            object.__setattr__(self, "engine", _closed("engine", self.engine, KNOWN_ENGINES))
        if self.capability_status is not None:
            object.__setattr__(
                self,
                "capability_status",
                _closed(
                    "capability_status", self.capability_status, _CAPABILITY_STATUS_VALUES
                ),
            )
        # Strict bool only (M3.3.1): exact ``bool`` type - never ``0``/``1``
        # integers and never a hostile object spoofing ``bool`` through a
        # faked ``__class__`` (which ``isinstance`` would believe but
        # ``type(...) is bool`` does not).  Absent stays ``None`` (omitted);
        # an explicit ``False`` is a real value and is serialised.
        if self.discovered is not None and type(self.discovered) is not bool:
            raise ValueError("discovered must be a boolean")
        if self.truncated is not None and not isinstance(self.truncated, bool):
            raise ValueError("truncated must be a boolean")
        if self.reason is not None:
            allowed_reasons = (
                JOB_REASON_VALUES
                if event in (EVENT_JOB_CREATED, EVENT_JOB_CREATE_REJECTED)
                else REFRESH_REASON_VALUES
            )
            object.__setattr__(self, "reason", _closed("reason", self.reason, allowed_reasons))

        present = {name for name in _PAYLOAD_FIELDS if getattr(self, name) is not None}
        allowed = _EVENT_FIELDS[event]
        if not present <= allowed:
            raise ValueError(f"fields not allowed for event {event}")
        if not _EVENT_REQUIRED[event] <= present:
            raise ValueError(f"required fields missing for event {event}")
        # Per-engine evidence (discovery, capability status, truncation)
        # only exists on a per-engine refresh record.
        if (present & {"discovered", "capability_status", "truncated"}) and (
            "engine" not in present
        ):
            raise ValueError("capability fields require an engine")

    def to_json(self) -> str:
        """The single-line JSON object: fixed envelope + present fields only."""

        payload: dict[str, object] = {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "event": self.event,
            "ts": self.ts,
            "simulation_only": SIMULATION_ONLY,
        }
        for name in _PAYLOAD_FIELDS:
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


class AuditLogger:
    """Best-effort structured audit emitter with injectable sink and clock.

    The default sink writes one JSON line to stdout (see module docstring for
    why it bypasses the ``logging`` module); the default clock returns UTC
    epoch seconds.  Tests inject a capturing sink or a failing sink and a
    deterministic clock without changing any global logging state.
    """

    __slots__ = ("_clock", "_sink")

    def __init__(
        self,
        *,
        sink: Sink | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._sink = _write_stdout if sink is None else sink
        self._clock = _epoch_seconds if clock is None else clock

    def emit(
        self,
        *,
        event: str,
        job_id: str | None = None,
        target: str | None = None,
        status: str | None = None,
        reason: str | None = None,
        engine: str | None = None,
        discovered: bool | None = None,
        capability_status: str | None = None,
        truncated: bool | None = None,
        instance_id: str | None = None,
    ) -> None:
        """Validate one event, serialize it, hand exactly one line to the sink.

        Failure boundary: **every** exception - invalid field values,
        clock failure, serialization failure, sink failure or stdout failure -
        is swallowed here.  Nothing is printed or logged in the failure case
        (the application never logs), nothing propagates to the caller, and
        job status and API responses are untouched.  Invalid input fails
        closed: no line rather than a malformed one.  There is no retry.

        M3.1 review finding F2: an empty ``reason`` (the authoritative
        orchestration's value on the successful simulated path, and refresh
        reports' ``_NO_REASON``) means "no reason" and is mapped to
        ``None`` before construction, so the event is emitted with the
        optional field omitted instead of being dropped - a valid non-empty
        closed code passes through unchanged and no code is ever invented.
        """

        try:
            if type(reason) is str and reason == "":
                reason = None
            record = AuditEvent(
                event=event,
                ts=self._clock(),
                job_id=job_id,
                target=target,
                status=status,
                reason=reason,
                engine=engine,
                discovered=discovered,
                capability_status=capability_status,
                truncated=truncated,
                instance_id=instance_id,
            )
            line = record.to_json() + "\n"
            if len(line.encode("utf-8")) > MAX_LINE_BYTES:
                return
            self._sink(line)
        except Exception:
            return

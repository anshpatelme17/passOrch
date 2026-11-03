"""M3.1/M3.2/M3.3 - audit-event core plus job and discovery emission: strict
schema, stdout delivery, failure isolation.

The M3.1 core touches no global logging state and proves the production sink
both locally (captured process stdout) and inside a real uvicorn process (the
load-bearing delivery test).  M3.2 wires the job-create route to the service -
``job_created`` after a successful persist and ``job_create_rejected`` on the
approved rejection paths.  M3.3 adds the discovery-refresh workflow -
``refresh_requested`` before the service call, ``refresh_rejected`` on the
understood rejections (lock busy, unknown engine), ``refresh_completed`` per
returned report.  Everything is captured below through injected sinks; reads,
export and delete stay silent, and a failing audit path never changes an API
response.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import logging
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import audit as audit_module
from app.api.audit import (
    APPROVED_FIELDS,
    AUDIT_SCHEMA_VERSION,
    EVENT_JOB_CREATE_REJECTED,
    EVENT_JOB_CREATED,
    EVENT_REFRESH_COMPLETED,
    EVENT_REFRESH_REJECTED,
    EVENT_REFRESH_REQUESTED,
    AuditEvent,
    AuditLogger,
    JOB_REASON_VALUES,
    MAX_EVENT_TS,
    REFRESH_REASON_VALUES,
    REASON_INSTANCE_NOT_FOUND,
    REASON_INSTANCE_TYPE_MISMATCH,
    REASON_PERSISTENCE_UNAVAILABLE,
    REASON_UNKNOWN_TARGET,
    TERMINAL_STATUS_VALUES,
)
from app.api.app import create_app
from app.api.schemas import (
    EngineRefreshReport,
    RefreshEnginesRequest,
    RefreshEnginesResponse,
)
from app.core.orchestrator.plan import PlanPreview, preview_plan
from app.engines.discovery.capability_map import TARGET_FORMAT_POLICY
from app.engines.discovery.errors import UnknownEngineError
from app.engines.discovery.models import (
    HASHCAT,
    KNOWN_ENGINES,
    CapabilityStatus,
    EngineCapabilities,
    EngineSnapshot,
    JOHN,
)
from app.engines.discovery.registry import DiscoveryRegistry
from app.engines.discovery.service import CapabilityRefreshService, RefreshReport
from app.engines.execution_policy import (
    REASON_AUTHORIZED,
    REASON_CAPABILITY_NOT_VERIFIED,
    REASON_INVALID_REQUEST,
    REASON_NON_SIMULATION,
    REASON_SELECTION_NOT_SELECTED,
    REASON_UNKNOWN_ENGINE,
    REASON_UNSUPPORTED_MODE,
)
from app.engines.job_persistence import JobPersistenceError
from app.engines.jobs import InvalidJobError, JobStatus, JobStore
from app.engines.target_instances import TargetInstanceStore

_ROOT = Path(__file__).resolve().parents[1]
_CLOCK_TS = 1_700_000_000
_UUID = "11111111-2222-4333-8444-a1b2c3d4e5f6"
_INSTANCE_UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

#: Hostile free text that must never be constructible into an event.
_HOSTILE = "pdf-document\r\nSet-Cookie: pwn=1"


def _lines_sink(lines: list[str]) -> Callable[[str], None]:
    def sink(line: str) -> None:
        lines.append(line)

    return sink


def _job_event(**overrides: object) -> AuditEvent:
    fields: dict[str, object] = {
        "event": EVENT_JOB_CREATED,
        "ts": _CLOCK_TS,
        "job_id": _UUID,
        "target": "pdf-document",
        "status": "denied",
        "reason": "capability_not_verified",
    }
    fields.update(overrides)
    return AuditEvent(**fields)


class _RecordingStdout:
    """A stdout stand-in recording every write and flush."""

    def __init__(self) -> None:
        self.writes: list[str] = []
        self.flushes = 0

    def write(self, text: str) -> int:
        self.writes.append(text)
        return len(text)

    def flush(self) -> None:
        self.flushes += 1


# --------------------------------------------------------------------------- #
# 1. frozen event structure
# --------------------------------------------------------------------------- #


def test_event_is_frozen_and_has_no_instance_dict() -> None:
    event = _job_event()
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.ts = 5  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.event = EVENT_REFRESH_REQUESTED  # type: ignore[misc]
    # Unknown names are rejected too: CPython's frozen+slots __setattr__
    # surfaces a TypeError for names outside the slot set (a zero-arg super()
    # artifact of the generated class); the assignment must not succeed under
    # any exception type.
    with pytest.raises((dataclasses.FrozenInstanceError, TypeError, AttributeError)):
        event.reference = "smuggled"  # type: ignore[attr-defined]
    assert not hasattr(event, "__dict__")
    assert isinstance(event, AuditEvent)


# --------------------------------------------------------------------------- #
# 2. exact schema and optional-field omission
# --------------------------------------------------------------------------- #


def test_minimal_event_serialises_only_the_fixed_envelope() -> None:
    payload = json.loads(AuditEvent(event=EVENT_REFRESH_REQUESTED, ts=_CLOCK_TS).to_json())
    assert set(payload) == {"schema_version", "event", "ts", "simulation_only"}
    assert payload["schema_version"] == AUDIT_SCHEMA_VERSION == "1"
    assert payload["simulation_only"] is True
    assert payload["ts"] == _CLOCK_TS


def test_full_event_carries_present_fields_and_omits_absent_ones() -> None:
    payload = json.loads(_job_event(instance_id=_INSTANCE_UUID).to_json())
    assert set(payload) == {
        "schema_version",
        "event",
        "ts",
        "simulation_only",
        "job_id",
        "target",
        "status",
        "reason",
        "instance_id",
    }
    assert set(payload) <= APPROVED_FIELDS
    for absent in ("engine", "capability_status", "truncated"):
        assert absent not in payload
    # absent means absent: no placeholder null ever appears in a line
    assert not any(value is None for value in payload.values())


def test_envelope_fields_are_not_constructor_fields() -> None:
    with pytest.raises(TypeError):
        AuditEvent(event=EVENT_JOB_CREATED, ts=_CLOCK_TS, schema_version="2")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        AuditEvent(event=EVENT_JOB_CREATED, ts=_CLOCK_TS, simulation_only=False)  # type: ignore[call-arg]


def test_emit_takes_no_free_form_message() -> None:
    audit = AuditLogger(sink=lambda line: None, clock=lambda: _CLOCK_TS)
    with pytest.raises(TypeError):
        audit.emit(event=EVENT_JOB_CREATED, message="job created")  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# 3. closed event / status / reason / engine / capability values
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad",
    ["real_audit", "", "JOB_CREATED", "job-created", "job_created "],
)
def test_unknown_event_names_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        AuditEvent(event=bad, ts=_CLOCK_TS)


def test_all_five_catalogue_events_are_accepted() -> None:
    AuditEvent(event=EVENT_JOB_CREATED, ts=_CLOCK_TS, job_id=_UUID, target="pdf-document", status="denied")
    AuditEvent(event=EVENT_JOB_CREATE_REJECTED, ts=_CLOCK_TS, reason="unknown_target")
    AuditEvent(event=EVENT_REFRESH_REQUESTED, ts=_CLOCK_TS)
    AuditEvent(
        event=EVENT_REFRESH_COMPLETED,
        ts=_CLOCK_TS,
        engine="john",
        capability_status="verified",
        truncated=False,
    )
    AuditEvent(event=EVENT_REFRESH_REJECTED, ts=_CLOCK_TS, reason="refresh_in_progress")


@pytest.mark.parametrize(
    "bad",
    ["pending", "PENDING", "simulated ", "running", "", 123],
)
def test_unknown_statuses_are_rejected(bad: object) -> None:
    with pytest.raises(ValueError):
        _job_event(status=bad)


def test_terminal_status_set_is_the_three_terminal_job_states() -> None:
    assert TERMINAL_STATUS_VALUES == {"simulated", "denied", "failed"}
    assert JobStatus.PENDING.value not in TERMINAL_STATUS_VALUES


def test_status_enum_members_normalise_to_plain_strings() -> None:
    payload = json.loads(_job_event(status=JobStatus.DENIED).to_json())
    assert payload["status"] == "denied"
    assert type(payload["status"]) is str


@pytest.mark.parametrize(
    "reason",
    [
        REASON_INVALID_REQUEST,
        REASON_UNSUPPORTED_MODE,
        REASON_NON_SIMULATION,
        REASON_CAPABILITY_NOT_VERIFIED,
        REASON_SELECTION_NOT_SELECTED,
        REASON_UNKNOWN_ENGINE,
        REASON_AUTHORIZED,
        "no_preview",
        "denied",
        "authorized_without_simulation",
        REASON_UNKNOWN_TARGET,
        REASON_INSTANCE_NOT_FOUND,
        REASON_INSTANCE_TYPE_MISMATCH,
        REASON_PERSISTENCE_UNAVAILABLE,
    ],
)
def test_every_job_reason_is_accepted(reason: str) -> None:
    _job_event(reason=reason)


@pytest.mark.parametrize(
    "bad",
    ["", "crack_it", "binary_not_found", "rate_limited", "authorized", "capability_not_verified\n"],
)
def test_reasons_outside_the_job_set_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        _job_event(reason=bad)


def test_policy_reasons_are_the_execution_policy_constants() -> None:
    assert {
        REASON_AUTHORIZED,
        REASON_CAPABILITY_NOT_VERIFIED,
        REASON_INVALID_REQUEST,
        REASON_NON_SIMULATION,
        REASON_SELECTION_NOT_SELECTED,
        REASON_UNKNOWN_ENGINE,
        REASON_UNSUPPORTED_MODE,
    } <= JOB_REASON_VALUES


@pytest.mark.parametrize(
    "bad",
    ["capability_not_verified", "no_preview", "unknown_target", ""],
)
def test_job_reasons_are_rejected_on_refresh_events(bad: str) -> None:
    with pytest.raises(ValueError):
        AuditEvent(event=EVENT_REFRESH_REJECTED, ts=_CLOCK_TS, reason=bad)


@pytest.mark.parametrize(
    "reason",
    [
        "refresh_in_progress",
        "discovery_refresh_failed",
        "unknown_engine",
        "binary_not_found",
        "foreign_output",
        "invalid_tokens",
        "probe_operation_not_allowlisted",
    ],
)
def test_refresh_reasons_are_accepted(reason: str) -> None:
    AuditEvent(event=EVENT_REFRESH_REJECTED, ts=_CLOCK_TS, reason=reason)


@pytest.mark.parametrize(
    "bad",
    ["hashcat2", "john the ripper", "JOHN", "", "john;rm -rf"],
)
def test_unknown_engines_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        AuditEvent(event=EVENT_REFRESH_REQUESTED, ts=_CLOCK_TS, engine=bad)


def test_the_two_known_engines_are_accepted() -> None:
    assert KNOWN_ENGINES == {"john", "hashcat"}
    for engine in sorted(KNOWN_ENGINES):
        AuditEvent(event=EVENT_REFRESH_REQUESTED, ts=_CLOCK_TS, engine=engine)


@pytest.mark.parametrize("bad", ["VERIFIED", "unverified", "", "verified "])
def test_unknown_capability_statuses_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        AuditEvent(
            event=EVENT_REFRESH_COMPLETED,
            ts=_CLOCK_TS,
            engine="john",
            capability_status=bad,
            truncated=False,
        )


def test_capability_status_enum_normalises_to_its_value() -> None:
    payload = json.loads(
        AuditEvent(
            event=EVENT_REFRESH_COMPLETED,
            ts=_CLOCK_TS,
            engine="john",
            capability_status=CapabilityStatus.VERIFIED,
            truncated=True,
        ).to_json()
    )
    assert payload["capability_status"] == "verified"
    assert type(payload["capability_status"]) is str


@pytest.mark.parametrize(
    "bad",
    ["pdf", "PDF-DOCUMENT", "not-a-target", "", "pdf-document\r\nX"],
)
def test_targets_outside_the_policy_map_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        _job_event(target=bad)


def test_every_policy_target_is_accepted() -> None:
    for target in TARGET_FORMAT_POLICY:
        _job_event(target=target)


# --------------------------------------------------------------------------- #
# 4. UUID validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad",
    ["", "not-a-uuid", "Z" * 36, _UUID.upper(), "{" + _UUID + "}", _UUID[:-1], 123],
)
def test_invalid_job_ids_are_rejected(bad: object) -> None:
    with pytest.raises(ValueError):
        _job_event(job_id=bad)


@pytest.mark.parametrize(
    "bad",
    ["", "not-a-uuid", "Z" * 36, _INSTANCE_UUID.upper(), 7],
)
def test_invalid_instance_ids_are_rejected(bad: object) -> None:
    with pytest.raises(ValueError):
        AuditEvent(
            event=EVENT_JOB_CREATE_REJECTED,
            ts=_CLOCK_TS,
            reason="target_instance_not_found",
            instance_id=bad,  # type: ignore[arg-type]
        )


def test_canonical_uuids_are_accepted_verbatim() -> None:
    payload = json.loads(_job_event(instance_id=_INSTANCE_UUID).to_json())
    assert payload["job_id"] == _UUID
    assert payload["instance_id"] == _INSTANCE_UUID


# --------------------------------------------------------------------------- #
# 5. timestamp validation (booleans are not integers here)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad",
    [True, False, 1.5, "1700000000", None, -1, MAX_EVENT_TS + 1, 10**15],
)
def test_invalid_timestamps_are_rejected(bad: object) -> None:
    with pytest.raises(ValueError):
        AuditEvent(event=EVENT_REFRESH_REQUESTED, ts=bad)  # type: ignore[arg-type]


def test_timestamp_boundaries_are_accepted() -> None:
    assert AuditEvent(event=EVENT_REFRESH_REQUESTED, ts=0).ts == 0
    assert AuditEvent(event=EVENT_REFRESH_REQUESTED, ts=MAX_EVENT_TS).ts == MAX_EVENT_TS


# --------------------------------------------------------------------------- #
# 6. strict boolean validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", [1, 0, "true", "false", [], {}])
def test_truncated_requires_a_real_boolean(bad: object) -> None:
    with pytest.raises(ValueError):
        AuditEvent(
            event=EVENT_REFRESH_COMPLETED,
            ts=_CLOCK_TS,
            engine="john",
            capability_status="unknown",
            truncated=bad,  # type: ignore[arg-type]
        )


def test_truncated_accepts_true_false_and_absence() -> None:
    for value in (True, False, None):
        AuditEvent(
            event=EVENT_REFRESH_COMPLETED,
            ts=_CLOCK_TS,
            engine="john",
            capability_status="unknown",
            truncated=value,
        )


def test_false_truncated_is_serialised_not_omitted() -> None:
    payload = json.loads(
        AuditEvent(
            event=EVENT_REFRESH_COMPLETED,
            ts=_CLOCK_TS,
            engine="john",
            capability_status="unknown",
            truncated=False,
        ).to_json()
    )
    assert payload["truncated"] is False


# --------------------------------------------------------------------------- #
# 7. unknown fields and matrix violations are rejected
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "field",
    [
        "reference",
        "mode",
        "detail",
        "problems",
        "argv",
        "command",
        "path",
        "stdout",
        "stderr",
        "ip",
        "client_ip",
        "correlation_id",
        "headers",
        "cookie",
        "body",
        "extra",
        "metadata",
    ],
)
def test_unapproved_fields_cannot_be_constructed(field: str) -> None:
    with pytest.raises(TypeError):
        AuditEvent(event=EVENT_JOB_CREATED, ts=_CLOCK_TS, **{field: "x"})  # type: ignore[call-arg]


def test_fields_outside_the_event_matrix_are_rejected() -> None:
    with pytest.raises(ValueError):
        _job_event(engine="john")
    with pytest.raises(ValueError):
        _job_event(capability_status="verified")
    with pytest.raises(ValueError):
        _job_event(truncated=True)
    with pytest.raises(ValueError):
        AuditEvent(event=EVENT_REFRESH_COMPLETED, ts=_CLOCK_TS, target="pdf-document")
    with pytest.raises(ValueError):
        AuditEvent(event=EVENT_REFRESH_REQUESTED, ts=_CLOCK_TS, reason="refresh_in_progress")


def test_required_fields_are_enforced_per_event() -> None:
    with pytest.raises(ValueError):
        AuditEvent(event=EVENT_JOB_CREATED, ts=_CLOCK_TS, target="pdf-document", status="denied")
    with pytest.raises(ValueError):
        AuditEvent(event=EVENT_JOB_CREATED, ts=_CLOCK_TS, job_id=_UUID, status="denied")
    with pytest.raises(ValueError):
        AuditEvent(event=EVENT_JOB_CREATED, ts=_CLOCK_TS, job_id=_UUID, target="pdf-document")
    with pytest.raises(ValueError):
        AuditEvent(event=EVENT_JOB_CREATE_REJECTED, ts=_CLOCK_TS, target="pdf-document")
    with pytest.raises(ValueError):
        AuditEvent(event=EVENT_REFRESH_REJECTED, ts=_CLOCK_TS)


def test_capability_fields_require_an_engine() -> None:
    with pytest.raises(ValueError):
        AuditEvent(event=EVENT_REFRESH_COMPLETED, ts=_CLOCK_TS, capability_status="unknown")
    with pytest.raises(ValueError):
        AuditEvent(event=EVENT_REFRESH_COMPLETED, ts=_CLOCK_TS, truncated=True)


# --------------------------------------------------------------------------- #
# 8. exactly one valid JSON line per event
# --------------------------------------------------------------------------- #


def test_emit_produces_exactly_one_valid_json_line() -> None:
    lines: list[str] = []
    audit = AuditLogger(sink=_lines_sink(lines), clock=lambda: _CLOCK_TS)
    audit.emit(
        event=EVENT_JOB_CREATED,
        job_id=_UUID,
        target="pdf-document",
        status="denied",
        reason="capability_not_verified",
    )
    assert len(lines) == 1
    line = lines[0]
    assert line.endswith("\n")
    assert line.count("\n") == 1
    assert "\r" not in line
    payload = json.loads(line)
    assert payload["event"] == "job_created"
    assert payload["schema_version"] == "1"
    assert payload["ts"] == _CLOCK_TS
    assert set(payload) <= APPROVED_FIELDS


# --------------------------------------------------------------------------- #
# 9. hostile strings cannot enter emitted events
# --------------------------------------------------------------------------- #


def test_hostile_values_fail_closed_without_reaching_the_sink() -> None:
    lines: list[str] = []
    audit = AuditLogger(sink=_lines_sink(lines), clock=lambda: _CLOCK_TS)
    audit.emit(
        event=EVENT_JOB_CREATED,
        job_id=_UUID,
        target=_HOSTILE,
        status="denied",
        reason="capability_not_verified",
    )
    audit.emit(
        event=EVENT_JOB_CREATED,
        job_id=_UUID,
        target="pdf-document",
        status="denied",
        reason="capability_not_verified\nINJECTED",
    )
    audit.emit(event=EVENT_REFRESH_REQUESTED, engine="john; curl http://evil")
    audit.emit(event="job_created\nfake-envelope")
    audit.emit(event=EVENT_JOB_CREATED, job_id="not-a-uuid", target="pdf-document", status="denied")
    assert lines == []


def test_hostile_values_are_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        _job_event(target=_HOSTILE)
    with pytest.raises(ValueError):
        _job_event(reason="ok\r\nSet-Cookie: x=1")
    with pytest.raises(ValueError):
        AuditEvent(event=EVENT_REFRESH_REQUESTED, ts=_CLOCK_TS, engine="john\nhashcat")


# --------------------------------------------------------------------------- #
# 10. the capturing sink receives the expected structured event
# --------------------------------------------------------------------------- #


def test_capturing_sink_receives_the_exact_structured_event() -> None:
    lines: list[str] = []
    audit = AuditLogger(sink=_lines_sink(lines), clock=lambda: _CLOCK_TS)
    audit.emit(
        event=EVENT_REFRESH_COMPLETED,
        engine="john",
        capability_status=CapabilityStatus.VERIFIED,
        truncated=False,
        reason="binary_not_found",
    )
    assert len(lines) == 1
    assert json.loads(lines[0]) == {
        "schema_version": "1",
        "event": "refresh_completed",
        "ts": _CLOCK_TS,
        "simulation_only": True,
        "engine": "john",
        "capability_status": "verified",
        "truncated": False,
        "reason": "binary_not_found",
    }


# --------------------------------------------------------------------------- #
# 11. a deliberately failing sink never propagates
# --------------------------------------------------------------------------- #


def test_a_failing_sink_never_propagates() -> None:
    def boom(line: str) -> None:
        raise RuntimeError("disk full: SECRET-DETAIL-XYZ")

    audit = AuditLogger(sink=boom, clock=lambda: _CLOCK_TS)
    assert (
        audit.emit(
            event=EVENT_JOB_CREATED,
            job_id=_UUID,
            target="pdf-document",
            status="denied",
            reason="capability_not_verified",
        )
        is None
    )


def test_a_failing_clock_never_propagates() -> None:
    def broken_clock() -> int:
        raise OSError("clock unavailable: SECRET")

    audit = AuditLogger(sink=lambda line: None, clock=broken_clock)
    assert audit.emit(event=EVENT_JOB_CREATED) is None


def test_invalid_fields_fail_closed_without_a_line() -> None:
    lines: list[str] = []
    audit = AuditLogger(sink=_lines_sink(lines), clock=lambda: _CLOCK_TS)
    audit.emit(
        event=EVENT_JOB_CREATED,
        job_id="not-a-uuid",
        target="pdf-document",
        status="denied",
        reason="capability_not_verified",
    )
    audit.emit(event="made_up_event")
    assert lines == []


# --------------------------------------------------------------------------- #
# 12. no exception message or traceback leaks
# --------------------------------------------------------------------------- #


def test_failures_leave_no_output_behind(capsys: pytest.CaptureFixture[str]) -> None:
    def boom(line: str) -> None:
        raise RuntimeError("SECRET-TOKEN-MUST-NOT-LEAK")

    audit = AuditLogger(sink=boom, clock=lambda: _CLOCK_TS)
    audit.emit(
        event=EVENT_JOB_CREATED,
        job_id=_UUID,
        target="pdf-document",
        status="denied",
        reason="capability_not_verified",
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert "SECRET-TOKEN-MUST-NOT-LEAK" not in captured.out + captured.err


# --------------------------------------------------------------------------- #
# 13. no duplicate output
# --------------------------------------------------------------------------- #


def test_repeated_emissions_produce_exactly_one_line_each() -> None:
    lines: list[str] = []
    audit = AuditLogger(sink=_lines_sink(lines), clock=lambda: _CLOCK_TS)
    for _ in range(3):
        audit.emit(
            event=EVENT_JOB_CREATED,
            job_id=_UUID,
            target="pdf-document",
            status="denied",
            reason="capability_not_verified",
        )
    assert len(lines) == 3
    parsed = [json.loads(line) for line in lines]
    assert all(payload["event"] == "job_created" for payload in parsed)
    assert all(payload["ts"] == _CLOCK_TS for payload in parsed)
    # one line per emit call: never doubled, never merged
    assert all(line.count("\n") == 1 for line in lines)


# --------------------------------------------------------------------------- #
# 14. no root logger configuration changes
# --------------------------------------------------------------------------- #


def test_default_production_path_leaves_the_root_logger_untouched(
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = logging.getLogger()
    handlers_before = list(root.handlers)
    level_before = root.level
    loggers_before = set(logging.Logger.manager.loggerDict)

    production = AuditLogger()  # the create_app default: stdout sink, real clock
    production.emit(
        event=EVENT_JOB_CREATED,
        job_id=_UUID,
        target="pdf-document",
        status="denied",
        reason="capability_not_verified",
    )
    application = create_app()
    application.state.audit.emit(event=EVENT_REFRESH_REQUESTED)

    assert list(root.handlers) == handlers_before
    assert root.level == level_before
    assert set(logging.Logger.manager.loggerDict) == loggers_before
    # the service deliberately never registers a logger name at all
    assert "passorch.audit" not in logging.Logger.manager.loggerDict

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 2
    assert all(json.loads(line)["schema_version"] == "1" for line in lines)


# --------------------------------------------------------------------------- #
# 15. audit code has no process-execution imports
# --------------------------------------------------------------------------- #


def test_audit_module_imports_no_process_execution_or_logging() -> None:
    source = (_ROOT / "app" / "api" / "audit.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    banned_exact = {"subprocess", "pty", "multiprocessing", "logging", "os"}
    banned_prefixes = (
        "app.api.routes",
        "app.api.app",
        "app.engines.adapter",
        "app.engines.adapter_registry",
        "app.core.orchestrator",
        "app.engines.discovery.runner",
        "app.engines.discovery.service",
    )
    assert imported.isdisjoint(banned_exact), imported & banned_exact
    for module in sorted(imported):
        for prefix in banned_prefixes:
            assert module != prefix and not module.startswith(prefix + "."), module

    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "print" not in called
    assert called.isdisjoint({"eval", "exec", "compile", "__import__"})
    # no environment variables: keeps the README/.env documentation contract
    assert "PASSORCH_" not in source


# --------------------------------------------------------------------------- #
# 16. the production stdout sink writes and flushes exactly one line
# --------------------------------------------------------------------------- #


def test_production_stdout_sink_writes_one_line_and_flushes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _RecordingStdout()
    monkeypatch.setattr(sys, "stdout", fake)
    production = AuditLogger()  # default sink resolves sys.stdout at call time
    production.emit(
        event=EVENT_JOB_CREATED,
        job_id=_UUID,
        target="pdf-document",
        status="denied",
        reason="capability_not_verified",
    )
    assert len(fake.writes) == 1
    assert fake.writes[0].endswith("\n")
    assert json.loads(fake.writes[0])["event"] == "job_created"
    assert fake.flushes == 1
    production.emit(event=EVENT_REFRESH_REQUESTED)
    # exactly one write + one flush per event - never doubled
    assert len(fake.writes) == 2
    assert fake.flushes == 2


# --------------------------------------------------------------------------- #
# wiring: application.state.audit
# --------------------------------------------------------------------------- #


def test_create_app_exposes_the_default_and_injected_audit_service() -> None:
    default_application = create_app()
    assert isinstance(default_application.state.audit, AuditLogger)

    lines: list[str] = []
    injected = AuditLogger(sink=_lines_sink(lines), clock=lambda: _CLOCK_TS)
    application = create_app(audit=injected)
    assert application.state.audit is injected
    application.state.audit.emit(
        event=EVENT_JOB_CREATED,
        job_id=_UUID,
        target="pdf-document",
        status="denied",
        reason="capability_not_verified",
    )
    assert json.loads(lines[0])["ts"] == _CLOCK_TS


# --------------------------------------------------------------------------- #
# M3.2 - job-route emission fixtures
# --------------------------------------------------------------------------- #


def _john_registry(
    status: CapabilityStatus = CapabilityStatus.VERIFIED,
    fmts: tuple[str, ...] = ("pdf",),
) -> DiscoveryRegistry:
    """One discovered John carrying ``status`` for the given formats."""

    registry = DiscoveryRegistry()
    registry.register(EngineSnapshot(engine=JOHN, discovered=True, binary_found=True))
    registry.set_capabilities(
        JOHN,
        EngineCapabilities(engine=JOHN, capability_status=status, formats=fmts),
    )
    return registry


def _audit_client(
    lines: list[str],
    *,
    registry: DiscoveryRegistry | None = None,
    job_store: JobStore | None = None,
    instance_store: TargetInstanceStore | None = None,
    sink: Callable[[str], None] | None = None,
) -> TestClient:
    """A client whose injected audit service captures into ``lines``.

    ``sink`` overrides the capturing sink (failing-sink tests); every store
    is a fresh isolated instance so nothing touches the process-wide
    defaults.
    """

    return TestClient(
        create_app(
            audit=AuditLogger(
                sink=_lines_sink(lines) if sink is None else sink,
                clock=lambda: _CLOCK_TS,
            ),
            discovery_registry=registry if registry is not None else _john_registry(),
            job_store=job_store if job_store is not None else JobStore(),
            target_instance_store=(
                instance_store
                if instance_store is not None
                else TargetInstanceStore(capacity=10)
            ),
        )
    )


def _create_instance(client: TestClient, target_type: str = "pdf-document") -> str:
    res = client.post(
        "/api/target-instances", json={"name": "inst", "target_type": target_type}
    )
    assert res.status_code == 201
    return res.json()["id"]


def _hostile_str(value: str, collide: str) -> str:
    """The M3.1 F1 bypass shape: str content, lying type.

    The subclass hashes to the allowed member ``collide`` and answers
    ``True`` to every equality - exactly the shape that previously satisfied
    ``isinstance`` + closed-set membership and reached the sink.
    """

    hostile = type(
        "HostileStr",
        (str,),
        {"__hash__": lambda self: hash(collide), "__eq__": lambda self, other: True},
    )
    return hostile(value)


def _failed_preview() -> PlanPreview:
    """An authorised preview without a simulation - the FAILED outcome."""

    base = preview_plan("pdf-document", discovery_registry=_john_registry())
    return PlanPreview(
        target=base.target,
        selection=base.selection,
        request=base.request,
        authorization=base.authorization,
        plan=base.plan,
        simulation=None,
    )


class _FailingPutJobStore(JobStore):
    """A store whose write raises - the Phase 8A persistence-failure path."""

    def put(self, job):
        raise JobPersistenceError("simulated persistence failure")


class _JobErrorPutJobStore(JobStore):
    """A store whose write rejects the record with a ``JobError``.

    Structural / duplicate rejections are *not* ``JobPersistenceError``
    subclasses, so before R5 they escaped ``POST /api/jobs`` as an unhandled
    500: a non-JSON body and no audit event at all.  The message is seeded
    with a marker to prove it never reaches the event or the response.
    """

    def put(self, job):
        raise InvalidJobError("job serialises forbidden fields: SECRET-JOB-ERROR")


# --------------------------------------------------------------------------- #
# M3.2 - exactly one event per job-create outcome (injected capturing sink)
# --------------------------------------------------------------------------- #


def test_simulated_job_emits_exactly_one_job_created() -> None:
    """Requirements 1 + 4: one event after persist; empty reason omitted."""

    lines: list[str] = []
    store = JobStore()
    client = _audit_client(lines, job_store=store)

    res = client.post(
        "/api/jobs",
        json={"target": "pdf-document", "reference": "operator-secret-ref"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "simulated"
    assert body["failure_reason"] == ""  # the API contract is unchanged (F2)

    assert len(lines) == 1  # exactly one, never zero, never two
    payload = json.loads(lines[0])
    assert payload["event"] == "job_created"
    assert payload["schema_version"] == "1"
    assert payload["simulation_only"] is True
    assert payload["job_id"] == body["job_id"]
    assert payload["target"] == "pdf-document"
    assert payload["status"] == "simulated"
    # F2: the simulated path's empty failure_reason is omitted - the event
    # is emitted with no placeholder, and no code is invented
    assert "reason" not in payload
    assert set(payload) <= APPROVED_FIELDS
    # security: the operator reference never reaches audit output
    assert "operator-secret-ref" not in lines[0]
    # emission never disturbs the persisted record
    assert store.get(body["job_id"]) is not None


def test_denied_job_emits_exactly_one_job_created_with_a_closed_reason() -> None:
    lines: list[str] = []
    client = _audit_client(lines, registry=_john_registry(CapabilityStatus.UNKNOWN))

    res = client.post("/api/jobs", json={"target": "pdf-document"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "denied"

    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == "job_created"
    assert payload["status"] == "denied"
    assert payload["reason"] == "capability_not_verified"
    assert payload["reason"] in JOB_REASON_VALUES
    assert payload["job_id"] == body["job_id"]
    assert payload["target"] == "pdf-document"


def test_failed_terminal_job_emits_exactly_one_job_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirement 3: a FAILED job whose persistence succeeded still emits."""

    lines: list[str] = []
    store = JobStore()
    client = _audit_client(lines, job_store=store)
    monkeypatch.setattr(
        "app.core.orchestrator.jobs.preview_plan",
        lambda *_args, **_kwargs: _failed_preview(),
    )

    res = client.post("/api/jobs", json={"target": "pdf-document"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "failed"
    assert body["failure_reason"] == "authorized_without_simulation"
    assert store.get(body["job_id"]) is not None  # persistence succeeded

    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == "job_created"
    assert payload["status"] == "failed"
    assert payload["reason"] == "authorized_without_simulation"
    assert payload["reason"] in JOB_REASON_VALUES
    assert payload["job_id"] == body["job_id"]


def test_unknown_target_emits_one_rejection_without_a_job_id() -> None:
    lines: list[str] = []
    store = JobStore()
    client = _audit_client(lines, job_store=store)

    res = client.post("/api/jobs", json={"target": "not-a-mapped-target"})
    assert res.status_code == 422
    assert res.json()["detail"]["error"] == "unknown_target"
    assert len(store) == 0

    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == "job_create_rejected"
    assert payload["reason"] == "unknown_target"
    assert payload["reason"] in JOB_REASON_VALUES
    # no job was minted; the unmapped target is outside the closed set, so
    # the user-controlled value itself never appears
    assert "job_id" not in payload
    assert "target" not in payload
    assert "status" not in payload
    assert set(payload) <= APPROVED_FIELDS


def test_missing_instance_emits_target_instance_not_found() -> None:
    lines: list[str] = []
    client = _audit_client(lines)

    # malformed identifier: never echoed as instance_id
    malformed = client.post(
        "/api/jobs", json={"target": "pdf-document", "instance_id": "not-a-uuid"}
    )
    assert malformed.status_code == 422
    assert malformed.json()["detail"]["error"] == "target_instance_not_found"
    assert len(lines) == 1
    first = json.loads(lines[0])
    assert first["event"] == "job_create_rejected"
    assert first["reason"] == "target_instance_not_found"
    assert first["reason"] in JOB_REASON_VALUES
    assert first["target"] == "pdf-document"
    assert "instance_id" not in first
    assert "job_id" not in first

    # canonical but unknown identifier: carried verbatim
    unknown = "00000000-0000-4000-8000-000000000000"
    res = client.post(
        "/api/jobs", json={"target": "pdf-document", "instance_id": unknown}
    )
    assert res.status_code == 422
    assert len(lines) == 2
    second = json.loads(lines[1])
    assert second["event"] == "job_create_rejected"
    assert second["reason"] == "target_instance_not_found"
    assert second["instance_id"] == unknown
    assert "job_id" not in second


def test_instance_type_mismatch_emits_its_closed_reason() -> None:
    lines: list[str] = []
    client = _audit_client(lines)
    zip_id = _create_instance(client, "zip-archive")
    # the instance-creation route emits nothing (emission is jobs-only)
    assert lines == []

    res = client.post(
        "/api/jobs", json={"target": "pdf-document", "instance_id": zip_id}
    )
    assert res.status_code == 422
    assert res.json()["detail"]["error"] == "target_instance_type_mismatch"

    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == "job_create_rejected"
    assert payload["reason"] == "target_instance_type_mismatch"
    assert payload["reason"] in JOB_REASON_VALUES
    assert payload["target"] == "pdf-document"
    assert payload["instance_id"] == zip_id
    assert "job_id" not in payload


def test_persistence_failure_emits_one_rejection_and_no_job_created() -> None:
    """Requirements 8 + 11: fixed reason, and never a job_created."""

    lines: list[str] = []
    store = _FailingPutJobStore()
    client = _audit_client(lines, job_store=store)

    res = client.post("/api/jobs", json={"target": "pdf-document"})
    assert res.status_code == 503
    assert res.json() == {
        "detail": {
            "error": "persistence_unavailable",
            "detail": "job storage is temporarily unavailable",
        }
    }
    assert len(store) == 0  # nothing was persisted

    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == "job_create_rejected"
    assert payload["reason"] == "persistence_unavailable"
    assert payload["reason"] in JOB_REASON_VALUES
    assert "job_id" not in payload
    # the store's exception text never reaches the event
    assert "simulated persistence failure" not in lines[0]
    assert not any(
        json.loads(line)["event"] == "job_created" for line in lines
    ), "no job_created may be emitted for a record that does not exist"


def test_job_error_rejection_uses_the_same_envelope_and_reason() -> None:
    """A structural ``JobError`` from the store: 503 + one rejection event.

    ``InvalidJobError`` / ``DuplicateJobError`` are not ``JobPersistenceError``
    subclasses.  They must answer with the identical closed envelope and the
    identical approved reason code, emit exactly one ``job_create_rejected``
    (never a ``job_created``), and never surface the exception text.
    """

    lines: list[str] = []
    store = _JobErrorPutJobStore()
    client = _audit_client(lines, job_store=store)

    res = client.post("/api/jobs", json={"target": "pdf-document"})
    assert res.status_code == 503
    assert res.json() == {
        "detail": {
            "error": "persistence_unavailable",
            "detail": "job storage is temporarily unavailable",
        }
    }
    assert len(store) == 0  # nothing was persisted

    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == "job_create_rejected"
    assert payload["reason"] == "persistence_unavailable"
    assert payload["reason"] in JOB_REASON_VALUES
    assert "job_id" not in payload
    # the store's exception text never reaches the event or the body
    assert "SECRET-JOB-ERROR" not in lines[0]
    assert "SECRET-JOB-ERROR" not in res.text
    assert "Traceback" not in res.text
    assert not any(
        json.loads(line)["event"] == "job_created" for line in lines
    ), "no job_created may be emitted for a record that does not exist"


def test_concurrent_http_job_creates_stay_whole_and_each_emit_one_event() -> None:
    """20 parallel ``POST /api/jobs``: whole records, one event each, bounded.

    The registry is built in-memory from synthetic snapshots - no binary is
    located and no probe is run - so the job path never touches a subprocess,
    and the autouse no-subprocess guard would fail this test if it did.
    """

    lines: list[str] = []
    lock = threading.Lock()

    def sink(line: str) -> None:
        with lock:
            lines.append(line)

    # Bounded store: 20 creates land newest-first, only the newest 7 survive.
    store = JobStore(capacity=7)
    app = create_app(
        audit=AuditLogger(sink=sink, clock=lambda: _CLOCK_TS),
        discovery_registry=_john_registry(),
        job_store=store,
        target_instance_store=TargetInstanceStore(capacity=10),
    )

    def create(index: int):
        client = TestClient(app)  # one client per thread
        return client.post(
            "/api/jobs",
            json={"target": "pdf-document", "reference": f"conc-{index:02d}"},
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(create, range(20)))

    # Every request passes the simulation-only policy and succeeds.
    assert [res.status_code for res in responses] == [200] * 20

    bodies = [res.json() for res in responses]
    ids = [body["job_id"] for body in bodies]
    assert len(set(ids)) == 20
    assert all(body["status"] == "simulated" for body in bodies)
    by_id = {body["job_id"]: body for body in bodies}

    # Bounded store: exactly capacity records are retained, the rest evicted.
    assert len(store) == 7
    surviving = store.list()
    assert len(surviving) == 7
    surviving_ids = {job.job_id for job in surviving}
    assert surviving_ids <= set(ids)
    assert len(surviving_ids) == 7  # FIFO eviction left no duplicates

    # Each surviving record agrees with what the API returned for that job.
    for job in surviving:
        body = by_id[job.job_id]
        assert job.status is JobStatus.SIMULATED
        assert job.target == "pdf-document" == body["target"]
        assert isinstance(job.created_at, int)
        # No instance association: both are the empty-string snapshot.
        assert job.instance_id == ""
        assert job.instance_target_type == ""
        assert job.preview is not None
        preview = body["preview"]
        authorized = preview["authorization"]["authorized"]
        assert authorized is True
        assert job.preview.target == preview["target"] == "pdf-document"
        assert job.preview.request.reference == preview["request"]["reference"]
        assert job.preview.request.engine == preview["request"]["engine"]
        assert job.preview.authorization.authorized is authorized

    # Audit: exactly one job_created per persist, nothing rejected, and the
    # emitted ids are exactly the response ids (order may vary, so compare
    # sorted).
    payloads = [json.loads(line) for line in lines]
    created = [p for p in payloads if p["event"] == EVENT_JOB_CREATED]
    rejected = [p for p in payloads if p["event"] == EVENT_JOB_CREATE_REJECTED]
    assert rejected == []
    assert len(created) == 20
    assert sorted(p["job_id"] for p in created) == sorted(ids)
    assert all(p["status"] == "simulated" for p in created)
    assert all(p["target"] == "pdf-document" for p in created)
    assert all(p["job_id"] in by_id for p in created)


def test_miswired_stores_emit_the_persistence_unavailable_rejection() -> None:
    """Both store-resolution 503 paths carry the same approved rejection."""

    # wrong-typed job store
    lines: list[str] = []
    client = _audit_client(lines)
    client.app.state.job_store = object()
    res = client.post("/api/jobs", json={"target": "pdf-document"})
    assert res.status_code == 503
    assert res.json()["detail"]["error"] == "persistence_unavailable"
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == "job_create_rejected"
    assert payload["reason"] == "persistence_unavailable"
    assert "job_id" not in payload

    # wrong-typed target-instance store (with an instance_id supplied)
    other: list[str] = []
    second_client = _audit_client(other)
    second_client.app.state.target_instance_store = object()
    second = second_client.post(
        "/api/jobs",
        json={
            "target": "pdf-document",
            "instance_id": "00000000-0000-4000-8000-000000000000",
        },
    )
    assert second.status_code == 503
    assert second.json()["detail"]["error"] == "persistence_unavailable"
    assert len(other) == 1
    assert json.loads(other[0])["event"] == "job_create_rejected"
    assert json.loads(other[0])["reason"] == "persistence_unavailable"


def test_a_failing_audit_sink_never_changes_the_api_response() -> None:
    """Requirement 9: sink failure leaves status, body and detail intact."""

    def boom(line: str) -> None:
        raise RuntimeError("audit sink exploded: SECRET-DETAIL")

    client = _audit_client([], sink=boom)
    res = client.post("/api/jobs", json={"target": "pdf-document"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "simulated"
    # the exact JobInfo contract of a healthy run - no field added, none lost
    assert set(body) == {
        "job_id",
        "status",
        "created_at",
        "target",
        "failure_reason",
        "preview",
        "instance_id",
        "instance_target_type",
    }
    assert body["job_id"]
    assert "SECRET-DETAIL" not in res.text

    # the rejection path is equally unaffected
    rejected = client.post("/api/jobs", json={"target": "not-mapped"})
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["error"] == "unknown_target"
    assert "SECRET" not in rejected.text


def test_a_broken_audit_service_never_changes_the_api_response() -> None:
    """Requirement 3's route boundary: a missing/unusable service is silent."""

    lines: list[str] = []
    client = _audit_client(lines)
    client.app.state.audit = None  # attribute present but unusable

    res = client.post("/api/jobs", json={"target": "pdf-document"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "simulated"
    assert body["job_id"]
    assert lines == []  # nothing emitted, nothing raised, nothing leaked

    client.app.state.audit = object()  # present, wrong shape, no emit()
    second = client.post("/api/jobs", json={"target": "pdf-document"})
    assert second.status_code == 200
    assert second.json()["status"] == "simulated"


def test_no_duplicate_job_event_across_reads_export_and_delete() -> None:
    """Requirements 10 + 12: one creation event, zero events everywhere else."""

    lines: list[str] = []
    client = _audit_client(lines)

    created = client.post("/api/jobs", json={"target": "pdf-document"})
    assert created.status_code == 200
    job_id = created.json()["job_id"]
    assert len(lines) == 1

    assert client.get("/api/jobs").status_code == 200
    assert (
        client.get("/api/jobs", params={"status": "simulated"}).status_code == 200
    )
    assert client.get(f"/api/jobs/{job_id}").status_code == 200
    exported = client.get(f"/api/jobs/{job_id}/export")
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("application/json")
    assert client.get("/api/jobs/stats").status_code == 200
    assert client.delete(f"/api/jobs/{job_id}").status_code == 204

    # still exactly the single creation event - reads never emit, never
    # duplicate, and the M2 export handler stays audit-free
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "job_created"


def test_reference_and_hostile_mode_never_reach_audit_output() -> None:
    """Requirement 13: free-text reference and mode strings stay out."""

    lines: list[str] = []
    client = _audit_client(lines)

    # a denial produced by a hostile mode, carrying an operator reference
    res = client.post(
        "/api/jobs",
        json={
            "target": "pdf-document",
            "reference": "SECRET-REFERENCE-XYZ",
            "mode": "hostile-mode<script>",
        },
    )
    assert res.status_code == 200
    assert res.json()["status"] == "denied"
    assert res.json()["failure_reason"] == "unsupported_execution_mode"

    # and a rejection carrying the same reference
    rejected = client.post(
        "/api/jobs",
        json={"target": "not-mapped", "reference": "SECRET-REFERENCE-XYZ"},
    )
    assert rejected.status_code == 422

    assert len(lines) == 2
    blob = "".join(lines)
    assert "SECRET-REFERENCE-XYZ" not in blob
    assert "hostile-mode" not in blob
    payloads = [json.loads(line) for line in lines]
    assert payloads[0]["event"] == "job_created"
    assert payloads[0]["reason"] == "unsupported_execution_mode"
    assert payloads[1]["event"] == "job_create_rejected"
    assert payloads[1]["reason"] == "unknown_target"
    for payload in payloads:
        assert set(payload) <= APPROVED_FIELDS


def test_concurrent_emissions_keep_one_intact_line_per_event() -> None:
    """Requirement 15: 8 threads x 60 emissions -> 480 intact JSON lines."""

    lines: list[str] = []
    lock = threading.Lock()

    def sink(line: str) -> None:
        with lock:
            lines.append(line)

    audit = AuditLogger(sink=sink, clock=lambda: _CLOCK_TS)

    def worker() -> None:
        for _ in range(60):
            audit.emit(
                event=EVENT_JOB_CREATED,
                job_id=_UUID,
                target="pdf-document",
                status="simulated",
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(worker) for _ in range(8)]
        for future in futures:
            future.result()

    assert len(lines) == 480
    assert all(line.count("\n") == 1 for line in lines)
    payloads = [json.loads(line) for line in lines]
    assert all(payload["event"] == "job_created" for payload in payloads)
    assert all(payload["ts"] == _CLOCK_TS for payload in payloads)
    assert all(payload["status"] == "simulated" for payload in payloads)


# --------------------------------------------------------------------------- #
# M3.1 F1 regression: hostile str subclasses can never satisfy a closed set
# --------------------------------------------------------------------------- #


def test_the_demonstrated_bypass_shape_still_fools_membership_but_not_audit() -> None:
    """Reproduce the M3.1 review's demonstrated bypass (target field).

    The subclass hashes to an allowed member and claims equality with
    everything, so plain ``in`` still reports True - before the exact-``str``
    gate this object was constructed into an event and ``emit`` wrote
    ``"target":"EVIL-VALUE"`` to the sink.  Both halves of the fix are
    pinned here: membership still lies, the validator no longer trusts it.
    """

    hostile = _hostile_str("EVIL-VALUE", "pdf-document")
    assert type(hostile) is not str
    assert hostile in frozenset({"pdf-document"})  # the attack shape itself

    with pytest.raises(ValueError):
        _job_event(target=hostile)

    lines: list[str] = []
    audit = AuditLogger(sink=_lines_sink(lines), clock=lambda: _CLOCK_TS)
    audit.emit(
        event=EVENT_JOB_CREATED,
        job_id=_UUID,
        target=hostile,
        status="denied",
        reason="capability_not_verified",
    )
    assert lines == []  # fail closed: no line, and no EVIL-VALUE anywhere


@pytest.mark.parametrize(
    ("field", "allowed_member"),
    [
        ("event", EVENT_JOB_CREATED),
        ("target", "pdf-document"),
        ("status", "denied"),
        ("reason", "capability_not_verified"),
        ("engine", "john"),
        ("capability_status", "verified"),
    ],
)
def test_hostile_str_subclass_is_rejected_for_every_closed_string_field(
    field: str, allowed_member: str
) -> None:
    """Requirement 14: the F1 bypass regression for every closed string.

    Each object hashes to a member of *that field's* closed set and lies
    about equality - the shape that previously passed
    ``isinstance(value, str) + in`` and reached the sink.  For ``event`` and
    ``target`` the required fields are supplied so that, before the fix,
    construction (and emission) would have succeeded.
    """

    hostile = _hostile_str("EVIL-VALUE", allowed_member)
    assert hash(hostile) == hash(allowed_member)

    if field == "event":
        kwargs: dict[str, object] = {
            "event": hostile,
            "job_id": _UUID,
            "target": "pdf-document",
            "status": "denied",
        }
    elif field in ("engine", "capability_status"):
        kwargs = {
            "event": EVENT_REFRESH_COMPLETED,
            "engine": hostile if field == "engine" else "john",
            "capability_status": (
                hostile if field == "capability_status" else "verified"
            ),
            "truncated": False,
        }
    else:
        kwargs = {
            "event": EVENT_JOB_CREATED,
            "job_id": _UUID,
            "target": hostile if field == "target" else "pdf-document",
            "status": hostile if field == "status" else "denied",
            "reason": hostile if field == "reason" else "capability_not_verified",
        }

    with pytest.raises(ValueError):
        AuditEvent(ts=_CLOCK_TS, **kwargs)

    # and never through the logger either: the emit path fails closed too
    lines: list[str] = []
    AuditLogger(sink=_lines_sink(lines), clock=lambda: _CLOCK_TS).emit(**kwargs)
    assert lines == []


def test_hostile_str_subclass_is_rejected_as_a_uuid_field() -> None:
    """The job_id/instance_id gate also removes the pre-fix TypeError leak."""

    hostile = _hostile_str(_UUID, _UUID)
    with pytest.raises(ValueError):
        _job_event(job_id=hostile)
    with pytest.raises(ValueError):
        AuditEvent(
            event=EVENT_JOB_CREATE_REJECTED,
            ts=_CLOCK_TS,
            reason="target_instance_not_found",
            instance_id=hostile,
        )

    lines: list[str] = []
    AuditLogger(sink=_lines_sink(lines), clock=lambda: _CLOCK_TS).emit(
        event=EVENT_JOB_CREATED,
        job_id=hostile,
        target="pdf-document",
        status="denied",
    )
    assert lines == []


def test_hostile_int_subclass_timestamp_is_rejected() -> None:
    hostile_int = type(
        "HostileInt", (int,), {"__eq__": lambda self, other: True}
    )(_CLOCK_TS)
    with pytest.raises(ValueError):
        AuditEvent(event=EVENT_REFRESH_REQUESTED, ts=hostile_int)


def test_project_enum_values_still_normalise_after_the_exact_str_gate() -> None:
    """Normal project Enum values keep working through the exact-type gate."""

    payload = json.loads(_job_event(status=JobStatus.DENIED).to_json())
    assert payload["status"] == "denied"
    assert type(payload["status"]) is str

    refresh = json.loads(
        AuditEvent(
            event=EVENT_REFRESH_COMPLETED,
            ts=_CLOCK_TS,
            engine="john",
            capability_status=CapabilityStatus.VERIFIED,
            truncated=False,
        ).to_json()
    )
    assert refresh["capability_status"] == "verified"
    assert type(refresh["capability_status"]) is str
    assert refresh["engine"] == "john"
    assert type(refresh["engine"]) is str


# --------------------------------------------------------------------------- #
# M3.1 F2 regression: empty reason -> omitted field, event still emitted
# --------------------------------------------------------------------------- #


def test_emit_maps_an_empty_reason_to_an_omitted_field() -> None:
    """F2: the simulated path's ``reason=""`` becomes omission, not a drop."""

    lines: list[str] = []
    audit = AuditLogger(sink=_lines_sink(lines), clock=lambda: _CLOCK_TS)

    # job_created with the authoritative empty reason: emitted, no reason key
    audit.emit(
        event=EVENT_JOB_CREATED,
        job_id=_UUID,
        target="pdf-document",
        status="simulated",
        reason="",
    )
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["status"] == "simulated"
    assert "reason" not in payload

    # a valid non-empty closed code passes through unchanged
    audit.emit(
        event=EVENT_JOB_CREATED,
        job_id=_UUID,
        target="pdf-document",
        status="denied",
        reason="capability_not_verified",
    )
    assert json.loads(lines[1])["reason"] == "capability_not_verified"

    # refresh reports' empty ``_NO_REASON`` maps the same way
    audit.emit(
        event=EVENT_REFRESH_COMPLETED,
        engine="john",
        capability_status="verified",
        truncated=False,
        reason="",
    )
    completed = json.loads(lines[2])
    assert completed["engine"] == "john"
    assert "reason" not in completed

    # construction-level "" is still rejected: the closed set stays intact
    with pytest.raises(ValueError):
        AuditEvent(
            event=EVENT_JOB_CREATED,
            ts=_CLOCK_TS,
            job_id=_UUID,
            target="pdf-document",
            status="simulated",
            reason="",
        )


# --------------------------------------------------------------------------- #
# scope isolation: reads, export and stats emit nothing (M3.3 discovery
# emission is covered by its own section below)
# --------------------------------------------------------------------------- #

_VERIFIED_JOHN = RefreshReport(
    engine=JOHN,
    discovered=True,
    capability_status=CapabilityStatus.VERIFIED,
    reason="",
    truncated=False,
)

#: Hashcat's canonical report: never discovered here, never verified.
_UNKNOWN_HASHCAT = RefreshReport(
    engine=HASHCAT,
    discovered=False,
    capability_status=CapabilityStatus.UNKNOWN,
    reason="binary_not_found",
    truncated=False,
)

#: Raw-output marker a misbehaving report must never smuggle into audit.
RAW_MARKER = "RAW_OUTPUT_MARKER_4418"

#: Keys that must never appear in any refresh audit payload: the closed
#: schema already forbids them, asserted explicitly as a security net.
#: (``discovered`` is deliberately NOT in this list: M3.3.1 approved it as
#: a strict boolean field of ``refresh_completed`` only.)
_NEVER_IN_AUDIT = (
    "argv",
    "binary",
    "cmd",
    "command",
    "executable",
    "output",
    "path",
    "probe_output",
    "raw_output",
    "reference",
    "shell",
    "stderr",
    "stdout",
)

_REPORT_KEYS = {"engine", "discovered", "capability_status", "reason", "truncated"}

_COMPLETE_BASE = {"schema_version", "event", "ts", "simulation_only"}


class _RefreshStub:
    """Injected refresh double (the route only requires a callable refresh).

    Canned reports or a canned exception; optionally snapshots the audit
    lines visible while it runs - the ordering proof for
    ``refresh_requested``.  No discovery, no subprocess, no registry
    mutation.
    """

    def __init__(
        self,
        reports: tuple[RefreshReport, ...] = (_VERIFIED_JOHN,),
        *,
        error: Exception | None = None,
        seen: list[str] | None = None,
    ) -> None:
        self.calls: list[object] = []
        self.during_call: list[str] = []
        self._reports = reports
        self._error = error
        self._seen = seen

    def refresh(self, engines: object = None) -> tuple[RefreshReport, ...]:
        self.calls.append(engines)
        if self._seen is not None:
            self.during_call = list(self._seen)
        if self._error is not None:
            raise self._error
        return self._reports


def _refresh_client(
    lines: list[str],
    service: object | None = None,
) -> tuple[TestClient, object]:
    """A client whose injected audit service captures refresh events."""

    stub = service if service is not None else _RefreshStub()
    application = create_app(
        audit=AuditLogger(sink=_lines_sink(lines), clock=lambda: _CLOCK_TS),
        discovery_refresh_service=stub,
        job_store=JobStore(),
    )
    return TestClient(application), stub


def _events(lines: list[str]) -> list[dict[str, object]]:
    return [json.loads(line) for line in lines]


def _names(lines: list[str]) -> list[str]:
    return [str(payload["event"]) for payload in _events(lines)]


def test_read_and_export_routes_emit_no_audit_events() -> None:
    lines: list[str] = []
    application = create_app(
        audit=AuditLogger(sink=_lines_sink(lines), clock=lambda: _CLOCK_TS),
        discovery_refresh_service=_RefreshStub(),
        job_store=JobStore(),
    )
    client = TestClient(application)

    assert client.get("/health").status_code == 200
    assert client.get("/api/jobs").status_code == 200

    created = client.post("/api/jobs", json={"target": "pdf-document"})
    assert created.status_code in (200, 201)
    job_id = created.json()["job_id"]
    # M3.2: creation is the one emitting path - exactly one event
    assert len(lines) == 1

    assert client.get(f"/api/jobs/{job_id}").status_code == 200
    exported = client.get(f"/api/jobs/{job_id}/export")
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("application/json")
    assert client.get("/api/jobs/stats").status_code == 200
    assert client.get("/api/engines").status_code == 200

    # M3.3: reads, the M2 export download, stats and the engine list still
    # add nothing - still exactly the single creation event
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "job_created"


# --------------------------------------------------------------------------- #
# M3.3 - requirement 1: exactly one refresh_requested per valid attempt
# --------------------------------------------------------------------------- #


def test_valid_refresh_emits_exactly_one_refresh_requested() -> None:
    lines: list[str] = []
    client, stub = _refresh_client(
        lines, _RefreshStub(reports=(_VERIFIED_JOHN, _UNKNOWN_HASHCAT))
    )

    response = client.post("/api/engines/refresh", json={"engines": [JOHN]})
    assert response.status_code == 200
    assert stub.calls == [[JOHN]]

    requested = [p for p in _events(lines) if p["event"] == EVENT_REFRESH_REQUESTED]
    assert len(requested) == 1  # exactly one, never zero, never two
    payload = requested[0]
    # only the schema-permitted optional field, and no outcome claim
    assert set(payload) == _COMPLETE_BASE | {"engine"}
    assert payload["engine"] == JOHN
    assert payload["schema_version"] == AUDIT_SCHEMA_VERSION
    assert payload["simulation_only"] is True
    for absent in ("reason", "capability_status", "truncated", "status", "job_id", "target"):
        assert absent not in payload

    # a default (all-engines) request also emits exactly one - as a bare
    # envelope, because no single engine is named
    all_lines: list[str] = []
    all_client, _ = _refresh_client(all_lines)
    assert all_client.post("/api/engines/refresh").status_code == 200
    all_requested = [
        p for p in _events(all_lines) if p["event"] == EVENT_REFRESH_REQUESTED
    ]
    assert len(all_requested) == 1
    assert set(all_requested[0]) == _COMPLETE_BASE

    # an empty subset is valid: the operation runs, one requested, zero
    # completed (there is no report to summarise)
    empty_lines: list[str] = []
    empty_client, empty_stub = _refresh_client(empty_lines, _RefreshStub(reports=()))
    assert empty_client.post("/api/engines/refresh", json={"engines": []}).status_code == 200
    assert empty_stub.calls == [[]]
    assert _names(empty_lines) == [EVENT_REFRESH_REQUESTED]


def test_requested_engine_is_only_a_single_known_name() -> None:
    """The route-level gate deciding when the optional engine field appears."""

    from app.api.routes.engine_refresh import _requested_engine

    assert _requested_engine(None) is None
    assert _requested_engine(RefreshEnginesRequest()) is None
    assert _requested_engine(RefreshEnginesRequest(engines=[])) is None
    assert _requested_engine(RefreshEnginesRequest(engines=[JOHN, HASHCAT])) is None
    assert _requested_engine(RefreshEnginesRequest(engines=["not-an-engine"])) is None
    assert _requested_engine(RefreshEnginesRequest(engines=[JOHN])) == JOHN

    # a hostile str subclass (M3.1 F1 shape) never counts as a known name
    hostile_body = type("Body", (), {"engines": [_hostile_str("john", "john")]})()
    assert _requested_engine(hostile_body) is None  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# M3.3 - requirement 2: refresh_completed only after the service returns
# --------------------------------------------------------------------------- #


def test_successful_refresh_emits_completed_after_the_service_returns() -> None:
    lines: list[str] = []
    stub = _RefreshStub(reports=(_VERIFIED_JOHN,), seen=lines)
    client, _ = _refresh_client(lines, stub)

    response = client.post("/api/engines/refresh", json={"engines": [JOHN]})
    assert response.status_code == 200

    # ordering proof: while the service was running only the requested
    # event existed - completion can never precede the returned report
    assert stub.during_call == [lines[0]]
    assert _names(lines) == [EVENT_REFRESH_REQUESTED, EVENT_REFRESH_COMPLETED]

    completed = _events(lines)[1]
    assert completed["engine"] == JOHN
    assert completed["discovered"] is True  # forwarded from the report (M3.3.1)
    assert completed["capability_status"] == "verified"
    assert completed["truncated"] is False
    assert completed["ts"] == _CLOCK_TS  # deterministic injected clock
    assert "reason" not in completed  # empty report reason omitted (F2)
    # the response body still carries the report - unchanged contract
    assert response.json()["engines"][0]["engine"] == JOHN


# --------------------------------------------------------------------------- #
# M3.3 - requirements 3 + 4: safe bounded fields, empty reasons omitted
# --------------------------------------------------------------------------- #


def test_completed_summaries_contain_only_safe_bounded_fields() -> None:
    lines: list[str] = []
    client, _ = _refresh_client(
        lines, _RefreshStub(reports=(_VERIFIED_JOHN, _UNKNOWN_HASHCAT))
    )
    assert client.post("/api/engines/refresh").status_code == 200

    completed = [p for p in _events(lines) if p["event"] == EVENT_REFRESH_COMPLETED]
    assert len(completed) == 2  # one bounded per-engine summary per report
    for payload in completed:
        allowed = _COMPLETE_BASE | {
            "engine",
            "discovered",
            "capability_status",
            "truncated",
        }
        assert set(payload) in (allowed, allowed | {"reason"})
        assert set(payload) <= APPROVED_FIELDS
        assert payload["engine"] in KNOWN_ENGINES
        assert type(payload["engine"]) is str
        assert type(payload["discovered"]) is bool
        assert type(payload["truncated"]) is bool
        assert len(json.dumps(payload).encode("utf-8")) <= audit_module.MAX_LINE_BYTES
        for never in _NEVER_IN_AUDIT:
            assert never not in payload

    by_engine = {str(p["engine"]): p for p in completed}
    # evidence is copied verbatim: verified stays verified, and hashcat -
    # which has no capability operation - stays UNKNOWN, never upgraded
    assert by_engine[JOHN]["capability_status"] == "verified"
    assert by_engine[HASHCAT]["capability_status"] == "unknown"
    # M3.3.1: `discovered` is present and equals the actual report value -
    # forwarded verbatim from RefreshReport, never inferred from status,
    # reason or truncation, and never upgraded to "verified"
    assert by_engine[JOHN]["discovered"] is True
    assert by_engine[HASHCAT]["discovered"] is False


def test_empty_report_reasons_are_omitted_from_completed_events() -> None:
    lines: list[str] = []
    client, _ = _refresh_client(
        lines, _RefreshStub(reports=(_VERIFIED_JOHN, _UNKNOWN_HASHCAT))
    )
    assert client.post("/api/engines/refresh").status_code == 200

    by_engine = {
        str(p["engine"]): p
        for p in _events(lines)
        if p["event"] == EVENT_REFRESH_COMPLETED
    }
    # F2 at route level: the empty reason becomes omission, not a drop -
    # the event is still emitted with the other fields intact
    assert "reason" not in by_engine[JOHN]
    assert set(by_engine[JOHN]) == _COMPLETE_BASE | {
        "engine",
        "discovered",
        "capability_status",
        "truncated",
    }
    # a valid non-empty closed code passes through unchanged
    assert by_engine[HASHCAT]["reason"] == "binary_not_found"
    assert by_engine[HASHCAT]["reason"] in REFRESH_REASON_VALUES


# --------------------------------------------------------------------------- #
# M3.3.1 - `discovered`: strict boolean on refresh_completed only
# --------------------------------------------------------------------------- #


def _completed_event(**overrides: object) -> AuditEvent:
    """A minimal valid ``refresh_completed`` event; override to probe a field."""

    fields: dict[str, object] = {
        "event": EVENT_REFRESH_COMPLETED,
        "ts": _CLOCK_TS,
        "engine": JOHN,
        "discovered": True,
        "capability_status": "verified",
        "truncated": False,
    }
    fields.update(overrides)
    return AuditEvent(**fields)


@pytest.mark.parametrize("value", [True, False])
def test_completed_accepts_a_strict_discovered_boolean(value: bool) -> None:
    event = _completed_event(discovered=value)
    assert event.discovered is value

    line = event.to_json()
    # a real JSON boolean literal, never 0/1 and never a quoted string
    assert f'"discovered":{str(value).lower()}' in line
    payload = json.loads(line)
    assert type(payload["discovered"]) is bool
    assert payload["discovered"] is value
    assert set(payload) <= APPROVED_FIELDS


def test_absent_discovered_is_omitted_but_explicit_false_is_serialised() -> None:
    # only ``None`` is omitted: an explicit False is a real value and
    # must round-trip as `false`, never be dropped as "absent"
    assert "discovered" not in json.loads(_completed_event(discovered=None).to_json())
    assert json.loads(_completed_event(discovered=False).to_json())["discovered"] is False


@pytest.mark.parametrize("bad", [0, 1, -1, 2])
def test_discovered_integers_are_rejected(bad: int) -> None:
    with pytest.raises(ValueError, match="discovered must be a boolean"):
        _completed_event(discovered=bad)

    # the emit path fails closed too: not one line leaves the sink
    lines: list[str] = []
    AuditLogger(sink=_lines_sink(lines), clock=lambda: _CLOCK_TS).emit(
        event=EVENT_REFRESH_COMPLETED,
        engine=JOHN,
        discovered=bad,  # type: ignore[arg-type]  # deliberately not a bool
        capability_status="verified",
        truncated=False,
    )
    assert lines == []


def test_hostile_boolean_like_values_are_rejected() -> None:
    class _ClassSpoof:
        """Fakes ``__class__`` to make ``isinstance(x, bool)`` return True."""

        @property
        def __class__(self) -> type[bool]:
            return bool

    spoof = _ClassSpoof()
    # the bypass shape itself: isinstance is fooled, the exact-type gate
    # (``type(...) is bool``) must not be
    assert isinstance(spoof, bool)
    assert type(spoof) is not bool

    hostile_int = type("HostileInt", (int,), {"__eq__": lambda self, other: True})(1)
    for bad in (spoof, hostile_int, object(), "true", 1.0):
        with pytest.raises(ValueError, match="discovered must be a boolean"):
            _completed_event(discovered=bad)

    # and the sink stays empty for the spoof too
    lines: list[str] = []
    AuditLogger(sink=_lines_sink(lines), clock=lambda: _CLOCK_TS).emit(
        event=EVENT_REFRESH_COMPLETED,
        engine=JOHN,
        discovered=spoof,  # type: ignore[arg-type]  # deliberately hostile
        capability_status="verified",
        truncated=False,
    )
    assert lines == []


@pytest.mark.parametrize(
    ("event", "required"),
    [
        (EVENT_REFRESH_REQUESTED, {"engine": JOHN}),
        (EVENT_REFRESH_REJECTED, {"reason": "unknown_engine", "engine": JOHN}),
        (
            EVENT_JOB_CREATED,
            {"job_id": _UUID, "target": "pdf-document", "status": "denied"},
        ),
        (EVENT_JOB_CREATE_REJECTED, {"reason": "unknown_target", "target": "pdf-document"}),
    ],
)
def test_discovered_is_forbidden_on_every_other_event(
    event: str, required: dict[str, object]
) -> None:
    with pytest.raises(ValueError, match="fields not allowed"):
        AuditEvent(ts=_CLOCK_TS, event=event, discovered=True, **required)

    # ...and the emit path drops such a line without a trace
    lines: list[str] = []
    AuditLogger(sink=_lines_sink(lines), clock=lambda: _CLOCK_TS).emit(
        event=event, discovered=True, **required  # type: ignore[arg-type]
    )
    assert lines == []


def test_route_forwards_report_discovered_verbatim() -> None:
    """Both booleans reach the event unchanged, never inferred from status.

    The combinations are deliberately "wrong-looking" (verified but not
    discovered; unknown but discovered): inferring the value from
    ``capability_status`` - or the status from ``discovered`` - would flip
    them.
    """

    flipped_john = RefreshReport(
        engine=JOHN,
        discovered=False,
        capability_status=CapabilityStatus.VERIFIED,
        reason="",
        truncated=False,
    )
    found_hashcat = RefreshReport(
        engine=HASHCAT,
        discovered=True,
        capability_status=CapabilityStatus.UNKNOWN,
        reason="binary_missing",
        truncated=False,
    )
    lines: list[str] = []
    client, _ = _refresh_client(
        lines, _RefreshStub(reports=(flipped_john, found_hashcat))
    )
    assert client.post("/api/engines/refresh").status_code == 200

    by_engine = {
        str(p["engine"]): p
        for p in _events(lines)
        if p["event"] == EVENT_REFRESH_COMPLETED
    }
    assert by_engine[JOHN]["discovered"] is False
    assert by_engine[HASHCAT]["discovered"] is True
    # capability_status is a separate field and stays untouched by forwarding
    assert by_engine[JOHN]["capability_status"] == "verified"
    assert by_engine[HASHCAT]["capability_status"] == "unknown"
    assert by_engine[HASHCAT]["reason"] == "binary_missing"


def test_discovered_preserves_f2_omission_and_the_closed_sets() -> None:
    # F2 at the emit path: an empty reason becomes omission, with
    # ``discovered`` present (construction-level "" stays rejected below)
    lines: list[str] = []
    AuditLogger(sink=_lines_sink(lines), clock=lambda: _CLOCK_TS).emit(
        event=EVENT_REFRESH_COMPLETED,
        engine=JOHN,
        discovered=True,
        capability_status="verified",
        truncated=False,
        reason="",
    )
    payload = json.loads(lines[0])
    assert payload["discovered"] is True
    assert "reason" not in payload

    # a valid non-empty closed reason passes unchanged alongside it
    closed = json.loads(
        _completed_event(discovered=False, reason="binary_not_found").to_json()
    )
    assert closed["reason"] == "binary_not_found"
    assert closed["discovered"] is False

    # invalid reasons and capability statuses still fail closed
    with pytest.raises(ValueError):
        _completed_event(reason="made_up_code")
    with pytest.raises(ValueError):
        _completed_event(capability_status="maybe")

    # ``discovered`` without an engine is refused like the other evidence
    with pytest.raises(ValueError, match="capability fields require an engine"):
        AuditEvent(
            event=EVENT_REFRESH_COMPLETED,
            ts=_CLOCK_TS,
            discovered=True,
        )


# --------------------------------------------------------------------------- #
# M3.3 - requirements 5 + 7: failures never produce completion/misleading
# --------------------------------------------------------------------------- #


def test_failed_refresh_does_not_emit_refresh_completed() -> None:
    lines: list[str] = []
    client, _ = _refresh_client(lines, _RefreshStub(error=RuntimeError("refresh exploded")))

    response = client.post("/api/engines/refresh")
    assert response.status_code == 503
    assert "refresh_completed" not in _names(lines)
    # the attempt did reach the operation, so the request event stands -
    # but no report came back, so nothing completed
    assert _names(lines) == [EVENT_REFRESH_REQUESTED]

    # the lock was released despite the failure: the same app accepts
    # another refresh (with the injected double, no real engine)
    client.app.state.discovery_refresh_service = _RefreshStub()
    assert client.post("/api/engines/refresh").status_code == 200


def test_unexpected_exceptions_produce_no_misleading_events() -> None:
    secret = "INTERNAL_SECRET_7731"
    lines: list[str] = []
    client, _ = _refresh_client(lines, _RefreshStub(error=RuntimeError(secret)))

    response = client.post("/api/engines/refresh")
    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "error": "discovery_refresh_failed",
            "detail": "engine discovery refresh could not be completed",
        }
    }
    assert secret not in response.text

    # exactly the preceding request event: an unexpected internal error is
    # not an understood rejection, so no refresh_rejected - and without a
    # report there is no refresh_completed either
    assert _names(lines) == [EVENT_REFRESH_REQUESTED]
    assert secret not in "".join(lines)

    # a mis-wired app (no refresh service) is a configuration error too:
    # generic 503, zero events
    broken_lines: list[str] = []
    broken, _ = _refresh_client(broken_lines)
    broken.app.state.discovery_refresh_service = None
    miswired = broken.post("/api/engines/refresh")
    assert miswired.status_code == 503
    assert miswired.json()["detail"]["error"] == "discovery_refresh_failed"
    assert broken_lines == []


# --------------------------------------------------------------------------- #
# M3.3 - requirement 6: the lock-busy rejection, closed reason, exactly one
# --------------------------------------------------------------------------- #


def test_lock_busy_emits_exactly_one_refresh_rejected() -> None:
    lines: list[str] = []
    client, _ = _refresh_client(lines)
    lock = client.app.state.discovery_refresh_lock

    assert lock.acquire(blocking=False)
    try:
        response = client.post("/api/engines/refresh")
    finally:
        lock.release()

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "refresh_in_progress"
    # exactly one event: an actual refused attempt, not a request that
    # never reached the operation
    assert _names(lines) == [EVENT_REFRESH_REJECTED]
    payload = _events(lines)[0]
    assert payload["reason"] == "refresh_in_progress"
    assert payload["reason"] in REFRESH_REASON_VALUES  # closed code, not invented
    assert set(payload) <= APPROVED_FIELDS
    assert "engine" not in payload  # default request names no engine
    assert "capability_status" not in payload

    # an explicit single-engine attempt carries the closed engine field
    assert lock.acquire(blocking=False)
    try:
        named = client.post("/api/engines/refresh", json={"engines": [JOHN]})
    finally:
        lock.release()
    assert named.status_code == 409
    assert _names(lines) == [EVENT_REFRESH_REJECTED, EVENT_REFRESH_REJECTED]
    second = _events(lines)[1]
    assert second["reason"] == "refresh_in_progress"
    assert second["engine"] == JOHN


def test_unknown_engine_rejection_is_requested_then_rejected() -> None:
    """The service's fail-closed validation: an understood rejection whose
    reason matches the 404 envelope, and whose user-supplied name is never
    echoed into the event."""

    hostile_name = "evil-engine<script>alert(1)</script>"
    lines: list[str] = []
    client, _ = _refresh_client(
        lines, _RefreshStub(error=UnknownEngineError(hostile_name))
    )

    response = client.post("/api/engines/refresh", json={"engines": [hostile_name]})
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "unknown_engine"
    assert hostile_name not in response.text

    # the attempt reached the operation (requested) and was refused there
    # (rejected) - one of each, no completion
    assert _names(lines) == [EVENT_REFRESH_REQUESTED, EVENT_REFRESH_REJECTED]
    rejection = _events(lines)[1]
    assert rejection["reason"] == "unknown_engine"
    assert rejection["reason"] in REFRESH_REASON_VALUES
    # the name is user input outside the closed engine set: omitted
    assert "engine" not in rejection
    assert hostile_name not in "".join(lines)


# --------------------------------------------------------------------------- #
# M3.3 - requirement 8: a failing sink changes nothing
# --------------------------------------------------------------------------- #


def test_failing_audit_sink_never_changes_the_refresh_response_or_registry() -> None:
    from tests.test_discovery_service import RecordingExecutor, RecordingWhich

    from app.engines.discovery import john as john_module

    good_output = (
        "Supported hash algorithms:\n"
        "  md5crypt, sha256crypt, sha512crypt, bcrypt,\n"
        "  ntlm, raw-md5, raw-sha1, raw-sha256,\n"
        "  zip, pkzip, pdf, office2007, office2010, office2013\n"
    )
    attempted: list[str] = []

    def boom(line: str) -> None:
        attempted.append(line)
        raise RuntimeError("audit sink exploded: SECRET-SINK")

    which = RecordingWhich()
    executor = RecordingExecutor(
        {john_module.JOHN_FORMAT_OPERATION: (0, good_output, "", False)}
    )
    registry = DiscoveryRegistry()
    service = CapabilityRefreshService(registry, which=which, executor=executor)
    application = create_app(
        audit=AuditLogger(sink=boom, clock=lambda: _CLOCK_TS),
        discovery_registry=registry,
        discovery_refresh_service=service,
        job_store=JobStore(),
    )
    client = TestClient(application)

    response = client.post("/api/engines/refresh", json={"engines": [JOHN]})
    assert response.status_code == 200
    entry = response.json()["engines"][0]
    assert set(entry) == _REPORT_KEYS  # the exact M1.1 contract
    assert entry["engine"] == JOHN
    assert entry["capability_status"] == "verified"
    assert entry["reason"] == ""

    # both emissions were attempted and both failed inside the sink
    assert len(attempted) == 2
    assert "SECRET-SINK" not in "".join(attempted)

    # discovery result unchanged: the registry still shows the evidence
    listed = client.get("/api/engines").json()["engines"]
    john_entry = next(item for item in listed if item["engine"] == JOHN)
    assert john_entry["capability_status"] == "verified"

    # lock behaviour unchanged: the same app accepts another refresh
    second = client.post("/api/engines/refresh", json={"engines": [JOHN]})
    assert second.status_code == 200


# --------------------------------------------------------------------------- #
# M3.3 - requirement 9: missing/misconfigured audit fails silently
# --------------------------------------------------------------------------- #


def test_missing_or_misconfigured_audit_fails_silently() -> None:
    # control: a healthy client emits requested + completed
    control: list[str] = []
    healthy, _ = _refresh_client(control)
    assert healthy.post("/api/engines/refresh").status_code == 200
    assert len(control) == 2

    # state.audit present but None: silent, response untouched
    lines: list[str] = []
    client, _ = _refresh_client(lines)
    client.app.state.audit = None
    response = client.post("/api/engines/refresh")
    assert response.status_code == 200
    assert response.json()["engines"][0]["engine"] == JOHN
    assert lines == []

    # present but wrong-shaped (no emit): silent too, no leaked exception
    client.app.state.audit = object()
    second = client.post("/api/engines/refresh")
    assert second.status_code == 200
    assert second.json()["engines"][0]["engine"] == JOHN
    assert lines == []


# --------------------------------------------------------------------------- #
# M3.3 - requirement 10: no duplicate events
# --------------------------------------------------------------------------- #


def test_no_duplicate_events_across_repeated_refreshes_and_jobs() -> None:
    lines: list[str] = []
    client, _ = _refresh_client(
        lines, _RefreshStub(reports=(_VERIFIED_JOHN, _UNKNOWN_HASHCAT))
    )

    first = client.post("/api/engines/refresh")
    assert first.status_code == 200
    assert _names(lines) == [
        EVENT_REFRESH_REQUESTED,
        EVENT_REFRESH_COMPLETED,
        EVENT_REFRESH_COMPLETED,
    ]

    second = client.post("/api/engines/refresh")
    assert second.status_code == 200
    # per call: one requested, one completed per report - never doubled
    assert _names(lines) == [
        EVENT_REFRESH_REQUESTED,
        EVENT_REFRESH_COMPLETED,
        EVENT_REFRESH_COMPLETED,
        EVENT_REFRESH_REQUESTED,
        EVENT_REFRESH_COMPLETED,
        EVENT_REFRESH_COMPLETED,
    ]

    # reads add nothing
    assert client.get("/api/engines").status_code == 200
    assert len(lines) == 6

    # coexists with the M3.2 job event without duplication
    created = client.post("/api/jobs", json={"target": "pdf-document"})
    assert created.status_code == 200
    assert len(lines) == 7
    assert _names(lines)[-1] == EVENT_JOB_CREATED


# --------------------------------------------------------------------------- #
# M3.3 - requirement 11: raw output, paths, argv and exception text stay out
# --------------------------------------------------------------------------- #


def test_sensitive_discovery_data_never_reaches_audit_output() -> None:
    # (a) an exception's text never reaches a line
    secret = "INTERNAL_SECRET_7731"
    lines: list[str] = []
    client, _ = _refresh_client(lines, _RefreshStub(error=RuntimeError(secret)))
    assert client.post("/api/engines/refresh").status_code == 503
    assert secret not in "".join(lines)

    # (b) an unknown engine name (user input outside the closed set) is
    # never echoed by the rejection event
    unknown_lines: list[str] = []
    hostile_name = "evil-engine<path>/usr/local/bin/john</path>"
    unknown_client, _ = _refresh_client(
        unknown_lines, _RefreshStub(error=UnknownEngineError(hostile_name))
    )
    rejected = unknown_client.post(
        "/api/engines/refresh", json={"engines": [hostile_name]}
    )
    assert rejected.status_code == 404
    assert hostile_name not in "".join(unknown_lines)
    assert all(
        hostile_name not in json.dumps(p) for p in _events(unknown_lines)
    )

    # (c) a misbehaving report's extra attributes (raw output, command,
    # path) are never copied into the event - only the five report fields
    class LeakyReport:
        engine = JOHN
        discovered = True
        capability_status = CapabilityStatus.VERIFIED
        reason = ""
        truncated = False
        stdout = RAW_MARKER
        command = RAW_MARKER
        path = RAW_MARKER

    class LeakyService:
        def refresh(self, engines: object = None) -> tuple[object, ...]:
            return (LeakyReport(),)

    leaky_lines: list[str] = []
    leaky, _ = _refresh_client(leaky_lines, LeakyService())
    assert leaky.post("/api/engines/refresh").status_code == 200
    assert RAW_MARKER not in "".join(leaky_lines)
    leaky_completed = [
        p for p in _events(leaky_lines) if p["event"] == EVENT_REFRESH_COMPLETED
    ]
    assert len(leaky_completed) == 1
    assert set(leaky_completed[0]) <= APPROVED_FIELDS


# --------------------------------------------------------------------------- #
# M3.3 - requirement 12: discovery API response contracts unchanged
# --------------------------------------------------------------------------- #


def test_discovery_api_response_contract_is_unchanged() -> None:
    lines: list[str] = []
    client, _ = _refresh_client(
        lines, _RefreshStub(reports=(_VERIFIED_JOHN, _UNKNOWN_HASHCAT))
    )
    response = client.post("/api/engines/refresh")
    assert response.status_code == 200
    # byte-for-byte the M1.1 response, emission notwithstanding
    assert response.json() == {
        "engines": [
            {
                "engine": JOHN,
                "discovered": True,
                "capability_status": "verified",
                "reason": "",
                "truncated": False,
            },
            {
                "engine": HASHCAT,
                "discovered": False,
                "capability_status": "unknown",
                "reason": "binary_not_found",
                "truncated": False,
            },
        ]
    }
    # the request/response schemas are the M1.1 field sets, untouched
    assert set(RefreshEnginesRequest.model_fields) == {"engines"}
    assert set(RefreshEnginesResponse.model_fields) == {"engines"}
    assert set(EngineRefreshReport.model_fields) == _REPORT_KEYS
    # exactly one requested + one completed per report, nothing more
    assert len(lines) == 3


# --------------------------------------------------------------------------- #
# M3.3 - requirement 14: auth, validation and rate-limit rejections emit
# nothing (they never reach the refresh operation)
# --------------------------------------------------------------------------- #


def test_auth_validation_and_rate_limit_rejections_emit_no_events() -> None:
    # 401 - bearer auth runs in middleware, before the handler
    auth_lines: list[str] = []
    auth_app = create_app(
        audit=AuditLogger(sink=_lines_sink(auth_lines), clock=lambda: _CLOCK_TS),
        discovery_refresh_service=_RefreshStub(),
        api_token="s3cret-token",
        job_store=JobStore(),
    )
    auth_client = TestClient(auth_app)
    assert auth_client.post("/api/engines/refresh").status_code == 401
    wrong = auth_client.post(
        "/api/engines/refresh", headers={"Authorization": "Bearer wrong-token"}
    )
    assert wrong.status_code == 401
    assert auth_lines == []

    # 422 - transport validation rejects before the handler runs
    invalid_lines: list[str] = []
    invalid_client, _ = _refresh_client(invalid_lines)
    invalid = invalid_client.post(
        "/api/engines/refresh", json={"engines": [JOHN], "timeout": 5}
    )
    assert invalid.status_code == 422
    assert invalid_lines == []

    # 429 - the refused attempt emits nothing; the accepted one emitted
    # exactly its requested + completed pair
    limited_lines: list[str] = []
    limited_app = create_app(
        audit=AuditLogger(sink=_lines_sink(limited_lines), clock=lambda: _CLOCK_TS),
        discovery_refresh_service=_RefreshStub(),
        rate_limit=1,
        job_store=JobStore(),
    )
    limited_client = TestClient(limited_app)
    assert limited_client.post("/api/engines/refresh").status_code == 200
    accepted = len(limited_lines)
    assert accepted == 2
    refused = limited_client.post("/api/engines/refresh")
    assert refused.status_code == 429
    assert len(limited_lines) == accepted  # zero new events


# --------------------------------------------------------------------------- #
# M3.3 - requirement 15: no real engine execution or subprocess added
# --------------------------------------------------------------------------- #


def test_refresh_emission_adds_no_process_execution_surface() -> None:
    """M3.3 edited the refresh route: the AST boundary still holds, and the
    injected double - never a real engine service - handled the call."""

    source = (_ROOT / "app" / "api" / "routes" / "engine_refresh.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
    # no process-execution library, and discovery internals stay behind the
    # single service surface (the route needs KNOWN_ENGINES for the audit
    # engine field, re-exported by the service module)
    assert modules.isdisjoint({"subprocess", "os", "shlex", "pty", "shutil"})
    assert {m for m in modules if m.startswith("app.engines.discovery")} == {
        "app.engines.discovery.errors",
        "app.engines.discovery.service",
    }
    referenced = {
        node.id if isinstance(node, ast.Name) else node.attr
        for node in ast.walk(tree)
        if isinstance(node, (ast.Name, ast.Attribute))
    }
    assert referenced.isdisjoint(
        {"run_safe_command", "locate_engine_binary", "DISCOVERY_ALLOWLIST"}
    )

    # behavioural: only the injected stub ran - no real service, no locate,
    # no probe (the suite's subprocess kill-switch backs this up)
    lines: list[str] = []
    client, stub = _refresh_client(lines)
    assert client.post("/api/engines/refresh", json={"engines": [JOHN]}).status_code == 200
    assert stub.calls == [[JOHN]]
    assert isinstance(client.app.state.discovery_refresh_service, _RefreshStub)


# --------------------------------------------------------------------------- #
# M3.3 - optional concurrency coverage: deterministic with the blocking double
# --------------------------------------------------------------------------- #


def test_concurrent_refresh_rejection_emits_one_event_in_order() -> None:
    lines: list[str] = []
    entered = threading.Event()
    release = threading.Event()

    class _BlockingRefresh:
        def refresh(self, engines: object = None) -> tuple[RefreshReport, ...]:
            entered.set()
            if not release.wait(timeout=30):  # safety net, never the synchroniser
                raise AssertionError("blocking refresh double was never released")
            return (_VERIFIED_JOHN,)

    client, _ = _refresh_client(lines, _BlockingRefresh())
    second_client = TestClient(client.app)

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.post, "/api/engines/refresh")
        # by the time the service runs, its requested event is already out
        assert entered.wait(timeout=10)
        rejected = second_client.post("/api/engines/refresh")
        assert rejected.status_code == 409
        release.set()
        assert first.result(timeout=10).status_code == 200

    # deterministic order: requested (first attempt), rejected (refused
    # attempt), completed (first attempt's returned report) - one each
    assert _names(lines) == [
        EVENT_REFRESH_REQUESTED,
        EVENT_REFRESH_REJECTED,
        EVENT_REFRESH_COMPLETED,
    ]
    rejection = _events(lines)[1]
    assert rejection["reason"] == "refresh_in_progress"
    assert rejection["reason"] in REFRESH_REASON_VALUES
    completed = _events(lines)[2]
    assert completed["engine"] == JOHN
    assert completed["capability_status"] == "verified"


# --------------------------------------------------------------------------- #
# authoritative pinning: transport codes and discovery reason coverage
# --------------------------------------------------------------------------- #


def test_transport_reason_codes_match_the_authoritative_api_error_codes() -> None:
    from app.api import store_resolution
    from app.api.routes import engine_refresh, jobs as jobs_routes

    assert audit_module.REASON_UNKNOWN_TARGET == jobs_routes.UNKNOWN_TARGET_ERROR
    assert audit_module.REASON_INSTANCE_NOT_FOUND == jobs_routes.INSTANCE_NOT_FOUND_ERROR
    assert audit_module.REASON_INSTANCE_TYPE_MISMATCH == jobs_routes.INSTANCE_TYPE_MISMATCH_ERROR
    assert audit_module.REASON_PERSISTENCE_UNAVAILABLE == store_resolution.PERSISTENCE_ERROR
    assert audit_module.REASON_REFRESH_IN_PROGRESS == engine_refresh.REFRESH_IN_PROGRESS_ERROR
    assert (
        audit_module.REASON_DISCOVERY_REFRESH_FAILED
        == engine_refresh.DISCOVERY_REFRESH_FAILED_ERROR
    )


def test_every_discovery_reason_literal_is_in_the_refresh_reason_set() -> None:
    found: set[str] = set()
    for path in sorted((_ROOT / "app" / "engines" / "discovery").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = {
            node.targets[0].id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "reason":
                    continue
                value = keyword.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    found.add(value.value)
                elif isinstance(value, ast.Name) and value.id in constants:
                    found.add(constants[value.id])
            if isinstance(node.func, ast.Name) and node.func.id == "_unknown" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    found.add(first.value)
                elif isinstance(first, ast.Name) and first.id in constants:
                    found.add(constants[first.id])

    assert found, "the scan must find the discovery reason literals"
    assert found <= REFRESH_REASON_VALUES, found - REFRESH_REASON_VALUES


# --------------------------------------------------------------------------- #
# load-bearing delivery proof: a real uvicorn process, real stdout
# --------------------------------------------------------------------------- #

_UVICORN_SCRIPT = """
import logging
import sys
import threading
import time

import uvicorn

from app.api.app import create_app

application = create_app()
server = uvicorn.Server(
    uvicorn.Config(application, host="127.0.0.1", port=0, log_level="info")
)


def drive() -> None:
    deadline = time.time() + 30
    while not server.started and not server.should_exit and time.time() < deadline:
        time.sleep(0.02)
    if not server.started:
        print("SERVER_NOT_STARTED", file=sys.stderr)
        server.should_exit = True
        return
    root = logging.getLogger()
    before = (root.level, list(root.handlers))
    application.state.audit.emit(
        event="job_created",
        job_id="11111111-2222-4333-8444-555555555555",
        target="pdf-document",
        status="denied",
        reason="capability_not_verified",
    )
    after = (root.level, list(root.handlers))
    print("ROOT_UNCHANGED=" + str(before == after), file=sys.stderr)
    server.should_exit = True


threading.Thread(target=drive, daemon=True).start()
server.run()
print("SERVER_STOPPED", file=sys.stderr)
"""


@pytest.mark.allow_subprocess
def test_real_uvicorn_delivers_exactly_one_audit_line_to_process_stdout() -> None:
    """The load-bearing acceptance test: production sink, real process stdout."""

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", _UVICORN_SCRIPT],
        cwd=str(_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        shell=False,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Application startup complete" in proc.stderr, proc.stderr
    assert "SERVER_STOPPED" in proc.stderr, proc.stderr
    # audit emission inside the running server left the root logger alone
    assert "ROOT_UNCHANGED=True" in proc.stderr, proc.stderr

    # stdout carries exactly one line in total: the audit event, no duplicates
    stdout_lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(stdout_lines) == 1, f"stdout was: {proc.stdout!r}"
    payload = json.loads(stdout_lines[0])
    assert set(payload) == {
        "schema_version",
        "event",
        "ts",
        "simulation_only",
        "job_id",
        "target",
        "status",
        "reason",
    }
    assert payload["schema_version"] == "1"
    assert payload["event"] == "job_created"
    assert payload["simulation_only"] is True
    assert isinstance(payload["ts"], int) and not isinstance(payload["ts"], bool)
    assert payload["job_id"] == "11111111-2222-4333-8444-555555555555"
    assert payload["target"] == "pdf-document"
    assert payload["status"] == "denied"
    assert payload["reason"] == "capability_not_verified"
    assert proc.stdout.count('"event":"job_created"') == 1

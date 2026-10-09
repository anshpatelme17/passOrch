"""Phase 3B.2 - job orchestration tests.

Coverage for ``app/core/orchestrator/jobs.py`` (the compose layer between the
unchanged ``preview_plan`` pipeline and the Phase 3B.1 data layer) plus the
3B.2 schemas (``JobSummary`` / ``JobInfo`` / ``JobListResponse``):

* ``job_status_for`` - explicit terminal mapping (never ``PENDING``);
* ``failure_reason_for`` - policy denial reason, internal failure, or ``""``;
* ``create_job`` - UUID/clock/store injection, terminal records stored,
  denial/failure persistence, duplicate and malformed rejection;
* schemas - fail-closed (``extra="forbid"``), ``JobInfo`` reuse of
  :class:`~app.api.schemas.PlanPreviewResponse` for the preview;
* static security boundary for the new orchestrator module.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.api.schemas import JobInfo, JobListResponse, JobSummary, PlanPreviewResponse
from app.core.orchestrator.jobs import create_job, failure_reason_for, job_status_for
from app.core.orchestrator.plan import PlanPreview, preview_plan
from app.engines.adapter import FORBIDDEN_REQUEST_FIELDS, ExecutionStatus
from app.engines.discovery.models import (
    CapabilityStatus,
    EngineCapabilities,
    EngineSnapshot,
    JOHN,
)
from app.engines.discovery.registry import DiscoveryRegistry
from app.engines.jobs import (
    TERMINAL_JOB_STATUSES,
    AuditJob,
    DuplicateJobError,
    InvalidJobError,
    JobStatus,
    JobStore,
    default_job_store,
)

JOBS_SERVICE_MODULE = Path("app/core/orchestrator/jobs.py")

VERIFIED = CapabilityStatus.VERIFIED
UNKNOWN = CapabilityStatus.UNKNOWN

UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _uuid(i: int) -> str:
    return f"00000000-0000-4000-8000-{i:012x}"


def _registry(status: CapabilityStatus) -> DiscoveryRegistry:
    registry = DiscoveryRegistry()
    registry.register(
        EngineSnapshot(engine=JOHN, discovered=True, binary_found=True)
    )
    registry.set_capabilities(
        JOHN,
        EngineCapabilities(
            engine=JOHN, capability_status=status, formats=("pdf",)
        ),
    )
    return registry


def _authorized_preview() -> PlanPreview:
    return preview_plan("pdf-document", discovery_registry=_registry(VERIFIED))


def _denied_preview() -> PlanPreview:
    return preview_plan("pdf-document", discovery_registry=_registry(UNKNOWN))


def _failed_preview() -> PlanPreview:
    base = _authorized_preview()
    return PlanPreview(
        target=base.target,
        selection=base.selection,
        request=base.request,
        authorization=base.authorization,
        plan=base.plan,
        simulation=None,
    )


def _forbidden_keys(payload: object) -> set[str]:
    found: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in FORBIDDEN_REQUEST_FIELDS:
                found.add(key)
            found |= _forbidden_keys(value)
    elif isinstance(payload, list):
        for item in payload:
            found |= _forbidden_keys(item)
    return found


@pytest.fixture(autouse=True)
def _reset_default_job_store() -> None:
    """Keep the process-wide store empty for every test that touches it."""

    default_job_store().reset()
    yield
    default_job_store().reset()


# --------------------------------------------------------------------------- #
# job_status_for
# --------------------------------------------------------------------------- #


def test_job_status_for_simulated_preview() -> None:
    assert job_status_for(_authorized_preview()) is JobStatus.SIMULATED


def test_job_status_for_denied_preview() -> None:
    assert job_status_for(_denied_preview()) is JobStatus.DENIED


def test_job_status_for_authorized_without_simulation_is_failed() -> None:
    assert job_status_for(_failed_preview()) is JobStatus.FAILED


def test_job_status_for_none_is_failed() -> None:
    assert job_status_for(None) is JobStatus.FAILED


def test_job_status_for_never_returns_pending() -> None:
    for preview in (_authorized_preview(), _denied_preview(), _failed_preview(), None):
        assert job_status_for(preview) in TERMINAL_JOB_STATUSES
        assert job_status_for(preview) is not JobStatus.PENDING


def test_job_status_for_real_mode_is_a_denial() -> None:
    preview = preview_plan(
        "pdf-document", discovery_registry=_registry(VERIFIED), mode="real"
    )
    assert job_status_for(preview) is JobStatus.DENIED


def test_job_status_for_unmapped_target_is_a_denial() -> None:
    preview = preview_plan(
        "unknown-target", discovery_registry=_registry(VERIFIED)
    )
    assert job_status_for(preview) is JobStatus.DENIED


# --------------------------------------------------------------------------- #
# failure_reason_for
# --------------------------------------------------------------------------- #


def test_failure_reason_for_simulated_is_empty() -> None:
    assert failure_reason_for(_authorized_preview()) == ""


def test_failure_reason_for_denied_is_the_policy_reason() -> None:
    assert failure_reason_for(_denied_preview()) == "capability_not_verified"


def test_failure_reason_for_authorized_without_simulation() -> None:
    assert failure_reason_for(_failed_preview()) == "authorized_without_simulation"


def test_failure_reason_for_none_is_no_preview() -> None:
    assert failure_reason_for(None) == "no_preview"


# --------------------------------------------------------------------------- #
# create_job: authorised simulation
# --------------------------------------------------------------------------- #


def test_create_job_records_an_authorised_simulation() -> None:
    store = JobStore()
    job = create_job("pdf-document", discovery_registry=_registry(VERIFIED), store=store)

    assert job.status is JobStatus.SIMULATED
    assert job.target == "pdf-document"
    assert job.failure_reason == ""
    assert UUID_PATTERN.fullmatch(job.job_id) is not None
    assert job.preview is not None
    assert job.preview.simulation is not None
    assert job.result() is job.preview.simulation
    assert job.result().status is ExecutionStatus.SIMULATED
    assert store.get(job.job_id) is job
    assert len(store) == 1
    assert job.job_id != job.result().simulation_id


def test_create_job_id_is_disjoint_from_the_simulation_id() -> None:
    job = create_job("pdf-document", discovery_registry=_registry(VERIFIED))
    assert job.job_id != job.result().simulation_id
    assert UUID_PATTERN.fullmatch(job.job_id) is not None
    assert re.fullmatch(r"[0-9a-f]{16}", job.result().simulation_id) is not None


def test_create_job_uses_the_injected_clock_for_created_at() -> None:
    job = create_job(
        "pdf-document",
        discovery_registry=_registry(VERIFIED),
        clock=lambda: 1_700_000_123,
    )
    assert job.created_at == 1_700_000_123


def test_create_job_mints_a_fresh_uuid_when_none_is_supplied() -> None:
    first = create_job("pdf-document", discovery_registry=_registry(VERIFIED))
    second = create_job("pdf-document", discovery_registry=_registry(VERIFIED))
    assert first.job_id != second.job_id
    assert UUID_PATTERN.fullmatch(first.job_id) is not None


def test_create_job_honours_an_injected_job_id() -> None:
    job = create_job(
        "pdf-document",
        discovery_registry=_registry(VERIFIED),
        job_id=_uuid(42),
    )
    assert job.job_id == _uuid(42)


def test_create_job_treats_an_empty_job_id_as_auto_generated() -> None:
    job = create_job(
        "pdf-document",
        discovery_registry=_registry(VERIFIED),
        job_id="",
    )
    assert UUID_PATTERN.fullmatch(job.job_id) is not None


def test_create_job_stores_in_the_injected_store() -> None:
    store = JobStore()
    job = create_job(
        "pdf-document",
        discovery_registry=_registry(VERIFIED),
        store=store,
    )
    assert len(store) == 1
    assert store.get(job.job_id) is job


def test_create_job_reuses_the_unchanged_pipeline_output() -> None:
    job = create_job("pdf-document", discovery_registry=_registry(VERIFIED))
    assert job.preview is not None
    assert job.preview.as_dict() == _authorized_preview().as_dict()


def test_create_job_threads_reference_through_the_preview() -> None:
    job = create_job(
        "pdf-document",
        discovery_registry=_registry(VERIFIED),
        reference="audit-42",
    )
    assert job.preview is not None
    assert job.preview.request.reference == "audit-42"


# --------------------------------------------------------------------------- #
# create_job: denials and failures
# --------------------------------------------------------------------------- #


def test_create_job_records_a_capability_denial() -> None:
    store = JobStore()
    job = create_job("pdf-document", discovery_registry=_registry(UNKNOWN), store=store)

    assert job.status is JobStatus.DENIED
    assert job.failure_reason == "capability_not_verified"
    assert job.preview is not None
    assert job.preview.simulation is None
    assert job.result() is None
    assert store.get(job.job_id) is job


def test_create_job_records_a_real_mode_denial() -> None:
    job = create_job(
        "pdf-document",
        discovery_registry=_registry(VERIFIED),
        mode="real",
    )
    assert job.status is JobStatus.DENIED
    assert job.failure_reason == "non_simulation_request"
    assert job.preview is not None
    assert job.preview.plan is None
    assert job.preview.simulation is None


def test_create_job_records_a_denial_for_an_unmapped_target() -> None:
    job = create_job("unknown-target", discovery_registry=_registry(VERIFIED))
    assert job.status is JobStatus.DENIED
    assert job.failure_reason == "invalid_request"


def test_create_job_handles_a_non_string_target_as_a_denial() -> None:
    job = create_job(42)  # type: ignore[arg-type]
    assert job.status is JobStatus.DENIED
    assert job.target == ""
    assert job.failure_reason == "invalid_request"


def test_create_job_records_failed_when_authorized_without_simulation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JobStore()
    monkeypatch.setattr(
        "app.core.orchestrator.jobs.preview_plan",
        lambda *_args, **_kwargs: _failed_preview(),
    )
    job = create_job("pdf-document", store=store)

    assert job.status is JobStatus.FAILED
    assert job.preview is None
    assert job.failure_reason == "authorized_without_simulation"
    assert job.result() is None
    assert store.get(job.job_id) is job


def test_create_job_never_produces_a_pending_record() -> None:
    store = JobStore()
    create_job("pdf-document", discovery_registry=_registry(VERIFIED), store=store)
    create_job("pdf-document", discovery_registry=_registry(UNKNOWN), store=store)
    assert {job.status for job in store.list()} <= set(TERMINAL_JOB_STATUSES)


# --------------------------------------------------------------------------- #
# create_job: store rules still apply
# --------------------------------------------------------------------------- #


def test_create_job_rejects_a_non_canonical_job_id() -> None:
    with pytest.raises(InvalidJobError):
        create_job("pdf-document", discovery_registry=_registry(VERIFIED), job_id="ABC")


def test_create_job_rejects_a_duplicate_job_id() -> None:
    store = JobStore()
    create_job(
        "pdf-document",
        discovery_registry=_registry(VERIFIED),
        job_id=_uuid(1),
        store=store,
    )
    with pytest.raises(DuplicateJobError):
        create_job(
            "pdf-document",
            discovery_registry=_registry(VERIFIED),
            job_id=_uuid(1),
            store=store,
        )
    assert len(store) == 1


def test_create_job_defaults_to_the_process_wide_store() -> None:
    store = default_job_store()
    job = create_job("pdf-document", discovery_registry=_registry(VERIFIED))
    assert store.get(job.job_id) is job


# --------------------------------------------------------------------------- #
# 3B.2 schemas
# --------------------------------------------------------------------------- #


def test_job_summary_round_trips_its_fields() -> None:
    summary = JobSummary(
        job_id=_uuid(1), status="simulated", created_at=1, target="pdf-document"
    )
    assert summary.job_id == _uuid(1)
    assert summary.status == "simulated"
    assert summary.created_at == 1
    assert summary.target == "pdf-document"


def test_job_summary_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        JobSummary(
            job_id=_uuid(1),
            status="simulated",
            created_at=1,
            target="pdf-document",
            command="john -w=x",
        )


def test_job_info_round_trips_a_simulated_job() -> None:
    job = create_job("pdf-document", discovery_registry=_registry(VERIFIED))
    info = JobInfo(**job.as_dict())

    assert info.job_id == job.job_id
    assert info.status == "simulated"
    assert info.created_at == job.created_at
    assert info.target == "pdf-document"
    assert info.failure_reason == ""
    assert isinstance(info.preview, PlanPreviewResponse)
    assert info.preview is not None
    assert info.preview.simulation is not None
    assert info.preview.simulation.simulation_id == job.result().simulation_id


def test_job_info_round_trips_a_denied_job() -> None:
    job = create_job("pdf-document", discovery_registry=_registry(UNKNOWN))
    info = JobInfo(**job.as_dict())

    assert info.status == "denied"
    assert info.failure_reason == "capability_not_verified"
    assert isinstance(info.preview, PlanPreviewResponse)
    assert info.preview is not None
    assert info.preview.simulation is None


def test_job_info_allows_failed_jobs_without_a_preview() -> None:
    failed = AuditJob(
        job_id=_uuid(3),
        status=JobStatus.FAILED,
        created_at=1,
        target="pdf-document",
        failure_reason="boom",
    )
    info = JobInfo(**failed.as_dict())
    assert info.preview is None
    assert info.failure_reason == "boom"


def test_job_info_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        JobInfo(
            job_id=_uuid(1),
            status="simulated",
            created_at=1,
            target="pdf-document",
            argv=["john"],
        )


def test_job_list_response_shape_and_defaults() -> None:
    assert JobListResponse().jobs == []
    assert JobListResponse().count == 0
    response = JobListResponse(
        jobs=[
            JobSummary(
                job_id=_uuid(1), status="simulated", created_at=1, target="pdf"
            )
        ],
        count=1,
    )
    assert response.count == 1
    assert response.jobs[0].job_id == _uuid(1)


def test_job_schema_serialisation_never_carries_command_fields() -> None:
    job = create_job(
        "pdf-document", discovery_registry=_registry(VERIFIED), job_id=_uuid(9)
    )
    info = JobInfo(**job.as_dict())
    assert not _forbidden_keys(info.model_dump(mode="json"))


# --------------------------------------------------------------------------- #
# static safety boundary (app/core/orchestrator/jobs.py)
# --------------------------------------------------------------------------- #


def test_job_service_module_never_imports_process_execution() -> None:
    tree = ast.parse(JOBS_SERVICE_MODULE.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not (imported & {"subprocess", "os", "shlex", "pty"})


def test_job_service_module_has_no_process_shell_or_eval_calls() -> None:
    tree = ast.parse(JOBS_SERVICE_MODULE.read_text(encoding="utf-8"))
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Attribute, ast.Name))
    }
    for forbidden in (
        "system",
        "popen",
        "Popen",
        "exec",
        "eval",
        "spawnv",
        "check_output",
        "subprocess",
    ):
        assert forbidden not in called


def test_job_service_module_imports_only_plan_and_data_layer_names() -> None:
    tree = ast.parse(JOBS_SERVICE_MODULE.read_text(encoding="utf-8"))
    from_plan: set[str] = set()
    from_jobs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "app.core.orchestrator.plan":
                from_plan.update(alias.name for alias in node.names)
            elif node.module == "app.engines.jobs":
                from_jobs.update(alias.name for alias in node.names)
    assert from_plan == {"PlanPreview", "preview_plan"}
    assert from_jobs == {"AuditJob", "JobStatus", "JobStore", "default_job_store"}


def test_job_service_module_never_imports_runtime_pipeline_modules() -> None:
    tree = ast.parse(JOBS_SERVICE_MODULE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    for banned in (
        "app.engines.discovery.john",
        "app.engines.discovery.executor",
        "app.engines.discovery.allowlist",
        "app.engines.discovery.capability_map",
        "app.engines.discovery.models",
        "app.engines.discovery.service",
        "app.engines.selection",
        "app.api",
    ):
        assert banned not in imported


def test_job_service_module_only_orchestrates_through_preview_plan() -> None:
    tree = ast.parse(JOBS_SERVICE_MODULE.read_text(encoding="utf-8"))
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Attribute, ast.Name))
    }
    assert "preview_plan" in called
    for forbidden in (
        "run_safe_command",
        "locate_engine_binary",
        "capabilities_from_probes",
        "refresh",
        "authorize",
        "simulate",
        "select",
        "build_execution_plan",
    ):
        assert forbidden not in called
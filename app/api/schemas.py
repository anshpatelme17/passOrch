"""Phase 2D - API schemas for the plan-preview surface.

Every model is structured and JSON-safe: no free-form command strings, no
argv lists, no executable paths.  ``extra="forbid"`` keeps unknown fields out
of requests, and response models only ever describe selection, authorisation,
plan steps and simulation outcomes.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.engines.discovery.models import CapabilityStatus

__all__ = [
    "AuthorizationInfo",
    "EngineInfo",
    "EngineRefreshReport",
    "EnginesResponse",
    "JobInfo",
    "JobCreateRequest",
    "JobExportEnvelope",
    "JobListResponse",
    "JobPageResponse",
    "JobSummary",
    "PlanInfo",
    "PlanPreviewRequest",
    "PlanPreviewResponse",
    "RefreshEnginesRequest",
    "RefreshEnginesResponse",
    "RequestInfo",
    "SelectionInfo",
    "SimulationInfo",
    "TargetListResponse",
    "TargetSummary",
    "TargetInstance",
    "TargetInstanceCreateRequest",
    "TargetInstancesResponse",
    "TargetInstancePageResponse",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanPreviewRequest(_Strict):
    """Body for ``POST /api/targets/{target_id}/compatibility/plan``.

    ``mode`` is a free string so an unsupported value (for example ``"real"``)
    reaches the execution policy and is answered with a structured denial
    rather than a transport-level error.

    ``reference`` is capped at 256 characters, matching the frontend inputs
    that mirror it; the bound keeps the stored label from becoming an
    unbounded string on an otherwise fully-validated surface.
    """

    mode: str = "simulated"
    reference: str = Field(default="", max_length=256)


class JobCreateRequest(_Strict):
    """Body for ``POST /api/jobs`` - one simulated audit job.

    ``target`` is **required** and is validated against the read-only
    ``TARGET_FORMAT_POLICY`` map before a job is created; an unmapped target is
    rejected at the transport level (422) instead of becoming a denial record.

    ``instance_id`` (Phase 3E) is **optional**: when supplied it must name an
    existing target instance whose ``target_type`` matches ``target``. The
    association is snapshot onto the job record at creation, so it is never
    re-resolved (and never invalidated) when the instance is later deleted or
    evicted from its bounded store.

    ``mode`` stays a free string for the same reason as
    :class:`PlanPreviewRequest`: an unsupported value (for example ``"real"``)
    must reach :class:`~app.engines.execution_policy.ExecutionPolicy` and be
    answered with a structured denial, not a transport error.

    ``reference`` is capped at 256 characters, matching the frontend input
    that mirrors it.
    """

    target: str
    instance_id: str | None = None
    mode: str = "simulated"
    reference: str = Field(default="", max_length=256)


class SelectionInfo(BaseModel):
    status: str
    reason: str
    selected_engine: str = ""
    candidates: list[str] = []


class RequestInfo(BaseModel):
    target: str
    engine: str
    mode: str
    selection_status: str
    capability_status: str
    selected_engine: str
    matched_formats: list[str] = []
    reference: str = ""


class AuthorizationInfo(BaseModel):
    authorized: bool
    status: str
    reason: str
    detail: str = ""
    problems: list[str] = []
    adapter: str = ""
    mode: str = ""


class PlanInfo(BaseModel):
    adapter: str
    mode: str
    simulated: bool
    steps: list[str]
    target: str
    selected_engine: str
    capability_status: str


class SimulationInfo(BaseModel):
    status: str
    adapter: str
    engine: str
    target: str
    mode: str
    steps: list[str]
    message: str
    simulation_id: str
    simulated: bool


class PlanPreviewResponse(BaseModel):
    """Structured plan preview - authorised or denied, never executed."""

    target: str
    selection: SelectionInfo
    request: RequestInfo
    authorization: AuthorizationInfo
    plan: PlanInfo | None = None
    simulation: SimulationInfo | None = None


class TargetSummary(BaseModel):
    target: str
    description: str
    formats: list[str]
    enabled: bool
    usable: bool


class TargetListResponse(BaseModel):
    targets: list[TargetSummary]
    count: int = 0


class EngineInfo(_Strict):
    """One engine's discovery + capability state (never raw probe output).

    ``extra="forbid"`` is fail-closed: a payload key this schema does not
    know about raises instead of being silently serialised.
    """

    engine: str
    discovered: bool
    binary_found: bool
    version: str | None = None
    operations: list[str] = []
    snapshot_error: str | None = None
    capability_status: str
    formats: list[str] = []
    targets: list[str] = []
    source_operation: str | None = None
    reason: str | None = None
    parser: str | None = None
    real_engine_compatible: bool


class EnginesResponse(_Strict):
    engines: list[EngineInfo]


class RefreshEnginesRequest(_Strict):
    """Optional body for ``POST /api/engines/refresh``.

    The **only** client-selectable input is a list of known engine *names*
    (``engines``).  ``None`` (missing body, ``{}`` or ``null``) refreshes every
    known engine; an explicit list refreshes only those engines, in the
    request's order, with the service's own de-duplication.

    There is deliberately no field for executable paths, binary names,
    commands, subprocess arguments, probe operations, parsers, timeouts,
    output limits or verification status: ``extra="forbid"`` rejects any
    unknown key at the transport layer, and the refresh service decides every
    operation internally.

    Unknown engine names are *not* rejected here - they reach the service,
    which fails the whole subset closed (``404 unknown_engine``) before any
    probe runs, so a mixed valid/invalid list never refreshes anything.
    """

    engines: list[str] | None = None


class EngineRefreshReport(_Strict):
    """One engine's refresh outcome - the ``RefreshReport`` field set, verbatim.

    Mirrors :meth:`app.engines.discovery.service.RefreshReport.as_dict`
    exactly: the same five fields, no more.  ``capability_status`` is the
    closed :class:`~app.engines.discovery.models.CapabilityStatus` enum, so an
    unsupported status can never be serialised, and raw probe output, command
    details and exception text are structurally impossible here.
    """

    engine: str
    discovered: bool
    capability_status: CapabilityStatus
    reason: str = ""
    truncated: bool = False


class RefreshEnginesResponse(_Strict):
    """Result of one refresh: deterministic per-engine reports, nothing else."""

    engines: list[EngineRefreshReport]


class JobSummary(_Strict):
    """One audit job as shown in the (read-only) job list."""

    job_id: str
    status: str
    created_at: int
    target: str


class JobInfo(_Strict):
    """Full audit record - reuses :class:`PlanPreviewResponse` for the preview.

    ``preview`` is ``None`` only for ``FAILED`` jobs (no preview was produced).

    ``instance_id`` / ``instance_target_type`` (Phase 3E) are the association
    snapshot: ``""`` when the job was created without an ``instance_id``. They
    are never re-resolved, so a job's details stay readable after the instance
    is deleted or evicted from the bounded store.
    """

    job_id: str
    status: str
    created_at: int
    target: str
    failure_reason: str = ""
    preview: PlanPreviewResponse | None = None
    instance_id: str = ""
    instance_target_type: str = ""


class JobListResponse(_Strict):
    jobs: list[JobSummary] = []
    count: int = 0


class JobPageResponse(_Strict):
    jobs: list[JobSummary] = []
    count: int = 0
    total: int = 0
    limit: int = 50
    offset: int = 0


class JobStatsResponse(_Strict):
    """Read-only aggregation over the job store."""

    total: int = 0
    simulated: int = 0
    denied: int = 0
    failed: int = 0
    by_status: dict[str, int] = {}
    by_target: dict[str, int] = {}


class JobExportEnvelope(_Strict):
    """Versioned download envelope for ``GET /api/jobs/{job_id}/export``.

    ``job`` is exactly :class:`JobInfo` - the authoritative record the detail
    endpoint returns - so an export can never carry more (or less) than the
    persisted job graph: the operator ``reference`` when the job has one, and
    a ``None`` preview for failed jobs, both preserved verbatim.  Nothing
    outside the persisted record (in particular the current capability
    registry) ever reaches this envelope.

    ``schema_version`` pins the envelope contract for consumers,
    ``exported_at`` records when *this* artifact was produced (never the
    job's ``created_at``), and ``simulation_only`` makes the no-execution
    boundary part of the artifact itself.  ``extra="forbid"`` keeps
    serialization fail-closed: an unknown key raises instead of leaking.
    """

    schema_version: Literal["1"]
    exported_at: int
    simulation_only: Literal[True]
    job: JobInfo


class TargetInstance(_Strict):
    id: str
    name: str
    target_type: str
    description: str = ""
    created_at: int


class TargetInstanceCreateRequest(_Strict):
    name: str
    target_type: str
    description: str = ""


class TargetInstanceUpdateRequest(_Strict):
    """Mutable fields only (Phase 4): ``id``, ``target_type`` and
    ``created_at`` are immutable - ``extra="forbid"`` rejects any attempt
    to send them, and at least one field must be present."""

    name: str | None = None
    description: str | None = None


class TargetInstancesResponse(_Strict):
    instances: list[TargetInstance] = []
    count: int = 0


class TargetInstancePageResponse(_Strict):
    instances: list[TargetInstance] = []
    count: int = 0
    total: int = 0
    limit: int = 50
    offset: int = 0

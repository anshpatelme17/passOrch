"""Phase 2D - API schemas for the plan-preview surface.

Every model is structured and JSON-safe: no free-form command strings, no
argv lists, no executable paths.  ``extra="forbid"`` keeps unknown fields out
of requests, and response models only ever describe selection, authorisation,
plan steps and simulation outcomes.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

__all__ = [
    "AuthorizationInfo",
    "EngineInfo",
    "EnginesResponse",
    "JobInfo",
    "JobCreateRequest",
    "JobListResponse",
    "JobSummary",
    "PlanInfo",
    "PlanPreviewRequest",
    "PlanPreviewResponse",
    "RequestInfo",
    "SelectionInfo",
    "SimulationInfo",
    "TargetListResponse",
    "TargetSummary",
    "TargetInstance",
    "TargetInstanceCreateRequest",
    "TargetInstancesResponse",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanPreviewRequest(_Strict):
    """Body for ``POST /api/targets/{target_id}/compatibility/plan``.

    ``mode`` is a free string so an unsupported value (for example ``"real"``)
    reaches the execution policy and is answered with a structured denial
    rather than a transport-level error.
    """

    mode: str = "simulated"
    reference: str = ""


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
    """

    target: str
    instance_id: str | None = None
    mode: str = "simulated"
    reference: str = ""


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


class JobStatsResponse(_Strict):
    """Read-only aggregation over the job store."""

    total: int = 0
    simulated: int = 0
    denied: int = 0
    failed: int = 0
    by_status: dict[str, int] = {}
    by_target: dict[str, int] = {}


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


class TargetInstancesResponse(_Strict):
    instances: list[TargetInstance] = []
    count: int = 0

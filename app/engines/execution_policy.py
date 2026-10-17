"""Phase 2D - execution policy: the single authorisation gate.

The policy sits between an :class:`~app.engines.adapter.ExecutionRequest` and
an :class:`~app.engines.adapter.ExecutionPlan`.  It answers exactly one
question: *may this request become a simulation plan?*

    Denial reasons (in evaluation order)::

    invalid_request                 structural problems / unusable target
    unsupported_execution_mode      mode value is not a known ExecutionMode
    non_simulation_request          mode is known but is not 'simulated'
    capability_not_verified         capability evidence is not VERIFIED
    selection_not_selected          selection status is not 'selected'
    unknown_engine                  no registered adapter for the engine
                                   (this also covers an empty engine)
    authorized_simulation           request may proceed to a plan

There is no code path that converts a denied request into an authorised one,
no 'real' execution branch, and no command / argv / executable surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.engines.adapter import (
    ExecutionMode,
    ExecutionRequest,
    coerce_execution_mode,
)
from app.engines.adapter_registry import (
    EngineAdapterRegistry,
    default_adapter_registry,
)
from app.engines.discovery.capability_map import is_usable

__all__ = [
    "REASON_AUTHORIZED",
    "REASON_CAPABILITY_NOT_VERIFIED",
    "REASON_INVALID_REQUEST",
    "REASON_NON_SIMULATION",
    "REASON_SELECTION_NOT_SELECTED",
    "REASON_UNKNOWN_ENGINE",
    "REASON_UNSUPPORTED_MODE",
    "AuthorizationStatus",
    "ExecutionPolicy",
    "PolicyDecision",
    "default_execution_policy",
    "mode_label",
]

REASON_INVALID_REQUEST = "invalid_request"
REASON_UNSUPPORTED_MODE = "unsupported_execution_mode"
REASON_NON_SIMULATION = "non_simulation_request"
REASON_CAPABILITY_NOT_VERIFIED = "capability_not_verified"
REASON_SELECTION_NOT_SELECTED = "selection_not_selected"
REASON_UNKNOWN_ENGINE = "unknown_engine"
REASON_AUTHORIZED = "authorized_simulation"

#: Normalised capability evidence value required for authorisation.
_VERIFIED = "verified"
#: Normalised selection status value required for authorisation.
_SELECTED = "selected"


class AuthorizationStatus(str, Enum):
    """Outcome of an execution-policy decision."""

    AUTHORIZED = "authorized"
    DENIED = "denied"


def mode_label(value: object) -> str:
    """JSON-safe label for a raw or enum execution-mode value."""

    if isinstance(value, ExecutionMode):
        return value.value
    return value if isinstance(value, str) else repr(value)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Structured authorisation outcome for one execution request."""

    status: AuthorizationStatus
    reason: str
    detail: str = ""
    problems: tuple[str, ...] = ()
    adapter: str = ""
    mode: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "problems", tuple(self.problems))

    @property
    def authorized(self) -> bool:
        return self.status is AuthorizationStatus.AUTHORIZED

    @property
    def denied(self) -> bool:
        return not self.authorized

    @classmethod
    def allow(cls, *, adapter: str, mode: str) -> PolicyDecision:
        return cls(
            status=AuthorizationStatus.AUTHORIZED,
            reason=REASON_AUTHORIZED,
            adapter=adapter,
            mode=mode,
        )

    @classmethod
    def deny(
        cls,
        reason: str,
        *,
        detail: str = "",
        problems: tuple[str, ...] = (),
        mode: str = "",
    ) -> PolicyDecision:
        return cls(
            status=AuthorizationStatus.DENIED,
            reason=reason,
            detail=detail,
            problems=problems,
            mode=mode,
        )

    def as_dict(self) -> dict[str, object]:
        """JSON-safe view - structured fields only."""

        return {
            "authorized": self.authorized,
            "status": self.status.value,
            "reason": self.reason,
            "detail": self.detail,
            "problems": list(self.problems),
            "adapter": self.adapter,
            "mode": self.mode,
        }


class ExecutionPolicy:
    """Authorises simulation-only execution requests.  Denies everything else."""

    def __init__(self, registry: EngineAdapterRegistry | None = None) -> None:
        self._registry = registry if registry is not None else default_adapter_registry()

    @property
    def registry(self) -> EngineAdapterRegistry:
        return self._registry

    def authorize(self, request: object) -> PolicyDecision:
        """Decide whether ``request`` may become an execution plan."""

        if not isinstance(request, ExecutionRequest):
            return PolicyDecision.deny(
                REASON_INVALID_REQUEST,
                detail="an ExecutionRequest is required",
                problems=("request_type_invalid",),
            )

        problems = request.problems()
        # ``engine_missing`` is deferred: an empty engine is the *consequence*
        # of a non-selected decision, so the capability / selection checks
        # answer first and the closed adapter registry answers last with
        # ``unknown_engine`` (which covers the empty engine as well).
        blocking = tuple(p for p in problems if p != "engine_missing")
        if blocking:
            return PolicyDecision.deny(
                REASON_INVALID_REQUEST,
                detail="execution request is structurally invalid",
                problems=blocking,
                mode=mode_label(request.mode),
            )

        if not is_usable(request.target):
            return PolicyDecision.deny(
                REASON_INVALID_REQUEST,
                detail="target is not mapped to an enabled format policy",
                problems=("target_unusable",),
                mode=mode_label(request.mode),
            )

        mode = coerce_execution_mode(request.mode)
        if mode is None:
            return PolicyDecision.deny(
                REASON_UNSUPPORTED_MODE,
                detail=f"unsupported execution mode: {mode_label(request.mode)!r}",
                mode=mode_label(request.mode),
            )

        if mode is not ExecutionMode.SIMULATED:
            return PolicyDecision.deny(
                REASON_NON_SIMULATION,
                detail=(
                    f"mode {mode.value!r} is not authorised; PassOrch only performs simulations"
                ),
                mode=mode.value,
            )

        if str(request.capability_status).strip().lower() != _VERIFIED:
            return PolicyDecision.deny(
                REASON_CAPABILITY_NOT_VERIFIED,
                detail="capability evidence is not VERIFIED",
                mode=mode.value,
            )

        if str(request.selection_status).strip().lower() != _SELECTED:
            return PolicyDecision.deny(
                REASON_SELECTION_NOT_SELECTED,
                detail=f"selection status is {request.selection_status!r}",
                mode=mode.value,
            )

        if request.engine not in self._registry:
            return PolicyDecision.deny(
                REASON_UNKNOWN_ENGINE,
                detail=f"no registered adapter for engine {request.engine!r}",
                mode=mode.value,
            )

        return PolicyDecision.allow(adapter=request.engine, mode=mode.value)


def default_execution_policy() -> ExecutionPolicy:
    """Process-wide policy used when no policy is injected."""

    return _DEFAULT_EXECUTION_POLICY


_DEFAULT_EXECUTION_POLICY = ExecutionPolicy()

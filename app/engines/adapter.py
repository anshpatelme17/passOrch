"""Phase 2D - simulation-only execution adapter architecture.

SECURITY BOUNDARY
-----------------
This module defines the *shape* of an execution pipeline plus a mock adapter
that only ever produces a deterministic :class:`SimulationResult`.

There is no process invocation anywhere in this module: no ``subprocess``,
no ``os.system``, no shell, no user-supplied command, argv, or executable
path.

Pipeline (selection happens *outside* this layer)::

    Target
      -> verified capability
      -> capability-aware selection          (app.engines.selection)
      -> ExecutionRequest                    (this module)
      -> ExecutionPolicy                     (app.engines.execution_policy)
      -> ExecutionPlan                       (this module)
      -> MockEngineAdapter.simulate()        (this module)
      -> SimulationResult                    (this module)

The adapter layer never selects an engine and never builds a command.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Protocol, runtime_checkable

__all__ = [
    "DEFAULT_PLAN_STEPS",
    "FORBIDDEN_REQUEST_FIELDS",
    "MOCK_ENGINE_NAMES",
    "MOCK_HASHCAT",
    "MOCK_JOHN",
    "AdapterError",
    "DuplicateAdapterError",
    "EngineAdapter",
    "ExecutionMode",
    "ExecutionPlan",
    "ExecutionRequest",
    "ExecutionStatus",
    "InvalidPlanError",
    "MockEngineAdapter",
    "SimulationNotAllowedError",
    "SimulationResult",
    "UnknownAdapterError",
    "build_execution_plan",
    "coerce_execution_mode",
]

MOCK_JOHN = "mock_john"
MOCK_HASHCAT = "mock_hashcat"

#: The only engine identities the adapter layer knows about.
MOCK_ENGINE_NAMES: tuple[str, ...] = (MOCK_HASHCAT, MOCK_JOHN)

#: Field names that must never appear on any execution data structure.
FORBIDDEN_REQUEST_FIELDS: frozenset[str] = frozenset(
    {
        "args",
        "argv",
        "binary",
        "cmd",
        "command",
        "commands",
        "executable",
        "exec",
        "popen",
        "program",
        "run",
        "shell",
        "shell_string",
        "path",
        "subprocess",
    }
)

#: Deterministic, human-readable simulation steps (never command strings).
DEFAULT_PLAN_STEPS: tuple[str, ...] = (
    "load_mock_engine",
    "simulate_compatibility_run",
    "collect_simulation_result",
)

_STEP_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class ExecutionMode(str, Enum):
    """Execution modes.

    ``SIMULATED`` is the default - and the only authorised - mode.
    ``REAL`` exists purely so that a real execution request can be *denied*
    explicitly; nothing in PassOrch implements it, and it is never silently
    converted into a simulation.
    """

    SIMULATED = "simulated"
    REAL = "real"


class ExecutionStatus(str, Enum):
    """Outcome of an execution-plan handling step."""

    PENDING = "pending"
    SIMULATED = "simulated"
    DENIED = "denied"
    FAILED = "failed"


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #


class AdapterError(Exception):
    """Base class for adapter-layer failures."""


class UnknownAdapterError(AdapterError):
    """Raised when an adapter identity is not part of the known set."""

    def __init__(self, name: object) -> None:
        super().__init__(f"unknown adapter engine: {name!r}")
        self.name = name


class DuplicateAdapterError(AdapterError):
    """Raised when an adapter engine is registered twice."""

    def __init__(self, name: str) -> None:
        super().__init__(f"adapter engine already registered: {name!r}")
        self.name = name


class InvalidPlanError(AdapterError):
    """Raised when a request cannot be normalised into a simulation plan."""


class SimulationNotAllowedError(AdapterError):
    """Raised when a non-simulation plan reaches an adapter."""


# --------------------------------------------------------------------------- #
# mode handling
# --------------------------------------------------------------------------- #


def coerce_execution_mode(value: object) -> ExecutionMode | None:
    """Return the :class:`ExecutionMode` for ``value`` or ``None`` if unknown."""

    if isinstance(value, ExecutionMode):
        return value
    if isinstance(value, str):
        try:
            return ExecutionMode(value)
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------- #
# request / plan / result
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    """Structured, command-free request produced from a selection decision.

    The dataclass deliberately accepts an ``mode`` value that is not a valid
    :class:`ExecutionMode`: unsupported requests must reach the policy so they
    can be *denied*, never silently repaired.
    """

    target: str
    engine: str
    mode: ExecutionMode | str = ExecutionMode.SIMULATED
    selection_status: str = ""
    capability_status: str = ""
    selected_engine: str = ""
    matched_formats: tuple[str, ...] = ()
    reference: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.matched_formats, (list, set)):
            object.__setattr__(self, "matched_formats", tuple(self.matched_formats))
        elif not isinstance(self.matched_formats, tuple):
            object.__setattr__(self, "matched_formats", ())

    def problems(self) -> tuple[str, ...]:
        """Structural problems that make the request invalid.

        Execution-mode validity is intentionally *not* reported here: the
        policy owns that decision and answers with its own denial reason.
        Engine presence is likewise owned by the policy, which validates it
        against the closed adapter registry (``unknown_engine``).
        """

        issues: list[str] = []
        if not isinstance(self.target, str) or not self.target.strip():
            issues.append("target_missing")
        if not isinstance(self.engine, str) or not self.engine.strip():
            issues.append("engine_missing")
        if not isinstance(self.selection_status, str):
            issues.append("selection_status_invalid")
        if not isinstance(self.capability_status, str):
            issues.append("capability_status_invalid")
        if not isinstance(self.matched_formats, tuple):
            issues.append("matched_formats_invalid")
        return tuple(issues)

    @property
    def is_simulation_request(self) -> bool:
        return coerce_execution_mode(self.mode) is ExecutionMode.SIMULATED

    @property
    def forbidden_fields(self) -> frozenset[str]:
        """Always empty: requests never expose a command surface."""

        present = set(self.__dataclass_fields__) & FORBIDDEN_REQUEST_FIELDS
        return frozenset(present)


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """A normalised, simulation-only plan derived from an authorised request."""

    request: ExecutionRequest
    mode: ExecutionMode
    adapter: str
    steps: tuple[str, ...]
    simulated: bool = True

    @property
    def target(self) -> str:
        return self.request.target

    @property
    def selected_engine(self) -> str:
        return self.request.selected_engine

    @property
    def capability_status(self) -> str:
        return self.request.capability_status

    def as_dict(self) -> dict[str, object]:
        """JSON-safe view - structured fields only."""

        return {
            "adapter": self.adapter,
            "mode": self.mode.value,
            "simulated": self.simulated,
            "steps": list(self.steps),
            "target": self.target,
            "selected_engine": self.selected_engine,
            "capability_status": self.capability_status,
        }


def build_execution_plan(
    request: ExecutionRequest, *, steps: tuple[str, ...] | None = None
) -> ExecutionPlan:
    """Normalise an authorised request into a simulation-only plan.

    Raises ``InvalidPlanError`` when the request is malformed and
    ``SimulationNotAllowedError`` when the requested mode is anything other
    than :attr:`ExecutionMode.SIMULATED`.
    """

    if not isinstance(request, ExecutionRequest):
        raise InvalidPlanError("an ExecutionRequest is required to build a plan")
    problems = request.problems()
    if problems:
        raise InvalidPlanError(f"invalid execution request: {', '.join(problems)}")

    mode = coerce_execution_mode(request.mode)
    if mode is None:
        raise InvalidPlanError(f"unsupported execution mode: {request.mode!r}")
    if mode is not ExecutionMode.SIMULATED:
        raise SimulationNotAllowedError(
            "only simulation plans may be built; refusing to convert a "
            f"{mode.value!r} request into a simulation"
        )

    plan_steps = DEFAULT_PLAN_STEPS if steps is None else tuple(steps)
    if not plan_steps:
        raise InvalidPlanError("a simulation plan requires at least one step")
    for step in plan_steps:
        if not isinstance(step, str) or not _STEP_RE.match(step):
            raise InvalidPlanError(f"invalid plan step: {step!r}")

    return ExecutionPlan(
        request=request,
        mode=mode,
        adapter=request.engine,
        steps=plan_steps,
        simulated=True,
    )


@dataclass(frozen=True, slots=True)
class SimulationResult:
    """Deterministic, structured outcome of a mock simulation run."""

    status: ExecutionStatus
    adapter: str
    engine: str
    target: str
    mode: ExecutionMode
    steps: tuple[str, ...]
    message: str
    simulation_id: str
    simulated: bool = True
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "steps", tuple(self.steps))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def as_dict(self) -> dict[str, object]:
        """JSON-safe view - structured fields only."""

        return {
            "status": self.status.value,
            "adapter": self.adapter,
            "engine": self.engine,
            "target": self.target,
            "mode": self.mode.value,
            "steps": list(self.steps),
            "message": self.message,
            "simulation_id": self.simulation_id,
            "simulated": self.simulated,
        }


# --------------------------------------------------------------------------- #
# adapter protocol + mock implementation
# --------------------------------------------------------------------------- #


@runtime_checkable
class EngineAdapter(Protocol):
    """Structural contract every registered adapter must satisfy."""

    @property
    def name(self) -> str: ...

    def simulate(self, plan: ExecutionPlan) -> SimulationResult: ...


class MockEngineAdapter:
    """Deterministic, side-effect free simulation of one mock engine.

    It performs no selection, builds no command and executes no process.
    """

    def __init__(self, name: str) -> None:
        if not isinstance(name, str) or not name.strip():
            raise AdapterError("an adapter requires a non-empty engine name")
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def simulate(self, plan: ExecutionPlan) -> SimulationResult:
        """Return a deterministic simulation result for ``plan``."""

        if not isinstance(plan, ExecutionPlan):
            raise InvalidPlanError("simulate() requires an ExecutionPlan")
        if plan.adapter != self.name:
            raise AdapterError(f"plan for {plan.adapter!r} cannot run on adapter {self.name!r}")
        if plan.mode is not ExecutionMode.SIMULATED or not plan.simulated:
            raise SimulationNotAllowedError("MockEngineAdapter only produces simulation results")

        token = _simulation_token(plan)
        return SimulationResult(
            status=ExecutionStatus.SIMULATED,
            adapter=self.name,
            engine=plan.selected_engine or self.name,
            target=plan.target,
            mode=plan.mode,
            steps=plan.steps,
            message="simulation completed",
            simulation_id=token,
            simulated=True,
            metadata={"capability_status": plan.capability_status},
        )


def _simulation_token(plan: ExecutionPlan) -> str:
    payload = "\x1f".join([plan.adapter, plan.target, plan.mode.value, *plan.steps])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def mock_adapter_names() -> Mapping[str, str]:
    """Read-only view of the mock engine identities."""

    return MappingProxyType({name: name for name in MOCK_ENGINE_NAMES})

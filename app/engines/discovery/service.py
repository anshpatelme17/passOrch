"""Phase 3A - capability refresh orchestration.

Composes the existing, individually-tested discovery components into one
small, injected service::

    locate_engine_binary(which=...)            Phase 2A discovery
      -> run_safe_command(...)                 Phase 2A runner (ONLY subprocess path)
      -> ProbeResult (transient, never stored) Phase 2A model
      -> capabilities_from_probes(...)         Phase 2B verified parsing
      -> DiscoveryRegistry.set_capabilities()  Phase 2B evidence store
      -> RefreshReport                         Phase 3A safe summary

Security boundary:

* this module never imports ``subprocess``, ``os``, ``shlex`` or ``pty`` -
  the only subprocess-capable path remains
  :func:`~app.engines.discovery.runner.run_safe_command`;
* operations are selected **internally** from :data:`CAPABILITY_OPERATIONS`;
  there is no operation parameter on any public method, therefore no
  operation can ever come from user or HTTP input;
* the internal table only contains operations the immutable discovery
  allowlist already permits (asserted by tests); an engine without an
  allowlisted capability operation (Hashcat) is never probed for capabilities;
* raw probe output lives only inside a transient :class:`ProbeResult` and is
  never stored in the registry, the report or any payload;
* ``UNKNOWN`` / ``ERROR`` semantics are preserved verbatim from the verified
  parsers - this module never upgrades evidence;
* the service is code/test-invoked only - **no HTTP endpoint triggers it**
  in Phase 3A.

There is deliberately no HTTP refresh endpoint here: refreshing is a local,
explicit, code-level action.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Sequence

from app.engines.discovery import hashcat, john
from app.engines.discovery.allowlist import DISCOVERY_ALLOWLIST
from app.engines.discovery.errors import (
    CapabilityParseError,
    DiscoveryError,
    EngineBinaryMissingError,
    UnknownEngineError,
)
from app.engines.discovery.locate import locate_engine_binary
from app.engines.discovery.models import (
    DISCOVERY_ENGINE_ORDER,
    KNOWN_ENGINES,
    CapabilityStatus,
    EngineCapabilities,
    EngineSnapshot,
    ProbeResult,
)
from app.engines.discovery.registry import DiscoveryRegistry, default_discovery_registry
from app.engines.discovery.runner import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_COMMAND_OUTPUT_BYTES,
    run_safe_command,
)

__all__ = [
    "CAPABILITY_OPERATIONS",
    "FORBIDDEN_REFRESH_FIELDS",
    "CapabilityRefreshService",
    "RefreshReport",
]

#: Fields a refresh report is allowed to expose - nothing else, ever.
FORBIDDEN_REFRESH_FIELDS: frozenset[str] = frozenset(
    {
        "argv",
        "binary",
        "cmd",
        "command",
        "executable",
        "output",
        "path",
        "probe_output",
        "raw_output",
        "shell",
        "stderr",
        "stdout",
    }
)

#: Engine -> capability-probe operations, selected **inside** this module.
#:
#: Only operations the immutable allowlist already permits may appear here.
#: Hashcat has none: no allowlisted mode-list operation exists, therefore its
#: capability evidence can never leave ``UNKNOWN`` through this pipeline.
CAPABILITY_OPERATIONS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        john.JOHN_ENGINE: john.JOHN_CAPABILITY_OPERATIONS,
        hashcat.HASHCAT_ENGINE: (),
    }
)

_NO_REASON = ""


@dataclass(frozen=True, slots=True)
class RefreshReport:
    """Safe, structured summary of one engine refresh - no raw output.

    Exactly these five fields; nothing else may ever be added without a
    security review (tests assert the closed field set).
    """

    engine: str
    discovered: bool
    capability_status: CapabilityStatus
    reason: str = ""
    truncated: bool = False

    def __post_init__(self) -> None:
        if self.engine not in KNOWN_ENGINES:
            raise UnknownEngineError(self.engine)
        if not isinstance(self.capability_status, CapabilityStatus):
            raise DiscoveryError(
                f"refresh reports must carry a CapabilityStatus (got {self.capability_status!r})"
            )
        object.__setattr__(self, "discovered", bool(self.discovered))
        object.__setattr__(
            self, "reason", self.reason if isinstance(self.reason, str) else _NO_REASON
        )
        object.__setattr__(self, "truncated", bool(self.truncated))

    @property
    def is_verified(self) -> bool:
        return self.capability_status is CapabilityStatus.VERIFIED

    def as_dict(self) -> dict[str, object]:
        """JSON-safe view - structured fields only."""

        return {
            "engine": self.engine,
            "discovered": self.discovered,
            "capability_status": self.capability_status.value,
            "reason": self.reason,
            "truncated": self.truncated,
        }


class CapabilityRefreshService:
    """Injected orchestrator: locate -> probe -> verify -> register -> report.

    Dependencies (``which``, ``executor``, ``registry``) are injected so tests
    never touch a real process and no second subprocess implementation exists.
    """

    def __init__(
        self,
        registry: DiscoveryRegistry | None = None,
        *,
        which: object | None = None,
        executor: object | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_output_bytes: int = MAX_COMMAND_OUTPUT_BYTES,
    ) -> None:
        self._registry = registry if registry is not None else default_discovery_registry()
        self._which = which
        self._executor = executor
        self._timeout = timeout
        self._max_output_bytes = max_output_bytes

    @property
    def registry(self) -> DiscoveryRegistry:
        return self._registry

    # -- public API -------------------------------------------------------- #

    def refresh(
        self, engine_subset: str | Sequence[str] | None = None
    ) -> tuple[RefreshReport, ...]:
        """Refresh capability evidence for one or more **known** engines.

        ``engine_subset`` accepts a single engine name, a sequence of engine
        names, or ``None`` for every known engine (deterministic order).
        The whole subset is validated **before** anything mutates, so a mixed
        valid/invalid subset fails closed with no partial refresh.

        There is intentionally no parameter for operations, commands, paths or
        output - those choices are made inside this module or by the immutable
        allowlist.
        """

        engines = self._resolve_subset(engine_subset)
        return tuple(self._refresh_engine(engine) for engine in engines)

    # -- internals --------------------------------------------------------- #

    def _resolve_subset(self, engine_subset: str | Sequence[str] | None) -> tuple[str, ...]:
        if engine_subset is None:
            return DISCOVERY_ENGINE_ORDER
        if isinstance(engine_subset, str):
            engines: tuple[object, ...] = (engine_subset,)
        else:
            try:
                engines = tuple(engine_subset)
            except TypeError:
                raise UnknownEngineError(engine_subset) from None
        for engine in engines:
            if not isinstance(engine, str) or engine not in KNOWN_ENGINES:
                raise UnknownEngineError(engine)
        return tuple(dict.fromkeys(engines))

    def _refresh_engine(self, engine: str) -> RefreshReport:
        binary_path = locate_engine_binary(engine, which=self._which)  # type: ignore[arg-type]
        if binary_path is None:
            return self._store(
                engine,
                EngineSnapshot(engine=engine, discovered=False, binary_found=False),
                EngineCapabilities(
                    engine=engine,
                    capability_status=CapabilityStatus.UNKNOWN,
                    reason="binary_not_found",
                ),
                discovered=False,
                truncated=False,
            )

        probes: dict[str, ProbeResult] = {}
        truncated = False
        try:
            for operation in CAPABILITY_OPERATIONS[engine]:
                # Defense in depth: the immutable allowlist is re-checked
                # here even though run_safe_command enforces it too.
                DISCOVERY_ALLOWLIST.require(engine, operation)
                result = run_safe_command(
                    engine,
                    operation,
                    executor=self._executor,  # type: ignore[arg-type]
                    which=self._which,  # type: ignore[arg-type]
                    timeout=self._timeout,
                    max_output_bytes=self._max_output_bytes,
                )
                probes[operation] = ProbeResult(
                    engine=result.engine,
                    operation=result.operation,
                    ok=result.ok,
                    returncode=result.returncode,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    truncated=result.truncated,
                )
                truncated = truncated or result.truncated
        except EngineBinaryMissingError:
            # Located at discovery time, gone at probe time: evidence is
            # UNKNOWN, never VERIFIED.
            return self._store(
                engine,
                EngineSnapshot(
                    engine=engine,
                    discovered=True,
                    binary_found=True,
                    operations=tuple(probes),
                ),
                EngineCapabilities(
                    engine=engine,
                    capability_status=CapabilityStatus.UNKNOWN,
                    reason="binary_missing",
                ),
                discovered=True,
                truncated=truncated,
            )

        capabilities = self._capabilities_for(engine, probes)
        snapshot = EngineSnapshot(
            engine=engine,
            discovered=True,
            binary_found=True,
            operations=tuple(probes),
        )
        return self._store(engine, snapshot, capabilities, discovered=True, truncated=truncated)

    def _capabilities_for(
        self, engine: str, probes: Mapping[str, ProbeResult]
    ) -> EngineCapabilities:
        """Delegate to the verified per-engine parsers (never upgrade here)."""

        try:
            if engine == john.JOHN_ENGINE:
                return john.capabilities_from_probes(probes)
            if engine == hashcat.HASHCAT_ENGINE:
                return hashcat.capabilities_from_probes(probes)
        except CapabilityParseError:
            return EngineCapabilities(
                engine=engine,
                capability_status=CapabilityStatus.ERROR,
                reason="foreign_output",
            )
        return EngineCapabilities(
            engine=engine,
            capability_status=CapabilityStatus.UNKNOWN,
            reason="capability_missing",
        )

    def _store(
        self,
        engine: str,
        snapshot: EngineSnapshot,
        capabilities: EngineCapabilities,
        *,
        discovered: bool,
        truncated: bool,
    ) -> RefreshReport:
        """Register snapshot + capability evidence, then summarise safely."""

        self._registry.register(snapshot)
        self._registry.set_capabilities(engine, capabilities)
        return RefreshReport(
            engine=engine,
            discovered=discovered,
            capability_status=capabilities.capability_status,
            reason=capabilities.reason or _NO_REASON,
            truncated=truncated,
        )

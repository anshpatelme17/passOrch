"""Phase 3A - capability refresh service tests.

Every test is injected: ``which`` and ``executor`` doubles are provided by
the tests, so no real engine is ever located or executed.  The suite proves
the refresh service composes the *existing* discovery components without
creating a second subprocess path and without ever manufacturing VERIFIED
capability evidence.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

from app.engines.discovery import john
from app.engines.discovery.allowlist import DISCOVERY_ALLOWLIST
from app.engines.discovery.capability_map import mapped_targets
from app.engines.discovery.errors import UnknownEngineError
from app.engines.discovery.models import (
    HASHCAT,
    JOHN,
    CapabilityStatus,
    KNOWN_ENGINES,
)
from app.engines.discovery.registry import DiscoveryRegistry, default_discovery_registry
from app.engines.discovery.service import (
    CAPABILITY_OPERATIONS,
    FORBIDDEN_REFRESH_FIELDS,
    CapabilityRefreshService,
    RefreshReport,
)

SOURCE = Path(inspect.getsourcefile(CapabilityRefreshService) or "").read_text(
    encoding="utf-8"
)

JOHN_OPERATION = john.JOHN_FORMAT_OPERATION

JOHN_GOOD_OUTPUT = (
    "Supported hash algorithms:\n"
    "  md5crypt, sha256crypt, sha512crypt, bcrypt,\n"
    "  ntlm, raw-md5, raw-sha1, raw-sha256,\n"
    "  zip, pkzip, pdf, office2007, office2010, office2013\n"
)

JOHN_FORMATS = {
    "md5crypt",
    "sha256crypt",
    "sha512crypt",
    "bcrypt",
    "ntlm",
    "raw-md5",
    "raw-sha1",
    "raw-sha256",
    "zip",
    "pkzip",
    "pdf",
    "office2007",
    "office2010",
    "office2013",
}

RAW_MARKER = "RAW_OUTPUT_MARKER_9271"

FAKE_BINARIES = {JOHN: "C:/fake-engines/john", HASHCAT: "C:/fake-engines/hashcat"}

REPORT_FIELDS = {"engine", "discovered", "capability_status", "reason", "truncated"}


class RecordingWhich:
    """Injected binary resolver double - records every lookup."""

    def __init__(self, mapping: dict[str, str | None] | None = None) -> None:
        self.calls: list[str] = []
        self._mapping = dict(mapping if mapping is not None else FAKE_BINARIES)

    def __call__(self, name: str) -> str | None:
        self.calls.append(name)
        return self._mapping.get(name)


class RecordingExecutor:
    """Injected executor double - records argv and returns canned tuples."""

    def __init__(
        self,
        responses: dict[str, tuple[int, str, str, bool]] | None = None,
        *,
        default: tuple[int, str, str, bool] = (0, "", "", False),
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._responses = dict(responses or {})
        self._default = default

    def __call__(
        self, argv: tuple[str, ...], *, timeout: float, max_output_bytes: int
    ) -> tuple[int, str, str, bool]:
        self.calls.append(tuple(argv))
        return self._responses.get(argv[1], self._default)


def _service(
    registry: DiscoveryRegistry | None = None,
    *,
    which: object | None = None,
    executor: object | None = None,
) -> tuple[CapabilityRefreshService, object, object]:
    which = which if which is not None else RecordingWhich()
    executor = (
        executor
        if executor is not None
        else RecordingExecutor({JOHN_OPERATION: (0, JOHN_GOOD_OUTPUT, "", False)})
    )
    service = CapabilityRefreshService(
        registry if registry is not None else DiscoveryRegistry(),
        which=which,
        executor=executor,
    )
    return service, which, executor


def _raw_scan(registry: DiscoveryRegistry, report: RefreshReport | None) -> str:
    """Every serialisable view of the service output, for marker hunting."""

    blob = json.dumps(
        {"payload": registry.payload(), "report": report.as_dict() if report else {}}
    )
    blob += repr([repr(entry) for entry in registry.entries()])
    return blob


# --------------------------------------------------------------------------- #
# happy path: known engine -> VERIFIED
# --------------------------------------------------------------------------- #


def test_refresh_known_engine_produces_verified_report() -> None:
    service, which, executor = _service()
    reports = service.refresh(JOHN)

    assert len(reports) == 1
    report = reports[0]
    assert isinstance(report, RefreshReport)
    assert report.engine == JOHN
    assert report.discovered is True
    assert report.capability_status is CapabilityStatus.VERIFIED
    assert report.is_verified is True
    assert report.reason == ""
    assert report.truncated is False
    # locate + runner both resolve through the injected which
    assert set(which.calls) == {"john"}  # type: ignore[union-attr]
    assert len(executor.calls) == 1  # type: ignore[union-attr]


def test_refresh_stores_verified_evidence_in_the_registry() -> None:
    service, _, _ = _service()
    service.refresh(JOHN)

    entry = service.registry.require(JOHN)
    assert entry.is_verified is True
    assert entry.snapshot.discovered is True
    assert entry.snapshot.binary_found is True
    assert entry.snapshot.operations == (JOHN_OPERATION,)
    assert entry.snapshot.version is None
    assert entry.real_engine_compatible is True
    assert set(entry.formats) == JOHN_FORMATS
    assert len(entry.formats) == len(JOHN_FORMATS)


def test_successful_probe_produces_supported_targets() -> None:
    service, _, _ = _service()
    service.refresh(JOHN)

    entry = service.registry.require(JOHN)
    assert set(entry.targets) == set(mapped_targets())
    assert len(entry.targets) == len(mapped_targets())


# --------------------------------------------------------------------------- #
# closed engine set / fail-closed subset validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("engine", ["zap", "john-the-ripper", "JOHN", "John", ""])
def test_refresh_rejects_unknown_engine_identities(engine: str) -> None:
    service, _, executor = _service()
    with pytest.raises(UnknownEngineError):
        service.refresh(engine)
    assert executor.calls == []  # type: ignore[union-attr]
    assert service.registry.entries() == ()


@pytest.mark.parametrize("value", [7, 7.5, object(), {"engine": "john"}, b"john"])
def test_refresh_rejects_non_engine_subset_values(value: object) -> None:
    service, _, executor = _service()
    with pytest.raises(UnknownEngineError):
        service.refresh(value)  # type: ignore[arg-type]
    assert executor.calls == []  # type: ignore[union-attr]
    assert service.registry.entries() == ()


def test_mixed_subset_fails_closed_before_any_mutation() -> None:
    service, _, executor = _service()
    with pytest.raises(UnknownEngineError):
        service.refresh([JOHN, "zap"])
    assert executor.calls == []  # type: ignore[union-attr]
    assert service.registry.entries() == ()


def test_empty_subset_is_a_no_op() -> None:
    service, _, executor = _service()
    assert service.refresh([]) == ()
    assert executor.calls == []  # type: ignore[union-attr]
    assert service.registry.entries() == ()


def test_refresh_all_engines_uses_deterministic_order() -> None:
    service, _, _ = _service()
    reports = service.refresh()
    assert tuple(report.engine for report in reports) == (HASHCAT, JOHN)


def test_refresh_accepts_sequences_and_deduplicates() -> None:
    service, _, _ = _service()
    assert len(service.refresh([JOHN, JOHN])) == 1
    ordered = service.refresh([JOHN, HASHCAT])
    assert tuple(r.engine for r in ordered) == (JOHN, HASHCAT)


# --------------------------------------------------------------------------- #
# UNKNOWN / ERROR semantics are preserved, never upgraded
# --------------------------------------------------------------------------- #


def test_truncated_probe_never_verifies() -> None:
    executor = RecordingExecutor({JOHN_OPERATION: (0, JOHN_GOOD_OUTPUT, "", True)})
    service, _, _ = _service(executor=executor)
    (report,) = service.refresh(JOHN)

    assert report.capability_status is CapabilityStatus.UNKNOWN
    assert report.truncated is True
    assert service.registry.require(JOHN).is_verified is False
    assert service.registry.require(JOHN).real_engine_compatible is False


def test_truncation_marker_tokens_never_verify() -> None:
    output = "Supported hash algorithms:\n  + . - _\n"
    executor = RecordingExecutor({JOHN_OPERATION: (0, output, "", False)})
    service, _, _ = _service(executor=executor)
    (report,) = service.refresh(JOHN)

    assert report.capability_status is CapabilityStatus.UNKNOWN
    assert service.registry.require(JOHN).is_verified is False


def test_oversized_probe_never_verifies() -> None:
    oversized = "Supported hash algorithms:\n" + ("raw-md5, " * 9000)
    assert len(oversized.encode("utf-8")) > 64 * 1024
    executor = RecordingExecutor({JOHN_OPERATION: (0, oversized, "", False)})
    service, _, _ = _service(executor=executor)
    (report,) = service.refresh(JOHN)

    # The runner clamps and/or the verified parser rejects oversized output:
    # either way evidence can never reach VERIFIED.
    assert report.capability_status is CapabilityStatus.UNKNOWN
    assert service.registry.require(JOHN).is_verified is False
    assert service.registry.require(JOHN).real_engine_compatible is False


def test_foreign_output_never_verifies() -> None:
    foreign = "Supported hash modes:\n  0 | MD5\n"
    executor = RecordingExecutor({JOHN_OPERATION: (0, foreign, "", False)})
    service, _, _ = _service(executor=executor)
    (report,) = service.refresh(JOHN)

    assert report.capability_status is CapabilityStatus.UNKNOWN
    assert report.reason == "missing_header"
    assert service.registry.require(JOHN).is_verified is False


def test_missing_header_never_verifies() -> None:
    executor = RecordingExecutor({JOHN_OPERATION: (0, "unrelated text\n", "", False)})
    service, _, _ = _service(executor=executor)
    (report,) = service.refresh(JOHN)

    assert report.capability_status is CapabilityStatus.UNKNOWN
    assert report.reason == "missing_header"


def test_failed_probe_is_error() -> None:
    executor = RecordingExecutor({JOHN_OPERATION: (1, "", "boom", False)})
    service, _, _ = _service(executor=executor)
    (report,) = service.refresh(JOHN)

    assert report.capability_status is CapabilityStatus.ERROR
    assert report.reason == "capability_probe_failed"
    entry = service.registry.require(JOHN)
    assert entry.is_verified is False
    assert entry.real_engine_compatible is False


def test_binary_not_found_never_verifies() -> None:
    which = RecordingWhich({JOHN: None, HASHCAT: None})
    service, _, executor = _service(which=which)
    (report,) = service.refresh(JOHN)

    assert report.discovered is False
    assert report.capability_status is CapabilityStatus.UNKNOWN
    assert report.reason == "binary_not_found"
    assert executor.calls == []  # type: ignore[union-attr]
    assert service.registry.require(JOHN).is_verified is False


def test_binary_vanishing_before_probe_never_verifies() -> None:
    class VanishingWhich:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, name: str) -> str | None:
            self.calls += 1
            return FAKE_BINARIES[name] if self.calls == 1 else None

    service, _, _ = _service(which=VanishingWhich())
    (report,) = service.refresh(JOHN)

    assert report.discovered is True
    assert report.capability_status is CapabilityStatus.UNKNOWN
    assert report.reason == "binary_missing"
    assert service.registry.require(JOHN).is_verified is False


def test_failed_refresh_revokes_stale_verified_evidence() -> None:
    registry = DiscoveryRegistry()
    service, _, _ = _service(registry)
    service.refresh(JOHN)
    assert registry.require(JOHN).is_verified is True

    failing = RecordingExecutor({JOHN_OPERATION: (1, "", "boom", False)})
    second, _, _ = _service(registry, executor=failing)
    (report,) = second.refresh(JOHN)

    assert report.capability_status is CapabilityStatus.ERROR
    assert registry.require(JOHN).is_verified is False
    assert registry.require(JOHN).real_engine_compatible is False


def test_hashcat_is_never_probed_for_capabilities() -> None:
    """Hashcat has no allowlisted mode-list operation - it must not be run."""

    service, which, executor = _service()
    (report,) = service.refresh(HASHCAT)

    assert executor.calls == []  # type: ignore[union-attr]
    assert which.calls == ["hashcat"]  # type: ignore[union-attr]
    assert report.discovered is True
    assert report.capability_status is CapabilityStatus.UNKNOWN
    assert report.reason == "formats_not_listed"
    entry = service.registry.require(HASHCAT)
    assert entry.is_verified is False
    assert entry.formats == ()
    assert entry.targets == ()


# --------------------------------------------------------------------------- #
# raw output is never stored
# --------------------------------------------------------------------------- #


def test_raw_probe_output_is_never_stored() -> None:
    # Marker travels on stderr of an otherwise successful probe.
    executor = RecordingExecutor(
        {JOHN_OPERATION: (0, JOHN_GOOD_OUTPUT, f"noise {RAW_MARKER}", False)}
    )
    service, _, _ = _service(executor=executor)
    (report,) = service.refresh(JOHN)

    assert report.capability_status is CapabilityStatus.VERIFIED
    assert RAW_MARKER not in _raw_scan(service.registry, report)

    # Marker travels on stdout: evidence can never be VERIFIED, still not stored.
    stdout_marked = RecordingExecutor({JOHN_OPERATION: (0, RAW_MARKER, "", False)})
    second, _, _ = _service(executor=stdout_marked)
    (second_report,) = second.refresh(JOHN)

    assert second_report.capability_status is not CapabilityStatus.VERIFIED
    assert RAW_MARKER not in _raw_scan(second.registry, second_report)


# --------------------------------------------------------------------------- #
# report shape / closed parameter surface
# --------------------------------------------------------------------------- #


def test_refresh_report_field_set_is_closed() -> None:
    assert set(RefreshReport.__dataclass_fields__) == REPORT_FIELDS
    assert not (REPORT_FIELDS & FORBIDDEN_REFRESH_FIELDS)


def test_refresh_report_as_dict_is_safe_and_json_round_trippable() -> None:
    service, _, _ = _service()
    (report,) = service.refresh(JOHN)
    payload = report.as_dict()

    assert set(payload) == REPORT_FIELDS
    assert payload["capability_status"] == "verified"
    assert not (set(payload) & FORBIDDEN_REFRESH_FIELDS)
    assert json.loads(json.dumps(payload)) == payload


def test_service_public_methods_accept_no_operation_or_command_input() -> None:
    public = {
        name: member
        for name, member in inspect.getmembers(
            CapabilityRefreshService, predicate=inspect.isfunction
        )
        if not name.startswith("_")
    }
    assert set(public) == {"refresh"}
    assert set(inspect.signature(CapabilityRefreshService.refresh).parameters) - {
        "self"
    } == {"engine_subset"}
    for function in public.values():
        params = set(inspect.signature(function).parameters) - {"self"}
        assert not (params & FORBIDDEN_REFRESH_FIELDS)
        assert not (
            params
            & {
                "operation",
                "operations",
                "command",
                "argv",
                "executable",
                "path",
                "shell",
            }
        )


def test_service_instance_exposes_no_command_surface_attributes() -> None:
    service, _, _ = _service()
    exposed = {name for name in dir(service) if not name.startswith("__")}
    assert not (exposed & FORBIDDEN_REFRESH_FIELDS)


def test_internal_capability_operations_are_allowlisted() -> None:
    assert set(CAPABILITY_OPERATIONS) == set(KNOWN_ENGINES)
    for engine, operations in CAPABILITY_OPERATIONS.items():
        for operation in operations:
            assert DISCOVERY_ALLOWLIST.allows(engine, operation), (engine, operation)
    assert CAPABILITY_OPERATIONS[HASHCAT] == ()


# --------------------------------------------------------------------------- #
# injected dependencies
# --------------------------------------------------------------------------- #


def test_service_uses_the_injected_which_and_executor() -> None:
    service, which, executor = _service()
    service.refresh(JOHN)

    # fixed binary name only, never a path from HTTP/user input
    assert set(which.calls) == {"john"}  # type: ignore[union-attr]
    argv = executor.calls[0]  # type: ignore[union-attr]
    assert len(argv) == 2
    assert argv[0] == FAKE_BINARIES[JOHN]
    assert argv[1] == JOHN_OPERATION
    assert DISCOVERY_ALLOWLIST.allows(JOHN, argv[1])


def test_service_never_calls_the_executor_when_which_reports_missing() -> None:
    class ExplodingExecutor:
        def __call__(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("executor must not run when the binary is missing")

    service = CapabilityRefreshService(
        DiscoveryRegistry(),
        which=RecordingWhich({JOHN: None}),
        executor=ExplodingExecutor(),
    )
    (report,) = service.refresh(JOHN)
    assert report.discovered is False
    assert report.capability_status is CapabilityStatus.UNKNOWN


def test_service_defaults_to_the_shared_registry_when_none_is_injected() -> None:
    service = CapabilityRefreshService()
    assert service.registry is default_discovery_registry()


# --------------------------------------------------------------------------- #
# static safety boundary
# --------------------------------------------------------------------------- #


def _imported_modules() -> set[str]:
    tree = ast.parse(SOURCE)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
    return modules


def test_service_module_never_imports_process_execution() -> None:
    modules = _imported_modules()
    for module in ("subprocess", "os", "shlex", "pty", "shutil"):
        assert module not in modules
    assert "app.engines.discovery.runner" in modules


def test_service_module_has_no_process_shell_or_eval_calls() -> None:
    tree = ast.parse(SOURCE)
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Attribute, ast.Name))
    }
    for forbidden in ("system", "popen", "Popen", "exec", "eval", "spawnv", "check_output"):
        assert forbidden not in called


def test_service_module_never_sets_a_shell_keyword() -> None:
    tree = ast.parse(SOURCE)
    keywords = {
        keyword.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
    }
    assert "shell" not in keywords


def test_service_module_never_references_the_hashcat_mode_list_operation() -> None:
    assert "--hash-info" not in SOURCE


def test_service_delegates_to_the_single_safe_runner() -> None:
    assert "run_safe_command(" in SOURCE
    assert "from app.engines.discovery.runner import" in SOURCE


def test_service_module_has_no_http_or_router_dependency() -> None:
    assert "fastapi" not in SOURCE
    assert "APIRouter" not in SOURCE
    assert "CapabilityRefreshService" in SOURCE

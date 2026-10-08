"""Phase 2A - engine discovery (availability only, no execution)."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app.engines.discovery import locate
from app.engines.discovery.errors import UnknownEngineError
from app.engines.discovery.models import (
    DISCOVERY_ENGINE_ORDER,
    ENGINE_BINARIES,
    HASHCAT,
    JOHN,
    KNOWN_ENGINES,
    CapabilityStatus,
    EngineCapabilities,
    EngineSnapshot,
    ProbeResult,
    sanitize_single_line,
)

LOCATE_SOURCE = Path(inspect.getfile(locate)).read_text(encoding="utf-8")


def _imported_modules(source: str) -> set[str]:
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_known_engine_set_is_closed() -> None:
    assert KNOWN_ENGINES == frozenset({JOHN, HASHCAT})


def test_discovery_engine_order_is_alphabetical() -> None:
    assert DISCOVERY_ENGINE_ORDER == (HASHCAT, JOHN)


def test_binary_table_is_immutable() -> None:
    with pytest.raises(TypeError):
        ENGINE_BINARIES["evil"] = "evil"  # type: ignore[index]


def test_binary_table_contains_no_paths() -> None:
    for binary in ENGINE_BINARIES.values():
        assert "/" not in binary
        assert "\\" not in binary
        assert not binary.startswith(".")


def test_binary_names_match_engine_identity() -> None:
    assert ENGINE_BINARIES[JOHN] == "john"
    assert ENGINE_BINARIES[HASHCAT] == "hashcat"


def test_engine_binary_name_known() -> None:
    assert locate.engine_binary_name(JOHN) == "john"


def test_engine_binary_name_unknown_raises() -> None:
    with pytest.raises(UnknownEngineError):
        locate.engine_binary_name("custom-tool")


def test_locate_returns_path_from_resolver() -> None:
    seen: list[str] = []

    def fake_which(binary: str) -> str | None:
        seen.append(binary)
        return f"/opt/tools/{binary}"

    assert locate.locate_engine_binary(JOHN, which=fake_which) == "/opt/tools/john"
    assert seen == ["john"]


def test_locate_unknown_engine_short_circuits() -> None:
    def explode(_binary: str) -> str | None:
        raise AssertionError("resolver must not be consulted for unknown engines")

    assert locate.locate_engine_binary("unknown", which=explode) is None


def test_discover_engines_marks_missing_engine() -> None:
    snapshots = locate.discover_engines(which=lambda _b: None)
    assert [s.engine for s in snapshots] == [HASHCAT, JOHN]
    assert all(s.discovered is False for s in snapshots)
    assert all(s.binary_found is False for s in snapshots)
    assert all(s.version is None for s in snapshots)


def test_discover_engines_marks_present_engine() -> None:
    snapshots = locate.discover_engines(which=lambda b: f"/usr/bin/{b}")
    assert all(s.discovered is True for s in snapshots)
    assert all(s.binary_found is True for s in snapshots)


def test_discover_engines_is_deterministic() -> None:
    first = locate.discover_engines(which=lambda _b: None)
    second = locate.discover_engines(which=lambda _b: None)
    assert first == second


def test_phase2a_module_has_no_subprocess_usage() -> None:
    modules = _imported_modules(LOCATE_SOURCE)
    assert "subprocess" not in modules
    assert "os" not in modules
    assert "app.engines.discovery.runner" not in modules


def test_phase2a_only_resolves_via_shutil_which() -> None:
    tree = ast.parse(LOCATE_SOURCE)
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "run_safe_command" not in calls
    assert "Popen" not in calls


# --------------------------------------------------------------------------- #
# shared models
# --------------------------------------------------------------------------- #


def test_capability_status_defaults_to_unknown_on_engine_capabilities() -> None:
    caps = EngineCapabilities(engine=JOHN)
    assert caps.capability_status is CapabilityStatus.UNKNOWN
    assert caps.is_verified is False


def test_capability_status_values() -> None:
    assert {s.value for s in CapabilityStatus} == {"verified", "unknown", "error"}


def test_engine_capabilities_is_immutable() -> None:
    caps = EngineCapabilities(engine=JOHN)
    with pytest.raises(Exception):
        caps.engine = HASHCAT  # type: ignore[misc]


def test_engine_capabilities_normalises_sequences() -> None:
    caps = EngineCapabilities(engine=JOHN, formats=["a", "b"], targets=("t",))
    assert caps.formats == ("a", "b")
    assert isinstance(caps.formats, tuple)


def test_engine_capabilities_metadata_is_read_only() -> None:
    caps = EngineCapabilities(engine=JOHN, metadata={"k": "v"})
    with pytest.raises(TypeError):
        caps.metadata["k"] = "other"  # type: ignore[index]


def test_listed_formats_hidden_unless_verified() -> None:
    unknown = EngineCapabilities(engine=JOHN, formats=("md5crypt",))
    assert unknown.listed_formats == ()
    verified = EngineCapabilities(
        engine=JOHN, capability_status=CapabilityStatus.VERIFIED, formats=("md5crypt",)
    )
    assert verified.listed_formats == ("md5crypt",)


def test_engine_capabilities_has_no_raw_output_fields() -> None:
    fields = {f.name for f in EngineCapabilities.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    assert not fields & {"stdout", "stderr", "raw_output", "probe_output", "output"}


def test_probe_result_is_the_only_raw_carrier() -> None:
    probe = ProbeResult(
        engine=JOHN,
        operation="--list=formats",
        ok=True,
        returncode=0,
        stdout="Supported hash algorithms:\n  md5crypt\n",
    )
    assert probe.has_output is True
    fields = {f.name for f in ProbeResult.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    assert fields & {"stdout", "stderr"}


def test_probe_result_defaults_to_no_output() -> None:
    probe = ProbeResult(engine=JOHN, operation="--version", ok=False, returncode=1)
    assert probe.has_output is False
    assert probe.stdout == ""


def test_engine_snapshot_sanitizes_version() -> None:
    snap = EngineSnapshot(engine=JOHN, discovered=True, version="  john  the \n ripper  ")
    assert snap.version == "john the ripper"


def test_engine_snapshot_version_is_single_line() -> None:
    snap = EngineSnapshot(engine=JOHN, version="line one\nline two")
    assert "\n" not in (snap.version or "")


def test_engine_snapshot_version_length_capped() -> None:
    snap = EngineSnapshot(engine=JOHN, version="x" * 500)
    assert len(snap.version or "") <= 64


def test_engine_snapshot_has_no_raw_output_fields() -> None:
    fields = {f.name for f in EngineSnapshot.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    assert not fields & {"stdout", "stderr", "raw_output", "probe_output"}


def test_sanitize_single_line_strips_control_characters() -> None:
    assert sanitize_single_line("a\x00b\x07c") == "abc"


def test_sanitize_single_line_collapses_whitespace() -> None:
    assert sanitize_single_line("  a \t b \n c  ") == "a b c"


def test_sanitize_single_line_respects_max_length() -> None:
    assert len(sanitize_single_line("y" * 200, max_length=10)) == 10


def test_probe_result_is_frozen() -> None:
    probe = ProbeResult(engine=JOHN, operation="--version", ok=True, returncode=0)
    with pytest.raises(Exception):
        probe.ok = False  # type: ignore[misc]

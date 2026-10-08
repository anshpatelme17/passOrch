"""run_safe_command - fail-closed validation, fixed argv, no shell."""

from __future__ import annotations

import ast
import inspect
import shutil
from pathlib import Path

import pytest

from app.engines.discovery import runner
from app.engines.discovery.allowlist import DISCOVERY_ALLOWLIST
from app.engines.discovery.errors import (
    EngineBinaryMissingError,
    OperationNotAllowedError,
    UnknownEngineError,
)
from app.engines.discovery.models import HASHCAT, JOHN
from app.engines.discovery.runner import clamp_output, run_safe_command

RUNNER_PATH = Path(inspect.getfile(runner))
RUNNER_SOURCE = RUNNER_PATH.read_text(encoding="utf-8")

FORBIDDEN_PARAMETERS = {
    "argv",
    "args",
    "cmd",
    "command",
    "commands",
    "executable",
    "path",
    "binary",
    "shell",
    "user_input",
}


class RecordingExecutor:
    """Test double: records every invocation, never touches a process."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        truncated: bool = False,
    ) -> None:
        self.calls: list[tuple[tuple[str, ...], float, int]] = []
        self._returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._truncated = truncated

    def __call__(self, argv, *, timeout, max_output_bytes):  # type: ignore[no-untyped-def]
        self.calls.append((tuple(argv), timeout, max_output_bytes))
        return self._returncode, self._stdout, self._stderr, self._truncated


def _which_ok(binary: str) -> str | None:
    return f"/tools/{binary}"


# --------------------------------------------------------------------------- #
# signature surface
# --------------------------------------------------------------------------- #


def test_signature_exposes_no_command_surface() -> None:
    signature = inspect.signature(run_safe_command)
    names = set(signature.parameters)
    assert not names & FORBIDDEN_PARAMETERS
    assert names == {
        "engine",
        "operation",
        "executor",
        "which",
        "timeout",
        "max_output_bytes",
    }


def test_signature_has_no_varargs_or_kwargs() -> None:
    for parameter in inspect.signature(run_safe_command).parameters.values():
        assert parameter.kind not in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        )


def test_only_engine_and_operation_are_positional() -> None:
    positional = [
        name
        for name, p in inspect.signature(run_safe_command).parameters.items()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    assert positional == ["engine", "operation"]


def test_source_never_enables_a_shell() -> None:
    assert "shell=True" not in RUNNER_SOURCE
    assert "os.system" not in RUNNER_SOURCE
    assert "popen" not in RUNNER_SOURCE
    assert "shlex" not in RUNNER_SOURCE


def test_source_shell_flag_is_explicitly_false() -> None:
    tree = ast.parse(RUNNER_SOURCE)
    flags = [
        kw.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "shell" and isinstance(kw.value, ast.Constant)
    ]
    assert flags == [False]


def test_no_user_supplied_executable_in_source() -> None:
    assert "D:\\passcrack" not in RUNNER_SOURCE
    assert "passcrack" not in RUNNER_SOURCE


# --------------------------------------------------------------------------- #
# validation ordering
# --------------------------------------------------------------------------- #


def test_unknown_engine_rejected_before_executor_runs() -> None:
    executor = RecordingExecutor()
    with pytest.raises(UnknownEngineError):
        run_safe_command("custom", "--version", executor=executor, which=_which_ok)
    assert executor.calls == []


def test_unknown_engine_rejected_before_resolver_runs() -> None:
    def exploding_resolver(_binary: str) -> str | None:
        raise AssertionError("resolver must not run for an unknown engine")

    with pytest.raises(UnknownEngineError):
        run_safe_command("custom", "--version", executor=RecordingExecutor(),
                         which=exploding_resolver)


def test_disallowed_operation_rejected_before_executor_runs() -> None:
    executor = RecordingExecutor()
    with pytest.raises(OperationNotAllowedError):
        run_safe_command(HASHCAT, "--hash-info", executor=executor, which=_which_ok)
    assert executor.calls == []


def test_disallowed_operation_rejected_before_resolver_runs() -> None:
    def exploding_resolver(_binary: str) -> str | None:
        raise AssertionError("resolver must not run for a blocked operation")

    with pytest.raises(OperationNotAllowedError):
        run_safe_command(JOHN, "--rules", executor=RecordingExecutor(),
                         which=exploding_resolver)


def test_engine_identity_is_checked_before_the_allowlist() -> None:
    with pytest.raises(UnknownEngineError):
        run_safe_command("custom", "--list=formats", executor=RecordingExecutor(),
                         which=_which_ok)


def test_bruteforce_style_operation_is_blocked() -> None:
    for operation in ("--stdout", "--session=x", "-o", "--incremental"):
        with pytest.raises(OperationNotAllowedError):
            run_safe_command(HASHCAT, operation, executor=RecordingExecutor(),
                             which=_which_ok)


def test_injected_executor_still_goes_through_validation() -> None:
    executor = RecordingExecutor()
    with pytest.raises(UnknownEngineError):
        run_safe_command("anything", "--version", executor=executor, which=_which_ok)
    assert executor.calls == []


# --------------------------------------------------------------------------- #
# fixed argv
# --------------------------------------------------------------------------- #


def test_argv_is_exactly_binary_and_operation() -> None:
    executor = RecordingExecutor(stdout="ok")
    result = run_safe_command(
        JOHN, "--list=formats", executor=executor, which=_which_ok
    )
    (argv, _timeout, _cap), = executor.calls
    assert argv == ("/tools/john", "--list=formats")
    assert len(argv) == 2
    assert result.binary == "/tools/john"


def test_resolver_receives_fixed_binary_name_only() -> None:
    seen: list[str] = []

    def tracking_resolver(binary: str) -> str | None:
        seen.append(binary)
        return f"/tools/{binary}"

    run_safe_command(HASHCAT, "--devices", executor=RecordingExecutor(),
                     which=tracking_resolver)
    assert seen == ["hashcat"]


def test_executor_receives_timeout_and_output_cap() -> None:
    executor = RecordingExecutor()
    run_safe_command(JOHN, "--version", executor=executor, which=_which_ok,
                     timeout=3.5, max_output_bytes=1024)
    _argv, timeout, cap = executor.calls[0]
    assert timeout == 3.5
    assert cap == 1024


def test_missing_binary_raises() -> None:
    with pytest.raises(EngineBinaryMissingError):
        run_safe_command(JOHN, "--version", executor=RecordingExecutor(),
                         which=lambda _b: None)


def test_executor_not_called_when_binary_is_missing() -> None:
    executor = RecordingExecutor()
    with pytest.raises(EngineBinaryMissingError):
        run_safe_command(HASHCAT, "--help", executor=executor, which=lambda _b: None)
    assert executor.calls == []


def test_default_resolver_is_used_when_not_injected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda b: f"/custom/{b}")
    executor = RecordingExecutor(stdout="john 1.9.0")
    result = run_safe_command(JOHN, "--version", executor=executor)
    assert result.binary == "/custom/john"
    assert result.stdout == "john 1.9.0"


# --------------------------------------------------------------------------- #
# results
# --------------------------------------------------------------------------- #


def test_successful_result_shape() -> None:
    executor = RecordingExecutor(returncode=0, stdout="john 1.9.0-jumbo\n")
    result = run_safe_command(JOHN, "--version", executor=executor, which=_which_ok)
    assert result.ok is True
    assert result.returncode == 0
    assert result.engine == JOHN
    assert result.operation == "--version"
    assert result.truncated is False


def test_nonzero_returncode_is_not_ok() -> None:
    executor = RecordingExecutor(returncode=3, stderr="boom")
    result = run_safe_command(JOHN, "--help", executor=executor, which=_which_ok)
    assert result.ok is False
    assert result.returncode == 3


def test_output_is_clamped_by_run_safe_command() -> None:
    executor = RecordingExecutor(stdout="z" * 5000)
    result = run_safe_command(JOHN, "--list=formats", executor=executor,
                              which=_which_ok, max_output_bytes=100)
    assert result.truncated is True
    assert len(result.stdout.encode("utf-8")) <= 100


def test_executor_reported_truncation_is_preserved() -> None:
    executor = RecordingExecutor(stdout="short", truncated=True)
    result = run_safe_command(JOHN, "--list=formats", executor=executor, which=_which_ok)
    assert result.truncated is True


def test_command_result_is_frozen() -> None:
    executor = RecordingExecutor()
    result = run_safe_command(JOHN, "--version", executor=executor, which=_which_ok)
    with pytest.raises(Exception):
        result.returncode = 9  # type: ignore[misc]


def test_allowlist_is_consulted_not_reimplemented() -> None:
    pairs = {
        (engine, operation)
        for engine, operations in DISCOVERY_ALLOWLIST.table().items()
        for operation in operations
    }
    assert pairs
    for engine, operation in pairs:
        assert DISCOVERY_ALLOWLIST.allows(engine, operation)


# --------------------------------------------------------------------------- #
# clamp_output
# --------------------------------------------------------------------------- #


def test_clamp_output_passthrough_when_within_limit() -> None:
    out, err, cut = clamp_output("hello", "warn", 100)
    assert (out, err, cut) == ("hello", "warn", False)


def test_clamp_output_exact_limit_is_not_truncated() -> None:
    out, _err, cut = clamp_output("12345", "", 5)
    assert out == "12345"
    assert cut is False


def test_clamp_output_over_limit_is_truncated() -> None:
    out, _err, cut = clamp_output("1234567890", "", 4)
    assert out == "1234"
    assert cut is True


def test_clamp_output_handles_multibyte_boundary() -> None:
    out, _err, cut = clamp_output("α" * 10, "", 5)
    assert cut is True
    assert len(out.encode("utf-8")) <= 5
    assert out.encode("utf-8", errors="strict") is not None


def test_clamp_output_clamps_stderr_independently() -> None:
    out, err, cut = clamp_output("ok", "e" * 50, 10)
    assert out == "ok"
    assert len(err) == 10
    assert cut is True


def test_clamp_output_non_positive_limit_yields_empty() -> None:
    out, err, cut = clamp_output("abc", "def", 0)
    assert (out, err, cut) == ("", "", True)


def test_default_executor_source_captures_and_bounds_output() -> None:
    assert "capture_output=True" in RUNNER_SOURCE
    assert "check=False" in RUNNER_SOURCE
    assert "timeout=timeout" in RUNNER_SOURCE

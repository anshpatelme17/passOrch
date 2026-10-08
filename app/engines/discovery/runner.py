"""Safe, allowlisted discovery command execution.

``run_safe_command`` is the *only* place in PassOrch that can ever reach
subprocess-related infrastructure, and it is fail closed by construction:

1. the engine identity must be a member of ``KNOWN_ENGINES`` - checked first,
   before anything else is consulted;
2. the operation must be present on the immutable discovery allowlist;
3. the executable is resolved from the fixed ``ENGINE_BINARIES`` table (never a
   user-supplied path, never user-supplied argv);
4. the argv is exactly ``(binary, operation)`` - no extra arguments, no shell,
   no string interpolation;
5. output is bounded and marked truncated when clamped.

Tests inject ``executor``/``which`` so that no real engine is ever executed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

from app.engines.discovery.allowlist import DISCOVERY_ALLOWLIST
from app.engines.discovery.errors import EngineBinaryMissingError, UnknownEngineError
from app.engines.discovery.models import ENGINE_BINARIES, KNOWN_ENGINES

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_COMMAND_OUTPUT_BYTES",
    "CommandExecutor",
    "CommandResult",
    "clamp_output",
    "run_safe_command",
]

DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_COMMAND_OUTPUT_BYTES = 64 * 1024


class CommandExecutor(Protocol):
    """Injected executor.  Always receives an exact two-element argv."""

    def __call__(
        self, argv: Sequence[str], *, timeout: float, max_output_bytes: int
    ) -> tuple[int, str, str, bool]: ...


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Structured result of one allowlisted discovery operation."""

    engine: str
    operation: str
    binary: str
    returncode: int
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def clamp_output(stdout: str, stderr: str, max_output_bytes: int) -> tuple[str, str, bool]:
    """Bound both streams to ``max_output_bytes`` and report truncation."""

    if max_output_bytes <= 0:
        return "", "", True
    out, out_cut = _clamp_one(stdout, max_output_bytes)
    err, err_cut = _clamp_one(stderr, max_output_bytes)
    return out, err, out_cut or err_cut


def _clamp_one(value: str, limit: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return value, False
    decoded = encoded[:limit].decode("utf-8", errors="replace")
    while len(decoded.encode("utf-8")) > limit and decoded:
        decoded = decoded[:-1]
    return decoded, True


def run_safe_command(
    engine: str,
    operation: str,
    *,
    executor: CommandExecutor | None = None,
    which: Callable[[str], str | None] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_output_bytes: int = MAX_COMMAND_OUTPUT_BYTES,
) -> CommandResult:
    """Run one allowlisted discovery operation for a known engine.

    Raises ``UnknownEngineError`` before any discovery infrastructure is
    touched when the engine identity is not known, and
    ``OperationNotAllowedError`` when the operation is not allowlisted.
    """

    if not isinstance(engine, str) or engine not in KNOWN_ENGINES:
        raise UnknownEngineError(engine)

    DISCOVERY_ALLOWLIST.require(engine, operation)

    binary_name = ENGINE_BINARIES[engine]
    resolver = which if which is not None else _default_which
    resolved = resolver(binary_name)
    if not resolved:
        raise EngineBinaryMissingError(engine)

    argv = (resolved, operation)
    run = executor if executor is not None else _default_executor
    returncode, raw_out, raw_err, reported_truncation = run(
        argv, timeout=timeout, max_output_bytes=max_output_bytes
    )
    stdout, stderr, clamped = clamp_output(raw_out, raw_err, max_output_bytes)

    return CommandResult(
        engine=engine,
        operation=operation,
        binary=resolved,
        returncode=int(returncode),
        stdout=stdout,
        stderr=stderr,
        truncated=bool(reported_truncation) or clamped,
    )


def _default_which(binary: str) -> str | None:
    import shutil

    return shutil.which(binary)


def _default_executor(
    argv: Sequence[str], *, timeout: float, max_output_bytes: int
) -> tuple[int, str, str, bool]:
    """Real subprocess wiring.

    Fixed properties: ``shell=False``, captured output, hard timeout and a
    bounded read.  ``argv`` is always the two-element tuple produced by
    :func:`run_safe_command`.
    """

    import subprocess

    try:
        completed = subprocess.run(
            list(argv),
            shell=False,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "", "discovery command timed out", True
    except OSError:
        return 127, "", "engine binary could not be executed", False

    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace")
    stdout, stderr, clamped = clamp_output(stdout, stderr, max_output_bytes)
    return int(completed.returncode), stdout, stderr, clamped

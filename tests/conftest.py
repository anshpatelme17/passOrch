"""PassOrch test suite.

Every test in this suite is a *reconstruction* test: it asserts the safety
boundaries of the controlled PassOrch reconstruction (discovery + verified
capability parsing + capability-aware selection + simulation only).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(ROOT))

#: Environment variable that may point at a specific Node.js executable.
NODE_ENV_VAR = "PASSORCH_NODE"

#: Verified Node.js installations, consulted only when Node is not on PATH.
_KNOWN_NODE_PATHS = (
    r"C:\Users\anshp\AppData\Local\hermes\tools\node-26.7.0-win32-x64\node.exe",
    r"C:\Program Files\Autodesk\webdeploy\production\b230f9201b63ad778a1857c78be04f6b7a3e465c\NODEJS\node.exe",
)

#: Glob for the JavaScript suites executed by the authorized runner below.
JS_TEST_GLOB = "frontend-tests/*.test.mjs"


def find_node() -> tuple[str | None, str]:
    """Resolve a usable Node.js executable.

    Order: ``$PASSORCH_NODE``, then ``node`` on PATH, then the verified
    installation paths. A candidate is only accepted once it answers
    ``--version``, so a stale or broken path can never reach the runner.

    Returns ``(executable_or_None, how_it_was_found)``.
    """

    candidates: list[tuple[str, str | None]] = [
        (NODE_ENV_VAR, os.environ.get(NODE_ENV_VAR)),
        ("PATH", shutil.which("node")),
    ]
    for index, path in enumerate(_KNOWN_NODE_PATHS):
        candidates.append((f"known#{index + 1}", path))

    for how, candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        candidate_path = Path(candidate)
        try:
            proc = subprocess.run(
                [str(candidate_path), "--version"],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        version = (proc.stdout or "").strip()
        if proc.returncode == 0 and version.startswith("v"):
            return str(candidate_path), f"{how} ({version})"
    return None, "not found"


def node_js_test_files() -> list[str]:
    """The individual JavaScript suite files, relative to the repo root.

    The glob is expanded here, in Python, rather than handed to Node as a
    pattern: ``node --test`` only understands glob arguments from Node 21
    onward, while ``package.json`` declares ``engines.node >=20``. The runner
    below uses ``shell=False``, so no OS shell expands it either.
    """

    files = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "frontend-tests").glob("*.test.mjs")
    )
    if not files:
        raise AssertionError(f"no JavaScript suites matched {JS_TEST_GLOB}")
    return files


def run_node_js_tests(node: str) -> subprocess.CompletedProcess[str]:
    """Run the JavaScript suites with the one authorised Node invocation.

    Deliberately narrow: the executable must be an already-resolved Node binary
    and the argument list is fixed. No shell string is built, so this cannot be
    turned into an arbitrary command. Only tests explicitly marked
    ``allow_subprocess`` may call it.
    """

    if Path(node).name.lower() not in {"node.exe", "node"}:
        raise AssertionError(f"refusing to run a non-Node executable: {node}")
    if not Path(node).is_file():
        raise AssertionError(f"Node executable does not exist: {node}")
    return subprocess.run(
        [node, "--test", *node_js_test_files()],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=600,
        shell=False,
        check=False,
    )


@pytest.fixture(autouse=True)
def _no_subprocess_leak(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Fail loudly if any test accidentally reaches for a real subprocess.

    Discovery code must always run through an injected executor in tests, so
    ``subprocess.run`` / ``subprocess.Popen`` / ``os.system`` must never be
    called by the suite.

    The JavaScript integration tests genuinely need to spawn the already
    installed Node.js; they opt in with the ``allow_subprocess`` marker, which
    disables this block for those tests only. Everything else stays blocked.
    """

    if "allow_subprocess" in (request.keywords or {}):
        return

    def _blocked(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("subprocess execution is forbidden in this test suite")

    monkeypatch.setattr(subprocess, "run", _blocked, raising=False)
    monkeypatch.setattr(subprocess, "Popen", _blocked, raising=False)
    monkeypatch.setattr(subprocess, "call", _blocked, raising=False)
    monkeypatch.setattr(subprocess, "check_output", _blocked, raising=False)

    monkeypatch.setattr(os, "system", _blocked, raising=False)
    monkeypatch.setattr(os, "popen", _blocked, raising=False)
    monkeypatch.setattr(os, "execv", _blocked, raising=False)
    monkeypatch.setattr(os, "execve", _blocked, raising=False)
    monkeypatch.setattr(os, "spawnv", _blocked, raising=False)


@pytest.fixture
def sample_john_formats_output() -> str:
    """A canned, well-formed ``john --list=formats`` probe payload."""

    return (
        "Supported hash algorithms:\n"
        "  md5crypt, sha256crypt, sha512crypt, bcrypt,\n"
        "  ntlm, raw-md5, raw-sha1, raw-sha256,\n"
        "  zip, pkzip, pdf, office2007, office2010, office2013\n"
    )
"""PassOrch test suite.

Every test in this suite is a *reconstruction* test: it asserts the safety
boundaries of the controlled PassOrch reconstruction (discovery + verified
capability parsing + capability-aware selection + simulation only).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

if str(ROOT) not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _no_subprocess_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if any test accidentally reaches for a real subprocess.

    Discovery code must always run through an injected executor in tests, so
    ``subprocess.run`` / ``subprocess.Popen`` / ``os.system`` must never be
    called by the suite.
    """

    import subprocess

    def _blocked(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("subprocess execution is forbidden in this test suite")

    monkeypatch.setattr(subprocess, "run", _blocked, raising=False)
    monkeypatch.setattr(subprocess, "Popen", _blocked, raising=False)
    monkeypatch.setattr(subprocess, "call", _blocked, raising=False)
    monkeypatch.setattr(subprocess, "check_output", _blocked, raising=False)

    import os

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

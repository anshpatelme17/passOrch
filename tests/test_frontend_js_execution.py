"""Phase 4C.2 - JavaScript execution integration.

Runs the dependency-free Node.js suite in ``frontend-tests/`` against the real
frontend modules. The suite executes the actual application JavaScript through a
small DOM shim and a scripted ``fetch`` - it is not a set of static source
assertions.

The JavaScript tests here only ever run the resolved Node binary with a fixed
argument list, under ``shell=False``. That is authorised per-test with the
``allow_subprocess`` marker; every other test in this suite remains blocked by
the autouse fixture in ``conftest.py``.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.conftest import ROOT, find_node, run_node_js_tests

FRONTEND_TESTS = ROOT / "frontend-tests"

#: Suites the harness is expected to provide.
EXPECTED_SUITES = {
    "api.test.mjs",
    "dashboard.test.mjs",
    "instances.test.mjs",
    "jobs.test.mjs",
    "plan.test.mjs",
    "safety.test.mjs",
    "targets.test.mjs",
    "views.test.mjs",
    "_fixtures.test.mjs",
    "_regression.test.mjs",
}


def _node_or_skip() -> tuple[str, str]:
    """The resolved Node binary, or a skip when none exists at all."""

    node, how = find_node()
    if node is None:
        pytest.skip(f"Node.js is not available ({how}); cannot execute the frontend suite")
    return node, how


def test_frontend_suites_are_present() -> None:
    assert FRONTEND_TESTS.is_dir(), "frontend-tests/ must exist"
    found = {p.name for p in FRONTEND_TESTS.glob("*.test.mjs")}
    missing = {"api.test.mjs", "safety.test.mjs", "jobs.test.mjs", "plan.test.mjs"} - found
    assert not missing, f"missing JavaScript suites: {sorted(missing)}"


def test_package_json_declares_es_modules() -> None:
    """Without ``type: module`` Node would parse the real ``.js`` files as CJS."""

    package = ROOT / "package.json"
    assert package.is_file(), "the root package.json must declare the ES module type"
    payload = package.read_text(encoding="utf-8")
    assert '"type": "module"' in payload
    assert "test:js" in payload


@pytest.mark.allow_subprocess
def test_javascript_suite_executes_and_passes() -> None:
    """The real frontend JavaScript actually runs and passes."""

    node, how = _node_or_skip()
    proc = run_node_js_tests(node)
    assert proc.returncode == 0, (
        f"the JavaScript suite failed using {how}.\n"
        f"exit code: {proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}"
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    # "ℹ fail 0" is the default spec reporter; "# fail 0" is the TAP reporter
    assert re.search(r"fail 0\b", combined), combined[-2000:]
    assert re.search(r"pass (\d+)", combined) is not None, combined[-2000:]


@pytest.mark.allow_subprocess
def test_javascript_suite_reports_nonzero_test_count() -> None:
    """A real execution must report tests; a skipped/aborted run reports none."""

    node, how = _node_or_skip()
    proc = subprocess.run(
        [node, "--test", "--test-reporter=tap", "frontend-tests/*.test.mjs"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=600,
        shell=False,
        check=False,
    )
    out = proc.stdout or ""
    counts: dict[str, int] = {}
    for line in out.splitlines():
        if line.startswith("# ") and line[2:].split(" ", 1)[0] in {"pass", "fail", "tests"}:
            _, key, value = line.split(" ", 2)
            counts[key] = int(value)
    assert proc.returncode == 0, f"suite failed using {how}\n{out[-3000:]}"
    assert counts.get("fail") == 0, out[-3000:]
    assert counts.get("pass", 0) >= 180, f"suspiciously few tests ran using {how}: {counts}"
    assert counts.get("tests", 0) >= counts.get("pass", 0)


@pytest.mark.allow_subprocess
def test_node_discovery_prefers_the_environment_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests import conftest

    node, how = conftest.find_node()
    assert node is not None, f"no Node.js could be resolved ({how})"
    assert Path(node).name.lower() in {"node.exe", "node"}

    # an explicit, valid path is honoured verbatim
    monkeypatch.setenv(conftest.NODE_ENV_VAR, node)
    again, how_again = conftest.find_node()
    assert again == node
    assert how_again.startswith(conftest.NODE_ENV_VAR)
    monkeypatch.delenv(conftest.NODE_ENV_VAR, raising=False)


@pytest.mark.allow_subprocess
def test_node_discovery_rejects_a_broken_explicit_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests import conftest

    monkeypatch.setenv(conftest.NODE_ENV_VAR, str(ROOT / "no-such-node.exe"))
    node, how = conftest.find_node()
    # a bogus candidate is skipped, and discovery falls through
    assert node is not None, how
    assert conftest.NODE_ENV_VAR not in how, how
    monkeypatch.delenv(conftest.NODE_ENV_VAR, raising=False)


def test_node_discovery_reports_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests import conftest

    real_paths = conftest._KNOWN_NODE_PATHS
    monkeypatch.setattr(conftest, "_KNOWN_NODE_PATHS", (str(ROOT / "nope"),))
    monkeypatch.setattr(conftest.shutil, "which", lambda _n: None)
    monkeypatch.setenv(conftest.NODE_ENV_VAR, str(ROOT / "nope"))
    node, how = conftest.find_node()
    assert node is None, "a nonexistent Node must not be reported as usable"
    assert how == "not found"
    monkeypatch.setattr(conftest, "_KNOWN_NODE_PATHS", real_paths)


@pytest.mark.allow_subprocess
def test_run_node_js_tests_refuses_non_node_executable() -> None:
    """The authorised runner only ever invokes a real Node binary.

    These assertions run before any spawn, so no subprocess is needed here.
    """

    with pytest.raises(AssertionError, match="refusing to run a non-Node executable"):
        run_node_js_tests(str(Path("python.exe")))
    with pytest.raises(AssertionError, match="refusing to run a non-Node executable"):
        run_node_js_tests(str(ROOT / "nope-node.exe"))
    # a path named like Node but absent never reaches the OS
    with pytest.raises(AssertionError, match="does not exist"):
        run_node_js_tests(str(ROOT / "sub/node.exe"))


def test_unrelated_subprocess_use_is_still_blocked() -> None:
    """Regression: the block must stay in force for ordinary tests."""

    import subprocess

    with pytest.raises(AssertionError, match="subprocess execution is forbidden"):
        subprocess.run(["echo", "should never run"])

    import os

    with pytest.raises(AssertionError, match="subprocess execution is forbidden"):
        os.system("echo should never run")
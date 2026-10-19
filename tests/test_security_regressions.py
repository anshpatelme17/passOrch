"""Phase 10B - whole-application security regression sweep.

Complements the phase-specific security suites (8D persistence, 9A auth,
8E rate limiting, 9B configuration, frontend safety) with cross-cutting
scans that a new module could silently violate:

* the only subprocess wiring in the application is the discovery runner,
  and it is hard-wired ``shell=False``; ``shell=True``, ``os.system``,
  ``os.popen``, ``pty`` and ``multiprocessing`` are banned everywhere;
* no dynamic code execution (``eval`` / ``exec`` / ``compile`` /
  ``__import__``) anywhere in ``app/``;
* ``X-Forwarded-For`` is read in exactly one place - the middleware's
  client-key resolution in ``app/api/app.py`` behind the Phase 12B
  opt-in trust anchor; every other module under ``app/`` is free of
  forwarded-header reads, and the pure resolver in ``rate_limit.py``
  never touches a request;
* the configured bearer token never lands on ``application.state`` (only
  the ``auth_enabled`` boolean does), and the bearer comparison in
  ``app/api/auth.py`` is the constant-time ``secrets.compare_digest``;
* THREAT_MODEL.md exists, is linked from README, and states the residual
risks the deployment docs rely on.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.api.app import create_app

_APP_MODULES = sorted(Path("app").rglob("*.py"))
_RUNNER = Path("app/engines/discovery/runner.py")
_AUTH_MODULE = Path("app/api/auth.py")
_APP_FACTORY_MODULE = Path("app/api/app.py")
_TOKEN = "regression-probe-token-value"
_DYNAMIC_CALLS = {"eval", "exec", "compile", "__import__"}
_PROCESS_MODULES = {"pty", "multiprocessing"}


def _trees() -> list[tuple[Path, ast.AST]]:
    return [(module, ast.parse(module.read_text(encoding="utf-8"))) for module in _APP_MODULES]


def _imports(tree: ast.AST) -> set[str]:
    return {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def test_subprocess_is_confined_to_the_discovery_runner_with_shell_disabled() -> None:
    for module, tree in _trees():
        imported = _imports(tree)
        assert "subprocess" not in imported or module == _RUNNER, module
        assert imported.isdisjoint(_PROCESS_MODULES), module
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            assert _call_name(node) not in {"system", "popen"}, module
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
            ):
                attribute = node.func.attr
                assert not (
                    attribute in {"system", "popen"}
                    or attribute.startswith(("spawn", "exec"))
                ), f"os.{attribute} in {module}"
            for keyword in node.keywords:
                if keyword.arg != "shell":
                    continue
                assert (
                    isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is False
                ), f"shell= must be the constant False in {module}"

    runner_tree = ast.parse(_RUNNER.read_text(encoding="utf-8"))
    run_calls = [
        node
        for node in ast.walk(runner_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    ]
    assert run_calls, "the runner must keep its single subprocess.run call"
    for call in run_calls:
        keywords = {keyword.arg: keyword.value for keyword in call.keywords}
        shell = keywords.get("shell")
        assert isinstance(shell, ast.Constant) and shell.value is False


def test_no_dynamic_code_execution_anywhere_in_the_app() -> None:
    for module, tree in _trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # Only the bare builtins count: ``re.compile`` and friends are
            # attribute calls and stay legal.
            if not isinstance(node.func, ast.Name):
                continue
            assert node.func.id not in _DYNAMIC_CALLS, (
                f"{node.func.id} call in {module}"
            )


def test_forwarded_headers_are_read_only_for_trusted_proxy_keying() -> None:
    """Phase 12B: the socket peer stays the default key.  ``X-Forwarded-For``
    may be read in exactly one place - the client-key resolution call in
    the ``app/api/app.py`` middleware - and only when an operator pinned
    the exact-IP trust anchor.  A second read anywhere else (or any read
    in the pure resolver, auth, or a route) fails this scan, so no new
    module can silently start trusting forwarded headers."""

    reads: list[Path] = []
    for module, tree in _trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) in {"get", "getitem"}:
                arguments = list(node.args)
                if arguments and isinstance(arguments[0], ast.Constant):
                    value = arguments[0].value
                    if isinstance(value, str) and "forwarded" in value.lower():
                        reads.append(module)
            if isinstance(node, ast.Subscript):
                slice_node = node.slice
                if isinstance(slice_node, ast.Constant) and isinstance(
                    slice_node.value, str
                ):
                    assert "forwarded" not in slice_node.value.lower(), module

    assert reads == [_APP_FACTORY_MODULE], sorted({str(path) for path in reads})
    factory_text = _APP_FACTORY_MODULE.read_text(encoding="utf-8")
    assert factory_text.count('"x-forwarded-for"') == 1


def test_bearer_token_never_lands_on_application_state() -> None:
    application = create_app(api_token=_TOKEN)
    assert application.state.auth_enabled is True
    serialized = repr(vars(application.state))
    assert _TOKEN not in serialized


def test_bearer_comparison_is_constant_time() -> None:
    tree = ast.parse(_AUTH_MODULE.read_text(encoding="utf-8"))
    comparisons = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _call_name(node) == "compare_digest"
    ]
    assert comparisons, "auth must compare tokens with secrets.compare_digest"


def test_threat_model_exists_is_linked_and_states_residual_risks() -> None:
    threat_model = Path("THREAT_MODEL.md").read_text(encoding="utf-8")
    for phrase in (
        "Residual risks",
        "bearer",
        "X-Forwarded-For",
        "SQLite",
        "simulation",
        "rate limit",
    ):
        assert phrase.lower() in threat_model.lower(), phrase

    readme = Path("README.md").read_text(encoding="utf-8")
    assert "THREAT_MODEL.md" in readme

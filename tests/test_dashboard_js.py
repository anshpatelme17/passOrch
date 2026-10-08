"""Phase 2D - read-only dashboard script (static safety tests).

The dashboard is plain JavaScript executed in a browser; this suite asserts
its safety boundary statically (no command construction, no unsafe DOM sinks,
read-only API usage).
"""

from __future__ import annotations

import re
from pathlib import Path

DASHBOARD = Path("frontend/js/dashboard.js")
SOURCE = DASHBOARD.read_text(encoding="utf-8")

#: Code with comments stripped: assertions only apply to executable code.
CODE = re.sub(r"/\*.*?\*/", "", SOURCE, flags=re.DOTALL)
CODE = re.sub(r"//[^\n]*", "", CODE)

#: Absolute paths plus the path fragments composed with them.
ALLOWED_PATHS = ("/api/targets", "/health", "/", "/compatibility/plan")


def test_dashboard_file_exists() -> None:
    assert DASHBOARD.is_file()
    assert SOURCE.strip()


def test_dashboard_never_uses_unsafe_dom_or_eval_sinks() -> None:
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert sink not in CODE


def test_dashboard_uses_text_content_only() -> None:
    assert "textContent" in CODE
    assert "createElement" in CODE
    assert "appendChild" in CODE


def test_dashboard_fetches_only_allowlisted_api_paths() -> None:
    strings = re.findall(r'"([^"]*)"', SOURCE)
    for value in strings:
        if value.startswith("/"):
            assert value in ALLOWED_PATHS, value
        assert "://" not in value
        assert not value.startswith("//")
    assert CODE.count("fetch(") == 1


def test_dashboard_plan_requests_are_posted_as_structured_json() -> None:
    assert 'method: "POST"' in CODE
    assert '"Content-Type": "application/json"' in CODE
    assert "JSON.stringify" in CODE
    body_keys = set(re.findall(r"^\s+(\w+): typeof", CODE, flags=re.MULTILINE))
    assert body_keys <= {"mode", "reference"}


def test_dashboard_never_mentions_a_command_surface() -> None:
    lowered = CODE.lower()
    for word in ("command", "argv", "subprocess", "shell", "executable", "binary"):
        assert word not in lowered, word


def test_dashboard_module_exposes_only_read_helpers() -> None:
    exposed = set(
        re.findall(r"^\s+(\w+): (?:init|loadTargets|previewPlan|renderPlan|"
                   r"renderTargets|planUrl|HEALTH_URL|TARGETS_URL)\s*,?\s*$",
                   CODE, flags=re.MULTILINE)
    )
    assert exposed == {
        "init",
        "loadTargets",
        "previewPlan",
        "renderPlan",
        "renderTargets",
        "planUrl",
        "HEALTH_URL",
        "TARGETS_URL",
    }


def test_dashboard_target_identifiers_are_constrained() -> None:
    assert "/^[a-z0-9-]+$/" in CODE


def test_dashboard_declares_a_security_boundary() -> None:
    assert "SECURITY BOUNDARY" in SOURCE
    assert "textContent" in SOURCE

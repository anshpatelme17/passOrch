"""Phase 2D - execution policy tests.

The policy is the single authorisation gate: every denial reason and its
evaluation order is asserted here, plus the guarantee that no denied request
can ever carry adapter/plan information.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app.engines.adapter import (
    FORBIDDEN_REQUEST_FIELDS,
    MOCK_JOHN,
    ExecutionMode,
    ExecutionRequest,
)
from app.engines.adapter_registry import EngineAdapterRegistry, default_adapter_registry
from app.engines.execution_policy import (
    REASON_AUTHORIZED,
    REASON_CAPABILITY_NOT_VERIFIED,
    REASON_INVALID_REQUEST,
    REASON_NON_SIMULATION,
    REASON_SELECTION_NOT_SELECTED,
    REASON_UNKNOWN_ENGINE,
    REASON_UNSUPPORTED_MODE,
    AuthorizationStatus,
    ExecutionPolicy,
    PolicyDecision,
    default_execution_policy,
    mode_label,
)
from app.engines.execution_policy import ExecutionPolicy as PolicyClass

SOURCE = Path(inspect.getsourcefile(ExecutionPolicy) or "").read_text(encoding="utf-8")

VALID = {
    "target": "pdf-document",
    "engine": MOCK_JOHN,
    "mode": "simulated",
    "selection_status": "selected",
    "capability_status": "verified",
    "selected_engine": "john",
    "matched_formats": ("pdf",),
    "reference": "",
}


def _request(**overrides: object) -> ExecutionRequest:
    payload = dict(VALID)
    payload.update(overrides)
    return ExecutionRequest(**payload)  # type: ignore[arg-type]


def _authorize(**overrides: object) -> PolicyDecision:
    return ExecutionPolicy().authorize(_request(**overrides))


# --------------------------------------------------------------------------- #
# authorisation (happy path)
# --------------------------------------------------------------------------- #


def test_valid_simulation_request_is_authorized() -> None:
    decision = _authorize()
    assert decision.status is AuthorizationStatus.AUTHORIZED
    assert decision.authorized is True
    assert decision.denied is False
    assert decision.reason == REASON_AUTHORIZED
    assert decision.adapter == MOCK_JOHN
    assert decision.mode == "simulated"
    assert decision.problems == ()


def test_policy_exposes_its_registry() -> None:
    registry = EngineAdapterRegistry()
    policy = ExecutionPolicy(registry)
    assert policy.registry is registry
    assert ExecutionPolicy().registry is default_adapter_registry()


def test_default_execution_policy_is_the_process_singleton() -> None:
    assert default_execution_policy() is default_execution_policy()
    assert isinstance(default_execution_policy(), PolicyClass)


# --------------------------------------------------------------------------- #
# denial reasons
# --------------------------------------------------------------------------- #


def test_non_request_object_is_denied_invalid_request() -> None:
    decision = ExecutionPolicy().authorize({"target": "pdf-document"})
    assert decision.reason == REASON_INVALID_REQUEST
    assert decision.problems == ("request_type_invalid",)
    assert decision.adapter == ""


def test_blank_target_is_denied_invalid_request() -> None:
    decision = _authorize(target="   ")
    assert decision.reason == REASON_INVALID_REQUEST
    assert decision.problems == ("target_missing",)


def test_unmapped_target_is_denied_invalid_request() -> None:
    decision = _authorize(target="unknown-target")
    assert decision.reason == REASON_INVALID_REQUEST
    assert "target_unusable" in decision.problems


def test_non_string_selection_status_is_denied_invalid_request() -> None:
    decision = _authorize(selection_status=7)  # type: ignore[arg-type]
    assert decision.reason == REASON_INVALID_REQUEST
    assert decision.problems == ("selection_status_invalid",)


def test_unknown_mode_value_is_denied_unsupported_execution_mode() -> None:
    decision = _authorize(mode="banana")
    assert decision.reason == REASON_UNSUPPORTED_MODE
    assert decision.mode == "banana"


@pytest.mark.parametrize("mode", ["real", ExecutionMode.REAL])
def test_real_mode_is_denied_non_simulation_request(mode: object) -> None:
    decision = _authorize(mode=mode)
    assert decision.reason == REASON_NON_SIMULATION
    assert decision.mode == "real"
    assert decision.adapter == ""
    assert decision.authorized is False


@pytest.mark.parametrize("status", ["unknown", "error", "", "UNKNOWN"])
def test_unverified_capability_is_denied_capability_not_verified(status: str) -> None:
    decision = _authorize(capability_status=status)
    assert decision.reason == REASON_CAPABILITY_NOT_VERIFIED


@pytest.mark.parametrize(
    "status", ["no_candidate", "no_verified_capability", "unsupported_target", ""]
)
def test_non_selected_status_is_denied_selection_not_selected(status: str) -> None:
    decision = _authorize(selection_status=status)
    assert decision.reason == REASON_SELECTION_NOT_SELECTED


@pytest.mark.parametrize("engine", ["john", "hashcat", "john-the-ripper", "unknown"])
def test_engine_without_registered_adapter_is_denied_unknown_engine(
    engine: str,
) -> None:
    decision = _authorize(engine=engine)
    assert decision.reason == REASON_UNKNOWN_ENGINE
    assert decision.adapter == ""


def test_empty_engine_reaches_unknown_engine_not_invalid_request() -> None:
    """An empty engine is a consequence of no selection, not a structural flaw."""

    decision = _authorize(engine="", selected_engine="")
    assert decision.reason == REASON_UNKNOWN_ENGINE


def test_unknown_engine_is_denied_when_the_registry_is_empty() -> None:
    policy = ExecutionPolicy(EngineAdapterRegistry())
    decision = policy.authorize(_request())
    assert decision.reason == REASON_UNKNOWN_ENGINE


# --------------------------------------------------------------------------- #
# evaluation order
# --------------------------------------------------------------------------- #


def test_real_mode_outranks_unverified_capability() -> None:
    decision = _authorize(mode="real", capability_status="unknown")
    assert decision.reason == REASON_NON_SIMULATION


def test_unusable_target_outranks_unknown_mode() -> None:
    decision = _authorize(target="unknown-target", mode="banana")
    assert decision.reason == REASON_INVALID_REQUEST


def test_capability_outranks_selection() -> None:
    decision = _authorize(capability_status="unknown", selection_status="no_candidate")
    assert decision.reason == REASON_CAPABILITY_NOT_VERIFIED


def test_selection_outranks_unknown_engine() -> None:
    decision = _authorize(selection_status="no_candidate", engine="john")
    assert decision.reason == REASON_SELECTION_NOT_SELECTED


def test_unsupported_mode_outranks_non_simulation_check() -> None:
    decision = _authorize(mode="banana", capability_status="unknown")
    assert decision.reason == REASON_UNSUPPORTED_MODE


# --------------------------------------------------------------------------- #
# decision payload
# --------------------------------------------------------------------------- #


def test_denied_decision_never_carries_an_adapter() -> None:
    for decision in (
        _authorize(mode="real"),
        _authorize(capability_status="unknown"),
        _authorize(engine="hashcat"),
        _authorize(target="unknown-target"),
    ):
        assert decision.status is AuthorizationStatus.DENIED
        assert decision.adapter == ""


def test_decision_as_dict_is_structured_and_json_safe() -> None:
    payload = _authorize().as_dict()
    assert payload == {
        "authorized": True,
        "status": "authorized",
        "reason": REASON_AUTHORIZED,
        "detail": "",
        "problems": [],
        "adapter": MOCK_JOHN,
        "mode": "simulated",
    }
    assert set(payload) - FORBIDDEN_REQUEST_FIELDS == set(payload)


def test_denied_decision_as_dict_is_structured() -> None:
    payload = _authorize(mode="real").as_dict()
    assert payload["authorized"] is False
    assert payload["status"] == "denied"
    assert payload["reason"] == REASON_NON_SIMULATION
    assert payload["problems"] == []


def test_mode_label_handles_enum_str_and_other_values() -> None:
    assert mode_label(ExecutionMode.SIMULATED) == "simulated"
    assert mode_label("real") == "real"
    assert mode_label(None) == "None"


def test_decision_problems_are_always_a_tuple() -> None:
    decision = PolicyDecision.deny(REASON_INVALID_REQUEST, problems=["a", "b"])
    assert decision.problems == ("a", "b")


# --------------------------------------------------------------------------- #
# static safety boundary
# --------------------------------------------------------------------------- #


def test_policy_module_never_imports_process_execution() -> None:
    tree = ast.parse(SOURCE)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "subprocess" not in imported
    assert "os" not in imported
    assert "shlex" not in imported


def test_policy_module_has_no_process_or_shell_calls() -> None:
    tree = ast.parse(SOURCE)
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Attribute, ast.Name))
    }
    for forbidden in ("system", "popen", "Popen", "run", "exec", "spawnv", "eval"):
        assert forbidden not in called

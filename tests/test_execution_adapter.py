"""Phase 2D - execution adapter tests (request / plan / mock simulation)."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import MappingProxyType

import pytest

from app.engines.adapter import (
    DEFAULT_PLAN_STEPS,
    FORBIDDEN_REQUEST_FIELDS,
    MOCK_ENGINE_NAMES,
    MOCK_HASHCAT,
    MOCK_JOHN,
    AdapterError,
    DuplicateAdapterError,
    EngineAdapter,
    ExecutionMode,
    ExecutionPlan,
    ExecutionRequest,
    ExecutionStatus,
    InvalidPlanError,
    MockEngineAdapter,
    SimulationNotAllowedError,
    UnknownAdapterError,
    build_execution_plan,
    coerce_execution_mode,
    mock_adapter_names,
)

SOURCE = Path(inspect.getsourcefile(MockEngineAdapter) or "").read_text(encoding="utf-8")

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


# --------------------------------------------------------------------------- #
# execution modes
# --------------------------------------------------------------------------- #


def test_only_simulated_and_real_modes_exist() -> None:
    assert [mode.value for mode in ExecutionMode] == ["simulated", "real"]
    assert ExecutionMode.SIMULATED.value == "simulated"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("simulated", ExecutionMode.SIMULATED),
        ("real", ExecutionMode.REAL),
        (ExecutionMode.SIMULATED, ExecutionMode.SIMULATED),
        (ExecutionMode.REAL, ExecutionMode.REAL),
        ("SIMULATED", None),
        ("banana", None),
        (None, None),
        (7, None),
        (["simulated"], None),
    ],
)
def test_coerce_execution_mode(value: object, expected: object) -> None:
    assert coerce_execution_mode(value) is expected


# --------------------------------------------------------------------------- #
# execution request
# --------------------------------------------------------------------------- #


def test_valid_request_has_no_structural_problems() -> None:
    assert _request().problems() == ()


def test_request_reports_missing_target_and_engine() -> None:
    assert "target_missing" in _request(target="").problems()
    assert "engine_missing" in _request(engine="").problems()


def test_request_does_not_report_execution_mode() -> None:
    """Mode validity is the policy's decision, not the request's."""

    assert "mode" not in " ".join(_request(mode="real").problems())
    assert "mode" not in " ".join(_request(mode="banana").problems())


def test_request_flags_invalid_field_types() -> None:
    problems = _request(selection_status=3).problems()
    assert problems == ("selection_status_invalid",)


def test_request_coerces_matched_formats_to_a_tuple() -> None:
    request = _request(matched_formats=["pdf", "pkzip"])  # type: ignore[arg-type]
    assert request.matched_formats == ("pdf", "pkzip")
    assert isinstance(request.matched_formats, tuple)


def test_request_exposes_no_forbidden_fields() -> None:
    request = _request()
    assert request.forbidden_fields == frozenset()
    field_names = set(ExecutionRequest.__dataclass_fields__)
    assert not (field_names & FORBIDDEN_REQUEST_FIELDS)


def test_simulation_request_flag() -> None:
    assert _request(mode="simulated").is_simulation_request is True
    assert _request(mode="real").is_simulation_request is False
    assert _request(mode="banana").is_simulation_request is False


def test_known_command_surface_words_stay_out_of_request_fields() -> None:
    for word in ("command", "argv", "executable", "shell", "subprocess"):
        assert word in FORBIDDEN_REQUEST_FIELDS


# --------------------------------------------------------------------------- #
# execution plan
# --------------------------------------------------------------------------- #


def test_plan_normalises_string_mode_into_enum() -> None:
    plan = build_execution_plan(_request())
    assert plan.mode is ExecutionMode.SIMULATED
    assert plan.adapter == MOCK_JOHN
    assert plan.steps == DEFAULT_PLAN_STEPS
    assert plan.simulated is True
    assert plan.target == "pdf-document"
    assert plan.selected_engine == "john"
    assert plan.capability_status == "verified"


def test_plan_as_dict_is_structured() -> None:
    payload = build_execution_plan(_request()).as_dict()
    assert payload == {
        "adapter": MOCK_JOHN,
        "mode": "simulated",
        "simulated": True,
        "steps": list(DEFAULT_PLAN_STEPS),
        "target": "pdf-document",
        "selected_engine": "john",
        "capability_status": "verified",
    }
    assert not (set(payload) & FORBIDDEN_REQUEST_FIELDS)


def test_custom_steps_are_preserved() -> None:
    plan = build_execution_plan(_request(), steps=("load_mock_engine", "simulate_run"))
    assert plan.steps == ("load_mock_engine", "simulate_run")


@pytest.mark.parametrize(
    "steps",
    [
        (),
        ("Run Now!",),
        ("UPPERCASE",),
        ("1leading_digit",),
        ("",),
        ("has space",),
        (42,),  # type: ignore[list-item]
    ],
)
def test_invalid_plan_steps_are_rejected(steps: object) -> None:
    with pytest.raises(InvalidPlanError):
        build_execution_plan(_request(), steps=steps)  # type: ignore[arg-type]


def test_plan_requires_a_request() -> None:
    with pytest.raises(InvalidPlanError):
        build_execution_plan({"target": "pdf-document"})  # type: ignore[arg-type]


def test_plan_rejects_structurally_invalid_request() -> None:
    with pytest.raises(InvalidPlanError):
        build_execution_plan(_request(target=""))


def test_plan_rejects_unsupported_mode() -> None:
    with pytest.raises(InvalidPlanError):
        build_execution_plan(_request(mode="banana"))


def test_plan_refuses_to_convert_a_real_request() -> None:
    with pytest.raises(SimulationNotAllowedError) as excinfo:
        build_execution_plan(_request(mode="real"))
    assert "refusing" in str(excinfo.value)
    assert "real" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# simulation result
# --------------------------------------------------------------------------- #


def _plan(**overrides: object) -> ExecutionPlan:
    return build_execution_plan(_request(**overrides))


def test_simulation_result_is_structured_and_simulated() -> None:
    result = MockEngineAdapter(MOCK_JOHN).simulate(_plan())
    assert result.status is ExecutionStatus.SIMULATED
    assert result.simulated is True
    assert result.mode is ExecutionMode.SIMULATED
    assert result.adapter == MOCK_JOHN
    assert result.engine == "john"
    assert result.target == "pdf-document"
    assert result.steps == DEFAULT_PLAN_STEPS
    assert result.message == "simulation completed"


def test_simulation_result_as_dict_has_no_forbidden_keys() -> None:
    payload = MockEngineAdapter(MOCK_JOHN).simulate(_plan()).as_dict()
    assert not (set(payload) & FORBIDDEN_REQUEST_FIELDS)
    assert payload["simulation_id"]
    assert payload["steps"] == list(DEFAULT_PLAN_STEPS)


def test_simulation_result_metadata_is_read_only() -> None:
    result = MockEngineAdapter(MOCK_JOHN).simulate(_plan())
    assert isinstance(result.metadata, MappingProxyType)
    with pytest.raises(TypeError):
        result.metadata["capability_status"] = "hacked"  # type: ignore[index]


def test_simulation_id_is_deterministic() -> None:
    adapter = MockEngineAdapter(MOCK_JOHN)
    first = adapter.simulate(_plan())
    second = adapter.simulate(_plan())
    assert first.simulation_id == second.simulation_id
    assert len(first.simulation_id) == 16


def test_simulation_id_changes_with_the_target_and_steps() -> None:
    adapter = MockEngineAdapter(MOCK_JOHN)
    base = adapter.simulate(_plan()).simulation_id
    assert adapter.simulate(_plan(target="zip-archive")).simulation_id != base
    assert (
        adapter.simulate(_plan()).simulation_id
        != adapter.simulate(
            build_execution_plan(_request(), steps=("load_mock_engine",))
        ).simulation_id
    )


def test_simulation_id_is_hex_only() -> None:
    simulation_id = MockEngineAdapter(MOCK_JOHN).simulate(_plan()).simulation_id
    int(simulation_id, 16)


# --------------------------------------------------------------------------- #
# mock adapter behaviour
# --------------------------------------------------------------------------- #


def test_mock_adapter_requires_a_non_empty_name() -> None:
    for bad in ("", "   ", None, 7):
        with pytest.raises(AdapterError):
            MockEngineAdapter(bad)  # type: ignore[arg-type]


def test_mock_adapter_name_is_exposed_read_only() -> None:
    adapter = MockEngineAdapter(MOCK_JOHN)
    assert adapter.name == MOCK_JOHN
    with pytest.raises(AttributeError):
        adapter.name = MOCK_HASHCAT  # type: ignore[misc]


def test_mock_adapter_refuses_a_plan_built_for_another_adapter() -> None:
    with pytest.raises(AdapterError) as excinfo:
        MockEngineAdapter(MOCK_HASHCAT).simulate(_plan())
    assert "cannot run" in str(excinfo.value)


def test_mock_adapter_refuses_non_plan_input() -> None:
    with pytest.raises(InvalidPlanError):
        MockEngineAdapter(MOCK_JOHN).simulate({"mode": "simulated"})  # type: ignore[arg-type]


def test_mock_adapter_refuses_a_non_simulation_plan() -> None:
    request = _request()
    plan = ExecutionPlan(
        request=request,
        mode=ExecutionMode.REAL,
        adapter=MOCK_JOHN,
        steps=DEFAULT_PLAN_STEPS,
        simulated=False,
    )
    with pytest.raises(SimulationNotAllowedError):
        MockEngineAdapter(MOCK_JOHN).simulate(plan)


def test_mock_adapter_satisfies_the_protocol() -> None:
    assert isinstance(MockEngineAdapter(MOCK_JOHN), EngineAdapter)
    assert not isinstance(object(), EngineAdapter)
    assert not isinstance({"name": MOCK_JOHN}, EngineAdapter)


def test_mock_adapter_names_are_the_closed_set() -> None:
    names = mock_adapter_names()
    assert tuple(sorted(names)) == tuple(sorted(MOCK_ENGINE_NAMES))
    with pytest.raises(TypeError):
        names["other"] = "value"  # type: ignore[index]


# --------------------------------------------------------------------------- #
# adapter errors
# --------------------------------------------------------------------------- #


def test_adapter_error_hierarchy() -> None:
    assert issubclass(UnknownAdapterError, AdapterError)
    assert issubclass(DuplicateAdapterError, AdapterError)
    assert issubclass(InvalidPlanError, AdapterError)
    assert issubclass(SimulationNotAllowedError, AdapterError)


def test_unknown_adapter_error_keeps_the_name() -> None:
    error = UnknownAdapterError("mock_zap")
    assert error.name == "mock_zap"
    assert "mock_zap" in str(error)


def test_duplicate_adapter_error_keeps_the_name() -> None:
    error = DuplicateAdapterError(MOCK_JOHN)
    assert error.name == MOCK_JOHN


# --------------------------------------------------------------------------- #
# static safety boundary
# --------------------------------------------------------------------------- #


def test_adapter_module_never_imports_process_execution() -> None:
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
    assert "app.core" not in " ".join(imported)
    assert "app.api" not in " ".join(imported)


def test_adapter_module_has_no_process_shell_or_eval_calls() -> None:
    tree = ast.parse(SOURCE)
    called = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            called.add(func.attr)
        elif isinstance(func, ast.Name):
            called.add(func.id)
    for forbidden in ("system", "popen", "Popen", "exec", "eval", "spawnv", "input"):
        assert forbidden not in called

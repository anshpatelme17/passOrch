"""Phase 2D - engine adapter registry tests (closed, simulation-only set)."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app.engines.adapter import (
    MOCK_ENGINE_NAMES,
    MOCK_HASHCAT,
    MOCK_JOHN,
    AdapterError,
    DuplicateAdapterError,
    EngineAdapter,
    MockEngineAdapter,
    UnknownAdapterError,
)
from app.engines.adapter_registry import (
    KNOWN_ADAPTER_ENGINES,
    EngineAdapterRegistry,
    build_default_registry,
    default_adapter_registry,
)

SOURCE = Path(inspect.getsourcefile(EngineAdapterRegistry) or "").read_text(
    encoding="utf-8"
)


# --------------------------------------------------------------------------- #
# default registry
# --------------------------------------------------------------------------- #


def test_default_registry_registers_both_mock_engines() -> None:
    registry = default_adapter_registry()
    assert registry.list() == ("mock_hashcat", "mock_john")
    assert len(registry) == 2
    assert MOCK_JOHN in registry
    assert MOCK_HASHCAT in registry
    assert "john" not in registry
    assert "hashcat" not in registry


def test_default_registry_adapters_are_mock_adapters() -> None:
    for adapter in default_adapter_registry().adapters():
        assert isinstance(adapter, MockEngineAdapter)
        assert isinstance(adapter, EngineAdapter)


def test_known_adapter_engines_match_mock_engine_names() -> None:
    assert KNOWN_ADAPTER_ENGINES == frozenset(MOCK_ENGINE_NAMES)


def test_build_default_registry_is_independent_of_singleton() -> None:
    registry = build_default_registry()
    registry.reset()
    assert len(registry) == 0
    assert len(default_adapter_registry()) == 2


def test_reset_clears_the_registry() -> None:
    registry = build_default_registry()
    registry.reset()
    assert registry.list() == ()
    assert registry.get(MOCK_JOHN) is None
    assert MOCK_JOHN not in registry


# --------------------------------------------------------------------------- #
# lookup
# --------------------------------------------------------------------------- #


def test_get_returns_registered_adapter() -> None:
    registry = default_adapter_registry()
    adapter = registry.get(MOCK_JOHN)
    assert adapter is not None
    assert adapter.name == MOCK_JOHN


def test_get_is_none_for_unknown_or_non_string_names() -> None:
    registry = default_adapter_registry()
    assert registry.get("john") is None
    assert registry.get("") is None
    assert registry.get(None) is None  # type: ignore[arg-type]
    assert registry.get(7) is None  # type: ignore[arg-type]


def test_require_raises_for_unknown_engine() -> None:
    registry = default_adapter_registry()
    with pytest.raises(UnknownAdapterError) as excinfo:
        registry.require("hashcat")
    assert excinfo.value.name == "hashcat"


def test_require_returns_the_registered_adapter() -> None:
    registry = default_adapter_registry()
    assert registry.require(MOCK_HASHCAT).name == MOCK_HASHCAT


def test_contains_only_matches_registered_string_names() -> None:
    registry = default_adapter_registry()
    assert (MOCK_JOHN in registry) is True
    assert ("nope" in registry) is False
    assert (None in registry) is False


# --------------------------------------------------------------------------- #
# registration rules
# --------------------------------------------------------------------------- #


def test_duplicate_registration_is_rejected() -> None:
    registry = build_default_registry()
    with pytest.raises(DuplicateAdapterError):
        registry.register(MockEngineAdapter(MOCK_JOHN))


def test_unknown_engine_identity_is_rejected() -> None:
    registry = EngineAdapterRegistry()
    for name in ("john", "hashcat", "john-the-ripper", "mock_john2"):
        with pytest.raises(UnknownAdapterError):
            registry.register(MockEngineAdapter(name))


def test_blank_engine_identity_is_rejected_before_registration() -> None:
    registry = EngineAdapterRegistry()
    with pytest.raises(AdapterError):
        registry.register(MockEngineAdapter(""))
    assert len(registry) == 0


def test_non_adapter_object_is_rejected() -> None:
    registry = EngineAdapterRegistry()

    class NotAnAdapter:
        name = MOCK_JOHN

    with pytest.raises(AdapterError):
        registry.register(NotAnAdapter())  # type: ignore[arg-type]


def test_none_is_rejected() -> None:
    registry = EngineAdapterRegistry()
    with pytest.raises(AdapterError):
        registry.register(None)  # type: ignore[arg-type]


def test_adapter_without_simulate_method_is_rejected() -> None:
    registry = EngineAdapterRegistry()

    class NameOnly:
        name = MOCK_JOHN

    with pytest.raises(AdapterError):
        registry.register(NameOnly())  # type: ignore[arg-type]


def test_adapter_exposing_forbidden_surface_is_rejected() -> None:
    registry = EngineAdapterRegistry()

    class CommandAdapter:
        name = MOCK_JOHN
        command = "john"

        def simulate(self, plan):  # pragma: no cover - never reached
            raise AssertionError("must not be used")

    with pytest.raises(AdapterError) as excinfo:
        registry.register(CommandAdapter())  # type: ignore[arg-type]
    assert "forbidden" in str(excinfo.value)


def test_registering_valid_custom_mock_adapter_is_allowed() -> None:
    registry = EngineAdapterRegistry()
    adapter = MockEngineAdapter(MOCK_HASHCAT)
    registry.register(adapter)
    assert registry.require(MOCK_HASHCAT) is adapter


def test_empty_registry_lookup_paths_are_safe() -> None:
    registry = EngineAdapterRegistry()
    assert registry.get(MOCK_JOHN) is None
    with pytest.raises(UnknownAdapterError):
        registry.require(MOCK_JOHN)
    assert registry.adapters() == ()


# --------------------------------------------------------------------------- #
# static safety boundary
# --------------------------------------------------------------------------- #


def test_registry_module_never_imports_process_execution() -> None:
    tree = ast.parse(SOURCE)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "subprocess" not in imported
    assert "os" not in imported
    assert not any(name.startswith("app.core") or name.startswith("app.api")
                   for name in imported)


def test_registry_module_has_no_process_calls() -> None:
    tree = ast.parse(SOURCE)
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Attribute, ast.Name))
    }
    for forbidden in ("system", "popen", "Popen", "run", "execv", "spawnv"):
        assert forbidden not in called

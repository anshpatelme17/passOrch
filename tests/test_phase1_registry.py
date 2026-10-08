"""Phase 1 - simulated registry and EngineSelector."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app.engines import registry as phase1
from app.engines.registry import (
    ENGINE_HASHCAT,
    ENGINE_JOHN,
    EngineRegistry,
    EngineSelector,
    SimulatedEngine,
    default_registry,
)

REGISTRY_SOURCE = Path(inspect.getfile(phase1)).read_text(encoding="utf-8")


def _imports(node: ast.AST) -> set[str]:
    found: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Import):
            found.update(alias.name for alias in sub.names)
        elif isinstance(sub, ast.ImportFrom) and sub.module:
            found.add(sub.module)
    return found


def test_phase1_registry_module_has_no_discovery_import() -> None:
    tree = ast.parse(REGISTRY_SOURCE)
    assert not any(name.startswith("app.engines.discovery") for name in _imports(tree))


def test_phase1_registry_module_never_touches_subprocess() -> None:
    tree = ast.parse(REGISTRY_SOURCE)
    names = _imports(tree)
    assert "subprocess" not in names
    assert "os" not in names
    assert "shutil" not in names


def test_default_registry_contains_both_simulated_engines() -> None:
    registry = default_registry()
    assert registry.names() == (ENGINE_HASHCAT, ENGINE_JOHN)


def test_every_phase1_entry_is_marked_simulated() -> None:
    for engine in default_registry().engines:
        assert engine.simulated is True


def test_registry_get_returns_entry() -> None:
    registry = default_registry()
    assert registry.get(ENGINE_JOHN) is not None
    assert registry.get(ENGINE_JOHN).name == ENGINE_JOHN


def test_registry_get_unknown_returns_none() -> None:
    assert default_registry().get("nope") is None


def test_registry_rejects_duplicate_engine_names() -> None:
    duplicate = SimulatedEngine(
        name="same", display_name="one", priority=1, targets=("zip-archive",)
    )
    with pytest.raises(ValueError):
        EngineRegistry([duplicate, duplicate])


def test_registry_supports_target() -> None:
    registry = default_registry()
    assert registry.supports_target("zip-archive") is True
    assert registry.supports_target("nonexistent-target") is False


def test_registry_engines_for_target_orders_by_priority_then_name() -> None:
    engines = default_registry().engines_for_target("pdf-document")
    assert [engine.name for engine in engines] == [ENGINE_JOHN, ENGINE_HASHCAT]
    assert engines[0].priority > engines[1].priority


def test_registry_tie_break_is_alphabetical() -> None:
    low = SimulatedEngine(name="zulu", display_name="z", priority=1, targets=("t",))
    high = SimulatedEngine(name="alpha", display_name="a", priority=1, targets=("t",))
    registry = EngineRegistry([low, high])
    ranked = registry.engines_for_target("t")
    assert [engine.name for engine in ranked] == ["alpha", "zulu"]


def test_registry_priority_order_is_stable_across_calls() -> None:
    registry = default_registry()
    first = [e.name for e in registry.engines_for_target("raw-password-hash")]
    second = [e.name for e in registry.engines_for_target("raw-password-hash")]
    assert first == second


def test_selector_selects_highest_priority_engine() -> None:
    selector = EngineSelector()
    chosen = selector.select("unix-password-hash")
    assert chosen is not None
    assert chosen.name == ENGINE_JOHN


def test_selector_returns_none_for_unsupported_target() -> None:
    assert EngineSelector().select("unknown-target") is None


def test_selector_rank_matches_registry_ordering() -> None:
    selector = EngineSelector()
    assert [e.name for e in selector.rank("zip-archive")] == [
        e.name for e in selector.registry.engines_for_target("zip-archive")
    ]


def test_selector_is_pure_stateless_decision_making() -> None:
    selector = EngineSelector()
    before = selector.select("pdf-document")
    for _ in range(5):
        selector.select("pdf-document")
    after = selector.select("pdf-document")
    assert before == after


def test_selector_uses_injected_registry() -> None:
    empty = EngineRegistry([])
    assert EngineSelector(empty).select("zip-archive") is None


def test_phase1_engine_names_tuple() -> None:
    assert set(phase1.PHASE1_ENGINE_NAMES) == {ENGINE_JOHN, ENGINE_HASHCAT}


def test_registry_as_dict_is_a_copy() -> None:
    registry = default_registry()
    snapshot = registry.as_dict()
    snapshot.pop(ENGINE_JOHN, None)
    assert registry.get(ENGINE_JOHN) is not None


def test_phase1_priority_constants() -> None:
    assert phase1.Phase1Priority.JOHN == 10
    assert phase1.Phase1Priority.HASHCAT == 5


def test_simulated_engine_supports_helper() -> None:
    engine = SimulatedEngine(
        name="x", display_name="x", priority=1, targets=("zip-archive",)
    )
    assert engine.supports("zip-archive") is True
    assert engine.supports("pdf-document") is False


def test_no_real_engine_claim_in_phase1_module() -> None:
    assert "VERIFIED" not in REGISTRY_SOURCE
    assert "capability_status" not in REGISTRY_SOURCE

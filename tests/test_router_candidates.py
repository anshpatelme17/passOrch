"""Phase 2C - router candidate resolution tests.

Covers the discovery-first preference, the guarded legacy fallback and the
separation between the static Phase 1 route table and the discovery registry.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.core.router import candidates as candidates_module
from app.core.router import table as table_module
from app.core.router.candidates import (
    CandidateSource,
    CandidatesOrHints,
    candidates_or_route_hints,
    discovered_candidates,
    discovered_decision,
    route_hints_for,
)
from app.core.router.table import ROUTE_TABLE, RouteSpec, route_for
from app.engines.discovery.models import (
    HASHCAT,
    JOHN,
    CapabilityStatus,
    EngineCapabilities,
    EngineSnapshot,
)
from app.engines.discovery.registry import DiscoveryRegistry
from app.engines.selection import SelectionStatus

VERIFIED = CapabilityStatus.VERIFIED
UNKNOWN = CapabilityStatus.UNKNOWN
ERROR = CapabilityStatus.ERROR

FULL_FORMATS: tuple[str, ...] = (
    "bcrypt",
    "md5crypt",
    "ntlm",
    "office2007",
    "office2010",
    "office2013",
    "pdf",
    "pkzip",
    "raw-md5",
    "raw-sha1",
    "raw-sha256",
    "sha256crypt",
    "sha512crypt",
    "zip",
)

TABLE_PATH = Path(table_module.__file__ or "")
CANDIDATES_PATH = Path(candidates_module.__file__ or "")
TABLE_SOURCE = TABLE_PATH.read_text(encoding="utf-8")
CANDIDATES_SOURCE = CANDIDATES_PATH.read_text(encoding="utf-8")


def _imports(source: str) -> set[str]:
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _registry(
    entries: dict[str, EngineCapabilities | None],
    *,
    discovered: dict[str, bool] | None = None,
) -> DiscoveryRegistry:
    registry = DiscoveryRegistry()
    for engine, capabilities in entries.items():
        is_discovered = (discovered or {}).get(engine, True)
        registry.register(
            EngineSnapshot(
                engine=engine, discovered=is_discovered, binary_found=is_discovered
            )
        )
        if capabilities is not None:
            registry.set_capabilities(engine, capabilities)
    return registry


def _caps(
    engine: str, status: CapabilityStatus, formats: tuple[str, ...] = FULL_FORMATS
) -> EngineCapabilities:
    return EngineCapabilities(engine=engine, capability_status=status, formats=formats)


@pytest.fixture
def verified_registry() -> DiscoveryRegistry:
    return _registry({JOHN: _caps(JOHN, VERIFIED)})


@pytest.fixture
def empty_registry() -> DiscoveryRegistry:
    return DiscoveryRegistry()


# --------------------------------------------------------------------------- #
# 1  discovered_candidates() uses the discovery registry
# --------------------------------------------------------------------------- #


def test_discovered_candidates_returns_registry_candidates(
    verified_registry: DiscoveryRegistry,
) -> None:
    candidates = discovered_candidates("pdf-document", registry=verified_registry)
    assert candidates
    assert candidates[0].engine == JOHN
    assert candidates[0].capability_status is VERIFIED


def test_discovered_candidates_empty_registry_returns_nothing(
    empty_registry: DiscoveryRegistry,
) -> None:
    # The static route table still has hints for this target, but discovery
    # - which is what this function consults - has nothing.
    assert route_for("pdf-document") is not None
    assert discovered_candidates("pdf-document", registry=empty_registry) == ()


def test_discovered_candidates_depends_on_injected_registry() -> None:
    verified = _registry({JOHN: _caps(JOHN, VERIFIED)})
    unknown = _registry({JOHN: _caps(JOHN, UNKNOWN)})

    assert discovered_candidates("pdf-document", registry=verified) != ()
    assert discovered_candidates("pdf-document", registry=unknown) == ()


def test_discovered_candidates_is_registry_driven_not_table_driven() -> None:
    """Every routed target resolves through the registry, never the table."""

    registry = _registry({JOHN: _caps(JOHN, VERIFIED, formats=("ntlm",))})
    for target in ROUTE_TABLE:
        candidates = discovered_candidates(target, registry=registry)
        if target == "windows-password-hash":
            assert [c.engine for c in candidates] == [JOHN]
        else:
            assert candidates == ()


def test_discovered_decision_uses_registry(
    verified_registry: DiscoveryRegistry, empty_registry: DiscoveryRegistry
) -> None:
    hit = discovered_decision("pdf-document", registry=verified_registry)
    miss = discovered_decision("pdf-document", registry=empty_registry)

    assert hit.status is SelectionStatus.SELECTED
    assert hit.selected is not None
    assert miss.status is SelectionStatus.NO_VERIFIED_CAPABILITY
    assert miss.selected is None


def test_discovered_candidates_rejects_unsupported_target(
    verified_registry: DiscoveryRegistry,
) -> None:
    assert discovered_candidates("report.pdf", registry=verified_registry) == ()


# --------------------------------------------------------------------------- #
# 2  VERIFIED discovery candidates are preferred
# --------------------------------------------------------------------------- #


def test_verified_discovery_candidates_are_preferred(
    verified_registry: DiscoveryRegistry,
) -> None:
    result = candidates_or_route_hints("pdf-document", registry=verified_registry)

    assert result.source is CandidateSource.DISCOVERY
    assert result.has_verified_candidates is True
    assert result.uses_legacy_hints is False
    assert result.candidates
    assert result.candidates[0].capability_status is VERIFIED
    assert result.selected_engine == JOHN
    assert result.route is None
    assert result.priority_hints == ()
    assert result.decision is not None
    assert result.decision.status is SelectionStatus.SELECTED


def test_preferred_discovery_result_carries_no_route_table_hint(
    verified_registry: DiscoveryRegistry,
) -> None:
    result = candidates_or_route_hints("zip-archive", registry=verified_registry)
    assert result.source is CandidateSource.DISCOVERY
    assert route_hints_for("zip-archive") != ()  # hints exist, but are unused
    assert result.priority_hints == ()


def test_discovery_preference_holds_for_every_routed_target() -> None:
    registry = _registry({JOHN: _caps(JOHN, VERIFIED)})
    for target in ROUTE_TABLE:
        result = candidates_or_route_hints(target, registry=registry)
        assert result.source is CandidateSource.DISCOVERY, target
        assert result.has_verified_candidates is True


# --------------------------------------------------------------------------- #
# 3  legacy fallback only when discovery yields no verified candidate
# --------------------------------------------------------------------------- #


def test_fallback_to_legacy_hints_when_registry_is_empty(
    empty_registry: DiscoveryRegistry,
) -> None:
    result = candidates_or_route_hints("pdf-document", registry=empty_registry)

    assert result.source is CandidateSource.ROUTE_TABLE
    assert result.uses_legacy_hints is True
    assert result.has_verified_candidates is False
    assert result.candidates == ()
    assert result.route == "document-audit"
    assert result.priority_hints == ("john-the-ripper", "hashcat")


@pytest.mark.parametrize("status", [UNKNOWN, ERROR])
def test_fallback_to_legacy_hints_when_capability_is_unverified(
    status: CapabilityStatus,
) -> None:
    registry = _registry({JOHN: _caps(JOHN, status)})
    result = candidates_or_route_hints("pdf-document", registry=registry)

    assert result.source is CandidateSource.ROUTE_TABLE
    assert result.candidates == ()
    assert result.uses_legacy_hints is True


def test_fallback_to_legacy_hints_when_capability_is_missing() -> None:
    registry = _registry({JOHN: None})
    result = candidates_or_route_hints("pdf-document", registry=registry)
    assert result.source is CandidateSource.ROUTE_TABLE
    assert result.candidates == ()


def test_fallback_to_legacy_hints_when_no_format_matches() -> None:
    registry = _registry({JOHN: _caps(JOHN, VERIFIED, formats=("md5crypt",))})
    result = candidates_or_route_hints("pdf-document", registry=registry)

    assert result.source is CandidateSource.ROUTE_TABLE
    assert result.candidates == ()
    assert result.uses_legacy_hints is True


def test_no_fallback_when_discovery_has_verified_candidate(
    verified_registry: DiscoveryRegistry,
) -> None:
    result = candidates_or_route_hints("pdf-document", registry=verified_registry)
    assert result.source is not CandidateSource.ROUTE_TABLE
    assert result.uses_legacy_hints is False


def test_unrouted_unknown_target_has_no_fallback(
    verified_registry: DiscoveryRegistry,
) -> None:
    result = candidates_or_route_hints("unknown-target", registry=verified_registry)
    assert result.source is CandidateSource.NONE
    assert result.candidates == ()
    assert result.priority_hints == ()
    assert result.route is None
    assert result.uses_legacy_hints is False
    assert result.has_verified_candidates is False


def test_route_hints_match_the_static_table() -> None:
    for target in ROUTE_TABLE:
        spec = route_for(target)
        assert spec is not None
        assert route_hints_for(target) == spec.priority_hints
    assert route_hints_for("unknown-target") == ()


# --------------------------------------------------------------------------- #
# 4  legacy fallback does not manufacture verified capability
# --------------------------------------------------------------------------- #


def test_legacy_fallback_produces_no_real_candidate() -> None:
    registry = _registry({JOHN: _caps(JOHN, UNKNOWN)})
    result = candidates_or_route_hints("pdf-document", registry=registry)

    assert result.source is CandidateSource.ROUTE_TABLE
    assert result.candidates == ()
    assert result.selected_engine is None
    assert result.decision is not None
    assert result.decision.selected is None
    assert result.decision.candidates == ()
    assert result.decision.status in (
        SelectionStatus.NO_VERIFIED_CAPABILITY,
        SelectionStatus.NO_CANDIDATE,
    )
    assert result.decision.status is not SelectionStatus.SELECTED


def test_legacy_hints_are_phase1_names_not_capability_evidence() -> None:
    result = candidates_or_route_hints(
        "windows-password-hash", registry=DiscoveryRegistry()
    )
    assert result.priority_hints == ("john-the-ripper", "hashcat")
    assert all(isinstance(hint, str) for hint in result.priority_hints)
    assert "verified" not in "".join(result.priority_hints)


def test_candidates_or_hints_exposes_no_capability_status_field() -> None:
    fields = set(CandidatesOrHints.__dataclass_fields__)  # type: ignore[attr-defined]
    assert "capability_status" not in fields
    assert not fields & {"stdout", "stderr", "raw_output", "command", "argv"}


def test_legacy_fallback_does_not_mutate_the_registry() -> None:
    registry = _registry({JOHN: _caps(JOHN, UNKNOWN)})
    before = registry.payload()
    candidates_or_route_hints("pdf-document", registry=registry)
    assert registry.payload() == before


def test_legacy_fallback_does_not_mutate_the_route_table() -> None:
    before = dict(ROUTE_TABLE)
    candidates_or_route_hints("pdf-document", registry=_registry({JOHN: _caps(JOHN, VERIFIED)}))
    candidates_or_route_hints("pdf-document", registry=DiscoveryRegistry())
    assert dict(ROUTE_TABLE) == before


# --------------------------------------------------------------------------- #
# 5  table.py does not import discovery
# --------------------------------------------------------------------------- #


def test_table_module_has_no_app_imports() -> None:
    modules = _imports(TABLE_SOURCE)
    assert not {m for m in modules if m.startswith("app")}, modules


def test_table_module_has_no_discovery_reference() -> None:
    tree = ast.parse(TABLE_SOURCE)
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                identifiers.add(alias.name)
            if isinstance(node, ast.ImportFrom) and node.module:
                identifiers.add(node.module)
    leaked = {
        name
        for name in identifiers
        if "discovery" in name.lower() or "selector" in name.lower()
    }
    assert leaked == set()


def test_table_module_namespace_has_no_discovery_symbols() -> None:
    leaked = {
        name
        for name in vars(table_module)
        if "discovery" in name.lower() or "selector" in name.lower()
    }
    assert leaked == set()


def test_table_is_purely_static_data() -> None:
    for spec in ROUTE_TABLE.values():
        assert isinstance(spec, RouteSpec)
        assert isinstance(spec.priority_hints, tuple)
        assert spec.priority_hints == ("john-the-ripper", "hashcat")


def test_route_table_is_read_only() -> None:
    with pytest.raises(TypeError):
        ROUTE_TABLE["new-target"] = RouteSpec(  # type: ignore[index]
            target="new-target", route="x", priority_hints=()
        )


# --------------------------------------------------------------------------- #
# 6  candidates.py maintains the intended separation from table.py
# --------------------------------------------------------------------------- #


def test_candidates_module_imports_both_layers_explicitly() -> None:
    modules = _imports(CANDIDATES_SOURCE)
    assert "app.core.router.table" in modules
    assert "app.engines.discovery.registry" in modules
    assert "app.engines.selection" in modules


def test_table_module_does_not_import_candidates() -> None:
    assert "app.core.router.candidates" not in _imports(TABLE_SOURCE)


def test_candidates_uses_table_only_for_legacy_hints() -> None:
    """Table usage is confined to the fallback branch."""

    assert "route_for" in CANDIDATES_SOURCE
    assert "priority_hints" in CANDIDATES_SOURCE
    # The discovery path is expressed through the selector/registry only.
    assert "CapabilityAwareEngineSelector" in CANDIDATES_SOURCE


def test_table_is_unaffected_by_discovery_state(
    verified_registry: DiscoveryRegistry, empty_registry: DiscoveryRegistry
) -> None:
    static = dict(ROUTE_TABLE)
    candidates_or_route_hints("pdf-document", registry=verified_registry)
    candidates_or_route_hints("pdf-document", registry=empty_registry)
    assert dict(ROUTE_TABLE) == static


def test_candidate_source_values() -> None:
    assert {s.value for s in CandidateSource} == {"discovery", "route_table", "none"}


def test_candidates_or_hints_is_frozen() -> None:
    result = candidates_or_route_hints("pdf-document", registry=DiscoveryRegistry())
    with pytest.raises(Exception):
        result.target = "other"  # type: ignore[misc]


def test_result_reports_selected_engine_only_for_discovery(
    verified_registry: DiscoveryRegistry,
) -> None:
    discovery = candidates_or_route_hints("pdf-document", registry=verified_registry)
    legacy = candidates_or_route_hints("pdf-document", registry=DiscoveryRegistry())
    assert discovery.selected_engine == JOHN
    assert legacy.selected_engine is None


# --------------------------------------------------------------------------- #
# 7  discovery candidate ordering is deterministic
# --------------------------------------------------------------------------- #


def test_discovery_candidate_ordering_is_deterministic() -> None:
    first = candidates_or_route_hints(
        "pdf-document",
        registry=_registry({JOHN: _caps(JOHN, VERIFIED), HASHCAT: _caps(HASHCAT, VERIFIED)}),
    )
    for _ in range(5):
        again = candidates_or_route_hints(
            "pdf-document",
            registry=_registry(
                {HASHCAT: _caps(HASHCAT, VERIFIED), JOHN: _caps(JOHN, VERIFIED)}
            ),
        )
        assert [c.engine for c in again.candidates] == [c.engine for c in first.candidates]
    assert [c.engine for c in first.candidates] == [JOHN, HASHCAT]


def test_discovery_ordering_matches_selection_priority() -> None:
    registry = _registry({JOHN: _caps(JOHN, VERIFIED), HASHCAT: _caps(HASHCAT, VERIFIED)})
    result = candidates_or_route_hints("zip-archive", registry=registry)
    priorities = [c.priority for c in result.candidates]
    assert priorities == sorted(priorities, reverse=True)
    assert result.candidates[0].priority > result.candidates[1].priority


def test_single_candidate_result_is_stable(
    verified_registry: DiscoveryRegistry,
) -> None:
    results = [
        candidates_or_route_hints("pdf-document", registry=verified_registry)
        for _ in range(5)
    ]
    engines = [[c.engine for c in r.candidates] for r in results]
    assert all(engine_list == engines[0] for engine_list in engines)

"""Phase 2C - capability-aware selection tests.

Note on fixtures: some ordering tests need *two* eligible candidates.  Where a
synthetic ``VERIFIED`` :class:`EngineCapabilities` record is used, it exists
only to exercise the selection policy in isolation; it never represents
pipeline output.  ``test_pipeline_never_verifies_hashcat`` guards the real
flow: verification can only come from the capability parsing pipeline.
"""

from __future__ import annotations

import ast
import inspect
import os
import subprocess
from pathlib import Path

import pytest

from app.engines.discovery.capability_map import mapped_targets
from app.engines.discovery.errors import SelectionError
from app.engines.discovery.models import (
    HASHCAT,
    JOHN,
    CapabilityStatus,
    EngineCapabilities,
    EngineSnapshot,
)
from app.engines.discovery.registry import DiscoveryRegistry
from app.engines.selection import (
    DISCOVERY_PRIORITIES,
    DISCOVERY_TO_PHASE1,
    CapabilityAwareEngineSelector,
    CapabilityMatcher,
    EngineSelectionPolicy,
    SelectionCandidate,
    SelectionDecision,
    SelectionStatus,
    phase1_name,
)

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

SELECTION_SOURCE = Path(
    inspect.getsourcefile(CapabilityAwareEngineSelector)  # type: ignore[arg-type]
).read_text(encoding="utf-8")

FORBIDDEN_SURFACE = ("command", "argv", "args", "cmd", "executable", "binary", "path",
                     "shell", "exec", "subprocess")


def _registry(
    entries: dict[str, EngineCapabilities | None],
    *,
    discovered: dict[str, bool] | None = None,
) -> DiscoveryRegistry:
    """Build a registry from engine -> capability evidence (None = missing)."""

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
    engine: str,
    status: CapabilityStatus,
    formats: tuple[str, ...] = FULL_FORMATS,
) -> EngineCapabilities:
    return EngineCapabilities(engine=engine, capability_status=status, formats=formats)


def _candidate(
    engine: str,
    *,
    status: CapabilityStatus = VERIFIED,
    formats: tuple[str, ...] = FULL_FORMATS,
    discovered: bool = True,
    priority: int | None = None,
) -> SelectionCandidate:
    return SelectionCandidate(
        engine=engine,
        phase1_name=phase1_name(engine),
        priority=DISCOVERY_PRIORITIES.get(engine, 0) if priority is None else priority,
        capability_status=status,
        formats=formats,
        targets=(),
        discovered=discovered,
    )


# --------------------------------------------------------------------------- #
# 1-6  capability status -> candidate production
# --------------------------------------------------------------------------- #


def test_verified_capability_produces_a_candidate() -> None:
    registry = _registry({JOHN: _caps(JOHN, VERIFIED)})
    selector = CapabilityAwareEngineSelector(registry)

    candidates = selector.candidates("pdf-document")
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.engine == JOHN
    assert candidate.phase1_name == "john-the-ripper"
    assert candidate.capability_status is VERIFIED
    assert candidate.discovered is True


def test_verified_capability_selects_engine() -> None:
    registry = _registry({JOHN: _caps(JOHN, VERIFIED)})
    decision = CapabilityAwareEngineSelector(registry).select("pdf-document")

    assert decision.status is SelectionStatus.SELECTED
    assert decision.reason == "verified_capability"
    assert decision.has_candidate is True
    assert decision.is_verified is True
    assert decision.selected_engine == JOHN
    assert decision.selected_phase1_name == "john-the-ripper"
    assert decision.selected is not None
    assert decision.selected.capability_status is VERIFIED


def test_unknown_capability_produces_no_candidate() -> None:
    registry = _registry({JOHN: _caps(JOHN, UNKNOWN)})
    selector = CapabilityAwareEngineSelector(registry)

    assert selector.candidates("unix-password-hash") == ()
    decision = selector.select("unix-password-hash")
    assert decision.status is SelectionStatus.NO_VERIFIED_CAPABILITY
    assert decision.selected is None
    assert decision.candidates == ()


def test_unknown_capability_with_formats_still_produces_no_candidate() -> None:
    registry = _registry(
        {JOHN: EngineCapabilities(engine=JOHN, capability_status=UNKNOWN,
                                  formats=FULL_FORMATS)}
    )
    assert CapabilityAwareEngineSelector(registry).candidates("pdf-document") == ()


def test_error_capability_produces_no_candidate() -> None:
    registry = _registry({JOHN: _caps(JOHN, ERROR)})
    selector = CapabilityAwareEngineSelector(registry)

    assert selector.candidates("windows-password-hash") == ()
    decision = selector.select("windows-password-hash")
    assert decision.status is SelectionStatus.NO_VERIFIED_CAPABILITY
    assert decision.selected is None


def test_missing_capability_produces_no_candidate() -> None:
    registry = _registry({JOHN: None})
    selector = CapabilityAwareEngineSelector(registry)

    entry = registry.require(JOHN)
    assert entry.capability_status is UNKNOWN
    assert entry.has_capability_evidence is False
    assert selector.candidates("pdf-document") == ()
    decision = selector.select("pdf-document")
    assert decision.status is SelectionStatus.NO_VERIFIED_CAPABILITY
    assert decision.reason == "capability_missing"


def test_empty_registry_produces_no_candidate() -> None:
    selector = CapabilityAwareEngineSelector(DiscoveryRegistry())
    assert selector.candidates("pdf-document") == ()
    assert selector.select("pdf-document").selected is None


@pytest.mark.parametrize("status", [UNKNOWN, ERROR])
def test_unverified_status_never_yields_a_candidate(status: CapabilityStatus) -> None:
    registry = _registry({JOHN: _caps(JOHN, status), HASHCAT: _caps(HASHCAT, status)})
    assert CapabilityAwareEngineSelector(registry).candidates("pdf-document") == ()


def test_unsupported_target_produces_no_candidate() -> None:
    registry = _registry({JOHN: _caps(JOHN, VERIFIED)})
    selector = CapabilityAwareEngineSelector(registry)

    for target in ("unknown-target", "", "report.pdf"):
        assert selector.candidates(target) == ()
        decision = selector.select(target)
        assert decision.status is SelectionStatus.UNSUPPORTED_TARGET
        assert decision.reason == "target_not_mapped"
        assert decision.selected is None


def test_supported_target_produces_a_candidate() -> None:
    registry = _registry({JOHN: _caps(JOHN, VERIFIED)})
    selector = CapabilityAwareEngineSelector(registry)

    for target in mapped_targets():
        decision = selector.select(target)
        assert decision.status is SelectionStatus.SELECTED, target
        assert decision.selected is not None
        assert decision.selected.engine == JOHN


def test_target_without_matching_format_yields_no_candidate() -> None:
    registry = _registry({JOHN: _caps(JOHN, VERIFIED, formats=("md5crypt",))})
    selector = CapabilityAwareEngineSelector(registry)

    assert selector.candidates("pdf-document") == ()
    decision = selector.select("pdf-document")
    assert decision.status is SelectionStatus.NO_CANDIDATE
    assert decision.reason == "no_capability_match"
    assert decision.selected is None
    # The same evidence does match a target whose formats it covers.
    assert selector.candidates("unix-password-hash") != ()


# --------------------------------------------------------------------------- #
# 7-8  filenames / user input cannot manufacture evidence
# --------------------------------------------------------------------------- #


def test_filename_alone_cannot_create_capability_evidence() -> None:
    registry = _registry({JOHN: _caps(JOHN, VERIFIED, formats=("md5crypt",))})
    selector = CapabilityAwareEngineSelector(registry)

    for target in ("report.pdf", "secrets.zip", "backup.docx", "hash.txt", ".pdf"):
        assert selector.candidates(target) == ()
        assert selector.select(target).status is SelectionStatus.UNSUPPORTED_TARGET


def test_extension_only_target_is_unsupported() -> None:
    selector = CapabilityAwareEngineSelector(
        _registry({JOHN: _caps(JOHN, VERIFIED)})
    )
    for extension in (".pdf", ".zip", ".doc", ".docx", ".rar", ".7z"):
        assert selector.select(extension).status is SelectionStatus.UNSUPPORTED_TARGET


def test_user_input_cannot_manufacture_verified_capability() -> None:
    registry = _registry({HASHCAT: _caps(HASHCAT, UNKNOWN, formats=("pdf",))})
    selector = CapabilityAwareEngineSelector(registry)

    decision = selector.select("pdf-document")
    assert decision.status is SelectionStatus.NO_VERIFIED_CAPABILITY
    assert decision.selected is None
    assert decision.candidates == ()


def test_format_name_used_as_target_is_not_a_mapped_target() -> None:
    selector = CapabilityAwareEngineSelector(
        _registry({JOHN: _caps(JOHN, VERIFIED)})
    )
    for name in ("md5crypt", "pdf", "ntlm", "pkzip"):
        assert selector.candidates(name) == ()
        assert selector.select(name).status is SelectionStatus.UNSUPPORTED_TARGET


def test_capability_evidence_is_only_read_from_the_registry() -> None:
    verified = CapabilityAwareEngineSelector(_registry({JOHN: _caps(JOHN, VERIFIED)}))
    unknown = CapabilityAwareEngineSelector(_registry({JOHN: _caps(JOHN, UNKNOWN)}))
    target = "pdf-document"

    assert verified.candidates(target) != ()
    assert unknown.candidates(target) == ()
    # Identical user input, different registry state -> different evidence.
    assert verified.select(target).status is SelectionStatus.SELECTED
    assert unknown.select(target).status is SelectionStatus.NO_VERIFIED_CAPABILITY


# --------------------------------------------------------------------------- #
# 9-10  priority ordering and deterministic tie-breaking
# --------------------------------------------------------------------------- #


def test_discovery_priorities_are_john_10_hashcat_5() -> None:
    assert DISCOVERY_PRIORITIES[JOHN] == 10
    assert DISCOVERY_PRIORITIES[HASHCAT] == 5
    assert DISCOVERY_PRIORITIES[JOHN] > DISCOVERY_PRIORITIES[HASHCAT]


def test_priority_ordering_prefers_john_regardless_of_input_order() -> None:
    policy = EngineSelectionPolicy()
    john = _candidate(JOHN)
    hashcat = _candidate(HASHCAT)

    assert [c.engine for c in policy.order([john, hashcat])] == [JOHN, HASHCAT]
    assert [c.engine for c in policy.order([hashcat, john])] == [JOHN, HASHCAT]


def test_priority_values_applied_to_candidates() -> None:
    policy = EngineSelectionPolicy()
    assert policy.priority(JOHN) == 10
    assert policy.priority(HASHCAT) == 5
    assert _candidate(JOHN).priority == 10
    assert _candidate(HASHCAT).priority == 5


def test_unknown_engine_gets_neutral_priority() -> None:
    assert EngineSelectionPolicy().priority("unknown-engine") == 0


def test_alphabetical_tie_breaking_when_priorities_equal() -> None:
    policy = EngineSelectionPolicy(priorities={JOHN: 5, HASHCAT: 5})
    john = _candidate(JOHN, priority=5)
    hashcat = _candidate(HASHCAT, priority=5)

    # "hashcat" sorts before "john" - the tie is broken alphabetically.
    assert [c.engine for c in policy.order([john, hashcat])] == [HASHCAT, JOHN]
    assert [c.engine for c in policy.order([hashcat, john])] == [HASHCAT, JOHN]


def test_injected_priorities_are_applied_by_candidate_for() -> None:
    registry = _registry({JOHN: _caps(JOHN, VERIFIED), HASHCAT: _caps(HASHCAT, VERIFIED)})
    policy = EngineSelectionPolicy(priorities={JOHN: 3, HASHCAT: 3})
    built = [policy.candidate_for(registry.require(engine)) for engine in (JOHN, HASHCAT)]
    assert all(c is not None for c in built)
    assert [c.priority for c in built if c] == [3, 3]  # type: ignore[union-attr]
    ordered = policy.order([c for c in built if c])  # type: ignore[misc]
    assert [c.engine for c in ordered] == [HASHCAT, JOHN]


def test_tie_breaking_is_deterministic_across_repeats() -> None:
    policy = EngineSelectionPolicy(priorities={JOHN: 7, HASHCAT: 7})
    first = [
        c.engine
        for c in policy.order([_candidate(JOHN, priority=7), _candidate(HASHCAT, priority=7)])
    ]
    for _ in range(10):
        again = [
            c.engine
            for c in policy.order(
                [_candidate(HASHCAT, priority=7), _candidate(JOHN, priority=7)]
            )
        ]
        assert again == first
    assert first == [HASHCAT, JOHN]


def test_selector_ordering_is_deterministic() -> None:
    registry = _registry({JOHN: _caps(JOHN, VERIFIED), HASHCAT: _caps(HASHCAT, VERIFIED)})
    selector = CapabilityAwareEngineSelector(registry)

    first = [c.engine for c in selector.candidates("pdf-document")]
    for _ in range(5):
        again = [c.engine for c in selector.candidates("pdf-document")]
        assert again == first
    assert first == [JOHN, HASHCAT]


def test_selector_honours_injected_policy_priorities() -> None:
    registry = _registry({JOHN: _caps(JOHN, VERIFIED), HASHCAT: _caps(HASHCAT, VERIFIED)})
    selector = CapabilityAwareEngineSelector(
        registry, policy=EngineSelectionPolicy(priorities={JOHN: 1, HASHCAT: 9})
    )
    assert [c.engine for c in selector.candidates("pdf-document")] == [HASHCAT, JOHN]


def test_selection_decision_candidates_are_ordered() -> None:
    registry = _registry({JOHN: _caps(JOHN, VERIFIED), HASHCAT: _caps(HASHCAT, VERIFIED)})
    decision = CapabilityAwareEngineSelector(registry).select("zip-archive")
    assert decision.status is SelectionStatus.SELECTED
    assert [c.engine for c in decision.candidates] == [JOHN, HASHCAT]
    assert decision.selected is decision.candidates[0]


# --------------------------------------------------------------------------- #
# 11  purity - no subprocess execution
# --------------------------------------------------------------------------- #


def test_selection_does_not_execute_subprocesses(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    def _record(*_args: object, **_kwargs: object) -> object:
        calls.append((_args, _kwargs))
        raise AssertionError("selection must not execute commands")

    monkeypatch.setattr(subprocess, "run", _record, raising=False)
    monkeypatch.setattr(subprocess, "Popen", _record, raising=False)
    monkeypatch.setattr(os, "system", _record, raising=False)
    monkeypatch.setattr(os, "popen", _record, raising=False)

    registry = _registry({JOHN: _caps(JOHN, VERIFIED), HASHCAT: _caps(HASHCAT, UNKNOWN)})
    selector = CapabilityAwareEngineSelector(registry)
    selector.candidates("pdf-document")
    selector.select("pdf-document")
    selector.select("unknown-target")
    EngineSelectionPolicy().order([_candidate(JOHN), _candidate(HASHCAT)])

    assert calls == []


def test_selection_module_has_no_execution_imports() -> None:
    tree = ast.parse(SELECTION_SOURCE)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)

    assert "subprocess" not in modules
    assert "os" not in modules
    assert "shutil" not in modules
    assert "app.engines.discovery.runner" not in modules
    assert "app.engines.discovery.allowlist" not in modules


def test_selection_module_never_calls_execution_helpers() -> None:
    for forbidden in ("run_safe_command", "Popen", "subprocess.run", "os.system",
                      "os.exec", "shell=True"):
        assert forbidden not in SELECTION_SOURCE


def test_selector_methods_are_pure_across_calls() -> None:
    registry = _registry({JOHN: _caps(JOHN, VERIFIED)})
    selector = CapabilityAwareEngineSelector(registry)
    before = selector.select("pdf-document")
    for _ in range(5):
        selector.select("pdf-document")
    assert selector.select("pdf-document") == before
    assert registry.payload() == registry.payload()


# --------------------------------------------------------------------------- #
# 12-14  no raw command / argv / executable exposure
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("model", [SelectionCandidate, SelectionDecision])
def test_models_expose_no_execution_surface(model: type) -> None:
    fields = set(model.__dataclass_fields__)  # type: ignore[attr-defined]
    assert not fields & set(FORBIDDEN_SURFACE), fields & set(FORBIDDEN_SURFACE)


@pytest.mark.parametrize("method_name", ["candidates", "select"])
def test_selector_signatures_expose_no_execution_surface(method_name: str) -> None:
    selector = CapabilityAwareEngineSelector(DiscoveryRegistry())
    method = getattr(selector, method_name)
    for parameter in inspect.signature(method).parameters.values():
        assert parameter.name not in FORBIDDEN_SURFACE
        assert parameter.kind not in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        )


def test_candidate_values_carry_no_executable_path() -> None:
    candidate = _candidate(JOHN)
    for value in (candidate.engine, candidate.phase1_name):
        assert "\\" not in value
        assert "/" not in value
        assert not value.endswith(".exe")


def test_decision_values_carry_no_executable_path() -> None:
    registry = _registry({JOHN: _caps(JOHN, VERIFIED)})
    decision = CapabilityAwareEngineSelector(registry).select("pdf-document")
    assert decision.target == "pdf-document"
    for candidate in decision.candidates:
        assert "\\" not in candidate.engine
        assert "/" not in candidate.engine


def test_no_command_fields_exist_on_public_models() -> None:
    for model in (SelectionCandidate, SelectionDecision, SelectionStatus):
        names = getattr(model, "__dataclass_fields__", {})
        names = set(names) if names else {member.name for member in model}  # type: ignore[union-attr]
        assert not names & {"command", "argv", "executable", "cmd"}


# --------------------------------------------------------------------------- #
# 15  unknown / unverified engines cannot become real candidates
# --------------------------------------------------------------------------- #


def test_policy_rejects_undiscovered_engine() -> None:
    policy = EngineSelectionPolicy()
    registry = _registry({JOHN: _caps(JOHN, VERIFIED)}, discovered={JOHN: False})

    assert policy.is_eligible(registry.require(JOHN)) is False
    assert policy.candidate_for(registry.require(JOHN)) is None
    assert policy.candidate_for(registry.require(JOHN)) is None


def test_selector_ignores_undiscovered_engine() -> None:
    registry = _registry({JOHN: _caps(JOHN, VERIFIED)}, discovered={JOHN: False})
    selector = CapabilityAwareEngineSelector(registry)
    assert selector.candidates("pdf-document") == ()
    assert selector.select("pdf-document").status is SelectionStatus.NO_VERIFIED_CAPABILITY


def test_candidate_construction_rejects_unverified_status() -> None:
    for status in (UNKNOWN, ERROR):
        with pytest.raises(SelectionError):
            _candidate(JOHN, status=status)


def test_candidate_construction_rejects_undiscovered_engine() -> None:
    with pytest.raises(SelectionError):
        _candidate(JOHN, discovered=False)


def test_candidate_requires_verified_status_message() -> None:
    with pytest.raises(SelectionError) as excinfo:
        _candidate(HASHCAT, status=UNKNOWN)
    assert "VERIFIED" in str(excinfo.value)


def test_unregistered_engine_is_not_a_candidate() -> None:
    selector = CapabilityAwareEngineSelector(
        _registry({JOHN: _caps(JOHN, VERIFIED)})
    )
    assert HASHCAT not in [c.engine for c in selector.candidates("pdf-document")]


def test_only_verified_entries_are_eligible() -> None:
    registry = _registry(
        {JOHN: _caps(JOHN, VERIFIED), HASHCAT: _caps(HASHCAT, ERROR)},
        discovered={JOHN: True, HASHCAT: True},
    )
    policy = EngineSelectionPolicy()
    assert policy.is_eligible(registry.require(JOHN)) is True
    assert policy.is_eligible(registry.require(HASHCAT)) is False
    assert policy.has_any_eligible(registry) is True


def test_no_eligible_entries_reports_missing_capability() -> None:
    registry = _registry({HASHCAT: _caps(HASHCAT, UNKNOWN)})
    decision = CapabilityAwareEngineSelector(registry).select("pdf-document")
    assert decision.status is SelectionStatus.NO_VERIFIED_CAPABILITY
    assert EngineSelectionPolicy().has_any_eligible(registry) is False


def test_pipeline_never_verifies_hashcat() -> None:
    """Guard: only the capability parsing pipeline may produce VERIFIED."""

    from app.engines.discovery import hashcat as hashcat_mod
    from app.engines.discovery.allowlist import HASHCAT_OPERATIONS
    from app.engines.discovery.models import ProbeResult

    for operation in HASHCAT_OPERATIONS:
        probe = ProbeResult(engine=HASHCAT, operation=operation, ok=True, returncode=0,
                            stdout="output")
        caps = hashcat_mod.capabilities_from_probe(probe)
        assert caps.capability_status is not VERIFIED


# --------------------------------------------------------------------------- #
# supporting API surface
# --------------------------------------------------------------------------- #


def test_discovery_to_phase1_mapping() -> None:
    assert phase1_name(JOHN) == "john-the-ripper"
    assert phase1_name(HASHCAT) == "hashcat"
    assert phase1_name("unknown-engine") == "unknown-engine"
    assert DISCOVERY_TO_PHASE1[JOHN] == "john-the-ripper"


def test_discovery_to_phase1_mapping_is_read_only() -> None:
    with pytest.raises(TypeError):
        DISCOVERY_TO_PHASE1["custom"] = "custom"  # type: ignore[index]


def test_capability_matcher_target_support() -> None:
    assert CapabilityMatcher.target_supported("pdf-document") is True
    assert CapabilityMatcher.target_supported("report.pdf") is False
    assert CapabilityMatcher.target_supported("") is False


def test_capability_matcher_matching_formats() -> None:
    matches = CapabilityMatcher.matching_formats("zip-archive", ("zip", "ntlm"))
    assert matches == ("zip",)
    assert CapabilityMatcher.matching_formats("zip-archive", ("ntlm",)) == ()


def test_capability_matcher_rejects_unsupported_target() -> None:
    assert CapabilityMatcher.matches("unknown-target", FULL_FORMATS) is False
    assert CapabilityMatcher.matches("pdf-document", FULL_FORMATS) is True


def test_policy_phase1_name_lookup() -> None:
    policy = EngineSelectionPolicy()
    assert policy.phase1_name(JOHN) == "john-the-ripper"
    assert policy.phase1_name(HASHCAT) == "hashcat"


def test_policy_priorities_property_is_read_only() -> None:
    priorities = EngineSelectionPolicy().priorities
    with pytest.raises(TypeError):
        priorities["x"] = 1  # type: ignore[index]


def test_selector_exposes_registry_and_policy() -> None:
    registry = _registry({JOHN: _caps(JOHN, VERIFIED)})
    selector = CapabilityAwareEngineSelector(registry)
    assert selector.registry is registry
    assert isinstance(selector.policy, EngineSelectionPolicy)


def test_selection_status_values() -> None:
    assert {s.value for s in SelectionStatus} == {
        "selected",
        "no_candidate",
        "no_verified_capability",
        "unsupported_target",
    }


def test_selection_decision_is_frozen() -> None:
    decision = SelectionDecision(target="pdf-document", status=SelectionStatus.SELECTED)
    with pytest.raises(Exception):
        decision.target = "other"  # type: ignore[misc]


def test_candidate_is_frozen() -> None:
    candidate = _candidate(JOHN)
    with pytest.raises(Exception):
        candidate.priority = 99  # type: ignore[misc]

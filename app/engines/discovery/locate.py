"""Phase 2A engine discovery: locate known engines.

This phase only answers *"is a known engine present on this machine?"*.

It deliberately does **not**:

* run any engine (no subprocess is started here),
* parse capabilities (that is Phase 2B.5),
* register anything (that is Phase 2B).

Binary names come from the fixed ``ENGINE_BINARIES`` table; nothing here
accepts a user-supplied path or executable name.
"""

from __future__ import annotations

from typing import Callable

from app.engines.discovery.errors import UnknownEngineError
from app.engines.discovery.models import (
    DISCOVERY_ENGINE_ORDER,
    ENGINE_BINARIES,
    KNOWN_ENGINES,
    EngineSnapshot,
)

__all__ = [
    "WhichFn",
    "discover_engines",
    "engine_binary_name",
    "locate_engine_binary",
]

WhichFn = Callable[[str], "str | None"]


def engine_binary_name(engine: str) -> str:
    """Return the fixed binary name for a known engine (never user input)."""

    if engine not in KNOWN_ENGINES:
        raise UnknownEngineError(engine)
    return ENGINE_BINARIES[engine]


def locate_engine_binary(engine: str, *, which: WhichFn | None = None) -> str | None:
    """Locate a known engine binary on ``PATH``.

    Returns ``None`` when the engine is unknown or not installed.  The lookup
    always uses the fixed binary name for the engine.
    """

    if engine not in KNOWN_ENGINES:
        return None
    resolver = which if which is not None else _default_which
    return resolver(ENGINE_BINARIES[engine])


def discover_engines(*, which: WhichFn | None = None) -> tuple[EngineSnapshot, ...]:
    """Produce availability snapshots for every known engine, sorted by name."""

    snapshots: list[EngineSnapshot] = []
    for engine in DISCOVERY_ENGINE_ORDER:
        path = locate_engine_binary(engine, which=which)
        snapshots.append(
            EngineSnapshot(
                engine=engine,
                discovered=path is not None,
                binary_found=path is not None,
            )
        )
    return tuple(sorted(snapshots, key=lambda s: s.engine))


def _default_which(binary: str) -> str | None:
    import shutil

    return shutil.which(binary)

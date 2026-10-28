#!/usr/bin/env python
"""Verify that requirements.lock satisfies pyproject.toml direct dependencies.

Checks every direct dependency requirement declared in pyproject.toml
(runtime and optional extras) against the pinned versions in
requirements.lock, using standards-compliant version specifiers and
package-name normalization.  Exits 0 on success, 1 on failure with a
concise, actionable message.

This does NOT regenerate or rewrite the lockfile, does NOT install
anything, and does NOT verify the full transitive graph or artifact
hashes (those are enforced separately by --require-hashes and
tests/test_deployment_artifacts.py).
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

try:
    from packaging.markers import default_environment
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
except ImportError:  # pragma: no cover - packaging ships with pip
    print("ERROR: 'packaging' is required but not installed.", file=sys.stderr)
    sys.exit(2)

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
LOCKFILE = ROOT / "requirements.lock"

_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\]+)")


def parse_lock(path: Path) -> dict[str, str]:
    """Return {canonical_name: version} from a pip-compile style lockfile."""
    result: dict[str, str] = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        match = _PIN_RE.match(lines[i].strip())
        if match:
            name = canonicalize_name(match.group(1))
            version = match.group(2)
            result[name] = version
            i += 1
            while i < len(lines) and lines[i].strip().startswith("--hash="):
                i += 1
            continue
        i += 1
    return result


def collect_direct_requirements(path: Path) -> tuple[list[Requirement], set[str]]:
    """Collect direct dependency requirements from pyproject.toml.

    Returns the requirements plus the set of declared extra names, which
    are used to decide whether ``extra == "..."`` markers are applicable.
    """
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    project = data.get("project", {})
    requirements: list[Requirement] = []
    for spec in project.get("dependencies", []) or []:
        requirements.append(Requirement(spec))
    optional = project.get("optional-dependencies") or {}
    for specs in optional.values():
        for spec in specs or []:
            requirements.append(Requirement(spec))
    declared_extras = set(optional.keys())
    return requirements, declared_extras


def _marker_applies(req: Requirement, declared_extras: set[str]) -> bool:
    if req.marker is None:
        return True
    # ``default_environment()`` has no ``extra`` key, so an
    # ``extra == "..."`` marker would always evaluate to False there.
    # Evaluate against the default environment AND against one environment
    # per declared extra so that a conditional requirement is checked
    # whenever its extra is part of this project's declared configuration.
    # This avoids both blindly treating every extra as active and silently
    # skipping a declared one.
    envs: list[dict[str, str]] = [default_environment()]
    for extra in declared_extras:
        env = dict(default_environment())
        env["extra"] = extra
        envs.append(env)
    saw_exception = False
    for env in envs:
        try:
            if req.marker.evaluate(env):
                return True
        except Exception:
            saw_exception = True
    # Conservative: if we could not evaluate the marker at all, check the
    # requirement rather than silently skipping it.
    return saw_exception


def check_lock(pyproject_path: Path, lock_path: Path) -> list[str]:
    """Return a list of error messages (empty means consistent)."""
    errors: list[str] = []
    lock = parse_lock(lock_path)
    requirements, declared_extras = collect_direct_requirements(pyproject_path)
    for req in requirements:
        if not _marker_applies(req, declared_extras):
            continue
        name = canonicalize_name(req.name)
        if name not in lock:
            errors.append(f"{req.name}: not found in {lock_path.name}")
            continue
        pinned = lock[name]
        try:
            if not req.specifier.contains(pinned, prereleases=True):
                errors.append(
                    f"{req.name}: lock pins {pinned}, which does not satisfy "
                    f"{req.specifier}"
                )
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(f"{req.name}: could not compare {pinned} against "
                          f"{req.specifier} ({exc})")
    return errors


def main() -> int:
    errors = check_lock(PYPROJECT, LOCKFILE)
    if errors:
        print("Dependency lock consistency check FAILED:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        print(
            "Regenerate requirements.lock per docs/DEVELOPMENT.md "
            "('Regenerating the pins') before merging.",
            file=sys.stderr,
        )
        return 1
    print("Dependency lock consistency check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
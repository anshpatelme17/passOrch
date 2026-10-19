"""Phase 11A - documentation consistency regression tests.

Locks the release-audit findings so they cannot drift back:

* README's Testing commands must not append a second ``-q`` (pyproject's
  ``addopts`` already sets it, so an explicit ``-q`` becomes ``-qq`` and
  hides the pass-count summary);
* README must not claim zero subprocess execution anywhere - the single
  allowlisted discovery path exists - and must carry the accurate
  allowlist phrasing;
* every local document link in README resolves to a real file;
* ``.env.example`` documents exactly the environment variables the
  application reads (same set README documents).
"""

from __future__ import annotations

import re
from pathlib import Path

_README = Path("README.md")

_DOCUMENTED_ENV_VARS = {
    "PASSORCH_API_TOKEN",
    "PASSORCH_JOB_DB",
    "PASSORCH_MAX_BODY_BYTES",
    "PASSORCH_RATE_LIMIT",
    "PASSORCH_RATE_WINDOW_SECONDS",
}


def test_readme_testing_command_does_not_double_quiet_pytest() -> None:
    readme = _README.read_text(encoding="utf-8")
    assert "python -m pytest --tb=short -q" not in readme
    assert "python -m pytest --tb=short" in readme


def test_readme_does_not_claim_zero_subprocess_execution() -> None:
    readme = _README.read_text(encoding="utf-8")
    assert "no subprocess or engine execution anywhere" not in readme
    assert "allowlisted metadata commands" in readme
    assert "allowlisted discovery metadata commands" in readme


def test_readme_local_document_links_resolve() -> None:
    readme = _README.read_text(encoding="utf-8")
    targets = set(
        re.findall(r"\]\(((?:docs|THREAT_MODEL)[^)]+)\)", readme)
    )
    assert targets, "README must link the document set"
    for target in targets:
        assert Path(target).is_file(), target


def test_env_example_documents_exactly_the_app_environment_variables() -> None:
    example = Path(".env.example").read_text(encoding="utf-8")
    found = set(re.findall(r"PASSORCH_[A-Z0-9_]+", example))
    assert found == _DOCUMENTED_ENV_VARS

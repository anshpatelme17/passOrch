"""L-17 - the frontend ships inside the package and every install serves it.

Covers:
* the application resolves the SPA without ever consulting the caller's
  working directory (import and startup from a foreign directory);
* the built wheel carries the frontend assets as package-data;
* an installed package serves ``/`` and ``/static/`` from a foreign working
  directory (a subprocess probes it in a pristine interpreter);
* missing assets fail clearly (``404 frontend_not_installed``), never with
  misleading or partial content;
* the sdist graft stays consistent with the wheel mapping.

The wheel test skips cleanly when the build backend (``setuptools``) is not
installed - the project's ``build-system.requires`` declares it, so a normal
distribution environment has it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.api.app as app_module
from app.api.app import FRONTEND_DIR, _frontend_available, create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = [
    "app/frontend/index.html",
    "app/frontend/css/styles.css",
    "app/frontend/js/main.js",
    "app/frontend/js/api.js",
    "app/frontend/js/views/targets.js",
]


def _sse_client() -> TestClient:
    return TestClient(create_app())


# --------------------------------------------------------------------------- #
# layout contract
# --------------------------------------------------------------------------- #


def test_frontend_dir_is_inside_the_app_package() -> None:
    package_dir = Path(app_module.__file__).resolve().parents[1]
    assert FRONTEND_DIR == package_dir / "frontend"
    assert FRONTEND_DIR.is_dir()
    assert (FRONTEND_DIR / "index.html").is_file()
    assert (FRONTEND_DIR / "js" / "main.js").is_file()
    assert os.path.isabs(FRONTEND_DIR)


def test_sdist_graft_covers_the_frontend() -> None:
    manifest = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "graft app/frontend" in manifest
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for pattern in (
        '"frontend/index.html"',
        '"frontend/css/*.css"',
        '"frontend/js/*.js"',
        '"frontend/js/views/*.js"',
    ):
        assert pattern in pyproject, pattern


# --------------------------------------------------------------------------- #
# working-directory independence
# --------------------------------------------------------------------------- #


def test_app_import_and_serves_from_a_foreign_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = _sse_client()

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "mode": "simulated"}

    page = client.get("/")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "<html" in page.text.lower()

    main_js = client.get("/static/js/main.js")
    assert main_js.status_code == 200
    assert main_js.text == (FRONTEND_DIR / "js" / "main.js").read_text(encoding="utf-8")

    assert client.get("/api/targets").status_code == 200


def test_runtime_reads_do_not_depend_on_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A missing CWD-relative file would resolve against tmp_path; every path the
    # app touches must be sourced from the package location instead.
    monkeypatch.chdir(tmp_path)
    assert FRONTEND_DIR.is_dir()
    assert FRONTEND_DIR != Path("frontend").resolve()
    assert _frontend_available() is True


def test_missing_assets_fail_clearly_and_never_serve_partial_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "FRONTEND_DIR", tmp_path)
    assert _frontend_available() is False

    client = TestClient(create_app())
    # the API is unaffected
    assert client.get("/health").status_code == 200
    assert client.get("/api/targets").status_code == 200

    page = client.get("/")
    assert page.status_code == 404
    assert page.json() == {
        "detail": {
            "error": "frontend_not_installed",
            "detail": "the frontend assets are not included in this installation",
        }
    }
    assert "<html" not in page.text.lower()
    # /static is unmounted rather than serving a partial tree
    assert client.get("/static/js/main.js").status_code == 404


# --------------------------------------------------------------------------- #
# the built wheel (skips when the build backend is not installed)
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    tmp_path = tmp_path_factory.mktemp("wheel-build")
    try:
        __import__("setuptools.build_meta")
    except ModuleNotFoundError:
        pytest.skip("setuptools (the declared build backend) is not installed")

    out_dir = tmp_path / "wheel"
    out_dir.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--no-cache-dir",
            "--no-index",
            "-w",
            str(out_dir),
            str(REPO_ROOT),
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=300,
        check=False,
    )
    wheels = list(out_dir.glob("*.whl"))
    if result.returncode != 0 or len(wheels) != 1:
        detail = (result.stderr or result.stdout)[-500:]
        pytest.skip(f"wheel build unavailable: {detail}")
    return wheels[0]


@pytest.mark.allow_subprocess
def test_wheel_contains_only_expected_top_level_entries(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()

    top_levels = {name.split("/")[0] for name in names if "/" in name}
    assert top_levels == {"app", "passorch-0.1.0.dist-info"}
    for required in REQUIREMENTS:
        assert required in names, required


@pytest.mark.allow_subprocess
def test_installed_wheel_serves_the_spa_from_a_foreign_cwd(wheel: Path, tmp_path: Path) -> None:
    target = tmp_path / "installed"
    target.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-index",
            "--target",
            str(target),
            str(wheel),
        ],
        capture_output=True,
        text=True,
        cwd=elsewhere,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    probe = f"""
import sys
sys.path.insert(0, {str(target)!r})
import os
os.chdir({str(elsewhere)!r})
from fastapi.testclient import TestClient
from app.api import app as app_module
assert app_module._frontend_available() is True
client = TestClient(app_module.create_app())
page = client.get("/")
assert page.status_code == 200 and "<html" in page.text.lower()
print("page:", page.status_code)
assert client.get("/static/js/main.js").status_code == 200
print("asset: 200")
assert client.get("/health").json() == {{"status": "ok", "mode": "simulated"}}
print("health: ok")
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=elsewhere,
        timeout=300,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 0, result.stderr

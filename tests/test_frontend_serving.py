"""Phase 4A - frontend serving and integration tests.

Covers the wiring added to ``create_app``:

* ``GET /`` serves ``frontend/index.html`` as HTML;
* ``/static/`` serves each frontend asset with the right content type;
* the API surface (``/health``, ``/docs``, ``/openapi.json``, every router)
  is unchanged by the mount;
* a missing frontend directory degrades to a working app instead of raising
  (``StaticFiles`` raises ``RuntimeError`` for a missing directory, so the
  guard is what keeps ``create_app`` usable headless);
* the mount is placed at a prefix that cannot shadow API or docs routes.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(ROOT))

FRONTEND = ROOT / "frontend"
INDEX = FRONTEND / "index.html"


def _client() -> TestClient:
    from app.api.app import create_app

    return TestClient(create_app())


# --------------------------------------------------------------------------- #
# frontend availability contract
# --------------------------------------------------------------------------- #


def test_frontend_directory_is_present_for_this_suite() -> None:
    assert INDEX.is_file(), "frontend/index.html must exist for the serving tests"
    assert (FRONTEND / "js" / "app.js").is_file()
    assert (FRONTEND / "css" / "styles.css").is_file()


def test_frontend_dir_is_derived_from_the_source_not_the_cwd() -> None:
    from app.api import app as app_module

    assert app_module.FRONTEND_DIR == ROOT / "frontend"
    # app/api/app.py -> app/api -> app -> repo root -> frontend
    assert app_module.FRONTEND_DIR == Path(app_module.__file__).resolve().parents[2] / "frontend"


def test_frontend_is_not_mounted_at_the_root_prefix() -> None:
    """Mounting at ``/`` would shadow API and docs routes; it must not happen."""

    from app.api.app import create_app

    app = create_app()
    prefixes = [
        getattr(route, "path", "")
        for route in app.routes
        if type(route).__name__ == "Mount"
    ]
    assert "/" not in prefixes, prefixes
    assert "/static" in [p.rstrip("/") for p in prefixes], prefixes


# --------------------------------------------------------------------------- #
# document and asset serving
# --------------------------------------------------------------------------- #


def test_root_serves_the_shell_document() -> None:
    client = _client()
    res = client.get("/")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    body = res.text
    assert "<!DOCTYPE html>" in body
    assert "PassOrch" in body
    # the shell must load the entry module from the static prefix
    assert 'src="/static/js/main.js"' in body
    assert 'href="/static/css/styles.css"' in body
    # the entry module is what pulls in the router and the registered views
    assert 'type="module"' in body
    assert 'src="/static/js/app.js"' not in body, "app.js is loaded by main.js, not directly"


def test_root_document_declares_the_simulation_only_notice() -> None:
    client = _client()
    body = client.get("/").text
    assert "Simulation only" in body
    assert "no engine is ever executed" in body
    assert body.count('id="simulation-notice"') == 1


def test_root_document_links_every_navigation_destination() -> None:
    from app.api.app import create_app  # noqa: F401  (import guard)

    client = _client()
    body = client.get("/").text
    for path in ("/dashboard", "/engines", "/targets", "/instances", "/plan", "/jobs"):
        assert f'href="#{path}"' in body, path


def test_static_prefix_serves_each_frontend_asset() -> None:
    client = _client()
    expectations = {
        "/static/js/api.js": "javascript",
        "/static/js/dom.js": "javascript",
        "/static/js/components.js": "javascript",
        "/static/js/app.js": "javascript",
        "/static/css/styles.css": "css",
    }
    for path, kind in expectations.items():
        res = client.get(path)
        assert res.status_code == 200, path
        assert kind in res.headers["content-type"], path
        assert res.text.strip(), path


def test_static_assets_match_the_files_on_disk() -> None:
    client = _client()
    for rel in ("js/api.js", "js/dom.js", "js/components.js", "js/app.js", "css/styles.css"):
        res = client.get(f"/static/{rel}")
        assert res.status_code == 200, rel
        assert res.text == (FRONTEND / rel).read_text(encoding="utf-8"), rel


def test_dashboard_legacy_script_is_still_served_unchanged() -> None:
    """Phase 2D's read-only library must remain reachable and byte-identical."""

    client = _client()
    res = client.get("/static/js/dashboard.js")
    assert res.status_code == 200
    assert res.text == (FRONTEND / "js" / "dashboard.js").read_text(encoding="utf-8")


def test_unknown_static_path_returns_404() -> None:
    client = _client()
    assert client.get("/static/js/does-not-exist.js").status_code == 404
    assert client.get("/static/../app.py").status_code == 404


def test_static_path_traversal_cannot_escape_the_frontend_directory() -> None:
    client = _client()
    for probe in (
        "/static/../app/api/app.py",
        "/static/../../pyproject.toml",
        "/static/%2e%2e/pyproject.toml",
    ):
        res = client.get(probe)
        assert res.status_code == 404, probe
        assert "passorch" not in res.text, probe


# --------------------------------------------------------------------------- #
# API and docs surface must be unaffected
# --------------------------------------------------------------------------- #


def test_health_docs_and_openapi_are_unchanged_by_the_mount() -> None:
    client = _client()
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "mode": "simulated"}
    assert client.get("/docs").status_code == 200
    assert client.get("/docs/oauth2-redirect").status_code == 200
    assert client.get("/redoc").status_code == 200
    openapi = client.get("/openapi.json")
    assert openapi.status_code == 200
    paths = set(openapi.json()["paths"])
    for expected in (
        "/api/targets",
        "/api/targets/{target_id}/compatibility/plan",
        "/api/engines",
        "/api/engines/{engine}",
        "/api/jobs",
        "/api/jobs/{job_id}",
        "/api/target-instances",
        "/api/target-instances/{instance_id}",
    ):
        assert expected in paths, expected
    # the frontend shell must not appear as an API operation
    assert "/" not in paths


def test_api_routes_still_work_through_the_mounted_app() -> None:
    from app.api.app import create_app
    from app.engines.jobs import JobStore
    from app.engines.target_instances import TargetInstanceStore
    from fastapi.testclient import TestClient

    # Injected stores: this test must never touch the process-wide defaults,
    # or it would contaminate the isolation assertions elsewhere.
    client = TestClient(
        create_app(job_store=JobStore(), target_instance_store=TargetInstanceStore())
    )

    targets = client.get("/api/targets")
    assert targets.status_code == 200
    assert targets.json()["count"] == 6

    instances = client.post(
        "/api/target-instances", json={"name": "n", "target_type": "pdf-document"}
    )
    assert instances.status_code == 201

    jobs = client.post("/api/jobs", json={"target": "pdf-document"})
    assert jobs.status_code == 200
    assert jobs.json()["instance_id"] == ""


def test_application_state_and_injection_are_preserved() -> None:
    from app.api.app import create_app
    from app.engines.jobs import JobStore
    from app.engines.target_instances import TargetInstanceStore
    from fastapi.testclient import TestClient

    job_store = JobStore(capacity=3)
    instance_store = TargetInstanceStore(capacity=4)
    client = TestClient(
        create_app(job_store=job_store, target_instance_store=instance_store)
    )
    assert client.app.state.job_store is job_store
    assert client.app.state.target_instance_store is instance_store
    assert client.get("/").status_code == 200


# --------------------------------------------------------------------------- #
# missing-frontend guard
# --------------------------------------------------------------------------- #


def test_create_app_works_when_the_frontend_directory_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.app import create_app
    from fastapi.testclient import TestClient

    import app.api.app as app_module

    empty = tmp_path / "no-frontend"
    empty.mkdir()
    monkeypatch.setattr(app_module, "FRONTEND_DIR", empty)
    # re-evaluate the guard's inputs by re-importing the module's predicate
    assert app_module._frontend_available() is False

    app = create_app()  # must not raise
    client = TestClient(app)

    assert client.get("/health").status_code == 200
    assert client.get("/api/targets").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200


def test_create_app_works_when_index_html_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.api.app as app_module

    partial = tmp_path / "partial-frontend"
    (partial / "js").mkdir(parents=True)
    (partial / "js" / "app.js").write_text("// no shell\n", encoding="utf-8")
    monkeypatch.setattr(app_module, "FRONTEND_DIR", partial)

    assert app_module._frontend_available() is False
    app = app_module.create_app()  # must not raise
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code != 200 or "text/html" in client.get("/").headers.get(
        "content-type", ""
    )


def test_frontend_predicate_requires_both_document_and_js_directory() -> None:
    import app.api.app as app_module

    real = app_module.FRONTEND_DIR
    try:
        # real frontend: True
        assert app_module._frontend_available() is True
        # index.html alone is not enough
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            only_doc = Path(tmp) / "a"
            only_doc.mkdir()
            (only_doc / "index.html").write_text("<html></html>", encoding="utf-8")
            app_module.FRONTEND_DIR = only_doc
            assert app_module._frontend_available() is False

            only_js = Path(tmp) / "b"
            only_js.mkdir()
            (only_js / "js").mkdir()
            app_module.FRONTEND_DIR = only_js
            assert app_module._frontend_available() is False

            both = Path(tmp) / "c"
            both.mkdir()
            (both / "js").mkdir()
            (both / "index.html").write_text("<html></html>", encoding="utf-8")
            app_module.FRONTEND_DIR = both
            assert app_module._frontend_available() is True
    finally:
        app_module.FRONTEND_DIR = real


@pytest.mark.parametrize("path", ["/api/engines", "/api/targets"])
def test_frontend_absence_does_not_change_api_status_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    import app.api.app as app_module

    empty = tmp_path / "no-frontend-2"
    empty.mkdir()
    monkeypatch.setattr(app_module, "FRONTEND_DIR", empty)
    client = TestClient(app_module.create_app())
    assert client.get(path).status_code == 200


def test_module_import_is_stable_after_guard_experiments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard must not leave the module-level constant mutated."""

    import app.api.app as app_module

    before = app_module.FRONTEND_DIR
    importlib.reload(app_module)
    assert app_module.FRONTEND_DIR == before

"""Phase 4A - frontend JavaScript static-safety and structure tests.

No JavaScript runtime is available in this environment (Node is absent and the
available Python JS engines either do not support ES modules or fail on Python
3.13), so these tests are *static*: they assert the safety boundary and the
module structure of the frontend source rather than executing it.

The dynamic behaviour of the API client and the safe DOM helpers is covered by
``tests/test_frontend_api_client.py`` and by the serving tests; this module is
the boundary that keeps unsafe sinks and undeclared frameworks out of the
tree.

These checks deliberately do NOT modify ``tests/test_dashboard_js.py``, which
continues to pin the Phase 2D ``frontend/js/dashboard.js`` library unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
JS = FRONTEND / "js"

#: Modules owned by Phase 4A/4B (relative to JS_DIR, which is adjusted below).
MODULES = (
    "api.js",
    "dom.js",
    "components.js",
    "app.js",
    "main.js",
    "views/dashboard.js",
    "views/targets.js",
    "views/instances.js",
    "views/plan.js",
    "views/jobs.js",
)

#: The Phase 2D library, owned by its own suite.
LEGACY = "dashboard.js"

#: DOM sinks that would allow markup injection.
UNSAFE_SINKS = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "document.writeln",
    "eval(",
    "new Function",
    "setTimeout(",
    "setInterval(",
)

#: Words that must never appear in executable frontend code.
FORBIDDEN_WORDS = (
    "subprocess",
    "popen",
    "spawn",
    "executable",
    "argv",
    "xmlhttprequest",
    "activexobject",
)

#: Any sign of a framework, bundler or runtime dependency.
FRAMEWORK_MARKERS = (
    "node_modules",
    "webpack",
    "esbuild",
    "rollup",
    "babel",
    "typescript",
    "require(",
    "module.exports",
    "__dirname",
    "process.env",
)


def _read(name: str) -> str:
    return (JS / name).read_text(encoding="utf-8")


def _dir_of(name: str) -> Path:
    """Directory a module lives in, so `../x.js` imports resolve correctly."""

    return (JS / name).parent


def _code_of(name: str) -> str:
    """Source with comments and block comments stripped."""
    src = _read(name)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"^\s*//[^\n]*$", "", src, flags=re.MULTILINE)
    return src


def _all_sources() -> dict[str, str]:
    return {name: _read(name) for name in (*MODULES, LEGACY)}


# --------------------------------------------------------------------------- #
# presence and structure
# --------------------------------------------------------------------------- #


def test_frontend_files_exist() -> None:
    assert (FRONTEND / "index.html").is_file()
    assert (FRONTEND / "css" / "styles.css").is_file()
    for name in MODULES:
        assert (JS / name).is_file(), name


def test_index_html_loads_the_shell_as_a_module_from_the_static_prefix() -> None:
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    # the entry module, which imports the router and the registered views
    assert 'src="/static/js/main.js"' in html
    assert 'type="module"' in html
    assert 'href="/static/css/styles.css"' in html
    # no third-party scripts, styles or fonts
    for marker in ("http://", "https://", "//cdn", "<script src=\"//"):
        assert marker not in html, marker


def test_css_has_no_remote_url_references() -> None:
    css = (FRONTEND / "css" / "styles.css").read_text(encoding="utf-8")
    assert "url(http" not in css
    assert "url('http" not in css
    assert 'url("http' not in css
    assert "url(//" not in css
    assert "@import" not in css


def test_modules_are_es_modules_not_scripts() -> None:
    """Every module either imports or exports - i.e. it uses ES module syntax."""

    for name in MODULES:
        src = _read(name)
        has_import = re.search(r"^\s*import\s", src, re.MULTILINE) is not None
        has_export = re.search(r"^\s*export\s", src, re.MULTILINE) is not None
        assert has_import or has_export, f"{name} does not use ES module syntax"


# --------------------------------------------------------------------------- #
# safety boundary
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", MODULES)
def test_no_unsafe_dom_sinks(name: str) -> None:
    code = _code_of(name)
    lowered = code.lower()
    for sink in UNSAFE_SINKS:
        assert sink.lower() not in lowered, f"{name}: {sink}"


@pytest.mark.parametrize("name", MODULES)
def test_no_forbidden_words_in_executable_code(name: str) -> None:
    lowered = _code_of(name).lower()
    for word in FORBIDDEN_WORDS:
        assert word not in lowered, f"{name}: {word}"


@pytest.mark.parametrize("name", MODULES)
def test_no_framework_or_bundler_dependency(name: str) -> None:
    src = _read(name)
    for marker in FRAMEWORK_MARKERS:
        assert marker not in src, f"{name}: {marker}"


@pytest.mark.parametrize("name", MODULES)
def test_no_package_json_or_lockfile_introduced(name: str) -> None:
    """Zero-dependency requirement: nothing may pull npm packages in.

    Phase 4C.2 adds a root ``package.json`` purely to declare ES modules for
    Node; it must carry no dependencies, and no lockfile or ``node_modules``
    may exist.
    """

    assert not (ROOT / "package-lock.json").exists()
    assert not (ROOT / "node_modules").exists()
    assert not (FRONTEND / "package.json").exists()
    manifest = ROOT / "package.json"
    if manifest.is_file():
        payload = manifest.read_text(encoding="utf-8")
        assert '"dependencies"' not in payload, "the harness must stay dependency-free"
        assert '"devDependencies"' not in payload


def test_dom_helpers_only_write_through_text_apis() -> None:
    code = _code_of("dom.js")
    assert "textContent" in code
    assert "createTextNode" in code
    assert "createElement" in code
    assert "appendChild" in code
    # and never a sink
    assert "innerHTML" not in code


def test_api_client_is_the_only_module_that_calls_fetch() -> None:
    for name in MODULES:
        code = _code_of(name)
        callers = re.findall(r"\bfetch\s*\(", code)
        if name == "api.js":
            assert callers, "api.js must be the fetch owner"
        else:
            assert not callers, f"{name} calls fetch directly"


# --------------------------------------------------------------------------- #
# API client contract
# --------------------------------------------------------------------------- #


def test_api_client_exposes_get_post_delete() -> None:
    code = _code_of("api.js")
    for method in ("get(", "post(", "delete("):
        assert method in code, method


def test_api_client_handles_204_without_body() -> None:
    code = _code_of("api.js")
    assert "204" in code
    assert "response.text()" in code
    # guards the empty-body case before JSON.parse
    assert "JSON.parse" in code


def test_api_client_normalises_both_error_shapes() -> None:
    code = _code_of("api.js")
    assert "Array.isArray(detail)" in code
    assert "describeError" in code
    # transport failures are distinguished from HTTP failures
    assert '"transport"' in code
    assert '"http"' in code


def test_api_client_restricts_paths_to_first_party_endpoints() -> None:
    code = _code_of("api.js")
    assert "API_PREFIXES" in code
    assert '"/api/"' in code
    assert '"/health"' in code
    assert "startsWith" in code


def test_api_client_never_throws_on_an_http_error_status() -> None:
    """The non-2xx branch returns a result; the only throws are path guards."""

    code = _code_of("api.js")
    # the non-2xx branch builds a result instead of raising
    assert "response.ok" in code
    assert 'kind: "http"' in code

    # every `throw` must live inside assertPath (the argument guard) and not in
    # the request path itself
    guard_start = code.find("function assertPath")
    guard_end = code.find("async function parseBody")
    assert 0 <= guard_start < guard_end
    guard_body = code[guard_start:guard_end]
    for match in re.finditer(r"\bthrow\b", code):
        assert guard_start <= match.start() < guard_end, (
            "api.js throws outside the path-argument guard"
        )
    assert "throw" in guard_body


def test_api_client_invalid_path_is_reported_as_a_result_not_an_exception() -> None:
    code = _code_of("api.js")
    # the guard's error is caught by request() and turned into invalid_request
    assert 'kind: "invalid_request"' in code
    assert "catch (err)" in code


# --------------------------------------------------------------------------- #
# router contract
# --------------------------------------------------------------------------- #


def test_router_declares_all_six_destinations() -> None:
    code = _code_of("app.js")
    for path in (
        "/dashboard",
        "/engines",
        "/targets",
        "/instances",
        "/plan",
        "/jobs",
    ):
        assert f'"{path}"' in code, path


def test_router_supports_view_registration_and_placeholder_fallback() -> None:
    code = _code_of("app.js")
    assert "registerView" in code
    assert "placeholder" in code
    assert "parseHash" in code
    assert "showRoute" in code


def test_router_never_invents_a_route() -> None:
    """Unknown hashes must resolve to a known route, never an arbitrary path."""

    code = _code_of("app.js")
    assert "ROUTE_PATHS.has(path)" in code
    assert '"/dashboard"' in code


def test_shell_renders_the_simulation_only_notice() -> None:
    code = _code_of("app.js")
    assert "SimulationNotice" in code


def test_components_handle_the_denied_plan_convention() -> None:
    """A denied plan is HTTP 200 with plan===null and simulation===null."""

    code = _code_of("components.js")
    assert "preview.plan" in code
    assert "preview.simulation" in code
    assert "none (denied)" in code


def test_components_render_the_job_association_snapshot() -> None:
    code = _code_of("components.js")
    assert "instance_id" in code
    assert "instance_target_type" in code
    assert "Created without a target-instance association." in code


# --------------------------------------------------------------------------- #
# legacy library untouched
# --------------------------------------------------------------------------- #


def test_dashboard_js_is_unchanged() -> None:
    """The Phase 2D library keeps its own API surface and no Phase 4A wiring.

    ``tests/test_dashboard_js.py`` is the authoritative pin for that file; this
    check guards the other direction - that the new shell has not absorbed it.
    """

    legacy = _read(LEGACY)
    # still exactly the Phase 2D read-only surface
    assert "PassOrchDashboard" in legacy
    assert "loadTargets" in legacy
    assert "previewPlan" in legacy
    # and it has not been wired into the new module graph
    assert 'from "./dashboard.js"' not in legacy


def test_legacy_dashboard_is_not_imported_by_the_new_shell() -> None:
    """No new module may load the Phase 2D library.

    Compared by *path*, not by substring: ``views/dashboard.js`` is a Phase 4B
    view and legitimately shares the file's base name with the legacy file.
    """

    legacy_specifiers = ('"./dashboard.js"', '"../dashboard.js"', '"./js/dashboard.js"')
    for name in MODULES:
        for spec in legacy_specifiers:
            assert spec not in _read(name), f"{name} imports the legacy library via {spec}"


# --------------------------------------------------------------------------- #
# module wiring
# --------------------------------------------------------------------------- #


_IMPORT = re.compile(r'^\s*import\s*\{([^}]*)\}\s*from\s*"([^"]+)";', re.MULTILINE)
_EXPORT = re.compile(
    r"^\s*export\s+(?:default\s+)?(?:const|let|var|function|async\s+function|class)\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)


def _exports(name: str) -> set[str]:
    src = _read(name)
    names = set(_EXPORT.findall(src))
    if re.search(r"^\s*export\s+default", src, re.MULTILINE):
        names.add("default")
    return names


def test_every_import_binding_is_exported_by_its_target_module() -> None:
    for name in MODULES:
        for bindings, target in _IMPORT.findall(_read(name)):
            assert target.startswith("."), f"{name} imports a bare specifier: {target}"
            target_path = (_dir_of(name) / target).resolve()
            assert target_path.is_file(), f"{name} imports missing module {target}"
            available = _exports(target_path.relative_to(JS).as_posix())
            for binding in (b.strip() for b in bindings.split(",") if b.strip()):
                local = binding.split(" as ")[-1].strip()
                assert local in available, f"{name}: '{local}' not exported by {target}"


def test_no_unused_import_bindings() -> None:
    """Catch an import that is declared but never used (dead wiring)."""

    for name in MODULES:
        src = _read(name)
        for bindings, target in _IMPORT.findall(src):
            after = src.split(f'from "{target}";', 1)[1]
            for binding in (b.strip() for b in bindings.split(",") if b.strip()):
                local = binding.split(" as ")[-1].strip()
                assert re.search(r"\b" + re.escape(local) + r"\b", after), (
                    f"{name}: unused import '{local}'"
                )


def test_dom_helpers_export_the_state_components_views_need() -> None:
    exported = _exports("dom.js")
    for required in ("h", "text", "clear", "render", "table", "badge", "kv", "errorState"):
        assert required in exported, required


def test_components_export_the_renderers_views_need() -> None:
    exported = _exports("components.js")
    for required in (
        "SimulationNotice",
        "PlanPreviewView",
        "JobDetailView",
        "JobTable",
        "EnginesTable",
        "TargetsTable",
        "InstancesTable",
        "TargetSelect",
    ):
        assert required in exported, required


def test_api_module_exports_the_client_and_error_codes() -> None:
    exported = _exports("api.js")
    for required in ("api", "request", "describeError", "API_ERRORS"):
        assert required in exported, required


def test_job_summary_row_reads_only_summary_fields() -> None:
    """JobSummary has no instance_id - the list row must not read one.

    `GET /api/jobs` returns JobSummary rows, which carry only job_id, status,
    created_at and target. The association is JobInfo-only, so rendering it in a
    list row would always be undefined.
    """

    from app.api.schemas import JobInfo, JobSummary

    summary_fields = set(JobSummary.model_fields)
    detail_only = set(JobInfo.model_fields) - summary_fields
    row_code = _read("components.js")
    row_at = row_code.find("export function JobRow")
    assert row_at > 0
    row_block = row_code[row_at : row_code.find("export function", row_at + 10)]
    for field in detail_only:
        assert f"job.{field}" not in row_block, f"JobRow reads JobInfo-only field {field}"
    table_at = row_code.find("export function JobTable")
    assert table_at > 0
    table_call = row_code[table_at : row_code.find("\n}", table_at)]
    for field in detail_only:
        assert field not in table_call, f"JobTable exposes JobInfo-only field {field}"

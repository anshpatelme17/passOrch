# Development Guide

## Prerequisites

* Python 3.13+ (`python --version`)
* Node.js 20+ (only for the executed frontend JS tests)
* Docker (only for container verification)

Runtime dependencies are FastAPI, Starlette, Pydantic and uvicorn. Test/CI
extras: `pytest`, `httpx`, `ruff`. Install with
`pip install -c constraints.txt -e ".[test]"` — `constraints.txt` pins the
exact dependency closure verified against this codebase (regenerate it only
from an environment that has passed the full suite). An optional local-only
extra is Playwright for the browser E2E suite (see below); it is deliberately
not part of `[test]` and not installed by CI.

## Everyday commands

```text
python -m uvicorn app.api.app:app --reload          # dev server on127.0.0.1:8000
python -m pytest --tb=short                         # full backend suite
python -m pytest tests/test_api_jobs.py -k list     # one area
ruff check .                                        # lint (F, E9)
git diff --check                                    # no stray conflict markers/whitespace
python -m pytest tests/test_frontend_js_execution.py  # executes frontend-tests/*.test.mjs (Node)
npm run test:js                                      # same harness directly via node --test
python -m pytest tests/test_browser_e2e.py --tb=short  # real-browser E2E (needs Playwright)
```

Notes:

* `pyproject.toml` sets pytest `addopts = "-q"`; pass `--tb=short` for readable
  failures and count. Do not add a second `-q` — pytest then runs as `-qq` and
  suppresses the pass-count summary.
* Run tests from the repository root (relative paths in tests resolve there).
* The JS-execution harness needs Node; discovery is automatic, or set
  `PASSORCH_NODE`. Running that file with bare `python` (instead of pytest)
  fails on `tests` imports — use pytest.

## Browser end-to-end tests (optional, local)

`tests/test_browser_e2e.py` drives the real built frontend in headless
Chromium against a live uvicorn server on an ephemeral localhost port:

```text
python -m pip install playwright        # local tooling only - never in [test]
python -m playwright install chromium   # browser binary
python -m pytest tests/test_browser_e2e.py --tb=short
```

* Without Playwright installed the module **skips cleanly** (`importorskip`),
  so the default suite and CI are unaffected; CI intentionally does not run
  browser tests.
* The suite is marked `allow_subprocess` because it starts an in-process HTTP
  server thread; no shell or engine process is ever spawned.
* The test bearer token exists only in the server fixture. It must never
  appear in screenshots, console output, the served DOM, or failure reports —
  tests assert it never reaches the page.

## Conventions (enforced by tests — keep them green)

1. **No logging or printing** in `app/` modules. Operational visibility is
   uvicorn's access/error log plus structured API errors.
2. **Parameterised SQL only**; statements are module constants. The single
   exception is `PRAGMA user_version = <int>` (values cannot be bound).
   `executescript` is banned.
3. **Strict schemas**: request models extend `_Strict` (`extra="forbid"`).
   Responses expose only whitelisted structured fields — never SQL text, DB
   paths, driver strings or tracebacks.
4. **Fail closed**: policy denials, persistence failures and configuration
   errors raise; they never fall back to permissive defaults or in-memory
   success.
5. **No GET side effects**: reads must not touch discovery, engines or the
   database (bytes/mtime are asserted in tests).
6. **No `innerHTML`** (or `insertAdjacentHTML`/`document.write`) in frontend
   JS — `textContent` via `components.js` only.
7. **Environment variables**: exactly the five documented names
   (`PASSORCH_JOB_DB`, `PASSORCH_RATE_LIMIT`, `PASSORCH_RATE_WINDOW_SECONDS`,
   `PASSORCH_MAX_BODY_BYTES`, `PASSORCH_API_TOKEN`). Adding a sixth requires
   README + `test_config_security` updates; prefer `create_app` parameters.
8. **Simulation only**: never add a code path that executes an engine or
   accepts attacker-supplied executables/argv.

## Protected files

Do not modify these to make unrelated features pass; their contracts are
load-bearing:

* `app/engines/adapter.py`, `adapter_registry.py`, `discovery/allowlist.py`,
  `capabilities.py`, `errors.py`, `john.py`, `registry.py`, `service.py`
* `app/engines/execution_policy.py`, `selection.py`
* `frontend-tests/safety.test.mjs`, `frontend-tests/views.test.mjs`

## Test isolation pattern

The process-wide singletons (`default_job_store()`,
`default_target_instance_store()`) must not be mutated by tests. Inject fresh
stores instead:

```python
create_app(job_store=JobStore(), target_instance_store=TargetInstanceStore())
```

For persistence scenarios use `job_db_path=tmp_path / "x.db"` (mutually
exclusive with injected stores — it builds *both* persistent stores). To get
`simulated` results, inject a discovery registry whose John capabilities are
`VERIFIED` for the target's format; the default registry yields the valid
terminal status `denied`.

## Adding things

* **A new endpoint**: schema in `app/api/schemas.py` (extend `_Strict`), route
  in the right `app/api/routes/*` router, generic error details, no GET
  writes; add contract tests and a README API-table row.
* **A new target format**: extend the read-only `TARGET_FORMAT_POLICY`
  (discovery capability map), the routing table entry, and tests that assert
  the new key appears in the unknown-target `supported_targets` list.
* **A new engine adapter**: protected-path change — follow the adapter test
  suites; registration goes through `EngineAdapterRegistry`; the execution
  policy must remain fail-closed for anything unverified.

## Review checklist before opening a PR

```text
python -m pytest --tb=short
python -m pytest tests/test_frontend_js_execution.py
python -m pytest tests/test_browser_e2e.py --tb=short   # optional, if Playwright is installed
ruff check .
git diff --check
```

CI (`.github/workflows/ci.yml`) runs the same core steps (minus the optional
browser suite) on pushes to `main` and on pull requests, installing with
`-c constraints.txt` for reproducible dependency resolution.

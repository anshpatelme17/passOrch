# Development Guide

## Prerequisites

* Python 3.13+ (`python --version`)
* Node.js 20+ (only for the executed frontend JS tests)
* Docker (only for container verification)

Runtime dependencies are FastAPI, Starlette, Pydantic and uvicorn.
Install with the hash-verified lock (the same flow CI and Docker use;
it already includes the test tooling):

```text
python -m pip install --require-hashes -r requirements.lock
python -m pip install --no-deps -e .
```

`requirements.lock` pins every dependency with PyPI SHA-256 hashes;
`constraints.txt` is the human-readable range-pin reference;
`pyproject.toml` keeps the compatible ranges. An optional local-only
extra is Playwright for the browser E2E suite (see below); it is
deliberately not part of the lock and not installed by CI.

### Regenerating the pins

Only from an environment that has passed the full suite:

1. Update the `==` pins in `constraints.txt` (and add/remove entries if
   the closure changed) from `pip freeze` of that environment.
2. Rebuild `requirements.lock`: for each pin, fetch
   `https://pypi.org/pypi/<name>/<version>/json` and collect every
   `urls[].digests.sha256` (sdist + all platform wheels), emitting
   `name==version \` lines with one indented
   `--hash=sha256:<digest> \` per artifact (backslash continuations
   required by pip).
3. Verify before committing:
   `python -m pip install --require-hashes --dry-run --ignore-installed -r requirements.lock`
   must succeed (fresh-environment simulation),
   `python scripts/check_dependency_lock.py` must print "Dependency lock
   consistency check passed.", and `tests/test_deployment_artifacts.py`
   must pass.
4. Action SHAs and the Docker base digest are pinned in `ci.yml` /
   `Dockerfile`; refresh them the same way (GitHub API tag deref,
   `docker buildx imagetools inspect`) when bumping versions.

## Dependency updates (Dependabot)

`.github/dependabot.yml` configures weekly Dependabot proposals for three
ecosystems that have real manifests in this repository:

* `github-actions` (`.github/workflows/ci.yml`) - Dependabot updates each
  action to the new release's full commit SHA and refreshes the trailing
  version comment.  Full-SHA pinning is preserved; no tag-pinned action
  is introduced.
* `pip` (`pyproject.toml`) - Dependabot proposes version-range bumps for
  the runtime and test dependencies.
* `docker` (`Dockerfile`) - Dependabot proposes digest bumps for the
  `python:3.13-slim` base image, keeping the `sha256:` digest pin.

### Limitation: hash-pinned `requirements.lock`

Dependabot does **not** regenerate the custom hash-pinned
`requirements.lock` (pip-compile style with `--hash=sha256:` lines) or
`constraints.txt`.  A Dependabot Python PR therefore bumps the ranges in
`pyproject.toml` only; CI still installs from the unchanged
`requirements.lock`, so such a PR can look green while the lockfile is
stale.

Before merging any Dependabot Python PR, a maintainer must regenerate
the lock by hand following "Regenerating the pins" above:

1. Update `constraints.txt` pins from a verified environment.
2. Rebuild `requirements.lock` with every artifact hash.
3. Run `python -m pip install --require-hashes --dry-run --ignore-installed -r requirements.lock`.
4. Run the full suite, Ruff, mypy, `git diff --check`, and
   `tests/test_deployment_artifacts.py`.

Nothing is auto-merged: every proposal must pass CI and be reviewed.

## Everyday commands

```text
python -m uvicorn app.api.app:app --reload          # dev server on127.0.0.1:8000
python -m pytest --tb=short                         # full backend suite
python -m pytest tests/test_api_jobs.py -k list     # one area
ruff check .                                        # lint (F, E9)
python -m mypy app/api app/engines                  # typing gate (Step 5 scope)
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

## Typing (mypy)

The incremental typing gate covers `app/api` and `app/engines` only; other
packages are not checked yet and must not regress the gate by proxy.

* Config lives in `pyproject.toml` `[tool.mypy]`: `check_untyped_defs` is on,
  so every function body in scope is checked even without annotations.
* Run `python -m mypy app/api app/engines` after touching either layer; CI
  runs the same command immediately after Ruff. mypy itself is hash-pinned
  in `requirements.lock` (with its full closure) and range-pinned in the
  `[test]` extra, so CI installs it with the same `--require-hashes` flow.
* Fix findings with real annotations or small behavior-preserving
  restructures (narrowing `object` at the trust boundary, explicit dict
  annotations, `model_validate` instead of `**dict` unpacking). Do not add
  blanket `# type: ignore` comments, `Any`-heavy signatures, or new
  per-module relaxations to make a check pass.
* A per-module override in `[tool.mypy]` is allowed only for a protected
  file whose contract cannot be edited; it must name the single error code,
  sit in a commented override block, and be recorded here. The one current
  exception: `app/engines/discovery/service.py` (protected) disables
  `arg-type` because `_resolve_subset` deliberately accepts untrusted
  engine names as `object` and verifies them with runtime isinstance checks
  before use.
* When widening the gate to a new package, extend `files`, keep the suite
  green, and add the package to this section.

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
7. **Environment variables**: exactly the six documented names
   (`PASSORCH_JOB_DB`, `PASSORCH_RATE_LIMIT`, `PASSORCH_RATE_WINDOW_SECONDS`,
   `PASSORCH_MAX_BODY_BYTES`, `PASSORCH_API_TOKEN`, `PASSORCH_TRUSTED_PROXIES`).
   Adding another requires README + `test_config_security` +
   `test_docs_consistency` + `test_deployment_artifacts` + `.env.example`
   updates; prefer `create_app` parameters.
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
python -m mypy app/api app/engines
git diff --check
```

CI (`.github/workflows/ci.yml`) runs the same core steps (minus the optional
browser suite) on pushes to `main` and on pull requests, installing from
`requirements.lock` with `--require-hashes` so every artifact is
digest-verified.

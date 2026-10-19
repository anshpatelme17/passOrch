# Architecture

PassOrch is a single-process Python application: a FastAPI API over a
simulation-only password-auditing domain, plus a dependency-free browser SPA
served from the same process. This page describes how the pieces fit; the
README carries the operator-facing reference, and `THREAT_MODEL.md` carries
the security posture.

## Layers

```
browser SPA (frontend/js, no build step)
        │  same-origin fetch of /api/* and /health
        ▼
request guards (app/api/app.py middleware)
  rate limit → bearer auth → body cap → security headers
        │
        ▼
API routes (app/api/routes/*)  ── strict Pydantic schemas (extra="forbid")
        │
        ▼
orchestrator (app/core/orchestrator/*)
  create_job / create_target_instance / plan preview
        │
        ▼
engines domain (app/engines/*)
  ExecutionPolicy (fail-closed) · selection · adapters · discovery registry
        │
        ▼
persistence (app/engines/*_persistence.py over persistence.py)
  in-memory FIFO stores + optional mirrored SQLite writes
```

## Job request lifecycle

`POST /api/jobs` with `{target, mode?, reference?, instance_id?}`:

1. **Guards** — mutation counted against the per-peer rate limiter; bearer
   token verified when configured; `Content-Length` capped (413);
   `Transfer-Encoding` rejected (400) so chunked framing cannot bypass the
   cap; every response carries the same-origin safety headers (nosniff,
   frame-deny, `Referrer-Policy: no-referrer`, strict CSP).
2. **Transport validation** — `JobCreateRequest` (`extra="forbid"`); `target`
   must be a key of the read-only `TARGET_FORMAT_POLICY` (else structured 422).
3. **Instance resolution** — an explicit `instance_id` must exist in *this*
   app's store and its `target_type` must equal `target`; the pair is
   snapshotted onto the job and never re-resolved.
4. **Orchestration** — `create_job` consults the discovery registry (static
   capability evidence; no probing on this path) and the adapter registry,
   then `ExecutionPolicy.authorize` decides. The policy is fail-closed:
   anything not explicitly verifiable is denied.
5. **Terminal result** — exactly one of `simulated`, `denied`, or `failed`
   with a structured preview/audit record. **No engine is ever executed.**
6. **Persistence** — `PersistentJobStore.put` writes memory first, then
   mirrors into SQLite in the same call; a mirror failure rolls memory back
   (including any FIFO eviction victim) and the API answers a structured 503.

Reads (`GET /api/jobs*`, `GET /api/target-instances*`, `GET /health`, …) are
side-effect free by contract (AST- and bytes/mtime-locked in the test suite)
and are served from the in-memory stores.

## Module map

| Area | Modules | Role |
| --- | --- | --- |
| API shell | `app/api/app.py` | Factory, guards middleware, health, SPA mount |
| API guards | `app/api/auth.py`, `app/api/rate_limit.py` | Bearer check, limits, structured details |
| Routes | `app/api/routes/{jobs,targets,engines,target_instances}.py` | HTTP contracts |
| Schemas | `app/api/schemas.py` | Strict request/response models |
| Orchestration | `app/core/orchestrator/*` | Job/instance/plan assembly |
| Policy & selection | `app/engines/{execution_policy,selection,capabilities}.py` | Fail-closed authorisation, engine choice |
| Adapters | `app/engines/{adapter,adapter_registry,john,registry,service}.py` | Protected simulation adapters |
| Discovery | `app/engines/discovery/*` | Allowlisted metadata discovery (the only subprocess-capable path, `shell=False`) |
| Persistence | `app/engines/{persistence,job_persistence,instance_persistence}.py` | Schema-versioned SQLite mirror, online backup |
| Frontend | `frontend/index.html`, `frontend/js/*` | SPA shell, API client, views/components |

The adapter/selection/policy/discovery-core files listed above are
**protected**: behaviour changes go through their test suites only.

## Concurrency and process model

* One uvicorn process (single worker) by default; stores are thread-safe
  (`RLock`) for handler concurrency.
* Rate-limit counters live in process memory (per peer address by
  default; `X-Forwarded-For` only behind the opt-in exact-IP trust
  anchor — see README "Trusted proxies").
* SQLite access opens a short-lived connection per operation and closes it in
  `finally`; default rollback-journal mode, no WAL sidecars.
* Persistent stores hydrate once at construction (startup failures are
  fatal), so a second process over the same file sees a stale snapshot.

## Frontend structure

* `frontend/js/api.js` — the only fetch site; URL prefixes are allowlisted
  (`/api/`, `/health`).
* `frontend/js/views/*.js` — one module per SPA view (dashboard, targets,
  jobs, instances, plan); rendering goes through `components.js` helpers that
  set `textContent` only (no `innerHTML` anywhere).
* No bundler, no framework, no external assets; the app serves `index.html`
  and `/js/*` directly.

## Simulation guarantees (summary)

* No cracking engine is ever executed; the only subprocess wiring is
  allowlisted engine *discovery* (`shell=False`, fixed binaries, timeout,
  captured output).
* No GET endpoint triggers discovery, refresh, or any write.
* `ExecutionPolicy.authorize` is the single fail-closed gate for execution.
* Unmapped targets and unsupported modes produce structured denials, never
  transport-level side effects.

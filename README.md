# PassOrch

Simulation-only password-audit planning platform. No engine is ever executed.

## Architecture

PassOrch is a FastAPI application with a vanilla-JavaScript single-page frontend. It follows a layered architecture:

- **Data layer** (`app/engines/`): Immutable records, bounded thread-safe stores, capability parsing, execution policy
- **Orchestration layer** (`app/core/orchestrator/`): Composes data layer with selection, authorization, and simulation
- **API layer** (`app/api/`): FastAPI routes with strict Pydantic schemas (`extra="forbid"`)
- **Frontend** (`frontend/`): Hash-routed SPA with safe DOM helpers (no `innerHTML`)

## Security Model

- **Threat model**: see [THREAT_MODEL.md](THREAT_MODEL.md) for assets, trust boundaries, mitigations (with the tests that enforce them), and accepted residual risks.
- **Simulation-only**: No real password cracking, no cracking-engine execution, no attacker-supplied commands. Engine discovery may run allowlisted metadata commands only (`shell=False`, fixed binary names, hard timeout, captured output) — never a cracking attack
- **Fail-closed authorization**: `ExecutionPolicy.authorize` gates everything; unknown/unmapped targets denied
- **Strict schemas**: Request models use `extra="forbid"`; responses expose only structured, whitelisted fields
- **No discovery via GET**: Read-only endpoints never trigger engine probing or capability refresh — the only refresh trigger is the explicit, guarded `POST /api/engines/refresh` (metadata discovery only, documented below)
- **Bounded storage**: In-memory stores with FIFO eviction, thread-safe with RLock; optional SQLite persistence mirrors writes fail-closed
- **Opt-in authentication**: Bearer token for state-changing requests (`secrets.compare_digest`, generic 401); reads stay open by default, and authentication never substitutes for the execution policy

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Liveness check — returns `{status, mode}` |
| GET | `/health/ready` | Readiness check — both stores listable → `{status, mode}`; a store failure answers a generic `503 not_ready` |
| GET | `/api/targets` | List read-only target format policy |
| POST | `/api/targets/{target_id}/compatibility/plan` | Simulation-only plan preview |
| GET | `/api/engines` | List all engine capability states (read-only; never probes) |
| GET | `/api/engines/{engine}` | One engine's capability state |
| POST | `/api/engines/refresh` | Metadata-only discovery refresh; optional body `{"engines": [...]}` selects a subset, no body refreshes all known engines (see **Engine discovery refresh** below) |
| POST | `/api/jobs` | Create terminal audit job (simulated/denied/failed); 503 if a configured persistent store write fails |
| GET | `/api/jobs` | List jobs. Query params: `status`, `target`, `instance_id` (empty matches unassociated jobs), `sort` (`created_desc` default, `created_asc`), `limit` (default 50, max 100), `offset` (default 0). Filters apply before pagination; unknown filter values return an empty page |
| GET | `/api/jobs/stats` | Aggregated job statistics |
| GET | `/api/jobs/{job_id}` | Fetch one job (404 for unknown) |
| GET | `/api/jobs/{job_id}/export` | Download one job as a versioned JSON audit-trail attachment (`passorch-job-{job_id}.json`; read-only, 404 for unknown) |
| DELETE | `/api/jobs/{job_id}` | Delete a job (204/404; 503 if a configured persistent store write fails) |
| POST | `/api/target-instances` | Create target instance |
| GET | `/api/target-instances` | List target instances (oldest-first). Query params: `target_type` (exact), `name` (case-insensitive substring), `limit` (default 50, max 100), `offset` (default 0). Filters apply before pagination; unknown filter values return an empty page |
| GET | `/api/target-instances/{id}` | Get one instance |
| PATCH | `/api/target-instances/{id}` | Update the mutable fields (`name`, `description` only — `id`, `target_type`, `created_at` are immutable; attempts to send them are 422, an empty patch is 422, unknown id is 404, 503 if a configured persistent store write fails) |
| DELETE | `/api/target-instances/{id}` | Delete instance |

### Job audit-trail export

`GET /api/jobs/{job_id}/export` downloads **one recorded job** as a JSON file. It is a read like any other `GET`: unauthenticated by default (see **Authentication**), rate-limited, and strictly side-effect free.

- **Envelope** (served as `application/json; charset=utf-8`):

  ```json
  {"schema_version": "1", "exported_at": 1793150000, "simulation_only": true, "job": {}}
  ```

  - **`schema_version`** is the pinned literal `"1"` so consumers can branch on it; the envelope model is `extra="forbid"` and every field is required, so serialization is fail-closed rather than permissive.
  - **`exported_at`** is a UTC Unix timestamp of *when this file was produced*. It is **not** `created_at`: the job's own `created_at` inside `job` remains the original record timestamp, unchanged by exporting.
  - **`simulation_only`** is the literal `true` — the artifact itself states that no engine was ever executed.
  - **`job`** is exactly the `JobInfo` object `GET /api/jobs/{job_id}` returns (asserted field-for-field in tests), including the operator `reference` in `preview.request.reference` when the job has one — and a `null` preview for failed jobs, preserved as the record has it.
- **Attachment filename**: `Content-Disposition: attachment; filename="passorch-job-{job_id}.json"` derives **exclusively from the server-minted job UUID**. Free text in the reference stays JSON body data — it never reaches a header or the filename, so it cannot inject or influence one.
- **Historical accuracy**: the export renders the original persisted record only, using the creation-time capability evidence embedded in the job's own `PlanPreview`. The *current* discovery registry is never consulted and never merged in, so a later refresh cannot rewrite an exported history. No completion events, lifecycle transitions, actor identity or engine versions are invented where the record holds none.
- **404**: an unknown or malformed id answers the standard `{"detail": {"error": "job_not_found", "detail": "job does not exist"}}` envelope with no attachment header.
- **Read-only**: a single in-memory store read — no store mutation, no database write, no discovery or refresh, no adapter or engine execution, and no change to the job's status or record.
- **SPA**: the Jobs detail panel's **Export JSON** button triggers this fetch — never automatically, never on page load — and downloads the response as `passorch-job-{job_id}.json`; the response body becomes the file and is never rendered into the page.

### Engine discovery refresh

`POST /api/engines/refresh` runs **metadata-only discovery**: it checks installed engine metadata and supported capabilities. It does not perform password auditing or cracking, never executes a cracking engine, and never returns raw probe output — the response is a closed five-field per-engine report.

- **Optional request body**: no body, `{}` or `{"engines": null}` refreshes **all** known engines; `{"engines": ["john"]}` refreshes only that subset (de-duplicated by the service, deterministic order). The schema is `extra="forbid"` and carries nothing but known engine *names* — no executable path, binary name, command, subprocess argument, probe operation, parser, timeout or output limit can be supplied by a client.
- **Closed engine set**: a name outside the known set fails closed with `404 unknown_engine` **before any probe runs**, so a mixed valid/invalid list refreshes nothing.
- **Example** (authentication and rate limiting apply exactly as for any other mutation — see **Authentication** and **Rate Limiting**):

  ```bash
  curl -X POST http://127.0.0.1:8000/api/engines/refresh \
    -H "Content-Type: application/json" -d '{"engines": ["john"]}'
  ```

  A `200` response (one report per refreshed engine, in the request's order):

  ```json
  {"engines": [{"engine": "john", "discovered": true, "capability_status": "verified", "reason": "", "truncated": false}]}
  ```

- **`capability_status`** is exactly one of `verified`, `unknown`, `error`. **`verified` means verified *metadata* evidence only** — capability metadata parsed from a bounded, allowlisted probe — never that real execution is enabled; this platform remains simulation-only. `unknown` means no verified evidence is held, `error` means the last probe attempt failed.
- **Hashcat limitation**: no capability operation for hashcat is on the discovery allowlist, so hashcat is never probed for capabilities and currently stays `capability_status: "unknown"` even when its binary is found (`discovered: true`).
- **`truncated: true`** means probe output hit the size bound and the captured metadata was incomplete. Truncated evidence can never become `verified` (the parsers answer `unknown`, e.g. `truncated_output` / `truncated_probe`), and the frontend flags it as incomplete — it must not be read as verified capability evidence.
- **Outcomes**:
  - `200` — per-engine refresh reports as above.
  - `401` — when authentication is enabled and the bearer token is missing/wrong: the standard generic `unauthorized` envelope from **Authentication**.
  - `404` — unknown engine name(s): `{"detail": {"error": "unknown_engine", "detail": "engine is not a known engine"}}`.
  - `409` — another refresh is already running in this process: `{"detail": {"error": "refresh_in_progress", "detail": "another discovery refresh is already running"}}`.
  - `413` / `400` — the standard body-cap and chunked-framing guard responses from **Rate Limiting** (`payload_too_large` / `unsupported_transfer_encoding`).
  - `422` — malformed body shape or an extra field (`extra="forbid"`), in FastAPI's documented Pydantic array form (see **Error Contract**).
  - `429` — rate limited: the standard `rate_limited` envelope with `Retry-After` from **Rate Limiting**.
  - `503` — unexpected internal refresh failure: `{"detail": {"error": "discovery_refresh_failed", "detail": "engine discovery refresh could not be completed"}}` — a fixed message with no exception text, path or engine output.
- **Concurrency**: refreshes are serialised by a **process-local, non-blocking lock** — a second concurrent request receives the structured `409 refresh_in_progress` instead of queueing waiters, and the lock is released in a `finally` on both success and failure. The lock coordinates **one process only**: multiple worker processes do not share it (or the discovery registry), which is a documented limitation, not a distributed lock.
- **GET semantics unchanged**: `GET /api/engines` and `GET /api/engines/{engine}` remain side-effect free and never trigger discovery. In the SPA, refresh runs only when an operator clicks **Refresh engines** on the `/engines` view — never automatically on page load.

## Error Contract

Every non-2xx response the application builds — request-guard rejections and every route `HTTPException` — uses one envelope:

```json
{"detail": {"error": "job_not_found", "detail": "job does not exist"}}
```

- **`error`** is a stable snake_case machine code a client can branch on (`unauthorized`, `rate_limited`, `payload_too_large`, `unsupported_transfer_encoding`, `persistence_unavailable`, `not_ready`, `invalid_request`, `job_not_found`, `target_instance_not_found`, `target_not_mapped`, `unknown_engine`, `engine_not_registered`, `unknown_target`, `target_instance_type_mismatch`, `refresh_in_progress`, `discovery_refresh_failed`). Some codes add structured fields (for example `supported_targets` and `target` on `unknown_target`, `retry_after` on `rate_limited`).
- **`detail`** is a short human sentence — never an exception text, SQL fragment, file path or traceback.
- **The single exception**: FastAPI's automatic request-body validation keeps its documented Pydantic `422` array shape (`[{"loc": [...], "msg": "...", "type": "..."}]`) so OpenAPI tooling and Pydantic-aware clients keep working. Domain and query-parameter rejections raised inside handlers are not Pydantic validation and use the envelope (`invalid_request`).
- The SPA's `describeError` renders the object shape as `"<message> (<code>)"`.

## Frontend Views

| Route | Description |
|-------|-------------|
| `/dashboard` | Health, engines, statistics, recent jobs, instances |
| `/engines` | Engine capability table with a user-triggered **Refresh engines** button (metadata discovery; loads with a plain GET, never refreshes automatically) |
| `/targets` | Read-only target catalog with plan links |
| `/instances` | CRUD for target instances |
| `/plan` | Plan preview form + result |
| `/jobs` | Job creation, list, detail, export (JSON attachment download), delete |

## Setup

```bash
python -m pip install --require-hashes -r requirements.lock
python -m pip install --no-deps -e .
```

(`requirements.lock` hash-verifies every dependency artifact against
digests taken from the PyPI API — the same flow CI and Docker use — and
already includes the test tooling (`pytest`, `httpx`, `ruff`, `mypy`).
`constraints.txt` is the human-readable range-pin reference; the ranges
in `pyproject.toml` remain the source of truth for compatibility. See
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for the pin-regeneration
procedure.)

## Running

```bash
uvicorn app.api.app:app --reload
```

Then open http://127.0.0.1:8000/

uvicorn binds to `127.0.0.1` by default, which is what you want for development — see **Deployment** before exposing the app beyond this machine.

## Persistence

Job history and target instances can be mirrored to a local SQLite database (stdlib `sqlite3`, no extra dependency). It is **opt-in**: without configuration everything stays in memory and no files are created.

```bash
export PASSORCH_JOB_DB=./data/jobs.db
uvicorn app.api.app:app --reload
```

or per application: `create_app(job_db_path="data/jobs.db")` (mutually exclusive with an injected `job_store` or `target_instance_store`). One configured path opens both stores over the **same database file**, so job and instance records always travel together. Surrounding whitespace in the environment value is ignored; unset or blank means "stay in memory".

- **Persisted records**: terminal job metadata only — `job_id`, status, `created_at`, target, failure reason, instance snapshot, and the structured plan-preview graph (selection / request / authorization / plan / simulation); plus target instance records (`id`, `name`, `target_type`, `description`, `created_at`). Credentials and secrets are never part of a record.
- **Schema versions**: files are stamped with `PRAGMA user_version` when a store opens; pending migrations (ordered, transactional, DDL-only) run then, so an older file upgrades in place without losing rows. A file stamped by a newer PassOrch is rejected fail-fast instead of opened optimistically, and migrations never run on a read path.
- **Backup and restore**: `backup_database` uses the SQLite online-backup API, so a live or closed database copies consistently:

  ```python
  from app.engines.persistence import backup_database

  backup_database("./data/jobs.db", "./backup/jobs.db")  # back up
  backup_database("./backup/jobs.db", "./data/jobs.db")  # restore
  ```

- **Lifecycle**: the database is created and validated when a store opens; an unusable, corrupt or newer-schema file fails fast with `JobPersistenceError` / `InstancePersistenceError` instead of silently falling back to memory-only. The newest records (store capacity, default 100) reload on open in insertion order and older rows are pruned; deletions and FIFO evictions remove their rows as well.
- **Failure behaviour**: a failed job or target-instance write or delete answers `HTTP 503` with `{"detail": {"error": "persistence_unavailable", ...}}` — no SQL text, file paths or driver messages are exposed, and a failed write never appears in a list. Reads are served from memory and never touch the database.
- **Instance deletion**: deleting a target instance removes its rows only; recorded jobs keep their immutable instance snapshot.
- **Limits**: each store hydrates once at construction, so two processes sharing one file do not see each other's writes live — and writes are not coordinated either: one process's capacity eviction or delete removes rows the other process still serves from memory. **Run exactly one worker process per database file.** Tests use temporary directories and never write into the repository.

## Rate Limiting

State-changing requests (`POST` / `PUT` / `PATCH` / `DELETE`) are counted per client address and bounded in size; reads are never limited. Defaults are generous — 120 mutations per 60 s window and a 64 KiB body cap — so normal use and the test suite never trip them.

```bash
export PASSORCH_RATE_LIMIT=120
export PASSORCH_RATE_WINDOW_SECONDS=60
export PASSORCH_MAX_BODY_BYTES=65536
```

or per application: `create_app(rate_limit=120, rate_window_seconds=60, max_body_bytes=65536)` — explicit arguments win over the environment variables, and an invalid or non-positive value fails fast with `ValueError` at construction instead of silently disabling the guard.

- **Over the limit**: `HTTP 429` with a `Retry-After` header and a structured body `{"detail": {"error": "rate_limited", "detail": "too many requests", "retry_after": N}}`; no internals are exposed.
- **Oversized body**: `HTTP 413` with `{"detail": {"error": "payload_too_large", "detail": "request body exceeds the configured size limit"}}`; the rejected body never reaches a handler.
- **Chunked framing**: state-changing requests that carry `Transfer-Encoding` are rejected with `HTTP 400` and `{"detail": {"error": "unsupported_transfer_encoding", "detail": "transfer-encoded request bodies are not supported"}}` before any body byte is read — chunked uploads cannot bypass the `Content-Length` cap and are unsupported by design.
- **Additive, not instead**: guards run before route validation and the execution policy — a rejected request records nothing, and allowing a request never relaxes validation, the execution policy or persistence checks.
- **Client key**: the socket peer address. `X-Forwarded-For` is deliberately ignored by default because it is spoofable without a trusted proxy in front; put a real reverse proxy with its own limits in front of the app for internet exposure. Operators who run behind a known reverse proxy can pin an exact-IP trust anchor with `PASSORCH_TRUSTED_PROXIES` (comma-separated exact IPs) or `create_app(trusted_proxies=...)` — see **Trusted proxies** below.
- **Limits**: counters are process-local — restarting clears them and separate worker processes do not share them — and the per-address table is bounded (newest 1000 addresses tracked; evicting an old address resets its counter). A mutation with neither `Content-Length` nor `Transfer-Encoding` is treated as bodyless (empty-body `DELETE` stays supported).

### Trusted proxies

By default every mutation is counted against the socket peer address and `X-Forwarded-For` is never consulted — the header is spoofable, so trusting it would let a client mint fresh buckets at will. An operator terminating TLS at a known reverse proxy can opt in to real-client keying:

```bash
export PASSORCH_TRUSTED_PROXIES=127.0.0.1,10.0.0.2
```

or per application: `create_app(trusted_proxies="127.0.0.1")` — an explicit argument wins over the environment variable, and every entry must be a valid exact IP address (IPv4 or IPv6; hostnames and CIDR ranges fail fast with a generic `ValueError` at construction). With the anchor set, only requests whose *socket peer is itself a trusted proxy* consult `X-Forwarded-For`: the chain is walked right-to-left past trusted hops and the first untrusted hop becomes the rate-limit key. Anything malformed — a missing header, an unparseable hop, a chain that is entirely trusted — fails closed to the socket peer, so a client identity is never invented. Only the rate-limit key is affected: authentication, body caps, the execution policy and every other guard are unchanged. Keep the trust list minimal (loopback and your proxy's address) — a client that can reach the app on a trusted address can choose its own key, which is rate-limit manipulation, never authorisation.

## Authentication

Authentication is **off by default** — the bundled SPA and every existing client keep working unchanged. Enable it with a bearer token:

```bash
export PASSORCH_API_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
```

or per application: `create_app(api_token="...")` — an explicit argument wins over the environment variable.

- **Scope**: only state-changing requests (`POST` / `PUT` / `PATCH` / `DELETE`) require `Authorization: Bearer <token>`. Reads — including `/health` and all list endpoints — stay unauthenticated: they are side-effect free and the browser frontend has no token store, so a token-protected deployment is aimed at API/automation clients (put a token-aware client or reverse proxy in front for the SPA).
- **Rejections**: a missing, malformed or wrong credential gets one identical generic `401` with a `WWW-Authenticate: Bearer` header and `{"detail": {"error": "unauthorized", "detail": "a valid bearer token is required"}}` — the token itself is never echoed, logged, or placed on application state.
- **Comparison**: `secrets.compare_digest` (constant time). An explicitly passed blank token fails fast with `ValueError`; an empty `PASSORCH_API_TOKEN` counts as unset (authentication stays off).
- **Authentication is not authorization**: a valid token never bypasses route validation, `ExecutionPolicy.authorize`, persistence checks, or the rate limiter — the rate limit is checked *before* authentication, so credential guessing is rate-limited too.

## Deployment

What PassOrch does and does not provide when deployed:

- **Designed posture**: local, single-user simulation tool. Run it bound to loopback — `uvicorn app.api.app:app --host 127.0.0.1` — and open http://127.0.0.1:8000/; that network restriction is the primary access control.
- **Built-in controls**: fail-closed execution policy, strict schemas, opt-in bearer authentication, per-address mutation rate limiting with `Retry-After`, a request-body cap with chunked-framing rejection, same-origin safety response headers (`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, a strict `Content-Security-Policy`), structured generic errors (no stack traces, SQL, or paths), no cracking-engine execution anywhere, and only one subprocess-capable path (allowlisted discovery metadata commands, `shell=False` — see **Security Model**).
- **Open reads**: unauthenticated `GET` endpoints expose job, instance, and engine metadata to anyone who can reach the port — safe on loopback, so keep the port local or behind an authenticated proxy.
- **CORS**: not configured — the API emits no `Access-Control-Allow-*` headers and browsers treat the SPA as same-origin. Do not add a wildcard CORS policy.
- **Still required when exposing it**: TLS termination and network restriction at a **reverse proxy** (the app speaks plain HTTP and does not terminate TLS); proxy-level rate limits and body caps for multi-worker deployments (the in-process counters are per worker and reset on restart); the `PASSORCH_TRUSTED_PROXIES` anchor if per-client rate limiting must see through that proxy (see **Trusted proxies**); regular backups of the configured database file; and secret management for `PASSORCH_API_TOKEN`.
- **Not provided**: multi-tenant isolation, user accounts or roles, sessions or CSRF tokens for the SPA, and shared cross-process rate-limit state. Do not expose PassOrch directly to the internet.

## Docker

Prerequisites: Docker with Compose v2. The image runs the real application from the source tree via its supported entry point; the frontend is served by the app itself (no separate build step), and no engine or password-cracking tool is included.

```bash
# Build the image
docker build -t passorch:local .

# Run it, publishing on loopback only, with a persistent data volume
docker run --rm -p 127.0.0.1:8000:8000 -v passorch-data:/data passorch:local
```

Or with Compose (recommended — one application service, no extra databases or caches):

```bash
cp .env.example .env       # optional: token / guard tuning (git-ignored)
docker compose up -d --build
docker compose down        # stop and remove the container; data survives
docker compose down -v     # ...or also delete the stored database
```

- **Persistence**: the container stores the shared SQLite database (jobs + target instances) at `/data/passorch.db` on the `passorch-data` named volume (default SQLite rollback-journal mode, so there are no WAL/SHM sidecar files); container recreation does not delete it, `down -v` does.
- **Secrets**: never baked into the image or Compose file — set `PASSORCH_API_TOKEN` and guard tuning in your local `.env` (see `.env.example`); the image ships with authentication off, matching local development.
- **Health**: the image `HEALTHCHECK` calls the unauthenticated, read-only `/health/ready` endpoint (in-memory store listings only — no SQLite access, discovery, or engine activity — and no writes), so the orchestrator stops routing to an instance whose stores are broken.
- **Network**: the server listens on `0.0.0.0` *inside* the container (required for the published port); Compose publishes it on `127.0.0.1:8000` only. Remote exposure still requires everything listed under **Deployment** (TLS, reverse proxy, authentication, network restriction) — containerization does not provide those.

## Operations

- **Health semantics**: `GET /health` returns exactly `{"status": "ok", "mode": "simulated"}`. It is a *liveness* probe — unauthenticated by design, side-effect free (it never touches the database, discovery, or engines), and it exposes no paths, tokens, or tracebacks. `GET /health/ready` is the *readiness* probe: it additionally lists each configured store once (a pure in-memory read — persistent stores hydrate at construction, so the probe never opens SQLite or writes anything) and answers `200` with the same body, or one generic `503 {"detail": {"error": "not_ready", "detail": "readiness probe failed"}}` when a store fails, with no exception text, SQL, path or traceback. Startup failures (unusable database, invalid schema, bad security configuration) still stop the process before it can answer either probe.
- **Logging**: operational request logging comes from uvicorn's access and error logs (method, path, status, duration; full server-side traceback for unhandled 500s); uvicorn's default access-log handler writes to **stdout**, so access lines and audit lines share the same stream. The application modules themselves **never log** — this is a tested security contract, so bearer tokens, authorization headers, request bodies, and database paths cannot reach stdout or a log file. The single, narrow exception is the dedicated audit-event service (`app/api/audit.py`): the sole approved structured application output, it emits only its closed `schema_version: "1"` schema as one JSON line per event directly to stdout (no file handlers, no logger names, no root-logger configuration). Because uvicorn's access logs may also appear on stdout, consumers must identify audit records by that JSON schema — stdout is never claimed to hold only audit records. Since M3.2/M3.3 **both workflow routes** emit through the service: the job-create route emits exactly one `job_created` after a successful persist (simulated, denied or failed) or one `job_create_rejected` on the approved rejection paths (unknown target, instance not found, instance type mismatch, persistence unavailable); the discovery-refresh route emits `refresh_requested` before the refresh operation runs, `refresh_rejected` on the two understood rejections (refresh already in progress, unknown engine) with closed reason codes, and one `refresh_completed` per returned per-engine report carrying only `engine`, `discovered`, `capability_status`, `reason` and `truncated` — `discovered` being a bounded summary boolean copied verbatim from the report (not probe output, not subprocess detail, and not a claim of verification: verification is `capability_status`) — never raw probe output, executable paths, subprocess arguments or exception messages. The read routes (list, detail, export) and every auth, rate-limit or validation rejection are deliberately excluded. The service is best-effort and **fail-silent**: a sink failure is swallowed and can never change a job status, a discovery result or an API response — and it is **not** the authoritative record: the persisted job record and the discovery registry are. There is no persistent audit-event store and no export-history feature — audit lines exist only on stdout. Retention and rotation belong to the container runtime, as with all stdout output. Client-facing errors stay generic; runtime persistence failures surface as structured `503 persistence_unavailable` responses, while startup persistence failures stop the process with a server-side traceback.
- **Shutdown**: `docker stop` sends SIGTERM, which uvicorn handles as a graceful shutdown; persisted records live on the volume, not in the container. Stopping or recreating the container never deletes the configured database (only `docker compose down -v` removes the volume).
- **Backup and restore**: use `backup_database` (see **Persistence**) — the SQLite online-backup API copies a live database consistently; reverse the arguments to restore. Back up the configured file (in a container: `docker compose exec` or a volume snapshot) before upgrades.
- **Operational limitations**: rate-limit counters are per process (reset on restart, not shared across workers); each store hydrates once at construction, so a second process sees stale reads until restart — and one process's eviction or delete can remove rows another process still serves, so run exactly one worker process per database file (see **Persistence**). Every database operation opens its own short-lived SQLite connection and closes it in `finally` (rolling back on failure), so no connection or write lock is held between operations (default rollback-journal mode — no WAL/SHM files).

## Continuous Integration

`.github/workflows/ci.yml` runs on pushes to `main` and on pull requests, with `contents: read` only. Three jobs: the Linux suite, the same suite on Windows (filesystem/path/SQLite behaviour Ubuntu cannot catch), and a Docker job:

1. Python test suite (`python -m pytest --tb=short`) — no real token, database, engine, or external service is required; tests use temporary directories and in-memory stores.
2. Ruff (`python -m ruff check .`).
3. Typing gate (`python -m mypy app/api app/engines` — scoped to the API and engine layers; see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)).
4. Whitespace check (`git diff --check` over the full committed tree — an empty-tree-to-HEAD range, so trailing whitespace or space-before-tab *anywhere* in the repository fails; a plain `git diff --check` with no range can never fail on a clean checkout).
5. Frontend harness via its verified invocation (`python -m pytest tests/test_frontend_js_execution.py`, which needs Node.js 20+).
6. Docker job: `docker compose config --quiet`, `docker build` of the production image (runtime-only hash-verified lock), then a container smoke test — `/health/ready` answers 200 and `/health` reports `"mode":"simulated"` — with the container removed afterwards.

CI needs no repository secrets and must not be granted any.

Dependabot (`.github/dependabot.yml`) proposes weekly updates for GitHub
Actions, Python dependencies, and the Docker base image; nothing is
auto-merged. Python proposals bump `pyproject.toml` ranges only - the
hash-pinned `requirements.lock` must be regenerated by hand before merge
(see `docs/DEVELOPMENT.md`).

## Testing

```bash
# Python tests (pyproject already adds -q; a second -q would hide the summary count)
python -m pytest --tb=short

# Lint
python -m ruff check .

# Typing gate (app/api + app/engines)
python -m mypy app/api app/engines

# JavaScript tests (requires Node.js 20+; this is the CI invocation)
python -m pytest tests/test_frontend_js_execution.py

# ...or directly
node --test frontend-tests/*.test.mjs

# Browser end-to-end tests (optional, local only - requires Playwright;
# skips itself cleanly when the package is absent, and CI does not run them)
python -m pytest tests/test_browser_e2e.py --tb=short
```

## Documentation

| Document | Audience | Contents |
|----------|----------|----------|
| [THREAT_MODEL.md](THREAT_MODEL.md) | Everyone | Assets, trust boundaries, threats → mitigations → enforcing tests, residual risks |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Contributors | Layers, job-request lifecycle, module map, concurrency, simulation guarantees |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Contributors | Toolchain, commands, enforced conventions, protected files, how to extend |
| [docs/DEMO.md](docs/DEMO.md) | Evaluators | Copy-paste `curl` tour: health → jobs → persistence → auth → Docker |
| [docs/LIMITATIONS.md](docs/LIMITATIONS.md) | Everyone | What the product deliberately does not do |

## Project Structure

```
app/
  api/           # FastAPI routes and schemas
  core/          # Orchestration layer
  engines/       # Data layer (stores, discovery, policy)
docs/            # Architecture, development, demo, limitations
frontend/
  js/            # ES modules (api, dom, components, app, main)
  css/           # Styles
  views/         # One module per route
frontend-tests/  # Node.js test harness
tests/           # Python test suite
THREAT_MODEL.md  # Security threat model
```

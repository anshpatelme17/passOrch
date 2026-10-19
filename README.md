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
- **No discovery via GET**: Read-only endpoints never trigger engine probing or capability refresh
- **Bounded storage**: In-memory stores with FIFO eviction, thread-safe with RLock; optional SQLite persistence mirrors writes fail-closed
- **Opt-in authentication**: Bearer token for state-changing requests (`secrets.compare_digest`, generic 401); reads stay open by default, and authentication never substitutes for the execution policy

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Liveness check — returns `{status, mode}` |
| GET | `/health/ready` | Readiness check — both stores listable → `{status, mode}`; a store failure answers a generic `503 not_ready` |
| GET | `/api/targets` | List read-only target format policy |
| POST | `/api/targets/{target_id}/compatibility/plan` | Simulation-only plan preview |
| GET | `/api/engines` | List all engine capability states |
| GET | `/api/engines/{engine}` | One engine's capability state |
| POST | `/api/jobs` | Create terminal audit job (simulated/denied/failed); 503 if a configured persistent store write fails |
| GET | `/api/jobs` | List jobs. Query params: `status`, `target`, `instance_id` (empty matches unassociated jobs), `sort` (`created_desc` default, `created_asc`), `limit` (default 50, max 100), `offset` (default 0). Filters apply before pagination; unknown filter values return an empty page |
| GET | `/api/jobs/stats` | Aggregated job statistics |
| GET | `/api/jobs/{job_id}` | Fetch one job (404 for unknown) |
| DELETE | `/api/jobs/{job_id}` | Delete a job (204/404; 503 if a configured persistent store write fails) |
| POST | `/api/target-instances` | Create target instance |
| GET | `/api/target-instances` | List target instances (oldest-first). Query params: `target_type` (exact), `name` (case-insensitive substring), `limit` (default 50, max 100), `offset` (default 0). Filters apply before pagination; unknown filter values return an empty page |
| GET | `/api/target-instances/{id}` | Get one instance |
| PATCH | `/api/target-instances/{id}` | Update the mutable fields (`name`, `description` only — `id`, `target_type`, `created_at` are immutable; attempts to send them are 422, an empty patch is 422, unknown id is 404, 503 if a configured persistent store write fails) |
| DELETE | `/api/target-instances/{id}` | Delete instance |

## Frontend Views

| Route | Description |
|-------|-------------|
| `/dashboard` | Health, engines, statistics, recent jobs, instances |
| `/engines` | Read-only engine capability table |
| `/targets` | Read-only target catalog with plan links |
| `/instances` | CRUD for target instances |
| `/plan` | Plan preview form + result |
| `/jobs` | Job creation, list, detail, delete |

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
- **Failure behaviour**: a failed job or target-instance write or delete answers `HTTP 503` with `{"error": "persistence_unavailable", ...}` — no SQL text, file paths or driver messages are exposed, and a failed write never appears in a list. Reads are served from memory and never touch the database.
- **Instance deletion**: deleting a target instance removes its rows only; recorded jobs keep their immutable instance snapshot.
- **Limits**: each store hydrates once at construction, so two processes sharing one file do not see each other's writes live. Tests use temporary directories and never write into the repository.

## Rate Limiting

State-changing requests (`POST` / `PUT` / `PATCH` / `DELETE`) are counted per client address and bounded in size; reads are never limited. Defaults are generous — 120 mutations per 60 s window and a 64 KiB body cap — so normal use and the test suite never trip them.

```bash
export PASSORCH_RATE_LIMIT=120
export PASSORCH_RATE_WINDOW_SECONDS=60
export PASSORCH_MAX_BODY_BYTES=65536
```

or per application: `create_app(rate_limit=120, rate_window_seconds=60, max_body_bytes=65536)` — explicit arguments win over the environment variables, and an invalid or non-positive value fails fast with `ValueError` at construction instead of silently disabling the guard.

- **Over the limit**: `HTTP 429` with a `Retry-After` header and a structured body `{"error": "rate_limited", "detail": "too many requests", "retry_after": N}`; no internals are exposed.
- **Oversized body**: `HTTP 413` with `{"error": "payload_too_large", "detail": "request body exceeds the configured size limit"}`; the rejected body never reaches a handler.
- **Chunked framing**: state-changing requests that carry `Transfer-Encoding` are rejected with `HTTP 400` and `{"error": "unsupported_transfer_encoding", "detail": "transfer-encoded request bodies are not supported"}` before any body byte is read — chunked uploads cannot bypass the `Content-Length` cap and are unsupported by design.
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
- **Rejections**: a missing, malformed or wrong credential gets one identical generic `401` with a `WWW-Authenticate: Bearer` header and `{"error": "unauthorized", "detail": "a valid bearer token is required"}` — the token itself is never echoed, logged, or placed on application state.
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

- **Health semantics**: `GET /health` returns exactly `{"status": "ok", "mode": "simulated"}`. It is a *liveness* probe — unauthenticated by design, side-effect free (it never touches the database, discovery, or engines), and it exposes no paths, tokens, or tracebacks. `GET /health/ready` is the *readiness* probe: it additionally lists each configured store once (a pure in-memory read — persistent stores hydrate at construction, so the probe never opens SQLite or writes anything) and answers `200` with the same body, or one generic `503 {"error": "not_ready", "detail": "readiness probe failed"}` when a store fails, with no exception text, SQL, path or traceback. Startup failures (unusable database, invalid schema, bad security configuration) still stop the process before it can answer either probe.
- **Logging**: operational request logging comes from uvicorn's access and error logs (method, path, status, duration; full server-side traceback for unhandled 500s). The application modules themselves **never log** — this is a tested security contract, so bearer tokens, authorization headers, request bodies, and database paths cannot reach stdout or a log file. Client-facing errors stay generic; runtime persistence failures surface as structured `503 persistence_unavailable` responses, while startup persistence failures stop the process with a server-side traceback.
- **Shutdown**: `docker stop` sends SIGTERM, which uvicorn handles as a graceful shutdown; persisted records live on the volume, not in the container. Stopping or recreating the container never deletes the configured database (only `docker compose down -v` removes the volume).
- **Backup and restore**: use `backup_database` (see **Persistence**) — the SQLite online-backup API copies a live database consistently; reverse the arguments to restore. Back up the configured file (in a container: `docker compose exec` or a volume snapshot) before upgrades.
- **Operational limitations**: rate-limit counters are per process (reset on restart, not shared across workers); each store hydrates once at construction, so a second process sees stale reads until restart. Every database operation opens its own short-lived SQLite connection and closes it in `finally` (rolling back on failure), so no connection or write lock is held between operations (default rollback-journal mode — no WAL/SHM files).

## Continuous Integration

`.github/workflows/ci.yml` runs on pushes to `main` and on pull requests, with `contents: read` only:

1. Python test suite (`python -m pytest --tb=short`) — no real token, database, engine, or external service is required; tests use temporary directories and in-memory stores.
2. Ruff (`python -m ruff check .`).
3. Typing gate (`python -m mypy app/api app/engines` — scoped to the API and engine layers; see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)).
4. Whitespace check (`git diff --check`).
5. Frontend harness via its verified invocation (`python -m pytest tests/test_frontend_js_execution.py`, which needs Node.js 20+).

CI needs no repository secrets and must not be granted any.

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

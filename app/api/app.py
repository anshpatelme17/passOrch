"""Phase 2D/3A/3B.3/4A/8A/8B/8E/9A/9B - FastAPI application factory.

Simulation-only surface: the app exposes a read-only target list, a read-only
engine capability list, a structured plan-preview endpoint, job endpoints
(`POST/GET /api/jobs` and `GET /api/jobs/{job_id}`), target-instance CRUD, and
the Phase 4A frontend shell. Nothing in this module (or anywhere it wires up)
accepts a command, argv, executable path or real execution mode. Engine
probing is reachable only through ``POST /api/engines/refresh`` (M1.1), which
delegates to the injected, allowlist-bound
``CapabilityRefreshService`` for metadata discovery only - no endpoint can
trigger a password audit or any cracking operation.

Phase 4A adds the frontend: ``GET /`` serves ``app/frontend/index.html`` and
``/static/`` serves the bundled frontend assets.  Both are optional - the
factory serves a clear ``404 frontend_not_installed`` detail when the frontend
assets are absent, so tests and other consumers can build the app without
shipping them; the API routes always work.

The assets live inside the ``app`` package (``app/frontend/``), so the wheel
carries them and the server finds them from any working directory.

Phases 8A/8B add opt-in persistence: when no store is injected, an explicit
``job_db_path`` (or the ``PASSORCH_JOB_DB`` environment variable) opens a
SQLite-backed persistent job store **and** a persistent target-instance
store over the same database file; without either, behaviour is unchanged -
the in-memory stores, no files, no filesystem access at import.

Phase 8E adds fail-closed request guards: state-changing requests are
rate-limited per client address (``HTTP 429`` + ``Retry-After``) and their
bodies are capped (``HTTP 413``), configured at construction. Guards only
ever reject - they never relax validation, policy or persistence.

Phase 9A adds opt-in bearer authentication: with ``api_token`` or
``PASSORCH_API_TOKEN`` set, state-changing requests must carry a valid
``Authorization: Bearer`` credential (constant-time compare, generic 401);
reads stay unauthenticated (documented). Authentication is off by default
and never substitutes for the execution policy.

Phase 9B hardens configuration and responses: security-sensitive env
settings fail fast without echoing their values, environment paths are
normalised, and every response carries ``X-Content-Type-Options:
nosniff``, ``X-Frame-Options: DENY``, ``Referrer-Policy: no-referrer``
and a strict same-origin ``Content-Security-Policy`` (no CORS is
configured - the SPA is same-origin; the policy allows only same-origin
scripts, styles, images and fetches, and forbids framing, base URI
overrides and form posts elsewhere).  State-changing requests that use
``Transfer-Encoding`` (chunked uploads) are rejected with ``HTTP 400``
before any body is read, so the body cap cannot be bypassed by omitting
``Content-Length``.

Phase 12B adds opt-in trusted-proxy client addressing for the rate
limiter: by default the key is the socket peer and ``X-Forwarded-For``
is never consulted; ``trusted_proxies`` (or ``PASSORCH_TRUSTED_PROXIES``)
pins an exact-IP trust anchor so requests arriving from a trusted proxy
are keyed by the real client hop.  Authentication, body caps and every
other guard are unaffected, and malformed forwarding data fails closed
to the peer.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.audit import AuditLogger
from app.api.auth import configured_api_token, is_authorized, unauthorized_detail
from app.api.rate_limit import (
    MUTATING_METHODS,
    RateLimitConfig,
    RateLimiter,
    configured_limits,
    configured_trusted_proxies,
    exceeds_body_limit,
    payload_too_large_detail,
    rate_limited_detail,
    resolve_client_address,
    unsupported_transfer_encoding_detail,
)
from app.api.store_resolution import (
    job_store_from_state,
    target_instance_store_from_state,
)
from app.api.routes.engine_refresh import router as engine_refresh_router
from app.api.routes.engines import router as engines_router
from app.api.routes.jobs import router as jobs_router
from app.api.routes.target_instances import router as target_instances_router
from app.api.routes.targets import router as targets_router
from app.engines.adapter_registry import EngineAdapterRegistry, default_adapter_registry
from app.engines.discovery.registry import DiscoveryRegistry, default_discovery_registry
from app.engines.discovery.service import CapabilityRefreshService
from app.engines.execution_policy import ExecutionPolicy
from app.engines.instance_persistence import configured_instance_store
from app.engines.job_persistence import configured_job_store
from app.engines.jobs import JobStore, default_job_store
from app.engines.target_instances import TargetInstanceStore, default_target_instance_store

__all__ = ["FRONTEND_DIR", "create_app", "app"]

#: ``<pkg>/frontend`` derived from this file: app/api/app.py -> app/api ->
#: app -> app/frontend. Never from the process working directory, so the mount
#: works regardless of where the server is started from or how the package
#: was installed (source tree, editable install or wheel).
FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"

_STATIC_PREFIX = "/static"

#: Same-origin policy for the SPA: external module script + stylesheet,
#: fetch() to the API on this origin, nothing else.  ``frame-ancestors
#: 'none'`` mirrors ``X-Frame-Options: DENY`` for CSP-aware agents.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "img-src 'self'; connect-src 'self'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)


def _frontend_available() -> bool:
    """True when both the shell document and the asset directory exist."""

    return (FRONTEND_DIR / "index.html").is_file() and (FRONTEND_DIR / "js").is_dir()


def _build_job_store(
    job_store: JobStore | None,
    job_db_path: str | Path | None,
) -> JobStore:
    """Injected store, else the configured persistent store, else in-memory.

    An injected store always wins.  Without one, an explicit
    ``job_db_path`` - or the ``PASSORCH_JOB_DB`` environment variable -
    opens a SQLite-backed store; it is created and validated here, so an
    unusable path fails fast with ``JobPersistenceError`` instead of
    silently running memory-only.  With neither configured, the plain
    in-memory default is used and no file is touched.
    """

    if isinstance(job_store, JobStore):
        return job_store
    persisted = configured_job_store(job_db_path)
    if persisted is not None:
        return persisted
    return default_job_store()


def _build_target_instance_store(
    target_instance_store: TargetInstanceStore | None,
    job_db_path: str | Path | None,
) -> TargetInstanceStore:
    """Injected store, else the configured persistent store, else in-memory.

    One ``job_db_path`` (parameter or ``PASSORCH_JOB_DB``) opens both the
    persistent job store and the persistent target-instance store over the
    same database file, so job and instance records always travel
    together.  An injected store always wins over environment
    configuration; the explicit ``job_db_path`` parameter is mutually
    exclusive with either store injection (checked in ``create_app``), and
    an unusable path fails fast instead of silently running memory-only.
    """

    if isinstance(target_instance_store, TargetInstanceStore):
        return target_instance_store
    persisted = configured_instance_store(job_db_path)
    if persisted is not None:
        return persisted
    return default_target_instance_store()


def _install_request_guards(
    application: FastAPI,
    limits: RateLimitConfig,
    expected_token: str | None,
    trusted_proxies: frozenset[str],
) -> None:
    """Phases 8E/9A/11D/12B: rate limiting (429), auth (401), body cap (413),
    transfer-encoding rejection (400), same-origin safety headers.

    Fail-closed and additive: the middleware runs before every handler and
    can only reject - it never relaxes validation, the execution policy or
    persistence.  For state-changing methods the checks run in a fixed
    order: count against the rate limiter first (so credential guessing is
    rate-limited too), then authenticate, then enforce the body bound
    (``Content-Length`` cap, then outright rejection of
    ``Transfer-Encoding`` so chunked framing cannot bypass the cap).
    Every response also carries the same-origin safety headers
    (``X-Content-Type-Options: nosniff``, ``X-Frame-Options: DENY``,
    ``Referrer-Policy: no-referrer`` and ``Content-Security-Policy``) -
    they do not change API or SPA behaviour.  Counters live on this
    application instance, keyed by the resolved client address: the
    socket peer by default (``X-Forwarded-For`` never consulted), or the
    real client hop when the peer is a trusted reverse proxy
    (``trusted_proxies``); the expected token is captured only in this
    closure - never on ``application.state`` (which carries just the
    boolean ``auth_enabled`` and the resolved trust anchor).  See the
    ``app.api.rate_limit`` and ``app.api.auth`` docstrings plus README
    for documented limits.
    """

    def harden(response: Response) -> Response:
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        return response

    limiter = RateLimiter(limits.limit, limits.window_seconds)
    application.state.rate_limiter = limiter
    application.state.max_body_bytes = limits.max_body_bytes
    application.state.auth_enabled = expected_token is not None
    application.state.trusted_proxies = trusted_proxies

    @application.middleware("http")
    async def request_guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.method in MUTATING_METHODS:
            client = request.client
            peer = "unknown" if client is None else client.host
            key = resolve_client_address(
                peer, request.headers.get("x-forwarded-for"), trusted_proxies
            )
            retry_after = limiter.check(key)
            if retry_after is not None:
                response = JSONResponse(
                    status_code=429,
                    content={"detail": rate_limited_detail(retry_after)},
                )
                response.headers["Retry-After"] = str(retry_after)
                return harden(response)
            if not is_authorized(request.headers.get("authorization"), expected_token):
                response = JSONResponse(
                    status_code=401, content={"detail": unauthorized_detail()}
                )
                response.headers["WWW-Authenticate"] = "Bearer"
                return harden(response)
            if exceeds_body_limit(
                request.headers.get("content-length"), limits.max_body_bytes
            ):
                return harden(
                    JSONResponse(
                        status_code=413, content={"detail": payload_too_large_detail()}
                    )
                )
            if request.headers.get("transfer-encoding") is not None:
                # Chunked/unknown framing would bypass the Content-Length
                # cap; refuse before any body byte is read.
                return harden(
                    JSONResponse(
                        status_code=400,
                        content={"detail": unsupported_transfer_encoding_detail()},
                    )
                )
        return harden(await call_next(request))


def create_app(
    *,
    discovery_registry: DiscoveryRegistry | None = None,
    discovery_refresh_service: CapabilityRefreshService | None = None,
    adapter_registry: EngineAdapterRegistry | None = None,
    execution_policy: ExecutionPolicy | None = None,
    job_store: JobStore | None = None,
    job_db_path: str | Path | None = None,
    target_instance_store: TargetInstanceStore | None = None,
    rate_limit: int | None = None,
    rate_window_seconds: float | None = None,
    max_body_bytes: int | None = None,
    trusted_proxies: str | Iterable[str] | None = None,
    api_token: str | None = None,
    audit: AuditLogger | None = None,
) -> FastAPI:
    """Build the PassOrch API with injectable read-only registries and store.

    M3.1 adds the audit-event core: ``audit`` injects the structured audit
    service, exposed as ``application.state.audit`` (default: the stdout
    JSON-line ``AuditLogger``).  M3.2 wires the job-create route to it -
    ``job_created`` after a successful persist and ``job_create_rejected``
    on the approved rejection paths - and M3.3 wires the discovery-refresh
    route (``refresh_requested`` / ``refresh_rejected`` /
    ``refresh_completed``), while the read routes stay silent.

    Job and target-instance persistence (Phases 8A/8B): when no store is
    injected, an explicit ``job_db_path`` - or the ``PASSORCH_JOB_DB``
    environment variable - opts into SQLite-backed persistent stores for
    jobs **and** target instances over the same database file; the
    database is opened and validated at construction (fail-fast
    ``JobPersistenceError`` / ``InstancePersistenceError``).  Each
    injected store (``job_store``, ``target_instance_store``) is
    mutually exclusive with the explicit ``job_db_path`` parameter.

    Request guards (Phase 8E/12B): state-changing requests are limited per
    client address (``rate_limit`` / ``rate_window_seconds`` default from
    ``PASSORCH_RATE_LIMIT`` / ``PASSORCH_RATE_WINDOW_SECONDS``) and their
    bodies capped at ``max_body_bytes`` (``PASSORCH_MAX_BODY_BYTES``,
    default 64 KiB).  Guards only reject - ``429`` with ``Retry-After``
    or ``413 payload_too_large`` - and invalid settings fail fast here at
    construction instead of silently disabling a guard.

    Trusted proxies (Phase 12B): by default the rate-limit key is the
    socket peer address and ``X-Forwarded-For`` is never consulted.  An
    operator running behind a reverse proxy can pass an exact-IP trust
    anchor as ``trusted_proxies`` (comma-separated string or iterable; a
    ``PASSORCH_TRUSTED_PROXIES`` environment variable is the process-wide
    equivalent, the explicit argument wins) so requests arriving *from* a
    trusted proxy are keyed by the real client hop instead.  Only the
    rate-limit key is affected: authentication, body caps and every
    other guard are unchanged, and malformed forwarding data always
    fails closed to the socket peer.

    Authentication (Phase 9A): an explicit ``api_token`` - or the
    ``PASSORCH_API_TOKEN`` environment variable - requires a valid
    ``Authorization: Bearer`` credential on state-changing requests
    (generic ``401``; reads stay open, documented).  Unset means
    authentication is off; an explicitly blank token fails fast with
    ``ValueError``.  Authentication never substitutes for the execution
    policy.

    Discovery refresh (M1.1): an optional ``discovery_refresh_service``
    (tests inject doubles) otherwise a ``CapabilityRefreshService`` is
    constructed over the *same* resolved ``DiscoveryRegistry`` the
    ``GET /api/engines`` routes read, so a refresh is immediately visible
    to them.  ``application.state.discovery_refresh_lock`` (a
    ``threading.Lock``) serialises refreshes **within this process only** -
    concurrent requests are answered with ``409 refresh_in_progress``
    instead of queueing, and multiple server processes do not coordinate.
    """

    if job_store is not None and job_db_path is not None:
        raise ValueError("job_store and job_db_path are mutually exclusive")
    if target_instance_store is not None and job_db_path is not None:
        raise ValueError("target_instance_store and job_db_path are mutually exclusive")
    limits = configured_limits(rate_limit, rate_window_seconds, max_body_bytes)
    expected_token = configured_api_token(api_token)
    trust_anchor = configured_trusted_proxies(trusted_proxies)
    application = FastAPI(
        title="PassOrch (simulated)",
        version="2d.0",
        description=(
            "Capability-aware password-auditing orchestration platform - "
            "simulation-only plan preview. No engine is ever executed."
        ),
    )
    effective_registry = (
        discovery_registry if discovery_registry is not None else default_discovery_registry()
    )
    application.state.discovery_registry = effective_registry
    # M1.1: the default refresh service is bound to the *same* registry the
    # GET routes read, so a refresh is immediately visible to them.  The lock
    # serialises refreshes within this process; it is not shared across
    # server processes (documented limitation, not a distributed lock).
    application.state.discovery_refresh_service = (
        discovery_refresh_service
        if discovery_refresh_service is not None
        else CapabilityRefreshService(registry=effective_registry)
    )
    application.state.discovery_refresh_lock = threading.Lock()
    effective_adapters = (
        adapter_registry if adapter_registry is not None else default_adapter_registry()
    )
    application.state.adapter_registry = effective_adapters
    # A default policy is bound to the injected adapter registry so an
    # injected registry is never bypassed by the process-wide default.
    application.state.execution_policy = (
        execution_policy if execution_policy is not None else ExecutionPolicy(effective_adapters)
    )
    application.state.job_store = _build_job_store(job_store, job_db_path)
    application.state.target_instance_store = _build_target_instance_store(
        target_instance_store, job_db_path
    )
    # M3.1/M3.2/M3.3: the audit-event service (closed schema, stdout JSON
    # lines, emit-never-raises).  Injectable for capturing/failing sinks in
    # tests; the job-create and discovery-refresh routes emit through it,
    # best-effort only.
    application.state.audit = audit if audit is not None else AuditLogger()
    _install_request_guards(application, limits, expected_token, trust_anchor)
    application.include_router(targets_router)
    application.include_router(engines_router)
    application.include_router(engine_refresh_router)
    application.include_router(jobs_router)
    application.include_router(target_instances_router)

    # Phase 4A frontend. Mounted at a dedicated prefix *after* the API routers so
    # nothing under /api, /docs, /redoc, /openapi.json or /health can be shadowed
    # by the static catch-all.
    _mount_frontend(application)

    @application.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok", "mode": "simulated"}

    @application.get("/health/ready", tags=["meta"])
    def readiness() -> JSONResponse:
        """Readiness probe: both stores answer a read-only listing.

        Liveness (``/health``) is unconditional; readiness additionally
        verifies - without writing anything and without touching SQLite
        (persistent stores hydrate at construction, so listings are pure
        memory reads) - that each configured store can still produce its
        snapshot.  A store failure answers one generic ``503
        not_ready`` detail: no exception text, path, SQL or traceback,
        and the failure is never logged (the application never logs).
        """

        try:
            job_store = job_store_from_state(application.state)
            target_store = target_instance_store_from_state(application.state)
            job_store.list()
            target_store.list()
        except Exception:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": {
                        "error": "not_ready",
                        "detail": "readiness probe failed",
                    }
                },
            )
        return JSONResponse(content={"status": "ok", "mode": "simulated"})

    return application


def _mount_frontend(application: FastAPI) -> None:
    """Wire the frontend shell, or serve a clear 404 when assets are absent.

    Missing assets never produce misleading content: the API keeps working,
    and ``/`` answers one generic ``404 frontend_not_installed`` detail - no
    paths, no exception text.  (The test-suite builds the shell-less app via
    this path deliberately.)
    """

    if not _frontend_available():
        @application.get("/", tags=["frontend"], include_in_schema=False)
        def frontend_not_installed() -> JSONResponse:
            return JSONResponse(
                status_code=404,
                content={
                    "detail": {
                        "error": "frontend_not_installed",
                        "detail": "the frontend assets are not included in this installation",
                    }
                },
            )

        return

    application.mount(
        _STATIC_PREFIX,
        StaticFiles(directory=FRONTEND_DIR),
        name="static",
    )

    @application.get("/", tags=["frontend"], include_in_schema=False)
    def frontend_shell() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")


app = create_app()

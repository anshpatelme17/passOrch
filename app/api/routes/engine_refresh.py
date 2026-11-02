"""M1.1 - authenticated discovery-refresh endpoint (metadata only).

``POST /api/engines/refresh`` delegates - without parameterising - to the
injected :class:`~app.engines.discovery.service.CapabilityRefreshService` and
returns its safe per-engine report.  The service decides every operation
internally from the immutable allowlist; this module adds no probe logic.

Security boundary (regression-tested by an AST scan of this module):

* the request schema carries **only** known engine *names* - never a path,
  binary name, command, subprocess argument, operation, parser, timeout,
  output limit or verification status (``extra="forbid"`` rejects any other
  key at the transport layer);
* an unknown name fails closed with ``404 unknown_engine`` - the service
  validates the whole subset *before* anything mutates, so a mixed
  valid/invalid list never runs a single probe;
* this module imports no process-execution library and nothing below
  ``CapabilityRefreshService`` (no runner, locator, allowlist or parser);
* raw probe output never reaches a response: only the closed five-field
  report is serialised, and an unexpected internal failure becomes a
  generic ``503 discovery_refresh_failed`` with **no exception text**;
* refreshes are serialised by the process-local
  ``application.state.discovery_refresh_lock``: a second request while one
  is running gets a generic ``409 refresh_in_progress`` instead of queueing
  waiters (the lock is released in a ``finally`` on every path).  The lock
  coordinates one process only - multiple server processes do not
  synchronise (a documented limitation, not a distributed lock).

M3.3 audit emission (best-effort, through ``request.app.state.audit`` only -
this module imports no ``logging``): one ``refresh_requested`` exactly once
per attempt that reaches the service call (strictly before it, and never
claiming success), one ``refresh_rejected`` on each understood rejection
(the process-local lock busy, the service's fail-closed unknown-engine
validation) with a closed reason code, and one ``refresh_completed`` per
returned ``RefreshReport`` carrying only the closed per-engine fields
(``engine``, ``discovered``, ``capability_status``, ``reason``,
``truncated``).  Unexpected internal failures and mis-wired state stay the
generic 503 and emit nothing - a generic error is not an understood
rejection.  The audit service's own failure boundary means an emission
problem can never change a status code, a response body, the refresh
result, the lock or the registry state.

The endpoint is a state-changing request, so the existing middleware chain
already applies to it unchanged: rate limit (429), bearer auth when enabled
(401), body cap (413), transfer-encoding rejection (400), security headers.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.api.audit import (
    EVENT_REFRESH_COMPLETED,
    EVENT_REFRESH_REJECTED,
    EVENT_REFRESH_REQUESTED,
)
from app.api.errors import UNKNOWN_ENGINE_ERROR, api_error, unknown_engine
from app.api.schemas import (
    EngineRefreshReport,
    RefreshEnginesRequest,
    RefreshEnginesResponse,
)
from app.engines.discovery.errors import UnknownEngineError
from app.engines.discovery.service import (
    CapabilityRefreshService,
    KNOWN_ENGINES,
    RefreshReport,
)

__all__ = [
    "DISCOVERY_REFRESH_FAILED_ERROR",
    "REFRESH_IN_PROGRESS_ERROR",
    "refresh_engines",
    "router",
]

router = APIRouter(prefix="/api/engines", tags=["engines"])

#: Structured 409: another refresh currently holds the process-local lock.
REFRESH_IN_PROGRESS_ERROR = "refresh_in_progress"

#: Structured 503: the refresh failed internally - never an exception text.
DISCOVERY_REFRESH_FAILED_ERROR = "discovery_refresh_failed"


def _refresh_in_progress() -> HTTPException:
    """Structured 409 for a refresh rejected by the process-local lock."""

    return api_error(
        409, REFRESH_IN_PROGRESS_ERROR, "another discovery refresh is already running"
    )


def _refresh_failed() -> HTTPException:
    """Structured 503 for an unexpected internal refresh failure.

    The message is fixed: no exception text, engine output, path or
    traceback ever leaves the process.
    """

    return api_error(
        503, DISCOVERY_REFRESH_FAILED_ERROR, "engine discovery refresh could not be completed"
    )


def _to_report(report: RefreshReport) -> EngineRefreshReport:
    """The ``RefreshReport`` field set, verbatim - nothing else is copied."""

    return EngineRefreshReport(
        engine=report.engine,
        discovered=report.discovered,
        capability_status=report.capability_status,
        reason=report.reason,
        truncated=report.truncated,
    )


def _requested_engine(body: RefreshEnginesRequest | None) -> str | None:
    """The one known engine a request names, else ``None``.

    An audit event may carry ``engine`` only for an exact single-element
    request drawn from the closed ``KNOWN_ENGINES`` set: a missing body, an
    empty or multi-engine list, or any name outside the closed set yields
    ``None`` so the event is emitted as a bare envelope instead.  A
    user-controlled name that is *not* a known engine is therefore never
    echoed (and the exact-type gate inside ``emit`` still applies on top).
    """

    if body is None or body.engines is None or len(body.engines) != 1:
        return None
    name = body.engines[0]
    if type(name) is not str or name not in KNOWN_ENGINES:
        return None
    return name


def _emit_audit(
    request: Request,
    *,
    event: str,
    reason: str | None = None,
    engine: str | None = None,
    discovered: bool | None = None,
    capability_status: str | None = None,
    truncated: bool | None = None,
) -> None:
    """Best-effort audit emission (M3.3): never raises into the handler.

    ``AuditLogger.emit`` already swallows construction, clock, serialization
    and sink failures; this narrow boundary additionally covers a missing or
    unusable ``state.audit``, so an audit problem can never change an HTTP
    status, a response body, the refresh result, the lock or the capability
    registry, and never leaks an exception detail.  Fields are the closed
    audit schema - nothing else can be passed.
    """

    try:
        request.app.state.audit.emit(
            event=event,
            reason=reason,
            engine=engine,
            discovered=discovered,
            capability_status=capability_status,
            truncated=truncated,
        )
    except Exception:
        return


@router.post("/refresh", response_model=RefreshEnginesResponse)
def refresh_engines(
    request: Request,
    body: RefreshEnginesRequest | None = None,
) -> RefreshEnginesResponse:
    """Run one allowlisted capability refresh (metadata discovery only).

    Missing body, ``{}`` or ``{"engines": null}`` refreshes every known
    engine; an explicit list refreshes only those engines (the service
    de-duplicates and orders deterministically).  ``404 unknown_engine``
    fails closed before any probe; ``409 refresh_in_progress`` rejects a
    concurrent refresh; ``503 discovery_refresh_failed`` covers unexpected
    internal failures without leaking exception text.

    M3.3: every attempt that reaches the service call emits exactly one
    ``refresh_requested`` first; each understood rejection (lock busy,
    unknown engine) adds exactly one ``refresh_rejected`` with the closed
    reason code matching the response envelope; each returned report adds
    one ``refresh_completed`` after the service returned and the lock was
    released.  Unexpected failures emit nothing beyond the preceding
    ``refresh_requested``, and an emission problem never changes this
    response.
    """

    service: CapabilityRefreshService | None = getattr(
        request.app.state, "discovery_refresh_service", None
    )
    lock = getattr(request.app.state, "discovery_refresh_lock", None)
    # Fail closed on missing wiring: create_app always sets both, so this
    # only trips for a mis-built app.  Test doubles are duck-typed (a
    # callable ``refresh``), mirroring the injected ``which``/``executor``
    # convention in the discovery service itself.  A mis-built app is an
    # unexpected configuration error, not an understood rejection: it emits
    # no audit event (M3.3).
    if service is None or lock is None or not callable(getattr(service, "refresh", None)):
        raise _refresh_failed()
    if not lock.acquire(blocking=False):
        # The one understood lock rejection: an actual refused attempt,
        # carrying the closed reason code that matches the 409 envelope.
        # The request never reached the refresh operation, so there is no
        # preceding refresh_requested - exactly one event, no duplicates.
        _emit_audit(
            request,
            event=EVENT_REFRESH_REJECTED,
            reason=REFRESH_IN_PROGRESS_ERROR,
            engine=_requested_engine(body),
        )
        raise _refresh_in_progress()
    try:
        # Exactly once per attempt that reaches the operation, strictly
        # before the service call, and never claiming success: only the
        # optional closed-set engine field may appear.
        _emit_audit(
            request,
            event=EVENT_REFRESH_REQUESTED,
            engine=_requested_engine(body),
        )
        try:
            reports = service.refresh(body.engines if body is not None else None)
        except UnknownEngineError:
            # The service's fail-closed subset validation - the only
            # understood in-operation rejection.  The offending name is
            # user input outside the closed engine set, so it is never
            # echoed: the event carries the closed reason (and ``engine``
            # only when the request named exactly one known engine).
            _emit_audit(
                request,
                event=EVENT_REFRESH_REJECTED,
                reason=UNKNOWN_ENGINE_ERROR,
                engine=_requested_engine(body),
            )
            raise unknown_engine() from None
        except HTTPException:
            raise
        except Exception:
            # Unexpected internal failure: the generic 503, not a
            # rejection - no misleading refresh_rejected, and no
            # refresh_completed because no report was returned.
            raise _refresh_failed() from None
    finally:
        lock.release()
    # Only a returned report reaches this point.  One bounded per-engine
    # summary per report, after the service returned and the lock was
    # released: closed fields only, with ``discovered`` forwarded verbatim
    # from the report (never inferred from status, reason or truncation);
    # an empty report reason is omitted by ``emit``; never raw output,
    # argv, paths or exception text.
    for report in reports:
        _emit_audit(
            request,
            event=EVENT_REFRESH_COMPLETED,
            engine=report.engine,
            discovered=report.discovered,
            capability_status=report.capability_status,
            reason=report.reason,
            truncated=report.truncated,
        )
    return RefreshEnginesResponse(engines=[_to_report(report) for report in reports])

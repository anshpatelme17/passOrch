"""Steps 12/13B follow-up - shared fail-closed store resolution for the API.

Every HTTP-facing read or write needs the store bound to *this* application's
state.  Application state is considered miswired (not merely absent) when an
attribute is present but fails its store's runtime type check; in that case
the request fails closed with the shared generic ``503
persistence_unavailable`` detail - no exception text, class name, repr or
path - and the foreign object is never read or written, so it can neither
contaminate a process-wide default nor crash a handler with a raw attribute
error.

Resolution rules, preserved verbatim from the previous per-module
implementations in ``app.api.routes.jobs`` (``_resolve_job_store``,
``_resolve_target_instance_store``) and ``app.api.routes.target_instances``
(``_get_store``):

* an absent or ``None`` ``job_store`` / ``target_instance_store`` attribute
  keeps the historical fallback to the process-wide default store (the
  documented unconfigured state) - it is never treated as a failure;
* a present store is accepted only once it passes its concrete
  :class:`~app.engines.jobs.JobStore` or
  :class:`~app.engines.target_instances.TargetInstanceStore` check;
* a wrong-typed store is a failure, never a silent fallback.

This module imports nothing from the routers, so both routers (and the
``app.api.app`` readiness probe) can depend on it without a cycle while the
error body stays byte-identical.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from app.engines.jobs import JobStore, default_job_store
from app.engines.target_instances import (
    TargetInstanceStore,
    default_target_instance_store,
)

__all__ = [
    "PERSISTENCE_ERROR",
    "job_store_from_state",
    "persistence_unavailable_detail",
    "resolve_job_store",
    "resolve_target_instance_store",
    "target_instance_store_from_state",
]

#: Stable, structured 503 code for a failed job-storage write (Phases 8A/8B).
PERSISTENCE_ERROR = "persistence_unavailable"


def persistence_unavailable_detail(subject: str = "job") -> dict[str, str]:
    """Structured 503 detail for a failed persistence write (Phases 8A/8B).

    Rebuilt per call like the other detail helpers and deliberately
    generic: SQL text, file paths and database-driver messages stay
    chained on the server-side exception and never reach an API response.
    ``subject`` names the store (``"job"``, ``"target instance"``) so each
    endpoint's message describes the store it actually uses.
    """

    return {
        "error": PERSISTENCE_ERROR,
        "detail": f"{subject} storage is temporarily unavailable",
    }


def job_store_from_state(state: Any) -> JobStore:
    """The job store behind this state (routers and the readiness probe).

    An absent store keeps the historical fallback to the process-wide
    default (unconfigured state).  A store that is present but fails the
    ``JobStore`` type check means application state was miswired: the
    request fails closed with the shared generic ``503
    persistence_unavailable`` detail - never the offending object's class
    name, repr or path - and the foreign object is never read or written.
    """

    store = getattr(state, "job_store", None)
    if store is None:
        return default_job_store()
    if not isinstance(store, JobStore):
        raise HTTPException(
            status_code=503,
            detail=persistence_unavailable_detail(),
        )
    return store


def target_instance_store_from_state(state: Any) -> TargetInstanceStore:
    """The target-instance store behind this state (routers and readiness).

    An absent store keeps the historical fallback to the process-wide
    default (unconfigured state).  A store that is present but fails the
    ``TargetInstanceStore`` type check means application state was
    miswired: the request fails closed with the shared generic ``503
    persistence_unavailable`` detail - never the offending object's class
    name, repr or path - and the foreign object is never read or written,
    so it can neither contaminate the default store nor crash a handler
    with a raw attribute error.
    """

    store = getattr(state, "target_instance_store", None)
    if store is None:
        return default_target_instance_store()
    if not isinstance(store, TargetInstanceStore):
        raise HTTPException(
            status_code=503,
            detail=persistence_unavailable_detail("target instance"),
        )
    return store


def resolve_job_store(request: Request) -> JobStore:
    """The job store bound to this application's request."""

    return job_store_from_state(request.app.state)


def resolve_target_instance_store(request: Request) -> TargetInstanceStore:
    """The target-instance store bound to this application's request."""

    return target_instance_store_from_state(request.app.state)

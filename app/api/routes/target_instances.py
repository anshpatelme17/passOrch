"""Phase 3C/7B/7D/8B/4 - individual target instance API endpoints.

Mutations (``POST`` / ``PATCH`` / ``DELETE``) answer ``HTTP 503`` with the
shared structured ``persistence_unavailable`` detail when a configured
persistent store fails the write (Phase 8B); reads are served from the
in-memory store and never touch the database.  All other contracts -
validation, ordering, filtering, pagination and 404 identifiers - are
unchanged from Phase 7D.  Phase 4 adds ``PATCH``: only ``name`` and
``description`` are mutable (``id``, ``target_type`` and ``created_at``
are immutable, enforced by ``extra="forbid"``), a record keeps its
insertion position, and unknown or malformed identifiers answer the same
generic 404 as ``GET``/``DELETE``.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from app.api.errors import instance_not_found, invalid_request
from app.api.routes.jobs import persistence_unavailable_detail
from app.api.schemas import (
    TargetInstance,
    TargetInstanceCreateRequest,
    TargetInstancePageResponse,
    TargetInstanceUpdateRequest,
)
from app.core.orchestrator.target_instances import (
    create_target_instance,
    update_target_instance,
)
from app.engines.persistence import PersistenceError
from app.engines.target_instances import (
    DuplicateTargetInstanceError,
    InvalidTargetInstanceError,
    TargetInstanceRecord,
    TargetInstanceStore,
    default_target_instance_store,
)

router = APIRouter(tags=["target-instances"])

__all__ = ["router"]


def _get_store(request: Request) -> TargetInstanceStore:
    store = getattr(request.app.state, "target_instance_store", None)
    if store is not None:
        return store
    return default_target_instance_store()


def _to_response(record: TargetInstanceRecord) -> TargetInstance:
    return TargetInstance(
        id=record.id,
        name=record.name,
        target_type=record.target_type,
        description=record.description,
        created_at=record.created_at,
    )


@router.post("/api/target-instances", response_model=TargetInstance, status_code=201)
def create_target_instance_endpoint(
    body: TargetInstanceCreateRequest, request: Request
) -> TargetInstance:
    store = _get_store(request)
    try:
        record = create_target_instance(
            body.name,
            body.target_type,
            body.description,
            store=store,
        )
    except ValueError as exc:
        raise invalid_request(str(exc)) from exc
    except (InvalidTargetInstanceError, DuplicateTargetInstanceError) as exc:
        raise invalid_request(str(exc)) from exc
    except PersistenceError as exc:
        raise HTTPException(
            status_code=503,
            detail=persistence_unavailable_detail("target instance"),
        ) from exc
    return _to_response(record)


@router.get("/api/target-instances", response_model=TargetInstancePageResponse)
def list_target_instances(
    request: Request,
    target_type: str | None = None,
    name: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> TargetInstancePageResponse:
    """List target instances, insertion-ordered (oldest-first), filtered and paged.

    ``target_type`` is an exact match and ``name`` a case-insensitive substring
    match; both are plain value filters applied **before** pagination, so
    ``total`` counts the filtered set. An unknown value simply yields an empty
    page (HTTP 200), consistent with the job list filters.
    """

    if limit < 1 or limit > 100:
        raise invalid_request("limit must be between 1 and 100")
    if offset < 0:
        raise invalid_request("offset must be non-negative")

    store = _get_store(request)
    records = list(store.list())
    if target_type is not None:
        records = [r for r in records if r.target_type == target_type]
    if name is not None:
        needle = name.lower()
        records = [r for r in records if needle in r.name.lower()]
    total = len(records)
    paginated = records[offset : offset + limit]
    instances = [
        TargetInstance(
            id=r.id,
            name=r.name,
            target_type=r.target_type,
            description=r.description,
            created_at=r.created_at,
        )
        for r in paginated
    ]
    return TargetInstancePageResponse(
        instances=instances,
        count=len(instances),
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/api/target-instances/{instance_id}", response_model=TargetInstance)
def get_target_instance(instance_id: str, request: Request) -> TargetInstance:
    store = _get_store(request)
    record = store.get(instance_id)
    if record is None:
        raise instance_not_found()
    return _to_response(record)


@router.patch("/api/target-instances/{instance_id}", response_model=TargetInstance)
def update_target_instance_endpoint(
    instance_id: str, body: TargetInstanceUpdateRequest, request: Request
) -> TargetInstance:
    """Update the mutable fields of one target instance.

    ``name`` and ``description`` are the only mutable fields - at least
    one must be present (empty patch is a structured 422), and ``id``,
    ``target_type`` and ``created_at`` are immutable (``extra="forbid"``
    turns any attempt to send them into a 422).  Unknown or malformed
    identifiers answer the same generic 404 as ``GET``; a configured
    persistent store that fails the mirrored write answers the shared
    ``503 persistence_unavailable`` with the in-memory record left
    truthful (the failed update is never half-applied).
    """

    store = _get_store(request)
    try:
        record = update_target_instance(
            instance_id,
            name=body.name,
            description=body.description,
            store=store,
        )
    except ValueError as exc:
        raise invalid_request(str(exc)) from exc
    except InvalidTargetInstanceError as exc:
        raise invalid_request(str(exc)) from exc
    except PersistenceError as exc:
        raise HTTPException(
            status_code=503,
            detail=persistence_unavailable_detail("target instance"),
        ) from exc
    if record is None:
        raise instance_not_found()
    return _to_response(record)


@router.delete("/api/target-instances/{instance_id}", status_code=204, response_class=Response)
def delete_target_instance(instance_id: str, request: Request) -> Response:
    store = _get_store(request)
    try:
        deleted = store.delete(instance_id)
    except PersistenceError as exc:
        raise HTTPException(
            status_code=503,
            detail=persistence_unavailable_detail("target instance"),
        ) from exc
    if not deleted:
        raise instance_not_found()
    return Response(status_code=204)

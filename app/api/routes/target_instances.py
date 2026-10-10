"""Phase 3C - individual target instance API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from app.api.schemas import (
    TargetInstance,
    TargetInstanceCreateRequest,
    TargetInstancesResponse,
)
from app.core.orchestrator.target_instances import create_target_instance
from app.engines.target_instances import (
    DuplicateTargetInstanceError,
    InvalidTargetInstanceError,
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
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (InvalidTargetInstanceError, DuplicateTargetInstanceError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return TargetInstance(
        id=record.id,
        name=record.name,
        target_type=record.target_type,
        description=record.description,
        created_at=record.created_at,
    )


@router.get("/api/target-instances", response_model=TargetInstancesResponse)
def list_target_instances(request: Request) -> TargetInstancesResponse:
    store = _get_store(request)
    records = store.list()
    instances = [
        TargetInstance(
            id=r.id,
            name=r.name,
            target_type=r.target_type,
            description=r.description,
            created_at=r.created_at,
        )
        for r in records
    ]
    return TargetInstancesResponse(instances=instances, count=len(instances))


@router.get("/api/target-instances/{instance_id}", response_model=TargetInstance)
def get_target_instance(instance_id: str, request: Request) -> TargetInstance:
    store = _get_store(request)
    record = store.get(instance_id)
    if record is None:
        raise HTTPException(status_code=404, detail="target_instance_not_found")
    return TargetInstance(
        id=record.id,
        name=record.name,
        target_type=record.target_type,
        description=record.description,
        created_at=record.created_at,
    )


@router.delete("/api/target-instances/{instance_id}", status_code=204, response_class=Response)
def delete_target_instance(instance_id: str, request: Request) -> Response:
    store = _get_store(request)
    deleted = store.delete(instance_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="target_instance_not_found")
    return Response(status_code=204)

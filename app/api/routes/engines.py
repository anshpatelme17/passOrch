"""Phase 3A - read-only engine capability API.

* ``GET /api/engines``         - every registered engine's capability state.
* ``GET /api/engines/{engine}`` - one engine's capability state.

Both endpoints are strictly read-only:

* they never invoke :class:`~app.engines.discovery.service.CapabilityRefreshService`,
  so no engine probing and no subprocess can ever be triggered by a GET;
* they never mutate the discovery registry;
* the payload is validated with the existing
  :meth:`DiscoveryRegistry.validate_payload` before it is returned, so raw
  probe output keys (``stdout``, ``stderr``, ``command``, ``argv``, ...) can
  never appear in a response.

Phase 3A deliberately adds **no** refresh/probe endpoint: refreshing stays a
code/test-invoked action through the injected service.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.api.schemas import EngineInfo, EnginesResponse
from app.engines.discovery.models import KNOWN_ENGINES
from app.engines.discovery.registry import (
    DiscoveryRegistry,
    default_discovery_registry,
)

__all__ = ["engine_detail", "list_engines", "router"]

router = APIRouter(prefix="/api/engines", tags=["engines"])


def _registry(request: Request) -> DiscoveryRegistry:
    registry = getattr(request.app.state, "discovery_registry", None)
    if isinstance(registry, DiscoveryRegistry):
        return registry
    return default_discovery_registry()


@router.get("", response_model=EnginesResponse)
def list_engines(request: Request) -> EnginesResponse:
    """Validated capability payload for every registered engine."""

    registry = _registry(request)
    payload = registry.payload()
    registry.validate_payload(payload)
    return EnginesResponse(**payload)


@router.get("/{engine}", response_model=EngineInfo)
def engine_detail(engine: str, request: Request) -> EngineInfo:
    """Capability payload for one engine (404 for unknown/unregistered)."""

    if engine not in KNOWN_ENGINES:
        raise HTTPException(status_code=404, detail="unknown_engine")

    registry = _registry(request)
    entry = registry.get(engine)
    if entry is None:
        raise HTTPException(status_code=404, detail="engine_not_registered")

    payload = registry.entry_payload(entry)
    registry.validate_payload({"engines": [payload]})
    return EngineInfo(**payload)

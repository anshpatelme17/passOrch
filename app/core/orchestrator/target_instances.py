"""Phase 3C/4 - target instance orchestration.

``create_target_instance`` is the only authorised way to mint and persist a
target instance record.  It validates the name / target type against the
existing canonical catalog (``app.engines.discovery.capability_map``), mints a
canonical UUID ``instance_id`` (freshly minted unless supplied), stamps an
epoch ``created_at`` via an injectable clock, and stores the record through an
injectable :class:`~app.engines.target_instances.TargetInstanceStore`
(default: the process-wide singleton).

``update_target_instance`` is the only authorised way to mutate one: the
mutable fields (``name``, ``description``) go through the same store gate;
``id``, ``target_type`` and ``created_at`` stay immutable.

Security boundary:

* no process execution, no subprocess, no shell;
* no discovery refresh, no engine invocation, no real execution;
* the store is the single gate: structural / duplicate failures raise
  :class:`~app.engines.target_instances.InvalidTargetInstanceError` /
  :class:`~app.engines.target_instances.DuplicateTargetInstanceError`;
* IDs for unknown or malformed lookups never expose a record.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

from app.engines.target_instances import (
    TargetInstanceRecord,
    TargetInstanceStore,
    default_target_instance_store,
    is_valid_target_type,
)

__all__ = ["create_target_instance", "resolve_target_instance", "update_target_instance"]


def resolve_target_instance(
    instance_id: object,
    *,
    store: TargetInstanceStore | None = None,
) -> TargetInstanceRecord | None:
    """The stored record for ``instance_id``, else ``None``.

    Malformed and unknown identifiers return ``None`` identically - no caller
    can distinguish them.  An omitted or ``None`` store keeps the historical
    fallback to the process-wide default; an explicitly supplied object that
    is not a ``TargetInstanceStore`` is rejected with ``ValueError`` rather
    than silently using the default store.
    """

    if store is None:
        effective = default_target_instance_store()
    elif not isinstance(store, TargetInstanceStore):
        raise ValueError("store must be a TargetInstanceStore")
    else:
        effective = store
    return effective.get(instance_id)


def create_target_instance(
    name: str,
    target_type: str,
    description: str = "",
    *,
    instance_id: str | None = None,
    clock: Callable[[], int] | None = None,
    store: TargetInstanceStore | None = None,
) -> TargetInstanceRecord:
    """Mint, validate and persist a target instance record.

    ``instance_id`` is a canonical lowercase UUID (freshly minted unless
    supplied); ``created_at`` is the epoch second from ``clock`` (real time by
    default).  Structural and duplicate failures raise from ``store.put``.
    An omitted or ``None`` store keeps the historical fallback to the
    process-wide default; an explicitly supplied object that is not a
    ``TargetInstanceStore`` is rejected with ``ValueError`` rather than
    silently using the default store.
    """

    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be non-empty")
    if not is_valid_target_type(target_type):
        raise ValueError(f"target_type is not a valid mapped type: {target_type!r}")

    now = clock if clock is not None else lambda: int(time.time())
    identifier = instance_id if isinstance(instance_id, str) and instance_id else str(uuid.uuid4())

    record = TargetInstanceRecord(
        id=identifier,
        name=name,
        target_type=target_type,
        description=description if isinstance(description, str) else "",
        created_at=now(),
    )

    if store is None:
        effective = default_target_instance_store()
    elif not isinstance(store, TargetInstanceStore):
        raise ValueError("store must be a TargetInstanceStore")
    else:
        effective = store
    return effective.put(record)


def update_target_instance(
    instance_id: object,
    *,
    name: str | None = None,
    description: str | None = None,
    store: TargetInstanceStore | None = None,
) -> TargetInstanceRecord | None:
    """Apply a mutable-field update to a stored target instance.

    Only ``name`` and ``description`` are mutable; the immutable fields
    (``id``, ``target_type``, ``created_at``) are never touched and an
    update keeps the record's insertion position.  Unknown or malformed
    identifiers answer ``None`` (the route turns that into a generic
    404); invalid replacement values raise
    :class:`~app.engines.target_instances.InvalidTargetInstanceError`
    from the store; an empty patch (no mutable field supplied) raises
    ``ValueError``.  No process execution, no discovery, no engine
    invocation - the store remains the single gate.  An omitted or ``None``
    store keeps the historical fallback to the process-wide default; an
    explicitly supplied object that is not a ``TargetInstanceStore`` is
    rejected with ``ValueError`` rather than silently using the default
    store.
    """

    if name is None and description is None:
        raise ValueError("at least one of name or description is required")
    if store is None:
        effective = default_target_instance_store()
    elif not isinstance(store, TargetInstanceStore):
        raise ValueError("store must be a TargetInstanceStore")
    else:
        effective = store
    return effective.update(instance_id, name=name, description=description)

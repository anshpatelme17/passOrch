"""Phase 3C - individual target instance management data layer.

Immutable target instance records and a bounded, thread-safe in-memory store.
Reuses the canonical target types from `app.engines.discovery.capability_map`
as the sole source of valid target types. No persistence, no auth, no workers,
no execution.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Iterator

from app.engines.discovery.capability_map import TARGET_FORMAT_POLICY

__all__ = [
    "DEFAULT_TARGET_INSTANCE_STORE_CAPACITY",
    "InvalidTargetInstanceError",
    "DuplicateTargetInstanceError",
    "TargetInstanceError",
    "TargetInstanceRecord",
    "TargetInstanceStore",
    "default_target_instance_store",
    "is_valid_target_type",
    "generate_instance_id",
    "MAX_NAME_LENGTH",
    "MAX_DESCRIPTION_LENGTH",
]

DEFAULT_TARGET_INSTANCE_STORE_CAPACITY = 100

#: Maximum accepted length of a target instance name.
MAX_NAME_LENGTH = 128

#: Maximum accepted length of a target instance description.
MAX_DESCRIPTION_LENGTH = 1024

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def is_valid_target_type(target_type: str) -> bool:
    if not isinstance(target_type, str):
        return False
    return target_type in TARGET_FORMAT_POLICY


def generate_instance_id() -> str:
    import uuid

    return str(uuid.uuid4()).lower()


class TargetInstanceError(Exception):
    pass


class InvalidTargetInstanceError(TargetInstanceError):
    pass


class DuplicateTargetInstanceError(TargetInstanceError):
    pass


def _require_str(value: object, message: str) -> str:
    """Narrow an untrusted value to ``str`` or raise (update path only)."""

    if not isinstance(value, str):
        raise InvalidTargetInstanceError(message)
    return value


@dataclass(frozen=True, slots=True)
class TargetInstanceRecord:
    id: str
    name: str
    target_type: str
    description: str
    created_at: int


class TargetInstanceStore:
    def __init__(self, capacity: int = DEFAULT_TARGET_INSTANCE_STORE_CAPACITY) -> None:
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1:
            raise InvalidTargetInstanceError(
                f"store capacity must be an integer >= 1 (got {capacity!r})"
            )
        self._capacity = capacity
        self._instances: OrderedDict[str, TargetInstanceRecord] = OrderedDict()
        self._lock = threading.RLock()

    @property
    def capacity(self) -> int:
        return self._capacity

    def put(self, instance: TargetInstanceRecord) -> TargetInstanceRecord:
        if not isinstance(instance, TargetInstanceRecord):
            raise InvalidTargetInstanceError("a TargetInstanceRecord is required")
        if not _UUID_RE.fullmatch(instance.id):
            raise InvalidTargetInstanceError(
                f"instance id is not a canonical UUID: {instance.id!r}"
            )
        self._validate_fields(instance)
        with self._lock:
            if instance.id in self._instances:
                raise DuplicateTargetInstanceError(instance.id)
            self._instances[instance.id] = instance
            if len(self._instances) > self._capacity:
                self._instances.popitem(last=False)
        return instance

    @staticmethod
    def _validate_fields(instance: TargetInstanceRecord) -> None:
        """Structural checks shared by ``put`` and ``update`` (no I/O, no locks)."""

        if not isinstance(instance.name, str) or not instance.name.strip():
            raise InvalidTargetInstanceError("name must be a non-empty string")
        if len(instance.name) > MAX_NAME_LENGTH:
            raise InvalidTargetInstanceError(f"name must be at most {MAX_NAME_LENGTH} characters")
        if not is_valid_target_type(instance.target_type):
            raise InvalidTargetInstanceError(
                f"target_type is not a valid mapped type: {instance.target_type!r}"
            )
        if not isinstance(instance.description, str):
            raise InvalidTargetInstanceError("description must be a string")
        if len(instance.description) > MAX_DESCRIPTION_LENGTH:
            raise InvalidTargetInstanceError(
                f"description must be at most {MAX_DESCRIPTION_LENGTH} characters"
            )
        if not isinstance(instance.created_at, int) or isinstance(instance.created_at, bool):
            raise InvalidTargetInstanceError("created_at must be an integer")

    def _updated_record(
        self,
        existing: TargetInstanceRecord,
        name: object,
        description: object,
    ) -> TargetInstanceRecord:
        """The validated replacement record for a mutable-field update.

        ``None`` means "leave unchanged"; the immutable fields (``id``,
        ``target_type``, ``created_at``) are always carried over from
        ``existing``.  Invalid replacement values raise
        :class:`InvalidTargetInstanceError` before anything is changed.
        """

        candidate = TargetInstanceRecord(
            id=existing.id,
            name=(
                existing.name
                if name is None
                else _require_str(name, "name must be a non-empty string")
            ),
            target_type=existing.target_type,
            description=(
                existing.description
                if description is None
                else _require_str(description, "description must be a string")
            ),
            created_at=existing.created_at,
        )
        self._validate_fields(candidate)
        return candidate

    def update(
        self,
        instance_id: object,
        *,
        name: object = None,
        description: object = None,
    ) -> TargetInstanceRecord | None:
        """Replace the mutable fields of a stored record in place.

        Only ``name`` and ``description`` are mutable; ``id``,
        ``target_type`` and ``created_at`` are immutable and the record
        keeps its position in insertion order (no FIFO eviction runs for
        an update).  Malformed or unknown identifiers answer ``None``
        identically, like :meth:`get`; invalid replacement values raise
        :class:`InvalidTargetInstanceError` before anything is changed.
        """

        if not isinstance(instance_id, str) or not _UUID_RE.fullmatch(instance_id):
            return None
        with self._lock:
            existing = self._instances.get(instance_id)
            if existing is None:
                return None
            candidate = self._updated_record(existing, name, description)
            self._instances[instance_id] = candidate
            return candidate

    def get(self, instance_id: object) -> TargetInstanceRecord | None:
        if not isinstance(instance_id, str) or not _UUID_RE.fullmatch(instance_id):
            return None
        with self._lock:
            return self._instances.get(instance_id)

    def exists(self, instance_id: object) -> bool:
        return self.get(instance_id) is not None

    def list(self) -> tuple[TargetInstanceRecord, ...]:
        with self._lock:
            return tuple(self._instances.values())

    def delete(self, instance_id: object) -> bool:
        if not isinstance(instance_id, str) or not _UUID_RE.fullmatch(instance_id):
            return False
        with self._lock:
            if instance_id in self._instances:
                del self._instances[instance_id]
                return True
            return False

    def reset(self) -> None:
        with self._lock:
            self._instances.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._instances)

    def __iter__(self) -> Iterator[TargetInstanceRecord]:
        with self._lock:
            return iter(tuple(self._instances.values()))


_DEFAULT_TARGET_INSTANCE_STORE = TargetInstanceStore()


def default_target_instance_store() -> TargetInstanceStore:
    return _DEFAULT_TARGET_INSTANCE_STORE

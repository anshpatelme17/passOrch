"""Phase 8B - durable target instance persistence: a SQLite mirror.

Adds an *opt-in* durable layer on top of the Phase 3C in-memory
:class:`~app.engines.target_instances.TargetInstanceStore`, configured by
the same single ``job_db_path`` / ``PASSORCH_JOB_DB`` path as the Phase 8A
job store so one database file always carries one consistent view:

* :class:`InstancePersistenceError` - the single, uniform failure this
  module raises (initialisation, read, corruption and write failures
  alike); it subclasses the shared :class:`~app.engines.persistence.PersistenceError`;
* :class:`PersistentTargetInstanceStore` - a ``TargetInstanceStore``
  subclass: the bounded, lock-guarded in-memory container stays
  authoritative for every read while ``put`` / ``update`` / ``delete`` /
  ``reset`` are mirrored to the database file;
* :func:`configured_instance_store` - shared path resolution (an explicit
  ``create_app(job_db_path=...)`` first, then ``PASSORCH_JOB_DB``).

Contract, mirroring the job module exactly:

* side-effect free until used - nothing runs at import, and a database
  file is only ever created for an explicitly configured path;
* one connection per operation - opened, committed or rolled back and
  closed inside a single call: no lifespan hook, no long-lived handle;
* fail-closed - any database failure raises ``InstancePersistenceError``;
  the store never silently degrades to memory-only, never discards an
  unreadable or forbidden record, and ``put`` compensates the in-memory
  copy when the mirrored insert fails so the two views never diverge
  silently;
* metadata only - rows carry the JSON form of ``TargetInstanceRecord``
  (``id``, ``name``, ``target_type``, ``description``, ``created_at``);
  every hydrated payload is re-scanned for forbidden command / argv /
  path style keys before it re-enters the store;
* parameterised SQL only, stdlib ``sqlite3`` only - no subprocess, no
  shell, no new dependency.

Rows are keyed by an autoincrement ``seq`` so hydration restores the
exact insertion order (oldest-first listing, FIFO-by-insertion eviction);
opening prunes any row beyond ``capacity``, which also self-heals the
rare case of a partially failed write.  Deleting an instance never
touches job rows: recorded jobs keep their immutable instance snapshot.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.engines.persistence import (
    PersistenceDatabase,
    PersistenceError,
    as_integer,
    as_mapping,
    as_text,
    collect_forbidden_keys,
    configured_database_path,
    validate_storage_path,
)
from app.engines.target_instances import (
    DEFAULT_TARGET_INSTANCE_STORE_CAPACITY,
    DuplicateTargetInstanceError,
    TargetInstanceError,
    TargetInstanceRecord,
    TargetInstanceStore,
)

__all__ = [
    "InstancePersistenceError",
    "PersistentTargetInstanceStore",
    "configured_instance_store",
]

#: Generic failure messages - never interpolated with SQL text, file paths
#: or driver messages, so nothing sensitive can reach an API response.
_INIT_FAILED = "failed to initialize instance database"
_READ_FAILED = "failed to read persisted instances"
_WRITE_FAILED = "failed to write an instance record"
_UPDATE_FAILED = "failed to update an instance record"
_DELETE_FAILED = "failed to delete an instance record"
_CLEAR_FAILED = "failed to clear persisted instances"
_INVALID_RECORD = "persisted instance record is invalid"

_INSERT_INSTANCE_SQL = "INSERT INTO passorch_instances (instance_id, payload) VALUES (?, ?)"
_SELECT_INSTANCES_SQL = "SELECT seq, payload FROM passorch_instances ORDER BY seq ASC"
_PRUNE_INSTANCES_SQL = "DELETE FROM passorch_instances WHERE seq < ?"
_DELETE_INSTANCE_SQL = "DELETE FROM passorch_instances WHERE instance_id = ?"
_UPDATE_INSTANCE_SQL = "UPDATE passorch_instances SET payload = ? WHERE instance_id = ?"
_CLEAR_INSTANCES_SQL = "DELETE FROM passorch_instances"


class InstancePersistenceError(PersistenceError):
    """A database operation (or its configuration) failed.

    The only failure this module surfaces.  Routes translate it into a
    structured HTTP 503; the message stays generic so no SQL text, path
    or driver string can leak to an API client.
    """


# --------------------------------------------------------------------------- #
# serialisation: typed records -> JSON (explicit fields only)
# --------------------------------------------------------------------------- #


def _record_to_payload(record: TargetInstanceRecord) -> str:
    payload = {
        "id": record.id,
        "name": record.name,
        "target_type": record.target_type,
        "description": record.description,
        "created_at": record.created_at,
    }
    try:
        return json.dumps(payload, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise InstancePersistenceError(_WRITE_FAILED) from exc


# --------------------------------------------------------------------------- #
# deserialisation: JSON -> typed records, failing closed on anything odd
# --------------------------------------------------------------------------- #


def _record_from_payload(payload: object) -> TargetInstanceRecord:
    """Parse one stored payload - fail closed on anything unexpected."""

    try:
        if not isinstance(payload, str):
            raise TypeError("payload must be a string")
        data = json.loads(payload)
        forbidden = collect_forbidden_keys(data)
        if forbidden:
            raise ValueError(f"payload contains forbidden fields: {', '.join(forbidden)}")
        record = as_mapping(data, "instance record")
        return TargetInstanceRecord(
            id=as_text(record["id"], "instance id"),
            name=as_text(record["name"], "instance name"),
            target_type=as_text(record["target_type"], "instance target_type"),
            description=as_text(record["description"], "instance description"),
            created_at=as_integer(record["created_at"], "instance created_at"),
        )
    except Exception as exc:
        raise InstancePersistenceError(_INVALID_RECORD) from exc


# --------------------------------------------------------------------------- #
# SQLite facade: one connection per operation, parameterised SQL only
# --------------------------------------------------------------------------- #


class _InstanceDatabase:
    """The ``passorch_instances`` table behind :class:`PersistentTargetInstanceStore`."""

    def __init__(self, path: Path) -> None:
        self._db = PersistenceDatabase(path, error_type=InstancePersistenceError)

    @property
    def path(self) -> Path:
        return self._db.path

    def open(self, failure: str) -> None:
        """Create the schema and run pending migrations (construction only)."""

        self._db.open(failure)

    def insert_instance(self, record: TargetInstanceRecord) -> None:
        payload = _record_to_payload(record)
        self._db.write(
            _INSERT_INSTANCE_SQL,
            (record.id, payload),
            failure=_WRITE_FAILED,
            on_integrity=DuplicateTargetInstanceError(record.id),
        )

    def insert_instance_with_eviction(
        self, record: TargetInstanceRecord, victim_id: str
    ) -> None:
        """Insert ``record`` and delete the FIFO victim row in ONE transaction.

        Both statements run on the same connection so the mirror change is
        all-or-nothing: a failure at either stage (especially the
        victim-row delete) rolls the insert back too, leaving the database
        with neither the new row nor the stale victim row.  The put
        compensation path then restores the in-memory view to match.

        The transaction commits only after both statements succeed.  The
        driver raises for the insert's uniqueness conflict is mapped here
        to the domain duplicate error (``transaction`` itself maps any
        other driver failure onto the generic ``failure`` message); the
        exception still trips the rollback before it propagates.
        """

        payload = _record_to_payload(record)
        with self._db.transaction(_WRITE_FAILED) as connection:
            try:
                connection.execute(_INSERT_INSTANCE_SQL, (record.id, payload))
            except sqlite3.IntegrityError as exc:
                raise DuplicateTargetInstanceError(record.id) from exc
            connection.execute(_DELETE_INSTANCE_SQL, (victim_id,))

    def delete_instance(self, instance_id: str) -> bool:
        return (
            self._db.write(_DELETE_INSTANCE_SQL, (instance_id,), failure=_DELETE_FAILED) > 0
        )

    def update_instance(self, record: TargetInstanceRecord) -> bool:
        payload = _record_to_payload(record)
        return (
            self._db.write(_UPDATE_INSTANCE_SQL, (payload, record.id), failure=_UPDATE_FAILED)
            > 0
        )

    def load_instances(self, capacity: int) -> tuple[TargetInstanceRecord, ...]:
        with self._db.transaction(_READ_FAILED) as connection:
            rows = connection.execute(_SELECT_INSTANCES_SQL).fetchall()
            if len(rows) > capacity:
                cutoff_seq = rows[len(rows) - capacity][0]
                connection.execute(_PRUNE_INSTANCES_SQL, (cutoff_seq,))
                rows = rows[len(rows) - capacity :]
            return tuple(_record_from_payload(payload) for _, payload in rows)

    def clear(self) -> None:
        self._db.write(_CLEAR_INSTANCES_SQL, (), failure=_CLEAR_FAILED)


# --------------------------------------------------------------------------- #
# public store
# --------------------------------------------------------------------------- #


class PersistentTargetInstanceStore(TargetInstanceStore):
    """Bounded in-memory instance store with a durable SQLite mirror.

    Reads are served from memory exactly like the base class.  ``put``,
    ``delete`` and ``reset`` additionally mirror the change to the
    database at ``db_path``; a failed mirror write raises
    :class:`InstancePersistenceError` and leaves the in-memory view
    truthful (a failed ``put`` rolls back its own insert, a failed
    ``delete`` removes nothing).  An eviction-bearing ``put`` mirrors its
    insert and FIFO-row delete as one atomic transaction, so neither view
    can report a row the other never saw.

    Opening happens in the constructor: the schema is brought to the
    current version (running any pending Phase 8C migrations, stamping
    ``PRAGMA user_version``) and the newest ``capacity`` rows are
    hydrated in insertion order.  An unusable, corrupt, newer-schema or
    invalid database raises ``InstancePersistenceError`` instead of
    silently starting empty.
    """

    def __init__(
        self,
        db_path: str | Path,
        capacity: int = DEFAULT_TARGET_INSTANCE_STORE_CAPACITY,
    ) -> None:
        super().__init__(capacity)
        validated = validate_storage_path(db_path, error_type=InstancePersistenceError)
        self._db = _InstanceDatabase(validated)
        self._db.open(_INIT_FAILED)
        with self._lock:
            for record in self._db.load_instances(self._capacity):
                try:
                    super().put(record)
                except TargetInstanceError as exc:
                    raise InstancePersistenceError(_INVALID_RECORD) from exc

    @property
    def db_path(self) -> Path:
        """The configured database file (created on first use)."""

        return self._db.path

    def put(self, instance: TargetInstanceRecord) -> TargetInstanceRecord:
        """Store a target instance in memory and in the database.

        Validation is the base class contract (identical exceptions for
        malformed or duplicate instances).  If the mirrored write fails
        after the in-memory insert, memory is rolled back - including
        restoring any FIFO victim the insert evicted - so neither view
        reports a write that the other side never saw.  When the insert
        also evicts a FIFO victim, the mirror (new row insert + victim
        row delete) is one atomic SQLite transaction: a failure at either
        stage rolls the insert back, so the stale victim row can never
        linger committed.
        """

        with self._lock:
            victim: tuple[str, TargetInstanceRecord] | None = None
            if (
                isinstance(instance, TargetInstanceRecord)
                and instance.id not in self._instances
                and len(self._instances) >= self._capacity
            ):
                first_id, first_instance = next(iter(self._instances.items()))
                victim = (first_id, first_instance)
            stored = super().put(instance)
            try:
                if victim is not None:
                    self._db.insert_instance_with_eviction(stored, victim[0])
                else:
                    self._db.insert_instance(stored)
            except (InstancePersistenceError, DuplicateTargetInstanceError):
                self._instances.pop(stored.id, None)
                if victim is not None and victim[0] not in self._instances:
                    self._instances[victim[0]] = victim[1]
                    self._instances.move_to_end(victim[0], last=False)
                raise
            return stored

    def delete(self, instance_id: object) -> bool:
        """Remove a stored instance from the database first, then from memory.

        A database failure raises before memory changes, so the instance
        stays deletable; malformed or unknown identifiers answer ``False``
        identically, mirroring the base class contract.
        """

        with self._lock:
            existing = self.get(instance_id)
            if existing is None:
                return False
            self._db.delete_instance(existing.id)
            return super().delete(instance_id)

    def update(
        self,
        instance_id: object,
        *,
        name: object = None,
        description: object = None,
    ) -> TargetInstanceRecord | None:
        """Update the database row first, then the in-memory record.

        A database failure raises before memory changes, so the stored
        values stay truthful and the update stays retryable; malformed or
        unknown identifiers answer ``None`` identically, mirroring the
        base class contract.  The row keeps its ``seq`` (insertion
        position) - only the JSON payload is rewritten.
        """

        with self._lock:
            existing = self.get(instance_id)
            if existing is None:
                return None
            candidate = self._updated_record(existing, name, description)
            if not self._db.update_instance(candidate):
                raise InstancePersistenceError(_UPDATE_FAILED)
            self._instances[existing.id] = candidate
            return candidate

    def reset(self) -> None:
        """Clear the database first, then memory (all-or-nothing on failure)."""

        with self._lock:
            self._db.clear()
            super().reset()


def configured_instance_store(
    job_db_path: str | Path | None = None,
) -> PersistentTargetInstanceStore | None:
    """The persistent store for the configured database path, else ``None``.

    Shares the path resolution of the job store: an explicit
    ``job_db_path`` always wins over ``PASSORCH_JOB_DB``.  An unset or
    blank environment variable means "stay in-memory" (``None``), while
    an explicit blank path is a configuration error - never a silent
    fallback.  Opening failures raise :class:`InstancePersistenceError`.
    """

    path = configured_database_path(job_db_path, error_type=InstancePersistenceError)
    if path is None:
        return None
    return PersistentTargetInstanceStore(path)

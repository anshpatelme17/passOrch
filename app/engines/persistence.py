"""Phase 8B/8C - shared SQLite persistence infrastructure.

Both durable stores (Phase 8A jobs, Phase 8B target instances) build on
this module so their database behaviour cannot drift:

* :class:`PersistenceError` - the base failure type; each store
  subclasses it (``JobPersistenceError``, ``InstancePersistenceError``)
  so a route catches its own store's errors while both share one
  implementation;
* :class:`PersistenceDatabase` - a small parameterised-SQL facade: one
  connection per operation, explicit transactions, generic failure
  messages, optional ``IntegrityError`` mapping to a domain duplicate
  error;
* schema versioning (Phase 8C) - this module owns the file's tables and
  stamps ``PRAGMA user_version``; :meth:`PersistenceDatabase.open` runs
  pending migrations transactionally **at store construction only**, so
  an older file upgrades without data loss while a file written by a
  newer schema is rejected fail-closed;
* :func:`backup_database` - an online-safe copy via the stdlib
  ``sqlite3`` backup API (also the restore direction);
* :func:`configured_database_path` - shared path resolution (an explicit
  ``create_app`` argument first, then ``PASSORCH_JOB_DB``);
* :func:`collect_forbidden_keys` - the recursive scan every hydrated
  payload must pass before it re-enters a store;
* :func:`validate_storage_path` and the ``as_*`` helpers - strict typed
  access for reconstruction.

Contract: nothing runs at import, no connection outlives the operation
that opened it, every database failure surfaces as a ``PersistenceError``
subclass whose message stays generic - no SQL text, file path or driver
string can reach an API response - and only parameterised SQL is ever
executed, with one documented exception: ``PRAGMA`` values cannot be
bound, so the schema version is interpolated as an internal integer
constant, never user input.  stdlib ``sqlite3`` only: no new dependency,
no subprocess, no shell.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

from app.engines.jobs import JobStore

__all__ = [
    "DATABASE_ENV_VAR",
    "SCHEMA_VERSION",
    "PersistenceDatabase",
    "PersistenceError",
    "as_boolean",
    "as_integer",
    "as_mapping",
    "as_string_tuple",
    "as_text",
    "backup_database",
    "collect_forbidden_keys",
    "configured_database_path",
    "validate_storage_path",
]

#: Environment variable consulted when no explicit path is configured.
DATABASE_ENV_VAR = "PASSORCH_JOB_DB"

#: Generic message for a blank or unsupported path argument - never the
#: offending value, which could contain a user directory.
_BLANK_PATH = "storage path must be a non-empty string"

#: Generic failure messages (schema / backup).
_UNSUPPORTED_SCHEMA = "persisted database schema version is not supported"
_BACKUP_FAILED = "failed to back up the database"
_BACKUP_SAME_FILE = "backup source and destination must be different files"

#: Seconds a connection waits for a write lock before failing.
_CONNECT_TIMEOUT_SECONDS = 5.0

#: The schema every store expects: ``seq`` fixes hydration order, the
#: domain key (``job_id`` / ``instance_id``) is UNIQUE, ``payload`` holds
#: one record's JSON form.  Owned here - not by a store - so one file has
#: one schema version regardless of which store opens it first.
_CREATE_JOBS_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS passorch_jobs ("
    "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
    "job_id TEXT NOT NULL UNIQUE, "
    "payload TEXT NOT NULL)"
)
_CREATE_INSTANCES_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS passorch_instances ("
    "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
    "instance_id TEXT NOT NULL UNIQUE, "
    "payload TEXT NOT NULL)"
)


class _Migration(NamedTuple):
    """One ordered, DDL-only schema step stamped into ``PRAGMA user_version``."""

    target: int
    statements: tuple[str, ...]


#: Migrations run in ascending ``target`` order from whatever version a
#: file carries.  Version 1 creates both tables; legacy files written
#: before versioning existed (user_version 0) simply gain the stamp -
#: ``IF NOT EXISTS`` keeps their rows untouched.  DDL only: no row
#: rewrites, no destructive transforms, ever.
_MIGRATIONS: tuple[_Migration, ...] = (
    _Migration(1, (_CREATE_JOBS_TABLE_SQL, _CREATE_INSTANCES_TABLE_SQL)),
)

#: The schema version a current store reads and writes.
SCHEMA_VERSION = _MIGRATIONS[-1].target


class PersistenceError(Exception):
    """A database operation (or its configuration) failed.

    The base type shared by every store.  Concrete subclasses
    (``JobPersistenceError``, ``InstancePersistenceError``) keep their
    own identity for ``except`` clauses and tests while inheriting the
    one rule: the message is generic, so no SQL text, path or driver
    string can leak into an API response.
    """


def validate_storage_path(
    value: object,
    *,
    error_type: type[PersistenceError] = PersistenceError,
) -> Path:
    """A non-empty ``str``/``Path`` storage path, else ``error_type``.

    Used both by :func:`configured_database_path` and by each store
    constructor, so a blank or unsupported argument always fails with
    the same generic message instead of falling back silently.
    """

    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise error_type(_BLANK_PATH)
    return Path(value)


def configured_database_path(
    explicit: str | Path | None = None,
    *,
    error_type: type[PersistenceError] = PersistenceError,
) -> Path | None:
    """The configured database path, else ``None`` for in-memory defaults.

    Resolution order: an explicit path always wins over the
    :data:`DATABASE_ENV_VAR` environment variable.  An unset or blank
    environment variable means "stay in-memory" (``None``), while an
    explicit blank path is a configuration error - never a silent
    fallback.  Surrounding whitespace in the environment value is
    ignored (shell quoting artifacts are not path characters), matching
    how the other ``PASSORCH_*`` settings normalise their input.
    """

    if explicit is not None:
        return validate_storage_path(explicit, error_type=error_type)
    environment_path = os.environ.get(DATABASE_ENV_VAR, "").strip()
    if environment_path:
        return Path(environment_path)
    return None


def collect_forbidden_keys(value: object) -> tuple[str, ...]:
    """Forbidden command / argv / path keys found anywhere inside ``value``."""

    return JobStore._forbidden_keys(value)


def as_mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"{field} must be an object")
    return value


def as_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    return value


def as_integer(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field} must be an integer")
    return value


def as_boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field} must be a boolean")
    return value


def as_string_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TypeError(f"{field} must be an array of strings")
    return tuple(value)


def _rollback_quietly(connection: sqlite3.Connection) -> None:
    """Roll back a failed transaction, ignoring a secondary rollback error."""

    try:
        connection.rollback()
    except sqlite3.Error:
        pass


class PersistenceDatabase:
    """One SQLite file behind a store: parameterised SQL, per-operation connections.

    ``error_type`` is the store's own ``PersistenceError`` subclass and
    is raised for every failure with the caller-supplied generic message,
    so nothing sensitive (SQL text, path, driver string) ever escapes.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        error_type: type[PersistenceError],
        timeout: float = _CONNECT_TIMEOUT_SECONDS,
    ) -> None:
        self._path = Path(path)
        self._error_type = error_type
        self._timeout = timeout

    @property
    def path(self) -> Path:
        return self._path

    def _connect(self, failure: str) -> sqlite3.Connection:
        try:
            return sqlite3.connect(str(self._path), timeout=self._timeout)
        except sqlite3.Error as exc:
            raise self._error_type(failure) from exc

    @contextmanager
    def transaction(self, failure: str) -> Iterator[sqlite3.Connection]:
        """A committed transaction, else a rollback and ``error_type(failure)``.

        Failures raised inside the body as ``PersistenceError`` (an
        invalid record, say) propagate unchanged after the rollback;
        any other ``sqlite3.Error`` becomes the generic ``failure``.
        The connection is always closed before this returns.
        """

        connection = self._connect(failure)
        try:
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            _rollback_quietly(connection)
            raise self._error_type(failure) from exc
        except PersistenceError:
            _rollback_quietly(connection)
            raise
        except BaseException:
            _rollback_quietly(connection)
            raise
        finally:
            connection.close()

    def open(self, failure: str) -> None:
        """Create the schema and run every pending migration.

        Called only from a store constructor, never from a read path.
        Each migration (``PRAGMA`` read, DDL statements, version stamp)
        commits as one transaction, so a failure rolls back to the
        previous version with the file intact; a file stamped by a
        *newer* schema than this code understands is rejected fail-closed
        instead of being opened optimistically.
        """

        connection = self._connect(failure)
        try:
            try:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            except sqlite3.Error as exc:
                raise self._error_type(failure) from exc
            if version > SCHEMA_VERSION:
                raise self._error_type(_UNSUPPORTED_SCHEMA)
            for target, statements in _MIGRATIONS:
                if target > version:
                    self._run_migration(connection, target, statements, failure)
        finally:
            connection.close()

    def _run_migration(
        self,
        connection: sqlite3.Connection,
        target: int,
        statements: Sequence[str],
        failure: str,
    ) -> None:
        """Apply one migration as a single all-or-nothing transaction.

        ``PRAGMA user_version`` cannot take bound parameters, so the
        target is formatted in as an internal constant from
        :data:`_MIGRATIONS` - never user input; every table statement
        still runs through ``execute`` with no interpolation.
        """

        try:
            connection.execute("BEGIN")
            for statement in statements:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {int(target)}")
            connection.commit()
        except sqlite3.Error as exc:
            _rollback_quietly(connection)
            raise self._error_type(failure) from exc

    def write(
        self,
        sql: str,
        parameters: tuple[object, ...],
        *,
        failure: str,
        on_integrity: BaseException | None = None,
    ) -> int:
        """Execute one parameterised statement and return its rowcount.

        An ``sqlite3.IntegrityError`` raises ``on_integrity`` when one is
        supplied (the store's domain duplicate error), else the generic
        ``failure``; every other driver error becomes ``failure`` too.
        """

        connection = self._connect(failure)
        try:
            try:
                cursor = connection.execute(sql, parameters)
                connection.commit()
                return int(cursor.rowcount)
            except sqlite3.IntegrityError as exc:
                _rollback_quietly(connection)
                if on_integrity is not None:
                    raise on_integrity from exc
                raise self._error_type(failure) from exc
            except sqlite3.Error as exc:
                _rollback_quietly(connection)
                raise self._error_type(failure) from exc
        finally:
            connection.close()


def backup_database(
    source: str | Path,
    destination: str | Path,
    *,
    error_type: type[PersistenceError] = PersistenceError,
) -> None:
    """Copy ``source`` to ``destination`` with SQLite's online backup API.

    Produces a consistent snapshot of a live **or** closed database - no
    manual file copying, no torn state if a writer commits mid-copy.
    Back up with ``backup_database(live_database, copy)``; restore by
    reversing the arguments (``backup_database(copy, live_database)``).
    After a restore, the next store construction migrates the file if the
    copy carries an older schema version.

    Raises ``error_type`` with a generic message - never a path, SQL text
    or driver string - when either argument is blank, the source does not
    exist, source and destination resolve to the same file, or the copy
    itself fails.  A failed copy may leave an incomplete destination
    behind; a destination that did not exist before is removed best-effort
    so a broken file is never mistaken for a good backup.
    """

    source_path = validate_storage_path(source, error_type=error_type)
    destination_path = validate_storage_path(destination, error_type=error_type)
    if source_path.resolve() == destination_path.resolve():
        raise error_type(_BACKUP_SAME_FILE)
    if not source_path.is_file():
        raise error_type(_BACKUP_FAILED)
    destination_existed = destination_path.exists()
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(str(source_path), timeout=_CONNECT_TIMEOUT_SECONDS)
        destination_connection = sqlite3.connect(
            str(destination_path), timeout=_CONNECT_TIMEOUT_SECONDS
        )
        source_connection.backup(destination_connection)
        destination_connection.commit()
    except sqlite3.Error as exc:
        if not destination_existed:
            try:
                destination_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise error_type(_BACKUP_FAILED) from exc
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()

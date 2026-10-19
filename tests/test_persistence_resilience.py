"""Step 9 - persistence resilience: integrity check and lock-timeout behaviour.

Complements the existing job/instance persistence suites with the two
hardening points from the architecture audit: ``PRAGMA quick_check`` at
construction (corrupt files fail closed before any table is touched) and
the bounded write-lock wait (a contended writer answers the generic
``PersistenceError`` inside the configured timeout instead of hanging or
leaking driver text).
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from app.engines.persistence import PersistenceDatabase, PersistenceError

_FAILURE = "storage is temporarily unavailable"


def _db(path: Path, *, timeout: float = 5.0) -> PersistenceDatabase:
    return PersistenceDatabase(
        path, error_type=PersistenceError, timeout=timeout
    )


def test_healthy_database_passes_the_open_integrity_check(tmp_path: Path) -> None:
    path = tmp_path / "ok.db"
    database = _db(path)
    database.open(_FAILURE)  # does not raise

    # The file is a real SQLite database that answers quick_check "ok".
    connection = sqlite3.connect(str(path))
    try:
        rows = connection.execute("PRAGMA quick_check").fetchall()
    finally:
        connection.close()
    assert rows == [("ok",)]


def test_file_with_sqlite_header_but_garbage_body_fails_closed(tmp_path: Path) -> None:
    """A plausible-looking corrupt file is rejected at construction."""

    path = tmp_path / "corrupt.db"
    path.write_bytes(b"SQLite format 3\x00" + b"\x00" * 512)

    with pytest.raises(PersistenceError) as error:
        _db(path).open(_FAILURE)
    message = str(error.value)
    assert message == _FAILURE
    assert "corrupt" not in message.lower()
    assert str(tmp_path).lower() not in message.lower()


def test_locked_database_write_fails_generic_inside_the_timeout(
    tmp_path: Path,
) -> None:
    """A contended writer waits at most ``timeout`` seconds, then fails
    closed with the generic message - no hang, no driver text, no path."""

    path = tmp_path / "locked.db"
    database = _db(path, timeout=0.2)
    database.open(_FAILURE)

    blocker = sqlite3.connect(str(path))
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        with pytest.raises(PersistenceError) as error:
            with database.transaction(_FAILURE) as connection:
                connection.execute(
                    "INSERT INTO passorch_jobs (job_id, payload) VALUES (?, ?)",
                    ("blocked", "{}"),
                )
        elapsed = time.monotonic() - started
        assert elapsed < 2.0  # bounded by the 0.2 s timeout, not a hang
        message = str(error.value)
        assert message == _FAILURE
        assert "locked" not in message.lower()
        assert str(tmp_path).lower() not in message.lower()
    finally:
        blocker.rollback()
        blocker.close()


def test_sequential_writers_on_two_connections_both_commit(tmp_path: Path) -> None:
    """Two PersistenceDatabase handles over one file interleave cleanly."""

    path = tmp_path / "shared.db"
    first = _db(path)
    second = _db(path)
    first.open(_FAILURE)
    second.open(_FAILURE)

    with first.transaction(_FAILURE) as connection:
        connection.execute(
            "INSERT INTO passorch_jobs (job_id, payload) VALUES (?, ?)",
            ("a", "{}"),
        )
    with second.transaction(_FAILURE) as connection:
        connection.execute(
            "INSERT INTO passorch_jobs (job_id, payload) VALUES (?, ?)",
            ("b", "{}"),
        )

    connection = sqlite3.connect(str(path))
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM passorch_jobs"
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == 2

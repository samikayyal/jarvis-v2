"""Shared support values for the controlled adapter persistence boundary."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..models import ensure_utc
from ..ports import StateStoreError
from ..sessions import ModelAvailability

SQLITE_OUTBOUND_ATTEMPT_MIGRATION_VERSION = 1
_SQLITE_OUTBOUND_ATTEMPT_MIGRATION_NAME = "ticket12_outbound_attempt_state"
_SQLITE_OUTBOUND_ATTEMPT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS outbound_attempt_record (
    transport_session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('unattempted', 'attempted', 'confirmed', 'unknown', 'not_started')
    ),
    outbound_id TEXT,
    reserved_at TEXT NOT NULL,
    attempted_at TEXT,
    terminal_at TEXT,
    PRIMARY KEY (transport_session_id, message_id)
)
"""


def migrate_sqlite_outbound_conversation_attempts(
    database: str | Path | sqlite3.Connection = ":memory:",
    *,
    applied_at: datetime | None = None,
) -> int:
    """Apply the versioned Ticket 12 outbound-state migration manually.

    This is an administrative/offline operation.  Normal state-store startup
    deliberately does not call it: a legacy outbound outbox remains untouched
    until an operator has chosen to run the migration after taking the required
    backup and completing the upgrade rehearsal.
    """

    owns_connection = not isinstance(database, sqlite3.Connection)
    connection = (
        database
        if isinstance(database, sqlite3.Connection)
        else sqlite3.connect(str(database))
    )
    connection.row_factory = sqlite3.Row
    migration_version = SQLITE_OUTBOUND_ATTEMPT_MIGRATION_VERSION
    try:
        if not owns_connection and connection.in_transaction:
            raise StateStoreError(
                "manual outbound-state migration requires an idle SQLite connection"
            )
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS jarvis_state_migrations (
                migration_name TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        existing = connection.execute(
            """
            SELECT version
            FROM jarvis_state_migrations
            WHERE migration_name = ?
            """,
            (_SQLITE_OUTBOUND_ATTEMPT_MIGRATION_NAME,),
        ).fetchone()
        if existing is not None and int(existing["version"]) >= migration_version:
            connection.commit()
            return int(existing["version"])

        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "outbound_conversation_outbox" not in tables:
            raise StateStoreError(
                "manual outbound-state migration requires the legacy outbox table"
            )

        connection.execute(_SQLITE_OUTBOUND_ATTEMPT_TABLE_SQL)
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(outbound_attempt_record)"
            ).fetchall()
        }
        required_columns = {
            "transport_session_id",
            "message_id",
            "request_id",
            "status",
            "reserved_at",
            "attempted_at",
            "terminal_at",
        }
        if not required_columns.issubset(columns):
            raise StateStoreError(
                "manual outbound-state migration found an unsupported attempt schema"
            )
        if "outbound_id" not in columns:
            connection.execute(
                "ALTER TABLE outbound_attempt_record ADD COLUMN outbound_id TEXT"
            )
        connection.execute(
            """
            INSERT OR IGNORE INTO outbound_attempt_record(
                transport_session_id, message_id, request_id, status,
                outbound_id, reserved_at, attempted_at, terminal_at
            )
            SELECT transport_session_id, message_id, request_id, 'attempted',
                   NULL, occurred_at, occurred_at, NULL
            FROM outbound_conversation_outbox
            """
        )
        connection.execute(
            """
            INSERT INTO jarvis_state_migrations(migration_name, version, applied_at)
            VALUES (?, ?, ?)
            ON CONFLICT(migration_name) DO UPDATE SET
                version = excluded.version,
                applied_at = excluded.applied_at
            """,
            (
                _SQLITE_OUTBOUND_ATTEMPT_MIGRATION_NAME,
                migration_version,
                ensure_utc(applied_at or datetime.now(UTC)).isoformat(),
            ),
        )
        connection.commit()
        return migration_version
    except StateStoreError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except sqlite3.Error as exc:
        if connection.in_transaction:
            connection.rollback()
        raise StateStoreError(
            "could not apply manual outbound-state migration"
        ) from exc
    finally:
        if owns_connection:
            connection.close()


class SystemClock:
    """Production clock port; tests should inject :class:`FixedClock`."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """Mutable deterministic clock for tests and local simulations."""

    def __init__(self, current: datetime) -> None:
        self.current = ensure_utc(current)

    def now(self) -> datetime:
        return self.current

    def advance(self, *, seconds: float = 0, minutes: float = 0) -> None:
        self.current = self.current + timedelta(seconds=seconds, minutes=minutes)


class FixedModelAvailabilityProvider:
    """Explicit controlled availability source for tests and local simulations."""

    def __init__(self, availability: ModelAvailability) -> None:
        if not isinstance(availability, ModelAvailability):
            raise TypeError("availability must be a ModelAvailability")
        self.availability = availability

    def current(self) -> ModelAvailability:
        return self.availability


class UuidIdGenerator:
    """Default runtime identifier source; tests use the deterministic variant."""

    def new_id(self, namespace: str) -> str:
        if not namespace or namespace.strip() != namespace:
            raise ValueError("namespace must be a non-empty canonical string")
        return f"{namespace}-{uuid.uuid4().hex}"


class DeterministicIdGenerator:
    """Predictable per-namespace identifiers for automated seams."""

    def __init__(self, prefix: str = "test") -> None:
        if not prefix or prefix.strip() != prefix:
            raise ValueError("prefix must be a non-empty canonical string")
        self.prefix = prefix
        self._counters: dict[str, int] = {}

    def new_id(self, namespace: str) -> str:
        if not namespace or namespace.strip() != namespace:
            raise ValueError("namespace must be a non-empty canonical string")
        next_value = self._counters.get(namespace, 0) + 1
        self._counters[namespace] = next_value
        return f"{self.prefix}-{namespace}-{next_value:04d}"

"""SQLite-only storage operations for the administrative archive service."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from ..models import ConversationMessage, _conversation_message_digest
from ..ports import DeletedConversationArchiveError
from .records import (
    _archive_content_values,
    _archive_message_from_row,
    _archive_values,
    _validate_batch_metadata,
)

ARCHIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS deleted_messages (
    transport_session_id TEXT NOT NULL,
    working_session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    text TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    request_id TEXT,
    credential_like INTEGER NOT NULL CHECK (credential_like IN (0, 1)),
    deletion_id TEXT NOT NULL,
    deleted_at TEXT NOT NULL,
    PRIMARY KEY (transport_session_id, message_id)
)
;
CREATE TABLE IF NOT EXISTS deleted_message_batches (
    deletion_id TEXT PRIMARY KEY,
    expected_count INTEGER NOT NULL CHECK (expected_count >= 0),
    expected_digest TEXT NOT NULL,
    deleted_at TEXT NOT NULL
)
;
CREATE TABLE IF NOT EXISTS deleted_message_batch_items (
    deletion_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    item_index INTEGER NOT NULL CHECK (item_index >= 0),
    transport_session_id TEXT NOT NULL,
    working_session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    text TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    request_id TEXT,
    credential_like INTEGER NOT NULL CHECK (credential_like IN (0, 1)),
    PRIMARY KEY (deletion_id, transport_session_id, message_id),
    UNIQUE (deletion_id, chunk_index, item_index)
)
"""


def archive_database_path(database: str | Path) -> Path:
    if str(database) == ":memory:":
        raise ValueError("deleted archive requires a durable database path")
    return Path(database).expanduser().resolve()


_archive_database_path = archive_database_path


def open_archive_connection(database: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(archive_database_path(database)), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.executescript(ARCHIVE_SCHEMA)
    connection.commit()
    return connection


def insert_archive_records(
    archive_connection: sqlite3.Connection,
    records: Sequence[ConversationMessage],
    *,
    deletion_id: str,
    deleted_at: datetime,
) -> None:
    for message in records:
        values = _archive_values(
            message,
            deletion_id=deletion_id,
            deleted_at=deleted_at,
        )
        archive_connection.execute(
            """
            INSERT INTO deleted_messages(
                transport_session_id, working_session_id, message_id,
                event_id, chat_id, sender_id, text, occurred_at,
                direction, request_id, credential_like, deletion_id,
                deleted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(transport_session_id, message_id) DO NOTHING
            """,
            values,
        )
        existing = archive_connection.execute(
            """
            SELECT transport_session_id, working_session_id, message_id,
                   event_id, chat_id, sender_id, text, occurred_at,
                   direction, request_id, credential_like
            FROM deleted_messages
            WHERE transport_session_id = ? AND message_id = ?
            """,
            (message.transport_session_id, message.message_id),
        ).fetchone()
        if existing is None or tuple(existing) != _archive_content_values(message):
            raise DeletedConversationArchiveError(
                "deleted archive record does not match a prior transfer"
            )
        archive_connection.execute(
            """
            UPDATE deleted_messages
            SET deletion_id = ?, deleted_at = ?
            WHERE transport_session_id = ? AND message_id = ?
            """,
            (
                deletion_id,
                deleted_at.isoformat(),
                message.transport_session_id,
                message.message_id,
            ),
        )


_insert_archive_records = insert_archive_records


def archive_batch(
    archive_connection: sqlite3.Connection,
    records: Sequence[ConversationMessage],
    *,
    deletion_id: str,
    deleted_at: datetime,
) -> None:
    archive_connection.execute("BEGIN IMMEDIATE")
    try:
        insert_archive_records(
            archive_connection,
            records,
            deletion_id=deletion_id,
            deleted_at=deleted_at,
        )
        archive_connection.commit()
    except Exception:
        archive_connection.rollback()
        raise


_archive_batch = archive_batch


def begin_archive_batch(
    archive_connection: sqlite3.Connection,
    *,
    deletion_id: str,
    deleted_at: datetime,
    expected_count: int,
    expected_digest: str,
) -> None:
    archive_connection.execute("BEGIN IMMEDIATE")
    try:
        _validate_batch_metadata(
            deletion_id=deletion_id,
            deleted_at=deleted_at,
            expected_count=expected_count,
            expected_digest=expected_digest,
        )
        archive_connection.execute(
            "DELETE FROM deleted_message_batch_items WHERE deletion_id = ?",
            (deletion_id,),
        )
        archive_connection.execute(
            "DELETE FROM deleted_message_batches WHERE deletion_id = ?",
            (deletion_id,),
        )
        archive_connection.execute(
            """
            INSERT INTO deleted_message_batches(
                deletion_id, expected_count, expected_digest, deleted_at
            ) VALUES (?, ?, ?, ?)
            """,
            (deletion_id, expected_count, expected_digest, deleted_at.isoformat()),
        )
        archive_connection.commit()
    except Exception:
        archive_connection.rollback()
        raise


_begin_archive_batch = begin_archive_batch


def append_archive_batch_chunk(
    archive_connection: sqlite3.Connection,
    records: Sequence[ConversationMessage],
    *,
    deletion_id: str,
    chunk_index: int,
) -> None:
    if not records:
        raise DeletedConversationArchiveError(
            "deleted archive chunks must contain at least one message"
        )
    archive_connection.execute("BEGIN IMMEDIATE")
    try:
        batch = archive_connection.execute(
            "SELECT deletion_id FROM deleted_message_batches WHERE deletion_id = ?",
            (deletion_id,),
        ).fetchone()
        if batch is None:
            raise DeletedConversationArchiveError(
                "deleted archive batch was not opened"
            )
        last_chunk = archive_connection.execute(
            """
            SELECT COALESCE(MAX(chunk_index), -1) AS chunk_index
            FROM deleted_message_batch_items
            WHERE deletion_id = ?
            """,
            (deletion_id,),
        ).fetchone()["chunk_index"]
        if chunk_index != last_chunk + 1:
            raise DeletedConversationArchiveError(
                "deleted archive chunks must arrive in order"
            )
        for item_index, message in enumerate(records):
            archive_connection.execute(
                """
                INSERT INTO deleted_message_batch_items(
                    deletion_id, chunk_index, item_index,
                    transport_session_id, working_session_id, message_id,
                    event_id, chat_id, sender_id, text, occurred_at,
                    direction, request_id, credential_like
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    deletion_id,
                    chunk_index,
                    item_index,
                    *_archive_content_values(message),
                ),
            )
        archive_connection.commit()
    except Exception:
        archive_connection.rollback()
        raise


_append_archive_batch_chunk = append_archive_batch_chunk


def commit_archive_batch(
    archive_connection: sqlite3.Connection,
    *,
    deletion_id: str,
) -> None:
    archive_connection.execute("BEGIN IMMEDIATE")
    try:
        batch = archive_connection.execute(
            """
            SELECT expected_count, expected_digest, deleted_at
            FROM deleted_message_batches
            WHERE deletion_id = ?
            """,
            (deletion_id,),
        ).fetchone()
        if batch is None:
            raise DeletedConversationArchiveError(
                "deleted archive batch was not opened"
            )
        rows = archive_connection.execute(
            """
            SELECT transport_session_id, working_session_id, message_id,
                   event_id, chat_id, sender_id, text, occurred_at,
                   direction, request_id, credential_like
            FROM deleted_message_batch_items
            WHERE deletion_id = ?
            ORDER BY occurred_at, transport_session_id, message_id
            """,
            (deletion_id,),
        ).fetchall()
        records = tuple(_archive_message_from_row(row) for row in rows)
        if len(records) != batch["expected_count"]:
            raise DeletedConversationArchiveError(
                "deleted archive batch count does not match its metadata"
            )
        if _conversation_message_digest(records) != batch["expected_digest"]:
            raise DeletedConversationArchiveError(
                "deleted archive batch digest does not match its metadata"
            )
        insert_archive_records(
            archive_connection,
            records,
            deletion_id=deletion_id,
            deleted_at=datetime.fromisoformat(batch["deleted_at"]),
        )
        archive_connection.execute(
            "DELETE FROM deleted_message_batch_items WHERE deletion_id = ?",
            (deletion_id,),
        )
        archive_connection.execute(
            "DELETE FROM deleted_message_batches WHERE deletion_id = ?",
            (deletion_id,),
        )
        archive_connection.commit()
    except Exception:
        archive_connection.rollback()
        raise


_commit_archive_batch = commit_archive_batch


def abort_archive_batch(
    archive_connection: sqlite3.Connection,
    *,
    deletion_id: str,
) -> None:
    archive_connection.execute("BEGIN IMMEDIATE")
    try:
        archive_connection.execute(
            "DELETE FROM deleted_message_batch_items WHERE deletion_id = ?",
            (deletion_id,),
        )
        archive_connection.execute(
            "DELETE FROM deleted_message_batches WHERE deletion_id = ?",
            (deletion_id,),
        )
        archive_connection.commit()
    except Exception:
        archive_connection.rollback()
        raise


_abort_archive_batch = abort_archive_batch


def abort_incomplete_archive_batches(
    archive_connection: sqlite3.Connection,
    deletion_ids: set[str],
) -> None:
    """Release staging owned by a client whose IPC connection disappeared."""

    for deletion_id in tuple(deletion_ids):
        try:
            abort_archive_batch(archive_connection, deletion_id=deletion_id)
        except (DeletedConversationArchiveError, sqlite3.Error):
            continue
    deletion_ids.clear()


_abort_incomplete_archive_batches = abort_incomplete_archive_batches


class SQLiteDeletedConversationArchiveStorage:
    """Small storage facade keeping SQL operations out of the service loop."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def archive(
        self,
        records: Sequence[ConversationMessage],
        *,
        deletion_id: str,
        deleted_at: datetime,
    ) -> None:
        archive_batch(
            self.connection,
            records,
            deletion_id=deletion_id,
            deleted_at=deleted_at,
        )

    def begin(
        self,
        *,
        deletion_id: str,
        deleted_at: datetime,
        expected_count: int,
        expected_digest: str,
    ) -> None:
        begin_archive_batch(
            self.connection,
            deletion_id=deletion_id,
            deleted_at=deleted_at,
            expected_count=expected_count,
            expected_digest=expected_digest,
        )

    def append(
        self,
        records: Sequence[ConversationMessage],
        *,
        deletion_id: str,
        chunk_index: int,
    ) -> None:
        append_archive_batch_chunk(
            self.connection,
            records,
            deletion_id=deletion_id,
            chunk_index=chunk_index,
        )

    def commit(self, *, deletion_id: str) -> None:
        commit_archive_batch(self.connection, deletion_id=deletion_id)

    def abort(self, *, deletion_id: str) -> None:
        abort_archive_batch(self.connection, deletion_id=deletion_id)


__all__ = [
    "ARCHIVE_SCHEMA",
    "SQLiteDeletedConversationArchiveStorage",
    "abort_archive_batch",
    "abort_incomplete_archive_batches",
    "append_archive_batch_chunk",
    "archive_batch",
    "archive_database_path",
    "begin_archive_batch",
    "commit_archive_batch",
    "insert_archive_records",
    "open_archive_connection",
]

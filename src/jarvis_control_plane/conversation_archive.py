"""Write-only deleted-conversation archival capabilities.

The ordinary state store receives only a write capability for this boundary.
The archival SQLite connection belongs to a helper process, and the manual
administration module opens the read side independently.  Keeping those
capabilities separate prevents the live Jarvis state connection from
attaching, querying, or otherwise exposing deleted message bodies.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from multiprocessing import Pipe, Process
from pathlib import Path
from threading import RLock
from typing import Any

from .models import ConversationMessage, ensure_utc
from .ports import DeletedConversationArchiveError, DeletedConversationArchiveWriter

_ARCHIVE_RESPONSE_TIMEOUT_SECONDS = 30.0
_ARCHIVE_CLOSE_TIMEOUT_SECONDS = 2.0
_ARCHIVE_SCHEMA = """
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
"""


@dataclass(frozen=True, slots=True)
class DeletedConversationArchiveRecord:
    """One retained message visible only to manual administration."""

    message: ConversationMessage
    deletion_id: str
    deleted_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.message, ConversationMessage):
            raise TypeError("archive record message must be a ConversationMessage")
        if not isinstance(self.deletion_id, str) or not self.deletion_id.strip():
            raise ValueError("archive record deletion_id must be non-blank")
        object.__setattr__(self, "deleted_at", ensure_utc(self.deleted_at))


def _validate_archive_request(
    messages: Sequence[ConversationMessage],
    *,
    deletion_id: str,
    deleted_at: datetime,
) -> tuple[tuple[ConversationMessage, ...], datetime]:
    if not isinstance(messages, Sequence):
        raise TypeError("deleted archive messages must be a sequence")
    records = tuple(messages)
    if any(not isinstance(message, ConversationMessage) for message in records):
        raise TypeError("deleted archive accepts only ConversationMessage values")
    if len(
        {(message.transport_session_id, message.message_id) for message in records}
    ) != len(records):
        raise ValueError("deleted archive request contains duplicate messages")
    if not isinstance(deletion_id, str) or not deletion_id.strip():
        raise ValueError("deletion_id must be non-blank")
    return records, ensure_utc(deleted_at)


def _archive_values(
    message: ConversationMessage,
    *,
    deletion_id: str,
    deleted_at: datetime,
) -> tuple[object, ...]:
    return (
        message.transport_session_id,
        message.working_session_id,
        message.message_id,
        message.event_id,
        message.chat_id,
        message.sender_id,
        message.text,
        message.occurred_at.isoformat(),
        message.direction,
        message.request_id,
        int(message.credential_like),
        deletion_id,
        deleted_at.isoformat(),
    )


def _archive_record_from_row(row: sqlite3.Row) -> DeletedConversationArchiveRecord:
    return DeletedConversationArchiveRecord(
        message=ConversationMessage(
            working_session_id=row["working_session_id"],
            transport_session_id=row["transport_session_id"],
            message_id=row["message_id"],
            event_id=row["event_id"],
            chat_id=row["chat_id"],
            sender_id=row["sender_id"],
            text=row["text"],
            occurred_at=datetime.fromisoformat(row["occurred_at"]),
            direction=row["direction"],
            request_id=row["request_id"],
            credential_like=bool(row["credential_like"]),
        ),
        deletion_id=row["deletion_id"],
        deleted_at=datetime.fromisoformat(row["deleted_at"]),
    )


class InMemoryDeletedConversationArchive:
    """Test-only write boundary with a separate administration read surface."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], DeletedConversationArchiveRecord] = {}
        self._lock = RLock()

    def archive(
        self,
        messages: Sequence[ConversationMessage],
        *,
        deletion_id: str,
        deleted_at: datetime,
    ) -> None:
        records, normalized_deleted_at = _validate_archive_request(
            messages,
            deletion_id=deletion_id,
            deleted_at=deleted_at,
        )
        with self._lock:
            for message in records:
                key = (message.transport_session_id, message.message_id)
                record = DeletedConversationArchiveRecord(
                    message=message,
                    deletion_id=deletion_id,
                    deleted_at=normalized_deleted_at,
                )
                existing = self._records.get(key)
                if existing is not None and existing != record:
                    raise DeletedConversationArchiveError(
                        "deleted archive record does not match a prior transfer"
                    )
                self._records[key] = record

    def read_records(self) -> tuple[DeletedConversationArchiveRecord, ...]:
        """Return records through the test's separate administration fixture."""

        with self._lock:
            return tuple(
                sorted(
                    self._records.values(),
                    key=lambda record: (
                        record.deleted_at,
                        record.message.transport_session_id,
                        record.message.message_id,
                    ),
                )
            )

    def close(self) -> None:
        return None


class SQLiteDeletedConversationArchiveWriter(DeletedConversationArchiveWriter):
    """Write-only capability backed by an isolated archival helper process.

    ``database`` must be an explicitly configured path in the separately
    permissioned administrative storage area.  The ordinary state store does
    not open this database, retain its connection, or receive a read method.
    In deployment the helper is run with the administrative storage identity;
    the process boundary is also exercised by the local tests.
    """

    def __init__(self, database: str | Path) -> None:
        if str(database) == ":memory:":
            raise ValueError("deleted archive requires a durable database path")
        path = Path(database).expanduser().resolve()
        self._connection, child_connection = Pipe(duplex=True)
        self._process = Process(
            target=_deleted_archive_process_main,
            args=(child_connection, str(path)),
            daemon=True,
        )
        self._lock = RLock()
        self._closed = False
        try:
            self._process.start()
        except Exception as exc:
            self._connection.close()
            child_connection.close()
            raise DeletedConversationArchiveError(
                "deleted archive helper could not start"
            ) from exc
        finally:
            child_connection.close()
        if not self._connection.poll(_ARCHIVE_RESPONSE_TIMEOUT_SECONDS):
            self.close()
            raise DeletedConversationArchiveError(
                "deleted archive helper did not become ready"
            )
        try:
            response = self._connection.recv()
        except (EOFError, OSError) as exc:
            self.close()
            raise DeletedConversationArchiveError(
                "deleted archive helper did not become ready"
            ) from exc
        if not isinstance(response, dict) or not response.get("ok", False):
            message = (
                str(response.get("message", "deleted archive helper failed"))
                if isinstance(response, dict)
                else "deleted archive helper failed"
            )
            self.close()
            raise DeletedConversationArchiveError(message[:200])

    def archive(
        self,
        messages: Sequence[ConversationMessage],
        *,
        deletion_id: str,
        deleted_at: datetime,
    ) -> None:
        records, normalized_deleted_at = _validate_archive_request(
            messages,
            deletion_id=deletion_id,
            deleted_at=deleted_at,
        )
        with self._lock:
            self._request(
                {
                    "operation": "archive",
                    "messages": records,
                    "deletion_id": deletion_id,
                    "deleted_at": normalized_deleted_at,
                }
            )

    def _request(self, request: dict[str, Any]) -> None:
        if self._closed or not self._process.is_alive():
            raise DeletedConversationArchiveError(
                "deleted archive helper is unavailable"
            )
        try:
            self._connection.send(request)
            if not self._connection.poll(_ARCHIVE_RESPONSE_TIMEOUT_SECONDS):
                raise DeletedConversationArchiveError(
                    "deleted archive helper did not acknowledge the transfer"
                )
            response = self._connection.recv()
        except DeletedConversationArchiveError:
            raise
        except (EOFError, OSError) as exc:
            raise DeletedConversationArchiveError(
                "deleted archive helper is unavailable"
            ) from exc
        if not isinstance(response, dict) or not response.get("ok", False):
            message = (
                str(response.get("message", "deleted archive transfer failed"))
                if isinstance(response, dict)
                else "deleted archive transfer failed"
            )
            raise DeletedConversationArchiveError(message[:200])

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._process.is_alive():
                try:
                    self._request({"operation": "close"})
                except DeletedConversationArchiveError:
                    pass
            self._connection.close()
            self._process.join(_ARCHIVE_CLOSE_TIMEOUT_SECONDS)
            if self._process.is_alive():
                self._process.terminate()
                self._process.join(_ARCHIVE_CLOSE_TIMEOUT_SECONDS)
            self._closed = True

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001 - finalizer must never raise
            return


def _deleted_archive_process_main(connection: Any, database: str) -> None:
    archive_connection: sqlite3.Connection | None = None
    try:
        archive_connection = sqlite3.connect(database, timeout=30)
        archive_connection.executescript(_ARCHIVE_SCHEMA)
        archive_connection.commit()
        connection.send({"ok": True})
        while True:
            request = connection.recv()
            operation = request.get("operation")
            if operation == "close":
                connection.send({"ok": True})
                break
            if operation != "archive":
                raise DeletedConversationArchiveError(
                    "deleted archive helper received an unsupported operation"
                )
            records, normalized_deleted_at = _validate_archive_request(
                request["messages"],
                deletion_id=request["deletion_id"],
                deleted_at=request["deleted_at"],
            )
            archive_connection.execute("BEGIN IMMEDIATE")
            try:
                for message in records:
                    values = _archive_values(
                        message,
                        deletion_id=request["deletion_id"],
                        deleted_at=normalized_deleted_at,
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
                        SELECT working_session_id, event_id, chat_id, sender_id,
                               text, occurred_at, direction, request_id,
                               credential_like, deletion_id, deleted_at
                        FROM deleted_messages
                        WHERE transport_session_id = ? AND message_id = ?
                        """,
                        (message.transport_session_id, message.message_id),
                    ).fetchone()
                    if existing is None or tuple(existing) != values[1:2] + values[3:]:
                        raise DeletedConversationArchiveError(
                            "deleted archive record does not match a prior transfer"
                        )
                archive_connection.commit()
            except Exception:
                archive_connection.rollback()
                raise
            connection.send({"ok": True})
    except Exception as exc:  # noqa: BLE001 - process boundary reports one typed error
        try:
            connection.send({"ok": False, "message": str(exc)[:200]})
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        if archive_connection is not None:
            archive_connection.close()
        try:
            connection.close()
        except OSError:
            pass


__all__ = [
    "DeletedConversationArchiveRecord",
    "InMemoryDeletedConversationArchive",
    "SQLiteDeletedConversationArchiveWriter",
]

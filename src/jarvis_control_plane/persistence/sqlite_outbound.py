# ruff: noqa: F401, I001, RUF100 -- mixin globals preserve compatibility seams.
"""SQLite durable state adapter."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from ..models import (
    AuditEvidence,
    ConversationDeletionPreview,
    ConversationDeletionScope,
    ConversationMessage,
    ConversationTombstone,
    DurableMemory,
    HistorySelection,
    IngressAdmissionResult,
    IngressClaim,
    MemoryLifecycle,
    MemorySelection,
    OutboundAttemptRecord,
    OutboundAttemptRecoveryProjection,
    OutboundAttemptStatus,
    RecoveryDegradedMarker,
    RequestState,
    ensure_utc,
    is_outbound_terminal_transition_allowed,
)
from ..ports import (
    AuditBoundary,
    AuditWriteError,
    DeletedConversationArchiveError,
    DeletedConversationArchiveWriter,
    MemorySearchLimitExceeded,
    StateStoreError,
)
from .adapter_support import _SQLITE_OUTBOUND_ATTEMPT_TABLE_SQL
from .audit_boundaries import SQLiteAuditBoundary
from .state_support import (
    _DELETION_SELECTOR_BATCH_SIZE,
    _MAX_HISTORY_RESULTS,
    _MAX_MEMORY_RESULTS,
    _MAX_MEMORY_SEARCH_SCAN_ROWS,
    _MEMORY_SEARCH_BATCH_SIZE,
    _abort_deleted_archive,
    _conversation_deletion_query,
    _conversation_message_from_row,
    _conversation_tombstone,
    _durable_memory_from_row,
    _export_conversation_messages,
    _filter_conversation_messages,
    _filter_memories,
    _finalize_deleted_archive,
    _fts_history_query,
    _history_search_terms,
    _locked_sqlite_state,
    _matches_memory_terms,
    _outbound_attempt_from_row,
    _preview_conversation_deletion,
    _recovered_terminal_attempt_record,
    _request_from_row,
    _request_values,
    _stage_deleted_archive,
    _validate_history_query,
    _validate_memory_query,
)


class _SQLiteOutboundMixin:
    @_locked_sqlite_state
    def list_conversation_messages(self) -> tuple[ConversationMessage, ...]:
        try:
            rows = self.connection.execute(
                """
                SELECT transport_session_id, working_session_id, message_id,
                       event_id, chat_id, sender_id, text, occurred_at, direction,
                       request_id, credential_like
                FROM conversation_history
                ORDER BY occurred_at, transport_session_id, message_id
                """
            ).fetchall()
        except sqlite3.Error as exc:
            raise StateStoreError("could not list conversation history") from exc
        return tuple(
            ConversationMessage(
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
            )
            for row in rows
        )

    @_locked_sqlite_state
    def append_conversation_message(self, message: ConversationMessage) -> None:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self._insert_conversation_message(message)
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            raise StateStoreError(
                "conversation message identifier already exists"
            ) from exc
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise StateStoreError("could not append conversation history") from exc

    @_locked_sqlite_state
    def reserve_outbound_conversation_message(
        self, message: ConversationMessage
    ) -> None:
        if message.direction != "outbound":
            raise StateStoreError(
                "only outbound messages can enter the outbound outbox"
            )
        if message.request_id is None:
            raise StateStoreError(
                "outbound outbox messages require a request identifier"
            )
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                """
                INSERT INTO outbound_conversation_outbox(
                    transport_session_id, message_id, working_session_id, event_id,
                    chat_id, sender_id, text, occurred_at, request_id, credential_like
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.transport_session_id,
                    message.message_id,
                    message.working_session_id,
                    message.event_id,
                    message.chat_id,
                    message.sender_id,
                    message.text,
                    ensure_utc(message.occurred_at).isoformat(),
                    message.request_id,
                    int(message.credential_like),
                ),
            )
            self.connection.execute(
                """
                INSERT INTO outbound_attempt_record(
                    transport_session_id, message_id, request_id, status,
                    outbound_id, reserved_at, attempted_at, terminal_at
                ) VALUES (?, ?, ?, 'unattempted', NULL, ?, NULL, NULL)
                """,
                (
                    message.transport_session_id,
                    message.message_id,
                    message.request_id,
                    ensure_utc(message.occurred_at).isoformat(),
                ),
            )
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            raise StateStoreError(
                "conversation message identifier already exists"
            ) from exc
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise StateStoreError(
                "could not reserve outbound conversation history"
            ) from exc

    @_locked_sqlite_state
    def mark_outbound_conversation_attempted(
        self,
        *,
        transport_session_id: str,
        message_id: str,
        attempted_at: datetime,
    ) -> None:
        try:
            cursor = self.connection.execute(
                """
                UPDATE outbound_attempt_record
                SET status = 'attempted', attempted_at = ?
                WHERE transport_session_id = ? AND message_id = ?
                  AND status = 'unattempted'
                """,
                (
                    ensure_utc(attempted_at).isoformat(),
                    transport_session_id,
                    message_id,
                ),
            )
            if cursor.rowcount != 1:
                self.connection.rollback()
                raise StateStoreError("outbound attempt is not known-unattempted")
            self.connection.commit()
        except StateStoreError:
            raise
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise StateStoreError("could not mark outbound attempt") from exc

    @_locked_sqlite_state
    def terminalize_outbound_conversation_attempt(
        self,
        *,
        transport_session_id: str,
        message_id: str,
        status: OutboundAttemptStatus | str,
        terminal_at: datetime,
        outbound_id: str | None = None,
    ) -> None:
        status = OutboundAttemptStatus(status)
        if status in {
            OutboundAttemptStatus.UNATTEMPTED,
            OutboundAttemptStatus.ATTEMPTED,
        }:
            raise StateStoreError("outbound terminal status is required")
        terminal_at = ensure_utc(terminal_at)
        if outbound_id is not None and (
            not isinstance(outbound_id, str) or not outbound_id.strip()
        ):
            raise StateStoreError("outbound gateway identifier must be non-blank")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            attempt = self.connection.execute(
                """
                SELECT status, outbound_id
                FROM outbound_attempt_record
                WHERE transport_session_id = ? AND message_id = ?
                """,
                (transport_session_id, message_id),
            ).fetchone()
            if attempt is None:
                raise StateStoreError("outbound attempt does not exist")
            current_status = OutboundAttemptStatus(attempt["status"])
            if not is_outbound_terminal_transition_allowed(current_status, status):
                raise StateStoreError("outbound terminal transition is not allowed")
            if current_status not in {
                OutboundAttemptStatus.UNATTEMPTED,
                OutboundAttemptStatus.ATTEMPTED,
            }:
                if current_status is status and (
                    outbound_id is None or outbound_id == attempt["outbound_id"]
                ):
                    self.connection.rollback()
                    return
                raise StateStoreError("outbound attempt is already terminal")
            if status is OutboundAttemptStatus.CONFIRMED:
                if current_status is not OutboundAttemptStatus.ATTEMPTED:
                    raise StateStoreError("outbound message was not durably attempted")
                row = self.connection.execute(
                    """
                    SELECT transport_session_id, working_session_id, message_id,
                           event_id, chat_id, sender_id, text, occurred_at,
                           request_id, credential_like
                    FROM outbound_conversation_outbox
                    WHERE transport_session_id = ? AND message_id = ?
                    """,
                    (transport_session_id, message_id),
                ).fetchone()
                if row is None:
                    raise StateStoreError(
                        "reserved outbound conversation message does not exist"
                    )
                self._insert_conversation_message(
                    ConversationMessage(
                        working_session_id=row["working_session_id"],
                        transport_session_id=row["transport_session_id"],
                        message_id=row["message_id"],
                        event_id=row["event_id"],
                        chat_id=row["chat_id"],
                        sender_id=row["sender_id"],
                        text=row["text"],
                        occurred_at=datetime.fromisoformat(row["occurred_at"]),
                        direction="outbound",
                        request_id=row["request_id"],
                        credential_like=bool(row["credential_like"]),
                    )
                )
            self.connection.execute(
                """
                DELETE FROM outbound_conversation_outbox
                WHERE transport_session_id = ? AND message_id = ?
                """,
                (transport_session_id, message_id),
            )
            cursor = self.connection.execute(
                """
                UPDATE outbound_attempt_record
                SET status = ?, outbound_id = ?, terminal_at = ?
                WHERE transport_session_id = ? AND message_id = ? AND status = ?
                """,
                (
                    status.value,
                    outbound_id,
                    terminal_at.isoformat(),
                    transport_session_id,
                    message_id,
                    current_status.value,
                ),
            )
            if cursor.rowcount != 1:
                raise StateStoreError("outbound attempt changed during terminalization")
            self.connection.commit()
        except StateStoreError:
            self.connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            raise StateStoreError(
                "conversation message identifier already exists"
            ) from exc
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise StateStoreError(
                "could not terminalize outbound conversation attempt"
            ) from exc

    @_locked_sqlite_state
    def accept_reserved_outbound_conversation_message(
        self,
        *,
        transport_session_id: str,
        message_id: str,
        terminal_at: datetime | None = None,
        outbound_id: str | None = None,
    ) -> None:
        """Compatibility wrapper for the confirmed terminal transition."""

        if terminal_at is None:
            row = self.connection.execute(
                """
                SELECT attempted_at, reserved_at
                FROM outbound_attempt_record
                WHERE transport_session_id = ? AND message_id = ?
                """,
                (transport_session_id, message_id),
            ).fetchone()
            if row is None:
                raise StateStoreError("outbound attempt does not exist")
            terminal_at = datetime.fromisoformat(
                row["attempted_at"] or row["reserved_at"]
            )
        self.terminalize_outbound_conversation_attempt(
            transport_session_id=transport_session_id,
            message_id=message_id,
            status=OutboundAttemptStatus.CONFIRMED,
            terminal_at=terminal_at,
            outbound_id=outbound_id,
        )

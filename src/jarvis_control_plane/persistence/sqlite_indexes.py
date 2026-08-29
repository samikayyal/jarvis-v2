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


class _SQLiteIndexesMixin:
    def _rebuild_durable_memory_index(self) -> None:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute("DELETE FROM durable_assistant_memory_fts")
            rows = self.connection.execute(
                """
                SELECT memory_id, content, created_at, updated_at,
                       source_message_id, status, credential_like,
                       replaced_by_memory_id
                FROM durable_assistant_memory
                WHERE status = 'active'
                ORDER BY created_at, memory_id
                """
            ).fetchall()
            for row in rows:
                memory = _durable_memory_from_row(row)
                credential_like = int(bool(memory.credential_like))
                if credential_like != int(row["credential_like"]):
                    self.connection.execute(
                        """
                        UPDATE durable_assistant_memory
                        SET credential_like = ?
                        WHERE memory_id = ?
                        """,
                        (credential_like, memory.memory_id),
                    )
                if memory.content is not None and not memory.credential_like:
                    self.connection.execute(
                        """
                        INSERT INTO durable_assistant_memory_fts(memory_id, content)
                        VALUES (?, ?)
                        """,
                        (memory.memory_id, memory.content),
                    )
            self.connection.commit()
        except sqlite3.Error:
            self.connection.rollback()
            raise

    def _rebuild_conversation_history_for_outbound(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        self.connection.execute(
            "ALTER TABLE conversation_history RENAME TO conversation_history_legacy"
        )
        self.connection.execute(
            """
            CREATE TABLE conversation_history (
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
                credential_like INTEGER NOT NULL DEFAULT 0 CHECK (credential_like IN (0, 1)),
                PRIMARY KEY (transport_session_id, message_id)
            )
            """
        )
        self.connection.execute(
            """
            INSERT INTO conversation_history(
                transport_session_id, working_session_id, message_id, event_id,
                chat_id, sender_id, text, occurred_at, direction, request_id,
                credential_like
            )
            SELECT transport_session_id, working_session_id, message_id, event_id,
                   chat_id, sender_id, text, occurred_at, direction, request_id,
                   credential_like
            FROM conversation_history_legacy
            """
        )
        self.connection.execute("DROP TABLE conversation_history_legacy")
        self.connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS conversation_history_by_working_session
                ON conversation_history(working_session_id, occurred_at, transport_session_id, message_id);
            CREATE INDEX IF NOT EXISTS conversation_history_by_request
                ON conversation_history(request_id, occurred_at, transport_session_id, message_id);
            CREATE INDEX IF NOT EXISTS conversation_history_by_direction
                ON conversation_history(direction, occurred_at, transport_session_id, message_id);
            CREATE INDEX IF NOT EXISTS conversation_history_by_occurred_at
                ON conversation_history(occurred_at, transport_session_id, message_id);
            """
        )
        self.connection.commit()

    def _classify_and_index_conversation_history(self) -> None:
        rows = self.connection.execute(
            """
            SELECT transport_session_id, message_id, text, credential_like
            FROM conversation_history ORDER BY occurred_at, transport_session_id, message_id
            """
        ).fetchall()
        self.connection.execute("DELETE FROM conversation_history_fts")
        for row in rows:
            message = ConversationMessage(
                working_session_id="classification-only",
                transport_session_id=row["transport_session_id"],
                message_id=row["message_id"],
                event_id="classification-only",
                chat_id="classification-only",
                sender_id="classification-only",
                text=row["text"],
                occurred_at=datetime.now(UTC),
                credential_like=bool(row["credential_like"]),
            )
            self.connection.execute(
                """
                UPDATE conversation_history SET credential_like = ?
                WHERE transport_session_id = ? AND message_id = ?
                """,
                (
                    int(message.credential_like),
                    row["transport_session_id"],
                    row["message_id"],
                ),
            )
            self.connection.execute(
                """
                INSERT INTO conversation_history_fts(
                    transport_session_id, message_id, text
                ) VALUES (?, ?, ?)
                """,
                (row["transport_session_id"], row["message_id"], row["text"]),
            )
        self.connection.commit()

    @_locked_sqlite_state
    def close(self) -> None:
        self._deleted_archive = None
        if self._owns_connection:
            self.connection.close()

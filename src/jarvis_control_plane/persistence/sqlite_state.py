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

from .sqlite_dispatch import _SQLiteDispatchMixin
from .sqlite_history import _SQLiteHistoryMixin
from .sqlite_indexes import _SQLiteIndexesMixin
from .sqlite_memory import _SQLiteMemoryMixin
from .sqlite_outbound import _SQLiteOutboundMixin
from .sqlite_outbound_recovery import _SQLiteOutboundRecoveryMixin
from .sqlite_recovery import _SQLiteRecoveryMixin


class SQLiteDurableStateStore(
    _SQLiteRecoveryMixin,
    _SQLiteDispatchMixin,
    _SQLiteOutboundMixin,
    _SQLiteOutboundRecoveryMixin,
    _SQLiteHistoryMixin,
    _SQLiteMemoryMixin,
    _SQLiteIndexesMixin,
):
    """Small SQLite-backed durable state adapter for the primary seam."""

    def __init__(
        self,
        database: str | Path | sqlite3.Connection = ":memory:",
        *,
        deleted_archive: DeletedConversationArchiveWriter | None = None,
    ) -> None:
        self._owns_connection = not isinstance(database, sqlite3.Connection)
        self.connection = (
            database
            if isinstance(database, sqlite3.Connection)
            else sqlite3.connect(str(database), check_same_thread=False)
        )
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conversation_has_legacy_session = False
        self._deleted_archive = deleted_archive
        try:
            self._assert_outbound_attempt_schema_is_current()
            self.connection.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS ingress_claims (
                    session_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    disposition TEXT NOT NULL DEFAULT 'admitted',
                    PRIMARY KEY (session_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS request_state (
                    request_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    operator_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT 'gpt-5.6-terra',
                    reasoning TEXT NOT NULL DEFAULT 'medium',
                    reply_id TEXT,
                    outcome TEXT,
                    error_code TEXT
                );
                CREATE TABLE IF NOT EXISTS conversation_history (
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
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS conversation_history_fts
                USING fts5(
                    transport_session_id UNINDEXED,
                    message_id UNINDEXED,
                    text
                );
                CREATE TABLE IF NOT EXISTS outbound_conversation_outbox (
                    transport_session_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    working_session_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    credential_like INTEGER NOT NULL CHECK (credential_like IN (0, 1)),
                    PRIMARY KEY (transport_session_id, message_id)
                );
                {_SQLITE_OUTBOUND_ATTEMPT_TABLE_SQL};
                CREATE INDEX IF NOT EXISTS conversation_history_by_working_session
                    ON conversation_history(working_session_id, occurred_at, transport_session_id, message_id);
                CREATE INDEX IF NOT EXISTS conversation_history_by_request
                    ON conversation_history(request_id, occurred_at, transport_session_id, message_id);
                CREATE INDEX IF NOT EXISTS conversation_history_by_direction
                    ON conversation_history(direction, occurred_at, transport_session_id, message_id);
                CREATE INDEX IF NOT EXISTS conversation_history_by_occurred_at
                    ON conversation_history(occurred_at, transport_session_id, message_id);
                CREATE TABLE IF NOT EXISTS conversation_tombstones (
                    tombstone_id TEXT PRIMARY KEY,
                    deletion_id TEXT NOT NULL,
                    history_id TEXT NOT NULL UNIQUE,
                    transport_session_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    working_session_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    deleted_at TEXT NOT NULL,
                    scope_type TEXT NOT NULL CHECK (scope_type IN ('message', 'conversation', 'date_range'))
                );
                CREATE INDEX IF NOT EXISTS conversation_tombstones_by_deleted_at
                    ON conversation_tombstones(deleted_at, tombstone_id);
                CREATE TABLE IF NOT EXISTS durable_assistant_memory (
                    memory_id TEXT PRIMARY KEY,
                    content TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    source_message_id TEXT,
                    status TEXT NOT NULL CHECK (status IN ('active', 'replaced', 'forgotten')),
                    credential_like INTEGER NOT NULL CHECK (credential_like IN (0, 1)),
                    replaced_by_memory_id TEXT
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS durable_assistant_memory_fts
                USING fts5(
                    memory_id UNINDEXED,
                    content
                );
                CREATE INDEX IF NOT EXISTS durable_assistant_memory_by_status
                    ON durable_assistant_memory(status, created_at, memory_id);
                CREATE TABLE IF NOT EXISTS knowledge_vault_synchronization (
                    slot INTEGER PRIMARY KEY CHECK (slot = 1),
                    synchronized_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recovery_degraded_marker (
                    marker_id INTEGER PRIMARY KEY CHECK (marker_id = 1),
                    reason TEXT NOT NULL,
                    marked_at TEXT NOT NULL
                );
                """
            )
            request_columns = {
                row["name"]
                for row in self.connection.execute(
                    "PRAGMA table_info(request_state)"
                ).fetchall()
            }
            if "model" not in request_columns:
                self.connection.execute(
                    "ALTER TABLE request_state "
                    "ADD COLUMN model TEXT NOT NULL DEFAULT 'gpt-5.6-terra'"
                )
            if "reasoning" not in request_columns:
                self.connection.execute(
                    "ALTER TABLE request_state "
                    "ADD COLUMN reasoning TEXT NOT NULL DEFAULT 'medium'"
                )
            self.connection.commit()
            columns = {
                row["name"]
                for row in self.connection.execute(
                    "PRAGMA table_info(ingress_claims)"
                ).fetchall()
            }
            if "disposition" not in columns:
                self.connection.execute(
                    """
                    ALTER TABLE ingress_claims
                    ADD COLUMN disposition TEXT NOT NULL DEFAULT 'admitted'
                    """
                )
                self.connection.commit()
            self.connection.execute(
                "UPDATE ingress_claims SET disposition = 'audit_blocked' "
                "WHERE disposition = 'pending_audit'"
            )
            self.connection.commit()

            conversation_columns = {
                row["name"]
                for row in self.connection.execute(
                    "PRAGMA table_info(conversation_history)"
                ).fetchall()
            }
            if "transport_session_id" not in conversation_columns:
                self.connection.execute(
                    "ALTER TABLE conversation_history "
                    "ADD COLUMN transport_session_id TEXT"
                )
                conversation_columns.add("transport_session_id")
            if "working_session_id" not in conversation_columns:
                self.connection.execute(
                    "ALTER TABLE conversation_history "
                    "ADD COLUMN working_session_id TEXT"
                )
                conversation_columns.add("working_session_id")
            if "request_id" not in conversation_columns:
                self.connection.execute(
                    "ALTER TABLE conversation_history ADD COLUMN request_id TEXT"
                )
                conversation_columns.add("request_id")
            if "credential_like" not in conversation_columns:
                self.connection.execute(
                    "ALTER TABLE conversation_history "
                    "ADD COLUMN credential_like INTEGER NOT NULL DEFAULT 0"
                )
                conversation_columns.add("credential_like")
            if "session_id" in conversation_columns:
                self._conversation_has_legacy_session = True
                self.connection.execute(
                    "UPDATE conversation_history "
                    "SET transport_session_id = session_id "
                    "WHERE transport_session_id IS NULL"
                )
                self.connection.execute(
                    "UPDATE conversation_history "
                    "SET working_session_id = 'legacy-working-' || session_id "
                    "WHERE working_session_id IS NULL"
                )
                self.connection.commit()
            history_schema = self.connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' "
                "AND name = 'conversation_history'"
            ).fetchone()["sql"]
            if "direction = 'inbound'" in history_schema:
                self._rebuild_conversation_history_for_outbound()
                self._conversation_has_legacy_session = False
            self._classify_and_index_conversation_history()
            self._rebuild_durable_memory_index()
        except sqlite3.Error as exc:
            self.close()
            raise StateStoreError("could not initialize SQLite state") from exc
        except StateStoreError:
            self.close()
            raise

    def _assert_outbound_attempt_schema_is_current(self) -> None:
        """Reject a Ticket 12-incompatible database without changing it."""

        tables = {
            row["name"]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        has_outbox = "outbound_conversation_outbox" in tables
        has_attempts = "outbound_attempt_record" in tables
        if has_outbox != has_attempts:
            raise StateStoreError(
                "SQLite outbound state requires the manual Ticket 12 migration"
            )
        if not has_attempts:
            return
        columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(outbound_attempt_record)"
            ).fetchall()
        }
        if "outbound_id" not in columns:
            raise StateStoreError(
                "SQLite outbound state requires the manual Ticket 12 migration"
            )

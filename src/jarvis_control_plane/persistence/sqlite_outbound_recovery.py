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


class _SQLiteOutboundRecoveryMixin:
    @_locked_sqlite_state
    def list_outbound_conversation_attempts(
        self,
    ) -> tuple[OutboundAttemptRecord, ...]:
        try:
            rows = self.connection.execute(
                """
                SELECT a.transport_session_id, a.message_id, a.request_id, a.status,
                       a.outbound_id, a.reserved_at, a.attempted_at, a.terminal_at,
                       o.working_session_id, o.event_id, o.chat_id, o.sender_id,
                       o.text, o.occurred_at, o.credential_like
                FROM outbound_attempt_record AS a
                LEFT JOIN outbound_conversation_outbox AS o
                  ON o.transport_session_id = a.transport_session_id
                 AND o.message_id = a.message_id
                ORDER BY a.reserved_at, a.transport_session_id, a.message_id
                """
            ).fetchall()
        except sqlite3.Error as exc:
            raise StateStoreError("could not list outbound attempts") from exc
        return tuple(_outbound_attempt_from_row(row) for row in rows)

    @_locked_sqlite_state
    def list_outbound_conversation_attempt_recovery(
        self,
    ) -> tuple[OutboundAttemptRecoveryProjection, ...]:
        """Load bounded attempt/outbox facts without constructing domain records."""

        try:
            attempt_rows = self.connection.execute(
                """
                SELECT a.transport_session_id, a.message_id, a.request_id,
                       a.status, a.outbound_id, a.reserved_at, a.attempted_at,
                       a.terminal_at, o.request_id AS outbox_request_id,
                       CASE WHEN o.message_id IS NULL THEN 0 ELSE 1 END
                           AS outbox_present
                FROM outbound_attempt_record AS a
                LEFT JOIN outbound_conversation_outbox AS o
                  ON o.transport_session_id = a.transport_session_id
                 AND o.message_id = a.message_id
                ORDER BY a.reserved_at, a.transport_session_id, a.message_id
                """
            ).fetchall()
            outbox_rows = self.connection.execute(
                """
                SELECT o.transport_session_id, o.message_id, o.request_id
                FROM outbound_conversation_outbox AS o
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM outbound_attempt_record AS a
                    WHERE a.transport_session_id = o.transport_session_id
                      AND a.message_id = o.message_id
                )
                ORDER BY o.occurred_at, o.transport_session_id, o.message_id
                """
            ).fetchall()
        except sqlite3.Error as exc:
            raise StateStoreError(
                "could not load outbound recovery projection"
            ) from exc

        projections: list[OutboundAttemptRecoveryProjection] = [
            OutboundAttemptRecoveryProjection(
                transport_session_id=row["transport_session_id"],
                message_id=row["message_id"],
                attempt_present=True,
                outbox_present=bool(row["outbox_present"]),
                attempt_request_id=row["request_id"],
                outbox_request_id=row["outbox_request_id"],
                status=row["status"],
                reserved_at=row["reserved_at"],
                attempted_at=row["attempted_at"],
                terminal_at=row["terminal_at"],
                outbound_id=row["outbound_id"],
            )
            for row in attempt_rows
        ]
        projections.extend(
            OutboundAttemptRecoveryProjection(
                transport_session_id=row["transport_session_id"],
                message_id=row["message_id"],
                attempt_present=False,
                outbox_present=True,
                outbox_request_id=row["request_id"],
            )
            for row in outbox_rows
        )
        return tuple(projections)

    @_locked_sqlite_state
    def reconcile_outbound_conversation_attempts(
        self, *, interrupted_at: datetime
    ) -> tuple[OutboundAttemptRecord, ...]:
        interrupted_at = ensure_utc(interrupted_at)
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            projections = self.list_outbound_conversation_attempt_recovery()
            reconciled: list[OutboundAttemptRecord] = []
            for projection in projections:
                if not projection.attempt_present:
                    self.connection.execute(
                        """
                        DELETE FROM outbound_conversation_outbox
                        WHERE transport_session_id = ? AND message_id = ?
                        """,
                        (
                            projection.transport_session_id,
                            projection.message_id,
                        ),
                    )
                    continue
                if projection.status not in {
                    OutboundAttemptStatus.UNATTEMPTED.value,
                    OutboundAttemptStatus.ATTEMPTED.value,
                }:
                    if projection.outbox_present:
                        self.connection.execute(
                            """
                            DELETE FROM outbound_conversation_outbox
                            WHERE transport_session_id = ? AND message_id = ?
                            """,
                            (
                                projection.transport_session_id,
                                projection.message_id,
                            ),
                        )
                    continue
                status = (
                    OutboundAttemptStatus.NOT_STARTED
                    if projection.status == OutboundAttemptStatus.UNATTEMPTED.value
                    else OutboundAttemptStatus.UNKNOWN
                )
                self.connection.execute(
                    """
                    UPDATE outbound_attempt_record
                    SET status = ?, outbound_id = NULL, terminal_at = ?
                    WHERE transport_session_id = ? AND message_id = ? AND status = ?
                    """,
                    (
                        status.value,
                        interrupted_at.isoformat(),
                        projection.transport_session_id,
                        projection.message_id,
                        projection.status,
                    ),
                )
                self.connection.execute(
                    """
                    DELETE FROM outbound_conversation_outbox
                    WHERE transport_session_id = ? AND message_id = ?
                    """,
                    (projection.transport_session_id, projection.message_id),
                )
                recovered = _recovered_terminal_attempt_record(
                    projection,
                    status=status,
                    terminal_at=interrupted_at,
                )
                if recovered is not None:
                    reconciled.append(recovered)
            self.connection.commit()
        except StateStoreError:
            self.connection.rollback()
            raise
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise StateStoreError("could not reconcile outbound attempts") from exc
        return tuple(reconciled)

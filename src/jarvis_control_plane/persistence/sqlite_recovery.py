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


class _SQLiteRecoveryMixin:
    @_locked_sqlite_state
    def load_recovery_degraded_marker(self) -> RecoveryDegradedMarker | None:
        try:
            row = self.connection.execute(
                """
                SELECT reason, marked_at
                FROM recovery_degraded_marker
                WHERE marker_id = 1
                """
            ).fetchone()
        except sqlite3.Error as exc:
            raise StateStoreError(
                "could not load the recovery-degraded marker"
            ) from exc
        if row is None:
            return None
        try:
            return RecoveryDegradedMarker(
                reason=row["reason"],
                marked_at=datetime.fromisoformat(row["marked_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise StateStoreError("stored recovery-degraded marker is invalid") from exc

    @_locked_sqlite_state
    def mark_recovery_degraded(self, *, reason: str, marked_at: datetime) -> None:
        marker = RecoveryDegradedMarker(reason=reason, marked_at=marked_at)
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                """
                INSERT INTO recovery_degraded_marker(marker_id, reason, marked_at)
                VALUES (1, ?, ?)
                ON CONFLICT(marker_id) DO NOTHING
                """,
                (marker.reason, marker.marked_at.isoformat()),
            )
            self.connection.commit()
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise StateStoreError(
                "could not persist the recovery-degraded marker"
            ) from exc

    @_locked_sqlite_state
    def acknowledge_recovery_degraded(self) -> None:
        """Clear the marker only when called by an explicit admin flow."""

        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                "DELETE FROM recovery_degraded_marker WHERE marker_id = 1"
            )
            self.connection.commit()
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise StateStoreError(
                "could not acknowledge the recovery-degraded marker"
            ) from exc

    @_locked_sqlite_state
    def admit_ingress(
        self,
        *,
        session_id: str,
        message_id: str,
        event_id: str,
        claimed_at: datetime,
        conversation_message: ConversationMessage | None,
        audit: AuditBoundary,
        audit_evidence: AuditEvidence,
        terminal_disposition: str,
        audit_blocked_disposition: str | None = None,
    ) -> IngressAdmissionResult:
        """Commit one ingress claim, history row, audit row, and disposition.

        SQLite state and audit share a transaction when they use the same
        connection.  When audit is an independent boundary, the state rows
        are still staged before the append and rolled back on an audit error;
        an admitted message can then be retained in a terminal blocked state.
        """

        key = (session_id, message_id)
        if (
            conversation_message is not None
            and (
                conversation_message.transport_session_id,
                conversation_message.message_id,
            )
            != key
        ):
            raise StateStoreError("conversation message key does not match claim")

        try:
            self.connection.execute("BEGIN IMMEDIATE")
            existing = self.connection.execute(
                """
                SELECT 1 FROM ingress_claims
                WHERE session_id = ? AND message_id = ?
                """,
                key,
            ).fetchone()
            if existing is not None:
                self.connection.rollback()
                return IngressAdmissionResult(
                    claimed=False,
                    disposition="duplicate",
                )

            self._insert_ingress_row(
                session_id=session_id,
                message_id=message_id,
                event_id=event_id,
                claimed_at=claimed_at,
                disposition=terminal_disposition,
            )
            if conversation_message is not None:
                self._insert_conversation_message(conversation_message)

            shared_audit = (
                isinstance(audit, SQLiteAuditBoundary)
                and audit._connection is self.connection
            )
            try:
                if shared_audit:
                    audit._append_batch_in_transaction((audit_evidence,))
                else:
                    audit.append(audit_evidence)
            except AuditWriteError:
                self.connection.rollback()
                if audit_blocked_disposition is None:
                    return IngressAdmissionResult(
                        claimed=False,
                        disposition=terminal_disposition,
                    )
                self.connection.execute("BEGIN IMMEDIATE")
                self._insert_ingress_row(
                    session_id=session_id,
                    message_id=message_id,
                    event_id=event_id,
                    claimed_at=claimed_at,
                    disposition=audit_blocked_disposition,
                )
                if conversation_message is not None:
                    self._insert_conversation_message(conversation_message)
                self.connection.commit()
                return IngressAdmissionResult(
                    claimed=True,
                    disposition=audit_blocked_disposition,
                )

            self.connection.commit()
            return IngressAdmissionResult(
                claimed=True,
                disposition=terminal_disposition,
            )
        except AuditWriteError:
            try:
                self.connection.rollback()
            except sqlite3.Error:
                pass
            raise
        except sqlite3.Error as exc:
            try:
                self.connection.rollback()
            except sqlite3.Error:
                pass
            raise StateStoreError("could not admit ingress") from exc

    def _insert_ingress_row(
        self,
        *,
        session_id: str,
        message_id: str,
        event_id: str,
        claimed_at: datetime,
        disposition: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO ingress_claims(
                session_id, message_id, event_id, claimed_at, disposition
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                session_id,
                message_id,
                event_id,
                ensure_utc(claimed_at).isoformat(),
                disposition,
            ),
        )

    def _insert_conversation_message(
        self,
        conversation_message: ConversationMessage,
    ) -> None:
        values = (
            conversation_message.transport_session_id,
            conversation_message.working_session_id,
            conversation_message.message_id,
            conversation_message.event_id,
            conversation_message.chat_id,
            conversation_message.sender_id,
            conversation_message.text,
            ensure_utc(conversation_message.occurred_at).isoformat(),
            conversation_message.direction,
            conversation_message.request_id,
            int(conversation_message.credential_like),
        )
        if self._conversation_has_legacy_session:
            self.connection.execute(
                """
                INSERT INTO conversation_history(
                    session_id, transport_session_id, working_session_id,
                    message_id, event_id, chat_id, sender_id, text,
                    occurred_at, direction, request_id, credential_like
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_message.transport_session_id,
                    conversation_message.transport_session_id,
                    *values,
                ),
            )
        else:
            self.connection.execute(
                """
                INSERT INTO conversation_history(
                    transport_session_id, working_session_id, message_id,
                    event_id, chat_id, sender_id, text, occurred_at, direction,
                    request_id, credential_like
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        self.connection.execute(
            """
            INSERT INTO conversation_history_fts(
                transport_session_id, message_id, text
            ) VALUES (?, ?, ?)
            """,
            (
                conversation_message.transport_session_id,
                conversation_message.message_id,
                conversation_message.text,
            ),
        )

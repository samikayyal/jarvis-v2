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


class _SQLiteDispatchMixin:
    @_locked_sqlite_state
    def claim_ingress(
        self,
        *,
        session_id: str,
        message_id: str,
        event_id: str,
        claimed_at: datetime,
        conversation_message: ConversationMessage | None = None,
        disposition: str = "admitted",
    ) -> bool:
        if disposition == "pending_audit":
            raise StateStoreError("ingress claims require a terminal disposition")
        try:
            cursor = self.connection.execute(
                """
                INSERT INTO ingress_claims(
                    session_id, message_id, event_id, claimed_at, disposition
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id, message_id) DO NOTHING
                """,
                (
                    session_id,
                    message_id,
                    event_id,
                    ensure_utc(claimed_at).isoformat(),
                    disposition,
                ),
            )
            claimed = cursor.rowcount == 1
            if claimed and conversation_message is not None:
                if (
                    conversation_message.transport_session_id,
                    conversation_message.message_id,
                ) != (session_id, message_id):
                    self.connection.rollback()
                    raise StateStoreError(
                        "conversation message key does not match claim"
                    )
                self._insert_conversation_message(conversation_message)
            self.connection.commit()
            return claimed
        except StateStoreError:
            raise
        except sqlite3.Error as exc:
            try:
                self.connection.rollback()
            except sqlite3.Error:
                pass
            raise StateStoreError("could not claim ingress") from exc

    @_locked_sqlite_state
    def update_ingress_disposition(
        self,
        *,
        session_id: str,
        message_id: str,
        disposition: str,
    ) -> None:
        if disposition == "pending_audit":
            raise StateStoreError("ingress claims require a terminal disposition")
        try:
            cursor = self.connection.execute(
                """
                UPDATE ingress_claims
                SET disposition = ?
                WHERE session_id = ? AND message_id = ?
                """,
                (disposition, session_id, message_id),
            )
            if cursor.rowcount != 1:
                self.connection.rollback()
                raise StateStoreError("ingress claim does not exist")
            self.connection.commit()
        except StateStoreError:
            raise
        except sqlite3.Error as exc:
            raise StateStoreError("could not update ingress disposition") from exc

    @_locked_sqlite_state
    def begin_next_ingress_dispatch(self) -> ConversationMessage | None:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                """
                SELECT h.transport_session_id, h.working_session_id,
                       h.message_id, h.event_id, h.chat_id, h.sender_id,
                       h.text, h.occurred_at, h.direction, h.request_id,
                       h.credential_like
                FROM ingress_claims AS i
                JOIN conversation_history AS h
                  ON h.transport_session_id = i.session_id
                 AND h.message_id = i.message_id
                WHERE i.disposition = 'admitted'
                ORDER BY i.claimed_at, i.session_id, i.message_id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                self.connection.commit()
                return None
            cursor = self.connection.execute(
                """
                UPDATE ingress_claims
                SET disposition = 'dispatching'
                WHERE session_id = ? AND message_id = ?
                  AND disposition = 'admitted'
                """,
                (row["transport_session_id"], row["message_id"]),
            )
            if cursor.rowcount != 1:
                self.connection.rollback()
                return None
            self.connection.commit()
            return _conversation_message_from_row(row)
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise StateStoreError("could not begin ingress dispatch") from exc

    @_locked_sqlite_state
    def begin_ingress_dispatch(
        self, *, transport_session_id: str, message_id: str
    ) -> bool:
        try:
            cursor = self.connection.execute(
                """
                UPDATE ingress_claims
                SET disposition = 'dispatching'
                WHERE session_id = ? AND message_id = ?
                  AND disposition = 'admitted'
                  AND EXISTS (
                      SELECT 1 FROM conversation_history AS h
                      WHERE h.transport_session_id = ingress_claims.session_id
                        AND h.message_id = ingress_claims.message_id
                  )
                """,
                (transport_session_id, message_id),
            )
            self.connection.commit()
            return cursor.rowcount == 1
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise StateStoreError("could not begin ingress dispatch") from exc

    @_locked_sqlite_state
    def finish_ingress_dispatch(
        self,
        *,
        transport_session_id: str,
        message_id: str,
        disposition: str,
    ) -> None:
        if disposition not in {"dispatched", "interrupted"}:
            raise StateStoreError("ingress dispatch disposition is invalid")
        try:
            cursor = self.connection.execute(
                """
                UPDATE ingress_claims
                SET disposition = ?
                WHERE session_id = ? AND message_id = ?
                  AND disposition = 'dispatching'
                """,
                (disposition, transport_session_id, message_id),
            )
            if cursor.rowcount != 1:
                self.connection.rollback()
                raise StateStoreError("ingress dispatch is not active")
            self.connection.commit()
        except StateStoreError:
            raise
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise StateStoreError("could not finish ingress dispatch") from exc

    @_locked_sqlite_state
    def reconcile_ingress_restart(
        self,
        *,
        audit: AuditBoundary,
        audit_evidence: AuditEvidence,
    ) -> int:
        """Atomically audit and interrupt all nonterminal ingress work."""

        try:
            self.connection.execute("BEGIN IMMEDIATE")
            cursor = self.connection.execute(
                """
                UPDATE ingress_claims
                SET disposition = 'interrupted'
                WHERE disposition IN ('admitted', 'dispatching')
                """
            )
            if cursor.rowcount == 0:
                self.connection.commit()
                return 0
            shared_audit = (
                isinstance(audit, SQLiteAuditBoundary)
                and audit._connection is self.connection
            )
            if shared_audit:
                audit._append_batch_in_transaction((audit_evidence,))
            else:
                audit.append(audit_evidence)
            self.connection.commit()
            return cursor.rowcount
        except AuditWriteError:
            self.connection.rollback()
            raise
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise StateStoreError("could not reconcile ingress restart") from exc

    @_locked_sqlite_state
    def has_ingress_claim(self, *, session_id: str, message_id: str) -> bool:
        try:
            row = self.connection.execute(
                """
                SELECT 1 FROM ingress_claims
                WHERE session_id = ? AND message_id = ?
                """,
                (session_id, message_id),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StateStoreError("could not inspect ingress claim") from exc
        return row is not None

    @_locked_sqlite_state
    def release_ingress_claim(self, *, session_id: str, message_id: str) -> bool:
        try:
            cursor = self.connection.execute(
                "DELETE FROM ingress_claims WHERE session_id = ? AND message_id = ?",
                (session_id, message_id),
            )
            self.connection.commit()
            return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise StateStoreError("could not release ingress claim") from exc

    @_locked_sqlite_state
    def save_request(self, request: RequestState) -> None:
        try:
            self.connection.execute(
                """
                INSERT INTO request_state(
                    request_id, event_id, message_id, operator_id, session_id,
                    chat_id, created_at, updated_at, status, phase,
                    model, reasoning, reply_id, outcome, error_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _request_values(request),
            )
            self.connection.commit()
        except sqlite3.Error as exc:
            raise StateStoreError("could not save request state") from exc

    @_locked_sqlite_state
    def update_request(self, request: RequestState) -> None:
        try:
            cursor = self.connection.execute(
                """
                UPDATE request_state SET
                    event_id = ?, message_id = ?, operator_id = ?, session_id = ?,
                    chat_id = ?, created_at = ?, updated_at = ?, status = ?,
                    phase = ?, model = ?, reasoning = ?, reply_id = ?, outcome = ?, error_code = ?
                WHERE request_id = ?
                """,
                (
                    request.event_id,
                    request.message_id,
                    request.operator_id,
                    request.session_id,
                    request.chat_id,
                    ensure_utc(request.created_at).isoformat(),
                    ensure_utc(request.updated_at).isoformat(),
                    request.status,
                    request.phase,
                    request.model,
                    request.reasoning,
                    request.reply_id,
                    request.outcome,
                    request.error_code,
                    request.request_id,
                ),
            )
            if cursor.rowcount != 1:
                self.connection.rollback()
                raise StateStoreError("request state does not exist")
            self.connection.commit()
        except StateStoreError:
            raise
        except sqlite3.Error as exc:
            raise StateStoreError("could not update request state") from exc

    @_locked_sqlite_state
    def delete_request(self, request_id: str) -> bool:
        try:
            cursor = self.connection.execute(
                "DELETE FROM request_state WHERE request_id = ?",
                (request_id,),
            )
            self.connection.commit()
            return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise StateStoreError("could not delete request state") from exc

    @_locked_sqlite_state
    def get_request(self, request_id: str) -> RequestState | None:
        try:
            row = self.connection.execute(
                "SELECT * FROM request_state WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StateStoreError("could not read request state") from exc
        return _request_from_row(row) if row else None

    @_locked_sqlite_state
    def list_requests(self) -> tuple[RequestState, ...]:
        try:
            rows = self.connection.execute(
                "SELECT * FROM request_state ORDER BY created_at, request_id"
            ).fetchall()
        except sqlite3.Error as exc:
            raise StateStoreError("could not list request state") from exc
        return tuple(_request_from_row(row) for row in rows)

    @_locked_sqlite_state
    def list_ingress_claims(self) -> tuple[IngressClaim, ...]:
        try:
            rows = self.connection.execute(
                """
                SELECT session_id, message_id, event_id, claimed_at, disposition
                FROM ingress_claims ORDER BY claimed_at, session_id, message_id
                """
            ).fetchall()
        except sqlite3.Error as exc:
            raise StateStoreError("could not list ingress claims") from exc
        return tuple(
            IngressClaim(
                session_id=row["session_id"],
                message_id=row["message_id"],
                event_id=row["event_id"],
                claimed_at=datetime.fromisoformat(row["claimed_at"]),
                disposition=row["disposition"],
            )
            for row in rows
        )

    @_locked_sqlite_state
    def load_knowledge_vault_synchronized_at(self) -> datetime | None:
        try:
            row = self.connection.execute(
                "SELECT synchronized_at FROM knowledge_vault_synchronization WHERE slot = 1"
            ).fetchone()
        except sqlite3.Error as exc:
            raise StateStoreError(
                "could not read knowledge-vault synchronization"
            ) from exc
        if row is None:
            return None
        try:
            synchronized_at = datetime.fromisoformat(row["synchronized_at"])
        except (TypeError, ValueError) as exc:
            raise StateStoreError(
                "knowledge-vault synchronization metadata is invalid"
            ) from exc
        if synchronized_at.tzinfo is None:
            raise StateStoreError("knowledge-vault synchronization metadata is invalid")
        return ensure_utc(synchronized_at)

    @_locked_sqlite_state
    def save_knowledge_vault_synchronized_at(self, synchronized_at: datetime) -> None:
        try:
            self.connection.execute(
                """
                INSERT INTO knowledge_vault_synchronization(slot, synchronized_at)
                VALUES (1, ?)
                ON CONFLICT(slot) DO UPDATE SET synchronized_at = excluded.synchronized_at
                """,
                (ensure_utc(synchronized_at).isoformat(),),
            )
            self.connection.commit()
        except sqlite3.Error as exc:
            raise StateStoreError(
                "could not save knowledge-vault synchronization"
            ) from exc

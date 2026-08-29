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


class _SQLiteHistoryMixin:
    @_locked_sqlite_state
    def search_conversation_messages(
        self,
        *,
        text: str | None = None,
        working_session_id: str | None = None,
        request_id: str | None = None,
        direction: str | None = None,
        history_ids: tuple[str, ...] = (),
        limit: int = 50,
    ) -> tuple[ConversationMessage, ...]:
        _validate_history_query(
            text=text,
            direction=direction,
            history_ids=history_ids,
            limit=limit,
        )
        terms = _history_search_terms(text or "")
        if text is not None and not terms:
            return ()
        clauses: list[str] = []
        values: list[object] = []
        join = ""
        if terms:
            join = (
                " JOIN conversation_history_fts AS f "
                "ON f.transport_session_id = h.transport_session_id "
                "AND f.message_id = h.message_id "
            )
            clauses.append("f.text MATCH ?")
            values.append(_fts_history_query(terms))
        if history_ids:
            selectors = tuple(
                ConversationMessage.history_id_parts(value) for value in history_ids
            )
            clauses.append(
                "("
                + " OR ".join(
                    "(h.transport_session_id = ? AND h.message_id = ?)"
                    for _ in selectors
                )
                + ")"
            )
            values.extend(item for selector in selectors for item in selector)
        if working_session_id is not None:
            clauses.append("h.working_session_id = ?")
            values.append(working_session_id)
        if request_id is not None:
            clauses.append("h.request_id = ?")
            values.append(request_id)
        if direction is not None:
            clauses.append("h.direction = ?")
            values.append(direction)
        rows = self.connection.execute(
            """
            SELECT h.transport_session_id, h.working_session_id, h.message_id,
                   h.event_id, h.chat_id, h.sender_id, h.text, h.occurred_at,
                   h.direction, h.request_id, h.credential_like
            FROM conversation_history AS h
            """
            + join
            + (" WHERE " + " AND ".join(clauses) if clauses else "")
            + " ORDER BY h.occurred_at, h.transport_session_id, h.message_id LIMIT ?",
            [*values, limit],
        ).fetchall()
        matches = _filter_conversation_messages(
            tuple(_conversation_message_from_row(row) for row in rows),
            text=text,
            working_session_id=working_session_id,
            request_id=request_id,
            direction=direction,
            history_ids=history_ids,
            limit=limit,
        )
        return matches

    @_locked_sqlite_state
    def export_conversation_messages(self, **query: object) -> str:
        return _export_conversation_messages(
            self.search_conversation_messages(**query)  # type: ignore[arg-type]
        )

    @_locked_sqlite_state
    def select_history_for_context(
        self,
        *,
        text: str,
        excluding_working_session_id: str,
        limit: int = 5,
    ) -> HistorySelection:
        _validate_history_query(text=text, direction=None, history_ids=(), limit=limit)
        terms = _history_search_terms(text)
        if not terms:
            return HistorySelection(())
        rows = self.connection.execute(
            """
            SELECT h.transport_session_id, h.working_session_id, h.message_id,
                   h.event_id, h.chat_id, h.sender_id, h.text, h.occurred_at,
                   h.direction, h.request_id, h.credential_like
            FROM conversation_history AS h
            JOIN conversation_history_fts AS f
              ON f.transport_session_id = h.transport_session_id
             AND f.message_id = h.message_id
            WHERE f.text MATCH ?
              AND h.working_session_id != ?
              AND h.credential_like = 0
            ORDER BY h.occurred_at, h.transport_session_id, h.message_id
            LIMIT ?
            """,
            (
                _fts_history_query(terms),
                excluding_working_session_id,
                _MAX_HISTORY_RESULTS,
            ),
        ).fetchall()
        return HistorySelection(
            _filter_conversation_messages(
                tuple(_conversation_message_from_row(row) for row in rows),
                text=text,
                limit=limit,
            )
        )

    @_locked_sqlite_state
    def preview_conversation_deletion(
        self, scope: ConversationDeletionScope
    ) -> ConversationDeletionPreview:
        try:
            return _preview_conversation_deletion(
                self._select_conversation_messages_for_deletion(scope), scope
            )
        except StateStoreError:
            raise
        except (TypeError, ValueError) as exc:
            raise StateStoreError("could not preview conversation deletion") from exc

    def _select_conversation_messages_for_deletion(
        self, scope: ConversationDeletionScope
    ) -> tuple[ConversationMessage, ...]:
        """Read only the indexed rows belonging to one deletion scope."""

        try:
            if scope.scope_type == "message":
                rows: list[sqlite3.Row] = []
                for offset in range(
                    0, len(scope.history_ids), _DELETION_SELECTOR_BATCH_SIZE
                ):
                    batch_scope = ConversationDeletionScope.message(
                        scope.history_ids[
                            offset : offset + _DELETION_SELECTOR_BATCH_SIZE
                        ]
                    )
                    clauses, values = _conversation_deletion_query(batch_scope)
                    rows.extend(
                        self.connection.execute(
                            """
                            SELECT transport_session_id, working_session_id, message_id,
                                   event_id, chat_id, sender_id, text, occurred_at,
                                   direction, request_id, credential_like
                            FROM conversation_history
                            WHERE """
                            + clauses,
                            values,
                        ).fetchall()
                    )
                rows.sort(
                    key=lambda row: (
                        row["occurred_at"],
                        row["transport_session_id"],
                        row["message_id"],
                    )
                )
            else:
                clauses, values = _conversation_deletion_query(scope)
                rows = self.connection.execute(
                    """
                    SELECT transport_session_id, working_session_id, message_id,
                           event_id, chat_id, sender_id, text, occurred_at,
                           direction, request_id, credential_like
                    FROM conversation_history
                    WHERE """
                    + clauses
                    + " ORDER BY occurred_at, transport_session_id, message_id",
                    values,
                ).fetchall()
        except sqlite3.Error as exc:
            raise StateStoreError(
                "could not select conversation history for deletion"
            ) from exc
        return tuple(_conversation_message_from_row(row) for row in rows)

    @_locked_sqlite_state
    def delete_conversation_history(
        self,
        preview: ConversationDeletionPreview,
        *,
        deletion_id: str,
        deleted_at: datetime,
    ) -> tuple[ConversationTombstone, ...]:
        if not isinstance(preview, ConversationDeletionPreview):
            raise TypeError("preview must be a ConversationDeletionPreview")
        if not isinstance(deletion_id, str) or not deletion_id.strip():
            raise ValueError("deletion_id must be non-blank")
        deleted_at = ensure_utc(deleted_at)
        if self._deleted_archive is None:
            raise StateStoreError(
                "deleted conversation archive writer is not configured"
            )
        try:
            _stage_deleted_archive(
                self._deleted_archive,
                preview.messages,
                deletion_id=deletion_id,
                deleted_at=deleted_at,
                expected_count=preview.count,
                expected_digest=preview.content_digest,
            )
        except DeletedConversationArchiveError as exc:
            raise StateStoreError(
                "could not transfer conversation history to the deleted archive"
            ) from exc
        transaction_started = False
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            transaction_started = True
            current = _preview_conversation_deletion(
                self._select_conversation_messages_for_deletion(
                    ConversationDeletionScope.message(preview.history_ids)
                ),
                ConversationDeletionScope.message(preview.history_ids),
            )
            if (
                current.history_ids != preview.history_ids
                or current.content_digest != preview.content_digest
            ):
                self.connection.rollback()
                transaction_started = False
                raise StateStoreError(
                    "conversation deletion preview no longer matches accessible history"
                )
            try:
                _finalize_deleted_archive(
                    self._deleted_archive,
                    deletion_id=deletion_id,
                )
            except DeletedConversationArchiveError as exc:
                self.connection.rollback()
                transaction_started = False
                raise StateStoreError(
                    "could not transfer conversation history to the deleted archive"
                ) from exc
            tombstones: list[ConversationTombstone] = []
            for ordinal, message in enumerate(preview.messages):
                tombstone = _conversation_tombstone(
                    message,
                    deletion_id=deletion_id,
                    deleted_at=deleted_at,
                    scope_type=preview.scope.scope_type,
                    ordinal=ordinal,
                )
                self.connection.execute(
                    """
                    DELETE FROM conversation_history_fts
                    WHERE transport_session_id = ? AND message_id = ?
                    """,
                    (message.transport_session_id, message.message_id),
                )
                self.connection.execute(
                    """
                    DELETE FROM conversation_history
                    WHERE transport_session_id = ? AND message_id = ?
                    """,
                    (message.transport_session_id, message.message_id),
                )
                self.connection.execute(
                    """
                    DELETE FROM outbound_conversation_outbox
                    WHERE transport_session_id = ? AND message_id = ?
                    """,
                    (message.transport_session_id, message.message_id),
                )
                self.connection.execute(
                    """
                    INSERT INTO conversation_tombstones(
                        tombstone_id, deletion_id, history_id,
                        transport_session_id, message_id, working_session_id,
                        occurred_at, deleted_at, scope_type
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tombstone.tombstone_id,
                        tombstone.deletion_id,
                        tombstone.history_id,
                        tombstone.transport_session_id,
                        tombstone.message_id,
                        tombstone.working_session_id,
                        tombstone.occurred_at.isoformat(),
                        tombstone.deleted_at.isoformat(),
                        tombstone.scope_type,
                    ),
                )
                tombstones.append(tombstone)
            try:
                self.connection.commit()
            except sqlite3.Error as exc:
                self.connection.rollback()
                transaction_started = False
                raise StateStoreError(
                    "could not delete conversation history; could not determine "
                    "whether the deletion committed",
                    may_have_dispatched=True,
                ) from exc
            transaction_started = False
            return tuple(tombstones)
        except StateStoreError:
            if transaction_started:
                try:
                    self.connection.rollback()
                except sqlite3.Error:
                    pass
            _abort_deleted_archive(
                self._deleted_archive,
                deletion_id=deletion_id,
            )
            raise
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            _abort_deleted_archive(
                self._deleted_archive,
                deletion_id=deletion_id,
            )
            raise StateStoreError(
                "conversation deletion tombstone already exists"
            ) from exc
        except sqlite3.Error as exc:
            try:
                self.connection.rollback()
            except sqlite3.Error:
                pass
            _abort_deleted_archive(
                self._deleted_archive,
                deletion_id=deletion_id,
            )
            raise StateStoreError("could not delete conversation history") from exc

    delete_conversation_messages = delete_conversation_history

    @_locked_sqlite_state
    def list_conversation_tombstones(
        self, *, history_ids: tuple[str, ...] = ()
    ) -> tuple[ConversationTombstone, ...]:
        for history_id in history_ids:
            ConversationMessage.history_id_parts(history_id)
        try:
            clauses: list[str] = []
            values: list[object] = []
            if history_ids:
                clauses.append(
                    "history_id IN (" + ",".join("?" for _ in history_ids) + ")"
                )
                values.extend(history_ids)
            query = (
                "SELECT tombstone_id, deletion_id, history_id, "
                "transport_session_id, message_id, working_session_id, "
                "occurred_at, deleted_at, scope_type "
                "FROM conversation_tombstones"
            )
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY deleted_at, tombstone_id"
            rows = self.connection.execute(query, values).fetchall()
        except sqlite3.Error as exc:
            raise StateStoreError("could not list conversation tombstones") from exc
        return tuple(
            ConversationTombstone(
                tombstone_id=row["tombstone_id"],
                deletion_id=row["deletion_id"],
                history_id=row["history_id"],
                transport_session_id=row["transport_session_id"],
                message_id=row["message_id"],
                working_session_id=row["working_session_id"],
                occurred_at=datetime.fromisoformat(row["occurred_at"]),
                deleted_at=datetime.fromisoformat(row["deleted_at"]),
                scope_type=row["scope_type"],
            )
            for row in rows
        )

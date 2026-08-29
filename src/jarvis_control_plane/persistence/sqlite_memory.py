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


class _SQLiteMemoryMixin:
    @_locked_sqlite_state
    def list_memories(
        self, *, include_terminal: bool = True, limit: int = 50
    ) -> tuple[DurableMemory, ...]:
        _validate_memory_query(
            text=None,
            memory_ids=(),
            include_terminal=include_terminal,
            limit=limit,
        )
        try:
            rows = self.connection.execute(
                """
                SELECT memory_id, content, created_at, updated_at,
                       source_message_id, status, credential_like,
                       replaced_by_memory_id
                FROM durable_assistant_memory
                """
                + ("" if include_terminal else "WHERE status = 'active' ")
                + " ORDER BY created_at, memory_id LIMIT ?",
                (limit,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise StateStoreError("could not list durable assistant memory") from exc
        return tuple(_durable_memory_from_row(row) for row in rows)

    @_locked_sqlite_state
    def get_memory(self, memory_id: str) -> DurableMemory | None:
        if not isinstance(memory_id, str) or not memory_id:
            raise ValueError("memory_id must be a non-empty string")
        try:
            row = self.connection.execute(
                """
                SELECT memory_id, content, created_at, updated_at,
                       source_message_id, status, credential_like,
                       replaced_by_memory_id
                FROM durable_assistant_memory WHERE memory_id = ?
                """,
                (memory_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StateStoreError("could not inspect durable assistant memory") from exc
        return None if row is None else _durable_memory_from_row(row)

    @_locked_sqlite_state
    def search_memories(
        self,
        *,
        text: str | None = None,
        memory_ids: tuple[str, ...] = (),
        include_terminal: bool = True,
        limit: int = 50,
    ) -> tuple[DurableMemory, ...]:
        _validate_memory_query(
            text=text,
            memory_ids=memory_ids,
            include_terminal=include_terminal,
            limit=limit,
        )
        terms = _history_search_terms(text or "")
        if text is not None and not terms:
            return ()

        clauses: list[str] = []
        parameters: list[object] = []
        if not include_terminal:
            clauses.append("m.status = 'active'")
        if memory_ids:
            placeholders = ", ".join("?" for _ in memory_ids)
            clauses.append(f"m.memory_id IN ({placeholders})")
            parameters.extend(memory_ids)
        if text is not None:
            clauses.extend(
                (
                    "m.content IS NOT NULL",
                    (
                        "(m.credential_like = 1 OR EXISTS ("
                        "SELECT 1 FROM durable_assistant_memory_fts AS f "
                        "WHERE f.memory_id = m.memory_id AND f.content MATCH ?))"
                    ),
                )
            )
            parameters.append(_fts_history_query(terms))

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        try:
            cursor = self.connection.execute(
                f"""
                SELECT m.memory_id, m.content, m.created_at, m.updated_at,
                       m.source_message_id, m.status, m.credential_like,
                       m.replaced_by_memory_id
                FROM durable_assistant_memory AS m
                {where}
                ORDER BY m.created_at, m.memory_id
                """
                + (" LIMIT ?" if text is None else ""),
                (*parameters, limit) if text is None else parameters,
            )
        except sqlite3.Error as exc:
            raise StateStoreError("could not search durable assistant memory") from exc
        if text is None:
            return tuple(
                _durable_memory_from_row(row) for row in cursor.fetchmany(limit)
            )

        matches: list[DurableMemory] = []
        scanned_rows = 0
        while scanned_rows < _MAX_MEMORY_SEARCH_SCAN_ROWS:
            batch = cursor.fetchmany(
                min(
                    _MEMORY_SEARCH_BATCH_SIZE,
                    _MAX_MEMORY_SEARCH_SCAN_ROWS - scanned_rows,
                )
            )
            if not batch:
                return tuple(matches)
            scanned_rows += len(batch)
            for row in batch:
                memory = _durable_memory_from_row(row)
                if _matches_memory_terms(memory, terms):
                    matches.append(memory)
                    if len(matches) == limit:
                        return tuple(matches)

        if cursor.fetchone() is None:
            return tuple(matches)
        raise MemorySearchLimitExceeded(
            "durable-memory search exceeded its bounded scan limit",
            scanned_rows=scanned_rows,
        )

    @_locked_sqlite_state
    def select_memories_for_context(
        self, *, text: str, limit: int = 5
    ) -> MemorySelection:
        _validate_memory_query(
            text=text,
            memory_ids=(),
            include_terminal=False,
            limit=limit,
        )
        terms = _history_search_terms(text)
        if not terms:
            return MemorySelection(())
        try:
            rows = self.connection.execute(
                """
                SELECT m.memory_id, m.content, m.created_at, m.updated_at,
                       m.source_message_id, m.status, m.credential_like,
                       m.replaced_by_memory_id
                FROM durable_assistant_memory AS m
                JOIN durable_assistant_memory_fts AS f
                  ON f.memory_id = m.memory_id
                WHERE m.status = 'active' AND m.credential_like = 0
                  AND f.content MATCH ?
                ORDER BY m.created_at, m.memory_id
                LIMIT ?
                """,
                (_fts_history_query(terms), _MAX_MEMORY_RESULTS),
            ).fetchall()
        except sqlite3.Error as exc:
            raise StateStoreError(
                "could not select durable assistant memory for context"
            ) from exc
        return MemorySelection(
            _filter_memories(
                tuple(_durable_memory_from_row(row) for row in rows),
                text=text,
                include_terminal=False,
                limit=limit,
            )
        )

    @_locked_sqlite_state
    def create_memory(self, memory: DurableMemory) -> None:
        if not isinstance(memory, DurableMemory):
            raise TypeError("memory must be a DurableMemory")
        if not memory.is_active:
            raise ValueError("only active memories may be created")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self._insert_memory(memory)
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            raise StateStoreError("memory identifier already exists") from exc
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise StateStoreError("could not create durable assistant memory") from exc

    save_memory = create_memory

    @_locked_sqlite_state
    def replace_memory(
        self,
        memory_id: str,
        replacement: DurableMemory,
        *,
        expected_revision: str | None = None,
    ) -> DurableMemory:
        if not isinstance(replacement, DurableMemory):
            raise TypeError("replacement must be a DurableMemory")
        if not replacement.is_active:
            raise ValueError("replacement must be an active memory")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            current = self.get_memory(memory_id)
            if current is None:
                raise StateStoreError("memory identifier does not exist")
            if not current.is_active:
                raise StateStoreError("only active memories may be replaced")
            if (
                expected_revision is not None
                and current.revision_digest != expected_revision
            ):
                raise StateStoreError("memory changed after its exact preview")
            if replacement.memory_id == memory_id:
                raise StateStoreError(
                    "replacement must receive a new memory identifier"
                )
            if self.get_memory(replacement.memory_id) is not None:
                raise StateStoreError("replacement memory identifier already exists")
            retired = replace(
                current,
                content=None,
                updated_at=replacement.updated_at,
                status=MemoryLifecycle.REPLACED,
                replaced_by_memory_id=replacement.memory_id,
            )
            self.connection.execute(
                """
                UPDATE durable_assistant_memory
                SET content = NULL, updated_at = ?, status = ?,
                    replaced_by_memory_id = ?
                WHERE memory_id = ?
                """,
                (
                    retired.updated_at.isoformat(),
                    retired.status.value,
                    retired.replaced_by_memory_id,
                    memory_id,
                ),
            )
            self.connection.execute(
                "DELETE FROM durable_assistant_memory_fts WHERE memory_id = ?",
                (memory_id,),
            )
            self._insert_memory(replacement)
            self.connection.commit()
            return replacement
        except StateStoreError:
            self.connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            raise StateStoreError(
                "replacement memory identifier already exists"
            ) from exc
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise StateStoreError("could not replace durable assistant memory") from exc

    @_locked_sqlite_state
    def forget_memory(
        self,
        memory_id: str,
        *,
        expected_revision: str | None = None,
        updated_at: datetime | None = None,
    ) -> DurableMemory:
        at = ensure_utc(updated_at or datetime.now(UTC))
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            current = self.get_memory(memory_id)
            if current is None:
                raise StateStoreError("memory identifier does not exist")
            if not current.is_active:
                raise StateStoreError("only active memories may be forgotten")
            if (
                expected_revision is not None
                and current.revision_digest != expected_revision
            ):
                raise StateStoreError("memory changed after its exact preview")
            forgotten = replace(
                current,
                content=None,
                updated_at=at,
                status=MemoryLifecycle.FORGOTTEN,
            )
            self.connection.execute(
                """
                UPDATE durable_assistant_memory
                SET content = NULL, updated_at = ?, status = ?
                WHERE memory_id = ?
                """,
                (forgotten.updated_at.isoformat(), forgotten.status.value, memory_id),
            )
            self.connection.execute(
                "DELETE FROM durable_assistant_memory_fts WHERE memory_id = ?",
                (memory_id,),
            )
            self.connection.commit()
            return forgotten
        except StateStoreError:
            self.connection.rollback()
            raise
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise StateStoreError("could not forget durable assistant memory") from exc

    def _insert_memory(self, memory: DurableMemory) -> None:
        self.connection.execute(
            """
            INSERT INTO durable_assistant_memory(
                memory_id, content, created_at, updated_at, source_message_id,
                status, credential_like, replaced_by_memory_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory.memory_id,
                memory.content,
                memory.created_at.isoformat(),
                memory.updated_at.isoformat(),
                memory.source_message_id,
                memory.status.value,
                int(memory.credential_like),
                memory.replaced_by_memory_id,
            ),
        )
        if memory.is_active and not memory.credential_like:
            self.connection.execute(
                """
                INSERT INTO durable_assistant_memory_fts(memory_id, content)
                VALUES (?, ?)
                """,
                (memory.memory_id, memory.content),
            )

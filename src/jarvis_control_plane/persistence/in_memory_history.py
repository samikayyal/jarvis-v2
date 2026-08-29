# ruff: noqa: F401, I001, RUF100 -- mixin globals preserve compatibility seams.
"""In-memory durable state adapter."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, datetime

from ..conversation_archive import InMemoryDeletedConversationArchive
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
    StateStoreError,
)
from .state_support import (
    _abort_deleted_archive,
    _conversation_tombstone,
    _export_conversation_messages,
    _filter_conversation_messages,
    _filter_memories,
    _finalize_deleted_archive,
    _locked_durable_state,
    _preview_conversation_deletion,
    _select_history_for_context,
    _select_memories_for_context,
    _stage_deleted_archive,
)


class _InMemoryHistoryMixin:
    def list_conversation_messages(self) -> tuple[ConversationMessage, ...]:
        with self._lock:
            return tuple(
                sorted(
                    self.conversation_messages.values(),
                    key=lambda message: (
                        message.occurred_at,
                        message.transport_session_id,
                        message.message_id,
                    ),
                )
            )

    def append_conversation_message(self, message: ConversationMessage) -> None:
        with self._lock:
            if self.fail_conversation:
                raise StateStoreError("controlled conversation write failure")
            key = (message.transport_session_id, message.message_id)
            if key in self.conversation_messages:
                raise StateStoreError("conversation message identifier already exists")
            self.conversation_messages[key] = message

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
        with self._lock:
            if self.fail_conversation:
                raise StateStoreError("controlled conversation write failure")
            key = (message.transport_session_id, message.message_id)
            if (
                key in self.conversation_messages
                or key in self.outbound_outbox
                or key in self.outbound_attempts
            ):
                raise StateStoreError("conversation message identifier already exists")
            self.outbound_outbox[key] = message
            self.outbound_attempts[key] = OutboundAttemptRecord(
                transport_session_id=message.transport_session_id,
                message_id=message.message_id,
                request_id=message.request_id,
                status=OutboundAttemptStatus.UNATTEMPTED,
                reserved_at=message.occurred_at,
                message=message,
            )

    def mark_outbound_conversation_attempted(
        self,
        *,
        transport_session_id: str,
        message_id: str,
        attempted_at: datetime,
    ) -> None:
        with self._lock:
            key = (transport_session_id, message_id)
            record = self.outbound_attempts.get(key)
            if record is None or record.status is not OutboundAttemptStatus.UNATTEMPTED:
                raise StateStoreError("outbound attempt is not known-unattempted")
            self.outbound_attempts[key] = replace(
                record,
                status=OutboundAttemptStatus.ATTEMPTED,
                attempted_at=ensure_utc(attempted_at),
            )

    def terminalize_outbound_conversation_attempt(
        self,
        *,
        transport_session_id: str,
        message_id: str,
        status: OutboundAttemptStatus | str,
        terminal_at: datetime | None = None,
        outbound_id: str | None = None,
    ) -> None:
        status = OutboundAttemptStatus(status)
        if status in {
            OutboundAttemptStatus.UNATTEMPTED,
            OutboundAttemptStatus.ATTEMPTED,
        }:
            raise StateStoreError("outbound terminal status is required")
        if terminal_at is None:
            raise StateStoreError("outbound terminal timestamp is required")
        terminal_at = ensure_utc(terminal_at)
        with self._lock:
            if self.fail_conversation:
                raise StateStoreError("controlled conversation write failure")
            key = (transport_session_id, message_id)
            record = self.outbound_attempts.get(key)
            if record is None:
                raise StateStoreError("outbound attempt does not exist")
            if not is_outbound_terminal_transition_allowed(record.status, status):
                raise StateStoreError("outbound terminal transition is not allowed")
            if record.status not in {
                OutboundAttemptStatus.UNATTEMPTED,
                OutboundAttemptStatus.ATTEMPTED,
            }:
                if record.status is status and (
                    outbound_id is None or outbound_id == record.outbound_id
                ):
                    return
                raise StateStoreError("outbound attempt is already terminal")
            message = self.outbound_outbox.get(key)
            if message is None:
                raise StateStoreError(
                    "reserved outbound conversation message does not exist"
                )
            if status is OutboundAttemptStatus.CONFIRMED:
                if record.status is not OutboundAttemptStatus.ATTEMPTED:
                    raise StateStoreError("outbound message was not durably attempted")
                if key in self.conversation_messages:
                    raise StateStoreError(
                        "conversation message identifier already exists"
                    )
                self.conversation_messages[key] = message
            del self.outbound_outbox[key]
            self.outbound_attempts[key] = replace(
                record,
                status=status,
                message=None,
                outbound_id=outbound_id,
                terminal_at=terminal_at,
            )

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
            with self._lock:
                record = self.outbound_attempts.get((transport_session_id, message_id))
            if record is None:
                raise StateStoreError("outbound attempt does not exist")
            terminal_at = record.attempted_at or record.reserved_at
        self.terminalize_outbound_conversation_attempt(
            transport_session_id=transport_session_id,
            message_id=message_id,
            status=OutboundAttemptStatus.CONFIRMED,
            terminal_at=terminal_at,
            outbound_id=outbound_id,
        )

    def list_outbound_conversation_attempts(
        self,
    ) -> tuple[OutboundAttemptRecord, ...]:
        with self._lock:
            return tuple(
                sorted(
                    self.outbound_attempts.values(),
                    key=lambda record: (
                        record.reserved_at,
                        record.transport_session_id,
                        record.message_id,
                    ),
                )
            )

    def list_outbound_conversation_attempt_recovery(
        self,
    ) -> tuple[OutboundAttemptRecoveryProjection, ...]:
        with self._lock:
            keys = set(self.outbound_attempts) | set(self.outbound_outbox)
            projections: list[OutboundAttemptRecoveryProjection] = []
            for transport_session_id, message_id in sorted(keys):
                record = self.outbound_attempts.get((transport_session_id, message_id))
                message = self.outbound_outbox.get((transport_session_id, message_id))
                projections.append(
                    OutboundAttemptRecoveryProjection(
                        transport_session_id=transport_session_id,
                        message_id=message_id,
                        attempt_present=record is not None,
                        outbox_present=message is not None,
                        attempt_request_id=record.request_id if record else None,
                        outbox_request_id=message.request_id if message else None,
                        status=record.status.value if record else None,
                        reserved_at=(
                            record.reserved_at.isoformat() if record else None
                        ),
                        attempted_at=(
                            record.attempted_at.isoformat()
                            if record and record.attempted_at is not None
                            else None
                        ),
                        terminal_at=(
                            record.terminal_at.isoformat()
                            if record and record.terminal_at is not None
                            else None
                        ),
                        outbound_id=record.outbound_id if record else None,
                    )
                )
            return tuple(projections)

    def reconcile_outbound_conversation_attempts(
        self, *, interrupted_at: datetime
    ) -> tuple[OutboundAttemptRecord, ...]:
        interrupted_at = ensure_utc(interrupted_at)
        with self._lock:
            reconciled: list[OutboundAttemptRecord] = []
            keys = set(self.outbound_attempts) | set(self.outbound_outbox)
            for key in sorted(keys):
                record = self.outbound_attempts.get(key)
                if record is None:
                    self.outbound_outbox.pop(key, None)
                    continue
                if record.status not in {
                    OutboundAttemptStatus.UNATTEMPTED,
                    OutboundAttemptStatus.ATTEMPTED,
                }:
                    self.outbound_outbox.pop(key, None)
                    continue
                status = (
                    OutboundAttemptStatus.NOT_STARTED
                    if record.status is OutboundAttemptStatus.UNATTEMPTED
                    else OutboundAttemptStatus.UNKNOWN
                )
                updated = replace(
                    record,
                    status=status,
                    message=None,
                    terminal_at=interrupted_at,
                )
                self.outbound_attempts[key] = updated
                self.outbound_outbox.pop(key, None)
                reconciled.append(updated)
            return tuple(reconciled)

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
        return _filter_conversation_messages(
            self.list_conversation_messages(),
            text=text,
            working_session_id=working_session_id,
            request_id=request_id,
            direction=direction,
            history_ids=history_ids,
            limit=limit,
        )

    def export_conversation_messages(self, **query: object) -> str:
        return _export_conversation_messages(
            self.search_conversation_messages(**query)  # type: ignore[arg-type]
        )

    def select_history_for_context(
        self,
        *,
        text: str,
        excluding_working_session_id: str,
        limit: int = 5,
    ) -> HistorySelection:
        return _select_history_for_context(
            self.list_conversation_messages(),
            text=text,
            excluding_working_session_id=excluding_working_session_id,
            limit=limit,
        )

    def preview_conversation_deletion(
        self, scope: ConversationDeletionScope
    ) -> ConversationDeletionPreview:
        with self._lock:
            return _preview_conversation_deletion(
                self.conversation_messages.values(), scope
            )

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
        with self._lock:
            if self.fail_conversation:
                raise StateStoreError("controlled conversation deletion failure")
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
        with self._lock:
            try:
                current = _preview_conversation_deletion(
                    self.conversation_messages.values(),
                    ConversationDeletionScope.message(preview.history_ids),
                )
                if (
                    current.history_ids != preview.history_ids
                    or current.content_digest != preview.content_digest
                ):
                    raise StateStoreError(
                        "conversation deletion preview no longer matches accessible history"
                    )
                try:
                    _finalize_deleted_archive(
                        self._deleted_archive,
                        deletion_id=deletion_id,
                    )
                except DeletedConversationArchiveError as exc:
                    raise StateStoreError(
                        "could not transfer conversation history to the deleted archive"
                    ) from exc
                archived: list[tuple[tuple[str, str], ConversationMessage]] = []
                tombstones: list[ConversationTombstone] = []
                for message in preview.messages:
                    key = (message.transport_session_id, message.message_id)
                    if key not in self.conversation_messages:
                        raise StateStoreError(
                            "conversation deletion record is no longer accessible"
                        )
                    archived.append((key, message))
                    tombstones.append(
                        _conversation_tombstone(
                            message,
                            deletion_id=deletion_id,
                            deleted_at=deleted_at,
                            scope_type=preview.scope.scope_type,
                            ordinal=len(tombstones),
                        )
                    )
                for key, message in archived:
                    self.conversation_messages.pop(key, None)
                    self.outbound_outbox.pop(key, None)
                for tombstone in tombstones:
                    self._conversation_tombstones[tombstone.history_id] = tombstone
                return tuple(tombstones)
            except StateStoreError:
                _abort_deleted_archive(
                    self._deleted_archive,
                    deletion_id=deletion_id,
                )
                raise

    delete_conversation_messages = delete_conversation_history

    def list_conversation_tombstones(
        self, *, history_ids: tuple[str, ...] = ()
    ) -> tuple[ConversationTombstone, ...]:
        with self._lock:
            selected = set(history_ids)
            for history_id in history_ids:
                ConversationMessage.history_id_parts(history_id)
            return tuple(
                sorted(
                    (
                        tombstone
                        for tombstone in self._conversation_tombstones.values()
                        if not selected or tombstone.history_id in selected
                    ),
                    key=lambda tombstone: (
                        tombstone.deleted_at,
                        tombstone.tombstone_id,
                    ),
                )
            )

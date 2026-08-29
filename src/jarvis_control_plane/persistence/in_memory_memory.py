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


class _InMemoryMemoryMixin:
    def list_memories(
        self, *, include_terminal: bool = True, limit: int = 50
    ) -> tuple[DurableMemory, ...]:
        with self._lock:
            return _filter_memories(
                tuple(
                    sorted(
                        self.memories.values(),
                        key=lambda memory: (
                            memory.created_at,
                            memory.memory_id,
                        ),
                    )
                ),
                include_terminal=include_terminal,
                limit=limit,
            )

    def get_memory(self, memory_id: str) -> DurableMemory | None:
        if not isinstance(memory_id, str) or not memory_id:
            raise ValueError("memory_id must be a non-empty string")
        with self._lock:
            return self.memories.get(memory_id)

    def search_memories(
        self,
        *,
        text: str | None = None,
        memory_ids: tuple[str, ...] = (),
        include_terminal: bool = True,
        limit: int = 50,
    ) -> tuple[DurableMemory, ...]:
        with self._lock:
            return _filter_memories(
                tuple(
                    sorted(
                        self.memories.values(),
                        key=lambda memory: (
                            memory.created_at,
                            memory.memory_id,
                        ),
                    )
                ),
                text=text,
                memory_ids=memory_ids,
                include_terminal=include_terminal,
                limit=limit,
            )

    def select_memories_for_context(
        self, *, text: str, limit: int = 5
    ) -> MemorySelection:
        with self._lock:
            return _select_memories_for_context(
                tuple(
                    sorted(
                        self.memories.values(),
                        key=lambda memory: (
                            memory.created_at,
                            memory.memory_id,
                        ),
                    )
                ),
                text=text,
                limit=limit,
            )

    def create_memory(self, memory: DurableMemory) -> None:
        if not isinstance(memory, DurableMemory):
            raise TypeError("memory must be a DurableMemory")
        if not memory.is_active:
            raise ValueError("only active memories may be created")
        with self._lock:
            if self.fail_memory:
                raise StateStoreError("controlled durable-memory write failure")
            if memory.memory_id in self.memories:
                raise StateStoreError("memory identifier already exists")
            self.memories[memory.memory_id] = memory

    save_memory = create_memory

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
        with self._lock:
            if self.fail_memory:
                raise StateStoreError("controlled durable-memory write failure")
            current = self.memories.get(memory_id)
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
            if replacement.memory_id in self.memories:
                raise StateStoreError("replacement memory identifier already exists")
            self.memories[memory_id] = replace(
                current,
                content=None,
                updated_at=replacement.updated_at,
                status=MemoryLifecycle.REPLACED,
                replaced_by_memory_id=replacement.memory_id,
            )
            self.memories[replacement.memory_id] = replacement
            return replacement

    def forget_memory(
        self,
        memory_id: str,
        *,
        expected_revision: str | None = None,
        updated_at: datetime | None = None,
    ) -> DurableMemory:
        with self._lock:
            if self.fail_memory:
                raise StateStoreError("controlled durable-memory write failure")
            current = self.memories.get(memory_id)
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
                updated_at=ensure_utc(updated_at or datetime.now(UTC)),
                status=MemoryLifecycle.FORGOTTEN,
            )
            self.memories[memory_id] = forgotten
            return forgotten

    @_locked_durable_state
    def has_ingress_claim(self, *, session_id: str, message_id: str) -> bool:
        with self._lock:
            if self.fail_claim:
                raise StateStoreError("controlled ingress claim failure")
            return (session_id, message_id) in self.claims

    @_locked_durable_state
    def release_ingress_claim(self, *, session_id: str, message_id: str) -> bool:
        with self._lock:
            key = (session_id, message_id)
            released = self.claims.pop(key, None) is not None
            self.conversation_messages.pop(key, None)
            return released

    @_locked_durable_state
    def save_request(self, request: RequestState) -> None:
        with self._lock:
            if self.fail_save:
                raise StateStoreError("controlled request save failure")
            if request.request_id in self.requests:
                raise StateStoreError("request identifier already exists")
            self.requests[request.request_id] = request

    @_locked_durable_state
    def update_request(self, request: RequestState) -> None:
        with self._lock:
            if self.fail_update:
                raise StateStoreError("controlled request update failure")
            if request.request_id not in self.requests:
                raise StateStoreError("request identifier does not exist")
            self.requests[request.request_id] = request

    @_locked_durable_state
    def delete_request(self, request_id: str) -> bool:
        with self._lock:
            return self.requests.pop(request_id, None) is not None

    @_locked_durable_state
    def get_request(self, request_id: str) -> RequestState | None:
        with self._lock:
            return self.requests.get(request_id)

    @_locked_durable_state
    def list_requests(self) -> tuple[RequestState, ...]:
        with self._lock:
            return tuple(self.requests.values())

    @_locked_durable_state
    def list_ingress_claims(self) -> tuple[IngressClaim, ...]:
        with self._lock:
            return tuple(self.claims.values())

    @_locked_durable_state
    def load_knowledge_vault_synchronized_at(self) -> datetime | None:
        with self._lock:
            return self._knowledge_vault_synchronized_at

    @_locked_durable_state
    def save_knowledge_vault_synchronized_at(self, synchronized_at: datetime) -> None:
        with self._lock:
            self._knowledge_vault_synchronized_at = ensure_utc(synchronized_at)

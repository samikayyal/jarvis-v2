# ruff: noqa: F401, I001, RUF100 -- facade globals preserve compatibility seams.
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

from .in_memory_history import _InMemoryHistoryMixin
from .in_memory_ingress import _InMemoryIngressMixin
from .in_memory_memory import _InMemoryMemoryMixin


class InMemoryDurableStateStore(
    _InMemoryIngressMixin, _InMemoryHistoryMixin, _InMemoryMemoryMixin
):
    """A failure-controllable state port for narrow unit tests."""

    def __init__(
        self,
        *,
        deleted_archive: DeletedConversationArchiveWriter | None = None,
    ) -> None:
        self.claims: dict[tuple[str, str], IngressClaim] = {}
        self.conversation_messages: dict[tuple[str, str], ConversationMessage] = {}
        self.outbound_outbox: dict[tuple[str, str], ConversationMessage] = {}
        self.outbound_attempts: dict[tuple[str, str], OutboundAttemptRecord] = {}
        self._deleted_archive = deleted_archive or InMemoryDeletedConversationArchive()
        self._conversation_tombstones: dict[str, ConversationTombstone] = {}
        self.memories: dict[str, DurableMemory] = {}
        self.requests: dict[str, RequestState] = {}
        self._knowledge_vault_synchronized_at: datetime | None = None
        self._recovery_degraded_marker: RecoveryDegradedMarker | None = None
        self.fail_claim = False
        self.fail_conversation = False
        self.fail_memory = False
        self.fail_save = False
        self.fail_update = False
        self._lock = threading.RLock()

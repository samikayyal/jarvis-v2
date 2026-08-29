# ruff: noqa: F401, I001, RUF100 -- compatibility exports and mirrors are intentional.
"""Compatibility surface for the controlled local adapters.

The concrete implementations live in cohesive persistence modules.  This
module intentionally re-exports their established names so the public
jarvis_control_plane.adapters path remains one stable seam.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys as _sys
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType as _ModuleType
from typing import Any, ClassVar

from ..conversation_archive import InMemoryDeletedConversationArchive
from ..models import (
    AuditEvidence,
    AuditFilter,
    ConversationDeletionPreview,
    ConversationDeletionScope,
    ConversationMessage,
    ConversationTombstone,
    DurableMemory,
    FrozenActionProposal,
    HistorySelection,
    IngressAdmissionResult,
    IngressClaim,
    MemoryLifecycle,
    MemorySelection,
    OrchestrationRequest,
    OrchestrationResult,
    OutboundAttemptRecord,
    OutboundAttemptRecoveryProjection,
    OutboundAttemptStatus,
    OutboundDelivery,
    OutboundReply,
    RecoveryDegradedMarker,
    RequestState,
    _conversation_message_digest,
    ensure_utc,
    is_outbound_terminal_transition_allowed,
)
from ..ports import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ActionDispatcherError,
    AuditBoundary,
    AuditWriteError,
    Clock,
    DeletedConversationArchiveError,
    DeletedConversationArchiveWriter,
    IdGenerator,
    MemorySearchLimitExceeded,
    OrchestrationAdapterError,
    OutboundConnectorError,
    StateStoreError,
)
from ..sessions import ModelAvailability
from ..worker_gateway import WorkerExecutionResult
from . import audit_boundaries as _audit_boundaries
from . import audit_read as _audit_read
from . import audit_write as _audit_write
from . import controlled_adapters as _controlled_adapters
from . import in_memory_history as _in_memory_history
from . import in_memory_ingress as _in_memory_ingress
from . import in_memory_memory as _in_memory_memory
from . import in_memory_state as _in_memory_state
from . import sqlite_dispatch as _sqlite_dispatch
from . import sqlite_history as _sqlite_history
from . import sqlite_indexes as _sqlite_indexes
from . import sqlite_memory as _sqlite_memory
from . import sqlite_outbound as _sqlite_outbound
from . import sqlite_outbound_recovery as _sqlite_outbound_recovery
from . import sqlite_recovery as _sqlite_recovery
from . import sqlite_state as _sqlite_state
from . import state_support as _state_support
from .adapter_support import (
    DeterministicIdGenerator,
    FixedClock,
    FixedModelAvailabilityProvider,
    SQLITE_OUTBOUND_ATTEMPT_MIGRATION_VERSION,
    SystemClock,
    UuidIdGenerator,
    _SQLITE_OUTBOUND_ATTEMPT_MIGRATION_NAME,
    _SQLITE_OUTBOUND_ATTEMPT_TABLE_SQL,
    migrate_sqlite_outbound_conversation_attempts,
)
from .audit_boundaries import (
    InMemoryAuditBoundary,
    SQLiteAuditBoundary,
    _ReadOnlyAuditRecords,
    _export_audit_json,
    _resolve_audit_filter,
)
from .controlled_adapters import (
    ControlledActionDispatcher,
    ControlledOrchestrationAdapter,
    ControlledOutboundConnector,
    _ControlledActionDispatch,
    replace_request,
)
from .in_memory_state import InMemoryDurableStateStore
from .sqlite_state import SQLiteDurableStateStore
from .state_support import (
    _DELETION_SELECTOR_BATCH_SIZE,
    _HISTORY_SEARCH_STOPWORDS,
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
    _locked_durable_state,
    _locked_sqlite_state,
    _matches_history_terms,
    _matches_memory_terms,
    _outbound_attempt_from_row,
    _preview_conversation_deletion,
    _recovered_terminal_attempt_record,
    _request_from_row,
    _request_values,
    _select_history_for_context,
    _select_memories_for_context,
    _stage_deleted_archive,
    _validate_history_query,
    _validate_memory_query,
)

__all__ = [
    "ControlledActionDispatcher",
    "ControlledOrchestrationAdapter",
    "ControlledOutboundConnector",
    "DeterministicIdGenerator",
    "FixedClock",
    "FixedModelAvailabilityProvider",
    "InMemoryAuditBoundary",
    "InMemoryDurableStateStore",
    "SQLiteAuditBoundary",
    "SQLiteDurableStateStore",
    "SystemClock",
    "UuidIdGenerator",
    "migrate_sqlite_outbound_conversation_attempts",
    "replace_request",
]


class _AdapterCompatibilityModule(_ModuleType):
    """Keep old module-level monkeypatch seams wired to moved implementations."""

    _mirror_targets: dict[str, tuple[_ModuleType, ...]]

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        for target in self.__dict__.get("_mirror_targets", {}).get(name, ()):
            setattr(target, name, value)


_mirror_targets: dict[str, list[_ModuleType]] = {}


def _mirror(target: _ModuleType, *names: str) -> None:
    for name in names:
        _mirror_targets.setdefault(name, []).append(target)


_state_names = (
    "_locked_durable_state",
    "_locked_sqlite_state",
    "_request_values",
    "_DELETION_SELECTOR_BATCH_SIZE",
    "_HISTORY_SEARCH_STOPWORDS",
    "_MAX_HISTORY_RESULTS",
    "_MAX_MEMORY_RESULTS",
    "_MAX_MEMORY_SEARCH_SCAN_ROWS",
    "_MEMORY_SEARCH_BATCH_SIZE",
    "_abort_deleted_archive",
    "_conversation_deletion_query",
    "_conversation_message_from_row",
    "_conversation_tombstone",
    "_durable_memory_from_row",
    "_export_conversation_messages",
    "_filter_conversation_messages",
    "_filter_memories",
    "_finalize_deleted_archive",
    "_fts_history_query",
    "_history_search_terms",
    "_matches_history_terms",
    "_matches_memory_terms",
    "_outbound_attempt_from_row",
    "_preview_conversation_deletion",
    "_recovered_terminal_attempt_record",
    "_request_from_row",
    "_select_history_for_context",
    "_select_memories_for_context",
    "_stage_deleted_archive",
    "_validate_history_query",
    "_validate_memory_query",
    "_conversation_message_digest",
    "ConversationDeletionPreview",
    "ConversationDeletionScope",
    "ConversationMessage",
    "ConversationTombstone",
    "DurableMemory",
    "HistorySelection",
    "MemoryLifecycle",
    "MemorySelection",
    "OutboundAttemptRecord",
    "OutboundAttemptRecoveryProjection",
    "OutboundAttemptStatus",
    "RequestState",
    "ensure_utc",
    "is_outbound_terminal_transition_allowed",
    "AuditEvidence",
    "AuditWriteError",
    "DeletedConversationArchiveError",
    "DeletedConversationArchiveWriter",
    "StateStoreError",
    "datetime",
    "UTC",
    "replace",
    "sqlite3",
    "threading",
)
_mirror(_state_support, *_state_names)

for _target in (
    _in_memory_state,
    _in_memory_ingress,
    _in_memory_history,
    _in_memory_memory,
):
    _mirror(
        _target,
        "_locked_durable_state",
        "_abort_deleted_archive",
        "_conversation_tombstone",
        "_export_conversation_messages",
        "_filter_conversation_messages",
        "_filter_memories",
        "_finalize_deleted_archive",
        "_preview_conversation_deletion",
        "_select_history_for_context",
        "_select_memories_for_context",
        "_stage_deleted_archive",
        "InMemoryDeletedConversationArchive",
        "AuditEvidence",
        "AuditWriteError",
        "DeletedConversationArchiveError",
        "DeletedConversationArchiveWriter",
        "StateStoreError",
        "ConversationDeletionPreview",
        "ConversationDeletionScope",
        "ConversationMessage",
        "ConversationTombstone",
        "DurableMemory",
        "HistorySelection",
        "IngressAdmissionResult",
        "IngressClaim",
        "MemoryLifecycle",
        "MemorySelection",
        "OutboundAttemptRecord",
        "OutboundAttemptRecoveryProjection",
        "OutboundAttemptStatus",
        "RecoveryDegradedMarker",
        "RequestState",
        "ensure_utc",
        "is_outbound_terminal_transition_allowed",
        "datetime",
        "UTC",
        "replace",
        "threading",
    )

for _target in (
    _sqlite_state,
    _sqlite_recovery,
    _sqlite_dispatch,
    _sqlite_outbound,
    _sqlite_outbound_recovery,
    _sqlite_history,
    _sqlite_memory,
    _sqlite_indexes,
):
    _mirror(
        _target,
        "_locked_sqlite_state",
        "_DELETION_SELECTOR_BATCH_SIZE",
        "_MAX_HISTORY_RESULTS",
        "_MAX_MEMORY_RESULTS",
        "_MAX_MEMORY_SEARCH_SCAN_ROWS",
        "_MEMORY_SEARCH_BATCH_SIZE",
        "_abort_deleted_archive",
        "_conversation_deletion_query",
        "_conversation_message_from_row",
        "_conversation_tombstone",
        "_durable_memory_from_row",
        "_export_conversation_messages",
        "_filter_conversation_messages",
        "_filter_memories",
        "_finalize_deleted_archive",
        "_fts_history_query",
        "_history_search_terms",
        "_matches_memory_terms",
        "_outbound_attempt_from_row",
        "_preview_conversation_deletion",
        "_recovered_terminal_attempt_record",
        "_request_from_row",
        "_request_values",
        "_stage_deleted_archive",
        "_validate_history_query",
        "_validate_memory_query",
        "SQLiteAuditBoundary",
        "AuditEvidence",
        "AuditWriteError",
        "DeletedConversationArchiveError",
        "DeletedConversationArchiveWriter",
        "MemorySearchLimitExceeded",
        "StateStoreError",
        "ConversationDeletionPreview",
        "ConversationDeletionScope",
        "ConversationMessage",
        "ConversationTombstone",
        "DurableMemory",
        "HistorySelection",
        "IngressAdmissionResult",
        "IngressClaim",
        "MemoryLifecycle",
        "MemorySelection",
        "OutboundAttemptRecord",
        "OutboundAttemptRecoveryProjection",
        "OutboundAttemptStatus",
        "RecoveryDegradedMarker",
        "RequestState",
        "ensure_utc",
        "is_outbound_terminal_transition_allowed",
        "datetime",
        "UTC",
        "replace",
        "sqlite3",
        "threading",
        "Path",
    )

for _target in (_audit_boundaries, _audit_read, _audit_write):
    _mirror(
        _target,
        "_ReadOnlyAuditRecords",
        "_export_audit_json",
        "_resolve_audit_filter",
        "AuditEvidence",
        "AuditFilter",
        "AuditWriteError",
        "ensure_utc",
        "json",
        "sqlite3",
        "threading",
        "datetime",
        "Path",
    )

_mirror(
    _controlled_adapters,
    "_ControlledActionDispatch",
    "replace_request",
    "FrozenActionProposal",
    "OrchestrationRequest",
    "OrchestrationResult",
    "OutboundDelivery",
    "OutboundReply",
    "RequestState",
    "ActionCancellationResult",
    "ActionCancellationStatus",
    "ActionDispatcherError",
    "AuditBoundary",
    "Clock",
    "IdGenerator",
    "OrchestrationAdapterError",
    "OutboundConnectorError",
    "WorkerExecutionResult",
    "threading",
    "replace",
)

_mirror_targets = {name: tuple(targets) for name, targets in _mirror_targets.items()}
_module = _sys.modules[__name__]
_module.__class__ = _AdapterCompatibilityModule
_module._mirror_targets = _mirror_targets

# ruff: noqa: F401, I001, RUF100 -- extracted helpers retain a stable context.
"""Shared durable-state, conversation, memory, and audit helpers.

No class in this module opens a network connection.  SQLite is used for the
durable local state/audit test boundary; the orchestration and outbound
implementations are deterministic controlled fakes with the same typed ports
that production adapters will later implement.
"""

from __future__ import annotations

# ruff: noqa: F401

import json
import re
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any, ClassVar

from ..conversation_archive import (
    InMemoryDeletedConversationArchive,
)
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
from .adapter_support import (
    DeterministicIdGenerator,  # noqa: F401
    FixedClock,  # noqa: F401
    FixedModelAvailabilityProvider,  # noqa: F401
    SQLITE_OUTBOUND_ATTEMPT_MIGRATION_VERSION,  # noqa: F401
    SystemClock,  # noqa: F401
    UuidIdGenerator,  # noqa: F401
    _SQLITE_OUTBOUND_ATTEMPT_MIGRATION_NAME,  # noqa: F401
    _SQLITE_OUTBOUND_ATTEMPT_TABLE_SQL,  # noqa: F401
    migrate_sqlite_outbound_conversation_attempts,  # noqa: F401
)
from ..sessions import ModelAvailability
from ..worker_gateway import WorkerExecutionResult


def _conversation_message_from_row(row: sqlite3.Row) -> ConversationMessage:
    return ConversationMessage(
        working_session_id=row["working_session_id"],
        transport_session_id=row["transport_session_id"],
        message_id=row["message_id"],
        event_id=row["event_id"],
        chat_id=row["chat_id"],
        sender_id=row["sender_id"],
        text=row["text"],
        occurred_at=datetime.fromisoformat(row["occurred_at"]),
        direction=row["direction"],
        request_id=row["request_id"],
        credential_like=bool(row["credential_like"]),
    )


def _recovered_terminal_attempt_record(
    projection: OutboundAttemptRecoveryProjection,
    *,
    status: OutboundAttemptStatus,
    terminal_at: datetime,
) -> OutboundAttemptRecord | None:
    """Materialize only after recovery has removed the invalid open edge."""

    if (
        not isinstance(projection.attempt_request_id, str)
        or not projection.attempt_request_id.strip()
        or not isinstance(projection.reserved_at, str)
    ):
        return None
    try:
        attempted_at = (
            datetime.fromisoformat(projection.attempted_at)
            if projection.attempted_at is not None
            else None
        )
        return OutboundAttemptRecord(
            transport_session_id=projection.transport_session_id,
            message_id=projection.message_id,
            request_id=projection.attempt_request_id,
            status=status,
            reserved_at=datetime.fromisoformat(projection.reserved_at),
            message=None,
            attempted_at=attempted_at,
            terminal_at=terminal_at,
            outbound_id=None,
        )
    except (TypeError, ValueError):
        return None


def _outbound_attempt_from_row(row: sqlite3.Row) -> OutboundAttemptRecord:
    message = None
    if row["text"] is not None:
        message = ConversationMessage(
            working_session_id=row["working_session_id"],
            transport_session_id=row["transport_session_id"],
            message_id=row["message_id"],
            event_id=row["event_id"],
            chat_id=row["chat_id"],
            sender_id=row["sender_id"],
            text=row["text"],
            occurred_at=datetime.fromisoformat(row["occurred_at"]),
            direction="outbound",
            request_id=row["request_id"],
            credential_like=bool(row["credential_like"]),
        )
    return OutboundAttemptRecord(
        transport_session_id=row["transport_session_id"],
        message_id=row["message_id"],
        request_id=row["request_id"],
        status=row["status"],
        reserved_at=datetime.fromisoformat(row["reserved_at"]),
        message=message,
        outbound_id=row["outbound_id"],
        attempted_at=(
            datetime.fromisoformat(row["attempted_at"])
            if row["attempted_at"] is not None
            else None
        ),
        terminal_at=(
            datetime.fromisoformat(row["terminal_at"])
            if row["terminal_at"] is not None
            else None
        ),
    )


def _durable_memory_from_row(row: sqlite3.Row) -> DurableMemory:
    return DurableMemory(
        memory_id=row["memory_id"],
        content=row["content"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        source_message_id=row["source_message_id"],
        status=row["status"],
        credential_like=bool(row["credential_like"]),
        replaced_by_memory_id=row["replaced_by_memory_id"],
    )


def _request_from_row(row: sqlite3.Row) -> RequestState:
    return RequestState(
        request_id=row["request_id"],
        event_id=row["event_id"],
        message_id=row["message_id"],
        operator_id=row["operator_id"],
        session_id=row["session_id"],
        chat_id=row["chat_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        status=row["status"],
        phase=row["phase"],
        model=row["model"],
        reasoning=row["reasoning"],
        reply_id=row["reply_id"],
        outcome=row["outcome"],
        error_code=row["error_code"],
    )


class _ReadOnlyAuditRecords(Sequence[AuditEvidence]):
    """A snapshot that cannot mutate the append-only in-memory store."""

    def __init__(self, records: Sequence[AuditEvidence]) -> None:
        self._records = tuple(records)

    def __getitem__(
        self, index: int | slice
    ) -> AuditEvidence | tuple[AuditEvidence, ...]:
        return self._records[index]

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[AuditEvidence]:
        return iter(self._records)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Sequence):
            return tuple(self) == tuple(other)
        return NotImplemented

    def __repr__(self) -> str:
        return repr(list(self._records))


def _resolve_audit_filter(
    query: AuditFilter | None,
    filters: dict[str, object],
) -> AuditFilter:
    if query is not None and filters:
        raise TypeError("pass either an AuditFilter or filter keyword arguments")
    if query is not None:
        return query
    aliases = {
        "operation": "operation_type",
        "target": "target_category",
        "approval": "approval_decision",
        "policy": "policy_decision",
        "date": "on_date",
    }
    for alias, canonical in aliases.items():
        if alias in filters:
            if canonical in filters:
                raise TypeError(f"pass only one of {alias} and {canonical}")
            filters[canonical] = filters.pop(alias)
    return AuditFilter(**filters)  # type: ignore[arg-type]


def _export_audit_json(records: Sequence[AuditEvidence]) -> str:
    return json.dumps(
        [record.as_safe_mapping() for record in records],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )

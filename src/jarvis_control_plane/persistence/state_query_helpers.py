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


def _request_values(request: RequestState) -> tuple[object, ...]:
    return (
        request.request_id,
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
    )


_MAX_HISTORY_RESULTS = 50
# Keep exact-selector predicates below SQLite's default expression-depth limit.
_DELETION_SELECTOR_BATCH_SIZE = 400
_MAX_MEMORY_RESULTS = 50
_MAX_MEMORY_SEARCH_SCAN_ROWS = 10_000
_MEMORY_SEARCH_BATCH_SIZE = 128
_HISTORY_SEARCH_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "for",
        "is",
        "of",
        "on",
        "the",
        "to",
        "what",
        "when",
        "where",
    }
)


def _conversation_deletion_query(
    scope: ConversationDeletionScope,
) -> tuple[str, tuple[object, ...]]:
    """Build one indexed SQL predicate for the requested deletion scope."""

    if not isinstance(scope, ConversationDeletionScope):
        raise TypeError("scope must be a ConversationDeletionScope")
    if scope.scope_type == "message":
        selectors = tuple(
            ConversationMessage.history_id_parts(history_id)
            for history_id in scope.history_ids
        )
        return (
            " OR ".join(
                "(transport_session_id = ? AND message_id = ?)" for _ in selectors
            ),
            tuple(value for selector in selectors for value in selector),
        )
    if scope.scope_type == "conversation":
        assert scope.conversation_id is not None
        return "working_session_id = ?", (scope.conversation_id,)
    assert scope.start_at is not None and scope.end_at is not None
    return (
        "occurred_at >= ? AND occurred_at <= ?",
        (scope.start_at.isoformat(), scope.end_at.isoformat()),
    )


def _preview_conversation_deletion(
    messages: Iterable[ConversationMessage],
    scope: ConversationDeletionScope,
) -> ConversationDeletionPreview:
    """Select accessible records once using one canonical ordering."""

    if not isinstance(scope, ConversationDeletionScope):
        raise TypeError("scope must be a ConversationDeletionScope")
    ordered = tuple(
        sorted(
            messages,
            key=lambda message: (
                message.occurred_at,
                message.transport_session_id,
                message.message_id,
            ),
        )
    )
    if scope.scope_type == "message":
        selected_ids = set(scope.history_ids)
        selected = tuple(
            message for message in ordered if message.history_id in selected_ids
        )
    elif scope.scope_type == "conversation":
        selected = tuple(
            message
            for message in ordered
            if message.working_session_id == scope.conversation_id
        )
    else:
        assert scope.start_at is not None and scope.end_at is not None
        selected = tuple(
            message
            for message in ordered
            if scope.start_at <= message.occurred_at <= scope.end_at
        )
    return ConversationDeletionPreview(
        scope=scope,
        messages=selected,
        content_digest=_conversation_message_digest(selected),
    )


def _conversation_tombstone(
    message: ConversationMessage,
    *,
    deletion_id: str,
    deleted_at: datetime,
    scope_type: str,
    ordinal: int,
) -> ConversationTombstone:
    """Build metadata that can reference a moved record without retaining text."""

    return ConversationTombstone(
        tombstone_id=f"tombstone-{ordinal + 1}-{message.history_id}",
        deletion_id=deletion_id,
        history_id=message.history_id,
        transport_session_id=message.transport_session_id,
        message_id=message.message_id,
        working_session_id=message.working_session_id,
        occurred_at=message.occurred_at,
        deleted_at=deleted_at,
        scope_type=scope_type,
    )


def _stage_deleted_archive(
    writer: DeletedConversationArchiveWriter,
    messages: Sequence[ConversationMessage],
    *,
    deletion_id: str,
    deleted_at: datetime,
    expected_count: int,
    expected_digest: str,
) -> None:
    """Stage content before the live-state transaction."""

    writer.stage(
        messages,
        deletion_id=deletion_id,
        deleted_at=deleted_at,
        expected_count=expected_count,
        expected_digest=expected_digest,
    )


def _finalize_deleted_archive(
    writer: DeletedConversationArchiveWriter,
    *,
    deletion_id: str,
) -> None:
    writer.finalize(deletion_id=deletion_id)


def _abort_deleted_archive(
    writer: DeletedConversationArchiveWriter,
    *,
    deletion_id: str,
) -> None:
    try:
        writer.abort(deletion_id=deletion_id)
    except DeletedConversationArchiveError:
        # A disconnected writer is cleaned up by the archive service which
        # owns the staged batch.  Do not hide the state-store failure with a
        # second transport error.
        pass


def _filter_memories(
    memories: tuple[DurableMemory, ...],
    *,
    text: str | None = None,
    memory_ids: tuple[str, ...] = (),
    include_terminal: bool = True,
    limit: int = _MAX_MEMORY_RESULTS,
) -> tuple[DurableMemory, ...]:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= _MAX_MEMORY_RESULTS
    ):
        raise ValueError(f"memory limit must be between 1 and {_MAX_MEMORY_RESULTS}")
    if text is not None and (not isinstance(text, str) or not text.strip()):
        raise ValueError("memory text query must be non-blank when provided")
    if any(not isinstance(memory_id, str) or not memory_id for memory_id in memory_ids):
        raise ValueError("memory selectors must be non-blank strings")
    terms = _history_search_terms(text or "")
    selected_ids = set(memory_ids)
    results = tuple(
        memory
        for memory in memories
        if (include_terminal or memory.is_active)
        and (not selected_ids or memory.memory_id in selected_ids)
        and (
            text is None
            or (
                bool(terms)
                and memory.content is not None
                and _matches_memory_terms(memory, terms)
            )
        )
    )
    return results[:limit]


def _validate_memory_query(
    *,
    text: str | None,
    memory_ids: tuple[str, ...],
    include_terminal: bool,
    limit: int,
) -> None:
    if not isinstance(include_terminal, bool):
        raise TypeError("include_terminal must be a boolean")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= _MAX_MEMORY_RESULTS
    ):
        raise ValueError(f"memory limit must be between 1 and {_MAX_MEMORY_RESULTS}")
    if text is not None and (not isinstance(text, str) or not text.strip()):
        raise ValueError("memory text query must be non-blank when provided")
    if any(not isinstance(memory_id, str) or not memory_id for memory_id in memory_ids):
        raise ValueError("memory selectors must be non-blank strings")


def _matches_memory_terms(memory: DurableMemory, terms: tuple[str, ...]) -> bool:
    if memory.content is None:
        return False
    content_terms = _history_search_terms(memory.content)
    return any(term in content_terms for term in terms)


def _select_memories_for_context(
    memories: tuple[DurableMemory, ...], *, text: str, limit: int
) -> MemorySelection:
    eligible = tuple(
        memory for memory in memories if memory.is_active and not memory.credential_like
    )
    return MemorySelection(
        _filter_memories(
            eligible,
            text=text,
            include_terminal=False,
            limit=limit,
        )
    )


def _filter_conversation_messages(
    messages: tuple[ConversationMessage, ...],
    *,
    text: str | None = None,
    working_session_id: str | None = None,
    request_id: str | None = None,
    direction: str | None = None,
    history_ids: tuple[str, ...] = (),
    limit: int = _MAX_HISTORY_RESULTS,
) -> tuple[ConversationMessage, ...]:
    _validate_history_query(
        text=text,
        direction=direction,
        history_ids=history_ids,
        limit=limit,
    )
    terms = _history_search_terms(text or "")
    selected_ids = set(history_ids)
    results = tuple(
        message
        for message in messages
        if (
            working_session_id is None
            or message.working_session_id == working_session_id
        )
        and (request_id is None or message.request_id == request_id)
        and (direction is None or message.direction == direction)
        and (not selected_ids or message.history_id in selected_ids)
        and (text is None or (bool(terms) and _matches_history_terms(message, terms)))
    )
    return results[:limit]


def _validate_history_query(
    *,
    text: str | None,
    direction: str | None,
    history_ids: tuple[str, ...],
    limit: int,
) -> None:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= _MAX_HISTORY_RESULTS
    ):
        raise ValueError(f"history limit must be between 1 and {_MAX_HISTORY_RESULTS}")
    if direction is not None and direction not in {"inbound", "outbound"}:
        raise ValueError("history direction must be inbound or outbound")
    if text is not None and (not isinstance(text, str) or not text.strip()):
        raise ValueError("history text query must be non-blank when provided")
    if any(
        not isinstance(history_id, str) or not history_id for history_id in history_ids
    ):
        raise ValueError("history message selectors must be non-blank strings")
    for history_id in history_ids:
        ConversationMessage.history_id_parts(history_id)


def _matches_history_terms(
    message: ConversationMessage, terms: tuple[str, ...]
) -> bool:
    return any(term in _history_search_terms(message.text) for term in terms)


def _select_history_for_context(
    messages: tuple[ConversationMessage, ...],
    *,
    text: str,
    excluding_working_session_id: str,
    limit: int,
) -> HistorySelection:
    eligible = tuple(
        message
        for message in messages
        if message.working_session_id != excluding_working_session_id
        and not message.credential_like
    )
    matches = _filter_conversation_messages(
        eligible,
        text=text,
        limit=_MAX_HISTORY_RESULTS,
    )
    return HistorySelection(matches[:limit])


def _export_conversation_messages(messages: tuple[ConversationMessage, ...]) -> str:
    return json.dumps(
        [
            {
                "conversation_id": message.working_session_id,
                "direction": message.direction,
                "event_id": message.event_id,
                "history_id": message.history_id,
                "message_id": message.message_id,
                "occurred_at": message.occurred_at.isoformat(),
                "request_id": message.request_id,
                "sender_id": message.sender_id,
                "text": message.text,
                "transport_session_id": message.transport_session_id,
            }
            for message in messages
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _history_search_terms(text: str) -> tuple[str, ...]:
    return tuple(
        term
        for term in re.findall(r"\w+", text.casefold())
        if len(term) > 2 and term not in _HISTORY_SEARCH_STOPWORDS
    )


def _fts_history_query(terms: tuple[str, ...]) -> str:
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)

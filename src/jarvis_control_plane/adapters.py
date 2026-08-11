"""Controlled local adapters used by the ticket01/ticket03 seam.

No class in this module opens a network connection.  SQLite is used for the
durable local state/audit test boundary; the orchestration and outbound
implementations are deterministic controlled fakes with the same typed ports
that production adapters will later implement.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any, ClassVar

from .conversation_archive import (
    InMemoryDeletedConversationArchive,
)
from .models import (
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
from .ports import (
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
from .sessions import ModelAvailability

SQLITE_OUTBOUND_ATTEMPT_MIGRATION_VERSION = 1
_SQLITE_OUTBOUND_ATTEMPT_MIGRATION_NAME = "ticket12_outbound_attempt_state"
_SQLITE_OUTBOUND_ATTEMPT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS outbound_attempt_record (
    transport_session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('unattempted', 'attempted', 'confirmed', 'unknown', 'not_started')
    ),
    outbound_id TEXT,
    reserved_at TEXT NOT NULL,
    attempted_at TEXT,
    terminal_at TEXT,
    PRIMARY KEY (transport_session_id, message_id)
)
"""


def migrate_sqlite_outbound_conversation_attempts(
    database: str | Path | sqlite3.Connection = ":memory:",
    *,
    applied_at: datetime | None = None,
) -> int:
    """Apply the versioned Ticket 12 outbound-state migration manually.

    This is an administrative/offline operation.  Normal state-store startup
    deliberately does not call it: a legacy outbound outbox remains untouched
    until an operator has chosen to run the migration after taking the required
    backup and completing the upgrade rehearsal.
    """

    owns_connection = not isinstance(database, sqlite3.Connection)
    connection = (
        database
        if isinstance(database, sqlite3.Connection)
        else sqlite3.connect(str(database))
    )
    connection.row_factory = sqlite3.Row
    migration_version = SQLITE_OUTBOUND_ATTEMPT_MIGRATION_VERSION
    try:
        if not owns_connection and connection.in_transaction:
            raise StateStoreError(
                "manual outbound-state migration requires an idle SQLite connection"
            )
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS jarvis_state_migrations (
                migration_name TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        existing = connection.execute(
            """
            SELECT version
            FROM jarvis_state_migrations
            WHERE migration_name = ?
            """,
            (_SQLITE_OUTBOUND_ATTEMPT_MIGRATION_NAME,),
        ).fetchone()
        if existing is not None and int(existing["version"]) >= migration_version:
            connection.commit()
            return int(existing["version"])

        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "outbound_conversation_outbox" not in tables:
            raise StateStoreError(
                "manual outbound-state migration requires the legacy outbox table"
            )

        connection.execute(_SQLITE_OUTBOUND_ATTEMPT_TABLE_SQL)
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(outbound_attempt_record)"
            ).fetchall()
        }
        required_columns = {
            "transport_session_id",
            "message_id",
            "request_id",
            "status",
            "reserved_at",
            "attempted_at",
            "terminal_at",
        }
        if not required_columns.issubset(columns):
            raise StateStoreError(
                "manual outbound-state migration found an unsupported attempt schema"
            )
        if "outbound_id" not in columns:
            connection.execute(
                "ALTER TABLE outbound_attempt_record ADD COLUMN outbound_id TEXT"
            )
        connection.execute(
            """
            INSERT OR IGNORE INTO outbound_attempt_record(
                transport_session_id, message_id, request_id, status,
                outbound_id, reserved_at, attempted_at, terminal_at
            )
            SELECT transport_session_id, message_id, request_id, 'attempted',
                   NULL, occurred_at, occurred_at, NULL
            FROM outbound_conversation_outbox
            """
        )
        connection.execute(
            """
            INSERT INTO jarvis_state_migrations(migration_name, version, applied_at)
            VALUES (?, ?, ?)
            ON CONFLICT(migration_name) DO UPDATE SET
                version = excluded.version,
                applied_at = excluded.applied_at
            """,
            (
                _SQLITE_OUTBOUND_ATTEMPT_MIGRATION_NAME,
                migration_version,
                ensure_utc(applied_at or datetime.now(UTC)).isoformat(),
            ),
        )
        connection.commit()
        return migration_version
    except StateStoreError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except sqlite3.Error as exc:
        if connection.in_transaction:
            connection.rollback()
        raise StateStoreError(
            "could not apply manual outbound-state migration"
        ) from exc
    finally:
        if owns_connection:
            connection.close()


class SystemClock:
    """Production clock port; tests should inject :class:`FixedClock`."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """Mutable deterministic clock for tests and local simulations."""

    def __init__(self, current: datetime) -> None:
        self.current = ensure_utc(current)

    def now(self) -> datetime:
        return self.current

    def advance(self, *, seconds: float = 0, minutes: float = 0) -> None:
        self.current = self.current + timedelta(seconds=seconds, minutes=minutes)


class FixedModelAvailabilityProvider:
    """Explicit controlled availability source for tests and local simulations."""

    def __init__(self, availability: ModelAvailability) -> None:
        if not isinstance(availability, ModelAvailability):
            raise TypeError("availability must be a ModelAvailability")
        self.availability = availability

    def current(self) -> ModelAvailability:
        return self.availability


class UuidIdGenerator:
    """Default runtime identifier source; tests use the deterministic variant."""

    def new_id(self, namespace: str) -> str:
        if not namespace or namespace.strip() != namespace:
            raise ValueError("namespace must be a non-empty canonical string")
        return f"{namespace}-{uuid.uuid4().hex}"


class DeterministicIdGenerator:
    """Predictable per-namespace identifiers for automated seams."""

    def __init__(self, prefix: str = "test") -> None:
        if not prefix or prefix.strip() != prefix:
            raise ValueError("prefix must be a non-empty canonical string")
        self.prefix = prefix
        self._counters: dict[str, int] = {}

    def new_id(self, namespace: str) -> str:
        if not namespace or namespace.strip() != namespace:
            raise ValueError("namespace must be a non-empty canonical string")
        next_value = self._counters.get(namespace, 0) + 1
        self._counters[namespace] = next_value
        return f"{self.prefix}-{namespace}-{next_value:04d}"


def _locked_sqlite_state(method: Callable[..., Any]) -> Callable[..., Any]:
    """Serialize one durable-state transaction on its shared state boundary."""

    @wraps(method)
    def locked(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return locked


class InMemoryDurableStateStore:
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

    @_locked_sqlite_state
    def load_recovery_degraded_marker(self) -> RecoveryDegradedMarker | None:
        with self._lock:
            return self._recovery_degraded_marker

    @_locked_sqlite_state
    def mark_recovery_degraded(self, *, reason: str, marked_at: datetime) -> None:
        marker = RecoveryDegradedMarker(reason=reason, marked_at=marked_at)
        with self._lock:
            if self._recovery_degraded_marker is None:
                self._recovery_degraded_marker = marker

    @_locked_sqlite_state
    def acknowledge_recovery_degraded(self) -> None:
        """Clear the marker only when called by an explicit admin flow."""

        with self._lock:
            self._recovery_degraded_marker = None

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
        """Atomically admit one keyed event with a terminal disposition.

        The in-memory adapter stages the state write only after the required
        audit append succeeds.  An admitted operator message may instead be
        retained as ``audit_blocked`` when audit is unavailable; rejected
        traffic is safely discarded in that case because it creates no work or
        conversation history.
        """

        with self._lock:
            key = (session_id, message_id)
            if key in self.claims:
                return IngressAdmissionResult(
                    claimed=False,
                    disposition="duplicate",
                )
            if self.fail_claim:
                raise StateStoreError("controlled ingress claim failure")
            if conversation_message is not None:
                if self.fail_conversation:
                    raise StateStoreError("controlled conversation write failure")
                if (
                    conversation_message.transport_session_id,
                    conversation_message.message_id,
                ) != key:
                    raise StateStoreError(
                        "conversation message key does not match claim"
                    )
            if self.fail_update:
                raise StateStoreError("controlled ingress disposition update failure")

            try:
                audit.append(audit_evidence)
            except AuditWriteError:
                if audit_blocked_disposition is None:
                    return IngressAdmissionResult(
                        claimed=False,
                        disposition=terminal_disposition,
                    )
                disposition = audit_blocked_disposition
            else:
                disposition = terminal_disposition

            self.claims[key] = IngressClaim(
                session_id=session_id,
                message_id=message_id,
                event_id=event_id,
                claimed_at=claimed_at,
                disposition=disposition,
            )
            if conversation_message is not None:
                self.conversation_messages[key] = conversation_message
            return IngressAdmissionResult(
                claimed=True,
                disposition=disposition,
            )

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
        with self._lock:
            if disposition == "pending_audit":
                raise StateStoreError("ingress claims require a terminal disposition")
            if self.fail_claim:
                raise StateStoreError("controlled ingress claim failure")
            key = (session_id, message_id)
            if key in self.claims:
                return False
            if conversation_message is not None:
                if self.fail_conversation:
                    raise StateStoreError("controlled conversation write failure")
                if (
                    conversation_message.transport_session_id,
                    conversation_message.message_id,
                ) != key:
                    raise StateStoreError(
                        "conversation message key does not match claim"
                    )
                self.conversation_messages[key] = conversation_message
            self.claims[key] = IngressClaim(
                session_id=session_id,
                message_id=message_id,
                event_id=event_id,
                claimed_at=claimed_at,
                disposition=disposition,
            )
            return True

    @_locked_sqlite_state
    def update_ingress_disposition(
        self,
        *,
        session_id: str,
        message_id: str,
        disposition: str,
    ) -> None:
        with self._lock:
            if disposition == "pending_audit":
                raise StateStoreError("ingress claims require a terminal disposition")
            if self.fail_update:
                raise StateStoreError("controlled ingress disposition update failure")
            key = (session_id, message_id)
            claim = self.claims.get(key)
            if claim is None:
                raise StateStoreError("ingress claim does not exist")
            self.claims[key] = IngressClaim(
                session_id=claim.session_id,
                message_id=claim.message_id,
                event_id=claim.event_id,
                claimed_at=claim.claimed_at,
                disposition=disposition,
            )

    @_locked_sqlite_state
    def begin_next_ingress_dispatch(self) -> ConversationMessage | None:
        with self._lock:
            pending = sorted(
                (
                    claim
                    for claim in self.claims.values()
                    if claim.disposition == "admitted"
                ),
                key=lambda claim: (
                    claim.claimed_at,
                    claim.session_id,
                    claim.message_id,
                ),
            )
            for claim in pending:
                key = (claim.session_id, claim.message_id)
                message = self.conversation_messages.get(key)
                if message is None:
                    continue
                self.claims[key] = replace(claim, disposition="dispatching")
                return message
            return None

    @_locked_sqlite_state
    def begin_ingress_dispatch(
        self, *, transport_session_id: str, message_id: str
    ) -> bool:
        with self._lock:
            key = (transport_session_id, message_id)
            claim = self.claims.get(key)
            if (
                claim is None
                or claim.disposition != "admitted"
                or key not in self.conversation_messages
            ):
                return False
            self.claims[key] = replace(claim, disposition="dispatching")
            return True

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
        with self._lock:
            key = (transport_session_id, message_id)
            claim = self.claims.get(key)
            if claim is None or claim.disposition != "dispatching":
                raise StateStoreError("ingress dispatch is not active")
            self.claims[key] = replace(claim, disposition=disposition)

    @_locked_sqlite_state
    def reconcile_ingress_restart(
        self,
        *,
        audit: AuditBoundary,
        audit_evidence: AuditEvidence,
    ) -> int:
        """Atomically audit and interrupt all nonterminal ingress work."""

        with self._lock:
            nonterminal = tuple(
                (key, claim)
                for key, claim in self.claims.items()
                if claim.disposition in {"admitted", "dispatching"}
            )
            if not nonterminal:
                return 0
            audit.append(audit_evidence)
            for key, claim in nonterminal:
                self.claims[key] = replace(claim, disposition="interrupted")
            return len(nonterminal)

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

    @_locked_sqlite_state
    def has_ingress_claim(self, *, session_id: str, message_id: str) -> bool:
        with self._lock:
            if self.fail_claim:
                raise StateStoreError("controlled ingress claim failure")
            return (session_id, message_id) in self.claims

    @_locked_sqlite_state
    def release_ingress_claim(self, *, session_id: str, message_id: str) -> bool:
        with self._lock:
            key = (session_id, message_id)
            released = self.claims.pop(key, None) is not None
            self.conversation_messages.pop(key, None)
            return released

    @_locked_sqlite_state
    def save_request(self, request: RequestState) -> None:
        with self._lock:
            if self.fail_save:
                raise StateStoreError("controlled request save failure")
            if request.request_id in self.requests:
                raise StateStoreError("request identifier already exists")
            self.requests[request.request_id] = request

    @_locked_sqlite_state
    def update_request(self, request: RequestState) -> None:
        with self._lock:
            if self.fail_update:
                raise StateStoreError("controlled request update failure")
            if request.request_id not in self.requests:
                raise StateStoreError("request identifier does not exist")
            self.requests[request.request_id] = request

    @_locked_sqlite_state
    def delete_request(self, request_id: str) -> bool:
        with self._lock:
            return self.requests.pop(request_id, None) is not None

    @_locked_sqlite_state
    def get_request(self, request_id: str) -> RequestState | None:
        with self._lock:
            return self.requests.get(request_id)

    @_locked_sqlite_state
    def list_requests(self) -> tuple[RequestState, ...]:
        with self._lock:
            return tuple(self.requests.values())

    @_locked_sqlite_state
    def list_ingress_claims(self) -> tuple[IngressClaim, ...]:
        with self._lock:
            return tuple(self.claims.values())

    @_locked_sqlite_state
    def load_knowledge_vault_synchronized_at(self) -> datetime | None:
        with self._lock:
            return self._knowledge_vault_synchronized_at

    @_locked_sqlite_state
    def save_knowledge_vault_synchronized_at(self, synchronized_at: datetime) -> None:
        with self._lock:
            self._knowledge_vault_synchronized_at = ensure_utc(synchronized_at)


class SQLiteDurableStateStore:
    """Small SQLite-backed durable state adapter for the primary seam."""

    def __init__(
        self,
        database: str | Path | sqlite3.Connection = ":memory:",
        *,
        deleted_archive: DeletedConversationArchiveWriter | None = None,
    ) -> None:
        self._owns_connection = not isinstance(database, sqlite3.Connection)
        self.connection = (
            database
            if isinstance(database, sqlite3.Connection)
            else sqlite3.connect(str(database), check_same_thread=False)
        )
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conversation_has_legacy_session = False
        self._deleted_archive = deleted_archive
        try:
            self._assert_outbound_attempt_schema_is_current()
            self.connection.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS ingress_claims (
                    session_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    disposition TEXT NOT NULL DEFAULT 'admitted',
                    PRIMARY KEY (session_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS request_state (
                    request_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    operator_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT 'gpt-5.6-terra',
                    reasoning TEXT NOT NULL DEFAULT 'medium',
                    reply_id TEXT,
                    outcome TEXT,
                    error_code TEXT
                );
                CREATE TABLE IF NOT EXISTS conversation_history (
                    transport_session_id TEXT NOT NULL,
                    working_session_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
                    request_id TEXT,
                    credential_like INTEGER NOT NULL DEFAULT 0 CHECK (credential_like IN (0, 1)),
                    PRIMARY KEY (transport_session_id, message_id)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS conversation_history_fts
                USING fts5(
                    transport_session_id UNINDEXED,
                    message_id UNINDEXED,
                    text
                );
                CREATE TABLE IF NOT EXISTS outbound_conversation_outbox (
                    transport_session_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    working_session_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    credential_like INTEGER NOT NULL CHECK (credential_like IN (0, 1)),
                    PRIMARY KEY (transport_session_id, message_id)
                );
                {_SQLITE_OUTBOUND_ATTEMPT_TABLE_SQL};
                CREATE INDEX IF NOT EXISTS conversation_history_by_working_session
                    ON conversation_history(working_session_id, occurred_at, transport_session_id, message_id);
                CREATE INDEX IF NOT EXISTS conversation_history_by_request
                    ON conversation_history(request_id, occurred_at, transport_session_id, message_id);
                CREATE INDEX IF NOT EXISTS conversation_history_by_direction
                    ON conversation_history(direction, occurred_at, transport_session_id, message_id);
                CREATE INDEX IF NOT EXISTS conversation_history_by_occurred_at
                    ON conversation_history(occurred_at, transport_session_id, message_id);
                CREATE TABLE IF NOT EXISTS conversation_tombstones (
                    tombstone_id TEXT PRIMARY KEY,
                    deletion_id TEXT NOT NULL,
                    history_id TEXT NOT NULL UNIQUE,
                    transport_session_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    working_session_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    deleted_at TEXT NOT NULL,
                    scope_type TEXT NOT NULL CHECK (scope_type IN ('message', 'conversation', 'date_range'))
                );
                CREATE INDEX IF NOT EXISTS conversation_tombstones_by_deleted_at
                    ON conversation_tombstones(deleted_at, tombstone_id);
                CREATE TABLE IF NOT EXISTS durable_assistant_memory (
                    memory_id TEXT PRIMARY KEY,
                    content TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    source_message_id TEXT,
                    status TEXT NOT NULL CHECK (status IN ('active', 'replaced', 'forgotten')),
                    credential_like INTEGER NOT NULL CHECK (credential_like IN (0, 1)),
                    replaced_by_memory_id TEXT
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS durable_assistant_memory_fts
                USING fts5(
                    memory_id UNINDEXED,
                    content
                );
                CREATE INDEX IF NOT EXISTS durable_assistant_memory_by_status
                    ON durable_assistant_memory(status, created_at, memory_id);
                CREATE TABLE IF NOT EXISTS knowledge_vault_synchronization (
                    slot INTEGER PRIMARY KEY CHECK (slot = 1),
                    synchronized_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recovery_degraded_marker (
                    marker_id INTEGER PRIMARY KEY CHECK (marker_id = 1),
                    reason TEXT NOT NULL,
                    marked_at TEXT NOT NULL
                );
                """
            )
            request_columns = {
                row["name"]
                for row in self.connection.execute(
                    "PRAGMA table_info(request_state)"
                ).fetchall()
            }
            if "model" not in request_columns:
                self.connection.execute(
                    "ALTER TABLE request_state "
                    "ADD COLUMN model TEXT NOT NULL DEFAULT 'gpt-5.6-terra'"
                )
            if "reasoning" not in request_columns:
                self.connection.execute(
                    "ALTER TABLE request_state "
                    "ADD COLUMN reasoning TEXT NOT NULL DEFAULT 'medium'"
                )
            self.connection.commit()
            columns = {
                row["name"]
                for row in self.connection.execute(
                    "PRAGMA table_info(ingress_claims)"
                ).fetchall()
            }
            if "disposition" not in columns:
                self.connection.execute(
                    """
                    ALTER TABLE ingress_claims
                    ADD COLUMN disposition TEXT NOT NULL DEFAULT 'admitted'
                    """
                )
                self.connection.commit()
            self.connection.execute(
                "UPDATE ingress_claims SET disposition = 'audit_blocked' "
                "WHERE disposition = 'pending_audit'"
            )
            self.connection.commit()

            conversation_columns = {
                row["name"]
                for row in self.connection.execute(
                    "PRAGMA table_info(conversation_history)"
                ).fetchall()
            }
            if "transport_session_id" not in conversation_columns:
                self.connection.execute(
                    "ALTER TABLE conversation_history "
                    "ADD COLUMN transport_session_id TEXT"
                )
                conversation_columns.add("transport_session_id")
            if "working_session_id" not in conversation_columns:
                self.connection.execute(
                    "ALTER TABLE conversation_history "
                    "ADD COLUMN working_session_id TEXT"
                )
                conversation_columns.add("working_session_id")
            if "request_id" not in conversation_columns:
                self.connection.execute(
                    "ALTER TABLE conversation_history ADD COLUMN request_id TEXT"
                )
                conversation_columns.add("request_id")
            if "credential_like" not in conversation_columns:
                self.connection.execute(
                    "ALTER TABLE conversation_history "
                    "ADD COLUMN credential_like INTEGER NOT NULL DEFAULT 0"
                )
                conversation_columns.add("credential_like")
            if "session_id" in conversation_columns:
                self._conversation_has_legacy_session = True
                self.connection.execute(
                    "UPDATE conversation_history "
                    "SET transport_session_id = session_id "
                    "WHERE transport_session_id IS NULL"
                )
                self.connection.execute(
                    "UPDATE conversation_history "
                    "SET working_session_id = 'legacy-working-' || session_id "
                    "WHERE working_session_id IS NULL"
                )
                self.connection.commit()
            history_schema = self.connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' "
                "AND name = 'conversation_history'"
            ).fetchone()["sql"]
            if "direction = 'inbound'" in history_schema:
                self._rebuild_conversation_history_for_outbound()
                self._conversation_has_legacy_session = False
            self._classify_and_index_conversation_history()
            self._rebuild_durable_memory_index()
        except sqlite3.Error as exc:
            self.close()
            raise StateStoreError("could not initialize SQLite state") from exc
        except StateStoreError:
            self.close()
            raise

    def _assert_outbound_attempt_schema_is_current(self) -> None:
        """Reject a Ticket 12-incompatible database without changing it."""

        tables = {
            row["name"]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        has_outbox = "outbound_conversation_outbox" in tables
        has_attempts = "outbound_attempt_record" in tables
        if has_outbox != has_attempts:
            raise StateStoreError(
                "SQLite outbound state requires the manual Ticket 12 migration"
            )
        if not has_attempts:
            return
        columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(outbound_attempt_record)"
            ).fetchall()
        }
        if "outbound_id" not in columns:
            raise StateStoreError(
                "SQLite outbound state requires the manual Ticket 12 migration"
            )

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

    @_locked_sqlite_state
    def list_conversation_messages(self) -> tuple[ConversationMessage, ...]:
        try:
            rows = self.connection.execute(
                """
                SELECT transport_session_id, working_session_id, message_id,
                       event_id, chat_id, sender_id, text, occurred_at, direction,
                       request_id, credential_like
                FROM conversation_history
                ORDER BY occurred_at, transport_session_id, message_id
                """
            ).fetchall()
        except sqlite3.Error as exc:
            raise StateStoreError("could not list conversation history") from exc
        return tuple(
            ConversationMessage(
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
            for row in rows
        )

    @_locked_sqlite_state
    def append_conversation_message(self, message: ConversationMessage) -> None:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self._insert_conversation_message(message)
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            raise StateStoreError(
                "conversation message identifier already exists"
            ) from exc
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise StateStoreError("could not append conversation history") from exc

    @_locked_sqlite_state
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
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                """
                INSERT INTO outbound_conversation_outbox(
                    transport_session_id, message_id, working_session_id, event_id,
                    chat_id, sender_id, text, occurred_at, request_id, credential_like
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.transport_session_id,
                    message.message_id,
                    message.working_session_id,
                    message.event_id,
                    message.chat_id,
                    message.sender_id,
                    message.text,
                    ensure_utc(message.occurred_at).isoformat(),
                    message.request_id,
                    int(message.credential_like),
                ),
            )
            self.connection.execute(
                """
                INSERT INTO outbound_attempt_record(
                    transport_session_id, message_id, request_id, status,
                    outbound_id, reserved_at, attempted_at, terminal_at
                ) VALUES (?, ?, ?, 'unattempted', NULL, ?, NULL, NULL)
                """,
                (
                    message.transport_session_id,
                    message.message_id,
                    message.request_id,
                    ensure_utc(message.occurred_at).isoformat(),
                ),
            )
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            raise StateStoreError(
                "conversation message identifier already exists"
            ) from exc
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise StateStoreError(
                "could not reserve outbound conversation history"
            ) from exc

    @_locked_sqlite_state
    def mark_outbound_conversation_attempted(
        self,
        *,
        transport_session_id: str,
        message_id: str,
        attempted_at: datetime,
    ) -> None:
        try:
            cursor = self.connection.execute(
                """
                UPDATE outbound_attempt_record
                SET status = 'attempted', attempted_at = ?
                WHERE transport_session_id = ? AND message_id = ?
                  AND status = 'unattempted'
                """,
                (
                    ensure_utc(attempted_at).isoformat(),
                    transport_session_id,
                    message_id,
                ),
            )
            if cursor.rowcount != 1:
                self.connection.rollback()
                raise StateStoreError("outbound attempt is not known-unattempted")
            self.connection.commit()
        except StateStoreError:
            raise
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise StateStoreError("could not mark outbound attempt") from exc

    @_locked_sqlite_state
    def terminalize_outbound_conversation_attempt(
        self,
        *,
        transport_session_id: str,
        message_id: str,
        status: OutboundAttemptStatus | str,
        terminal_at: datetime,
        outbound_id: str | None = None,
    ) -> None:
        status = OutboundAttemptStatus(status)
        if status in {
            OutboundAttemptStatus.UNATTEMPTED,
            OutboundAttemptStatus.ATTEMPTED,
        }:
            raise StateStoreError("outbound terminal status is required")
        terminal_at = ensure_utc(terminal_at)
        if outbound_id is not None and (
            not isinstance(outbound_id, str) or not outbound_id.strip()
        ):
            raise StateStoreError("outbound gateway identifier must be non-blank")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            attempt = self.connection.execute(
                """
                SELECT status, outbound_id
                FROM outbound_attempt_record
                WHERE transport_session_id = ? AND message_id = ?
                """,
                (transport_session_id, message_id),
            ).fetchone()
            if attempt is None:
                raise StateStoreError("outbound attempt does not exist")
            current_status = OutboundAttemptStatus(attempt["status"])
            if not is_outbound_terminal_transition_allowed(current_status, status):
                raise StateStoreError("outbound terminal transition is not allowed")
            if current_status not in {
                OutboundAttemptStatus.UNATTEMPTED,
                OutboundAttemptStatus.ATTEMPTED,
            }:
                if current_status is status and (
                    outbound_id is None or outbound_id == attempt["outbound_id"]
                ):
                    self.connection.rollback()
                    return
                raise StateStoreError("outbound attempt is already terminal")
            if status is OutboundAttemptStatus.CONFIRMED:
                if current_status is not OutboundAttemptStatus.ATTEMPTED:
                    raise StateStoreError("outbound message was not durably attempted")
                row = self.connection.execute(
                    """
                    SELECT transport_session_id, working_session_id, message_id,
                           event_id, chat_id, sender_id, text, occurred_at,
                           request_id, credential_like
                    FROM outbound_conversation_outbox
                    WHERE transport_session_id = ? AND message_id = ?
                    """,
                    (transport_session_id, message_id),
                ).fetchone()
                if row is None:
                    raise StateStoreError(
                        "reserved outbound conversation message does not exist"
                    )
                self._insert_conversation_message(
                    ConversationMessage(
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
                )
            self.connection.execute(
                """
                DELETE FROM outbound_conversation_outbox
                WHERE transport_session_id = ? AND message_id = ?
                """,
                (transport_session_id, message_id),
            )
            cursor = self.connection.execute(
                """
                UPDATE outbound_attempt_record
                SET status = ?, outbound_id = ?, terminal_at = ?
                WHERE transport_session_id = ? AND message_id = ? AND status = ?
                """,
                (
                    status.value,
                    outbound_id,
                    terminal_at.isoformat(),
                    transport_session_id,
                    message_id,
                    current_status.value,
                ),
            )
            if cursor.rowcount != 1:
                raise StateStoreError("outbound attempt changed during terminalization")
            self.connection.commit()
        except StateStoreError:
            self.connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            raise StateStoreError(
                "conversation message identifier already exists"
            ) from exc
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise StateStoreError(
                "could not terminalize outbound conversation attempt"
            ) from exc

    @_locked_sqlite_state
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
            row = self.connection.execute(
                """
                SELECT attempted_at, reserved_at
                FROM outbound_attempt_record
                WHERE transport_session_id = ? AND message_id = ?
                """,
                (transport_session_id, message_id),
            ).fetchone()
            if row is None:
                raise StateStoreError("outbound attempt does not exist")
            terminal_at = datetime.fromisoformat(
                row["attempted_at"] or row["reserved_at"]
            )
        self.terminalize_outbound_conversation_attempt(
            transport_session_id=transport_session_id,
            message_id=message_id,
            status=OutboundAttemptStatus.CONFIRMED,
            terminal_at=terminal_at,
            outbound_id=outbound_id,
        )

    @_locked_sqlite_state
    def list_outbound_conversation_attempts(
        self,
    ) -> tuple[OutboundAttemptRecord, ...]:
        try:
            rows = self.connection.execute(
                """
                SELECT a.transport_session_id, a.message_id, a.request_id, a.status,
                       a.outbound_id, a.reserved_at, a.attempted_at, a.terminal_at,
                       o.working_session_id, o.event_id, o.chat_id, o.sender_id,
                       o.text, o.occurred_at, o.credential_like
                FROM outbound_attempt_record AS a
                LEFT JOIN outbound_conversation_outbox AS o
                  ON o.transport_session_id = a.transport_session_id
                 AND o.message_id = a.message_id
                ORDER BY a.reserved_at, a.transport_session_id, a.message_id
                """
            ).fetchall()
        except sqlite3.Error as exc:
            raise StateStoreError("could not list outbound attempts") from exc
        return tuple(_outbound_attempt_from_row(row) for row in rows)

    @_locked_sqlite_state
    def list_outbound_conversation_attempt_recovery(
        self,
    ) -> tuple[OutboundAttemptRecoveryProjection, ...]:
        """Load bounded attempt/outbox facts without constructing domain records."""

        try:
            attempt_rows = self.connection.execute(
                """
                SELECT a.transport_session_id, a.message_id, a.request_id,
                       a.status, a.outbound_id, a.reserved_at, a.attempted_at,
                       a.terminal_at, o.request_id AS outbox_request_id,
                       CASE WHEN o.message_id IS NULL THEN 0 ELSE 1 END
                           AS outbox_present
                FROM outbound_attempt_record AS a
                LEFT JOIN outbound_conversation_outbox AS o
                  ON o.transport_session_id = a.transport_session_id
                 AND o.message_id = a.message_id
                ORDER BY a.reserved_at, a.transport_session_id, a.message_id
                """
            ).fetchall()
            outbox_rows = self.connection.execute(
                """
                SELECT o.transport_session_id, o.message_id, o.request_id
                FROM outbound_conversation_outbox AS o
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM outbound_attempt_record AS a
                    WHERE a.transport_session_id = o.transport_session_id
                      AND a.message_id = o.message_id
                )
                ORDER BY o.occurred_at, o.transport_session_id, o.message_id
                """
            ).fetchall()
        except sqlite3.Error as exc:
            raise StateStoreError(
                "could not load outbound recovery projection"
            ) from exc

        projections: list[OutboundAttemptRecoveryProjection] = [
            OutboundAttemptRecoveryProjection(
                transport_session_id=row["transport_session_id"],
                message_id=row["message_id"],
                attempt_present=True,
                outbox_present=bool(row["outbox_present"]),
                attempt_request_id=row["request_id"],
                outbox_request_id=row["outbox_request_id"],
                status=row["status"],
                reserved_at=row["reserved_at"],
                attempted_at=row["attempted_at"],
                terminal_at=row["terminal_at"],
                outbound_id=row["outbound_id"],
            )
            for row in attempt_rows
        ]
        projections.extend(
            OutboundAttemptRecoveryProjection(
                transport_session_id=row["transport_session_id"],
                message_id=row["message_id"],
                attempt_present=False,
                outbox_present=True,
                outbox_request_id=row["request_id"],
            )
            for row in outbox_rows
        )
        return tuple(projections)

    @_locked_sqlite_state
    def reconcile_outbound_conversation_attempts(
        self, *, interrupted_at: datetime
    ) -> tuple[OutboundAttemptRecord, ...]:
        interrupted_at = ensure_utc(interrupted_at)
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            projections = self.list_outbound_conversation_attempt_recovery()
            reconciled: list[OutboundAttemptRecord] = []
            for projection in projections:
                if not projection.attempt_present:
                    self.connection.execute(
                        """
                        DELETE FROM outbound_conversation_outbox
                        WHERE transport_session_id = ? AND message_id = ?
                        """,
                        (
                            projection.transport_session_id,
                            projection.message_id,
                        ),
                    )
                    continue
                if projection.status not in {
                    OutboundAttemptStatus.UNATTEMPTED.value,
                    OutboundAttemptStatus.ATTEMPTED.value,
                }:
                    if projection.outbox_present:
                        self.connection.execute(
                            """
                            DELETE FROM outbound_conversation_outbox
                            WHERE transport_session_id = ? AND message_id = ?
                            """,
                            (
                                projection.transport_session_id,
                                projection.message_id,
                            ),
                        )
                    continue
                status = (
                    OutboundAttemptStatus.NOT_STARTED
                    if projection.status == OutboundAttemptStatus.UNATTEMPTED.value
                    else OutboundAttemptStatus.UNKNOWN
                )
                self.connection.execute(
                    """
                    UPDATE outbound_attempt_record
                    SET status = ?, outbound_id = NULL, terminal_at = ?
                    WHERE transport_session_id = ? AND message_id = ? AND status = ?
                    """,
                    (
                        status.value,
                        interrupted_at.isoformat(),
                        projection.transport_session_id,
                        projection.message_id,
                        projection.status,
                    ),
                )
                self.connection.execute(
                    """
                    DELETE FROM outbound_conversation_outbox
                    WHERE transport_session_id = ? AND message_id = ?
                    """,
                    (projection.transport_session_id, projection.message_id),
                )
                recovered = _recovered_terminal_attempt_record(
                    projection,
                    status=status,
                    terminal_at=interrupted_at,
                )
                if recovered is not None:
                    reconciled.append(recovered)
            self.connection.commit()
        except StateStoreError:
            self.connection.rollback()
            raise
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise StateStoreError("could not reconcile outbound attempts") from exc
        return tuple(reconciled)

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

    def _rebuild_durable_memory_index(self) -> None:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute("DELETE FROM durable_assistant_memory_fts")
            rows = self.connection.execute(
                """
                SELECT memory_id, content, created_at, updated_at,
                       source_message_id, status, credential_like,
                       replaced_by_memory_id
                FROM durable_assistant_memory
                WHERE status = 'active'
                ORDER BY created_at, memory_id
                """
            ).fetchall()
            for row in rows:
                memory = _durable_memory_from_row(row)
                credential_like = int(bool(memory.credential_like))
                if credential_like != int(row["credential_like"]):
                    self.connection.execute(
                        """
                        UPDATE durable_assistant_memory
                        SET credential_like = ?
                        WHERE memory_id = ?
                        """,
                        (credential_like, memory.memory_id),
                    )
                if memory.content is not None and not memory.credential_like:
                    self.connection.execute(
                        """
                        INSERT INTO durable_assistant_memory_fts(memory_id, content)
                        VALUES (?, ?)
                        """,
                        (memory.memory_id, memory.content),
                    )
            self.connection.commit()
        except sqlite3.Error:
            self.connection.rollback()
            raise

    def _rebuild_conversation_history_for_outbound(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        self.connection.execute(
            "ALTER TABLE conversation_history RENAME TO conversation_history_legacy"
        )
        self.connection.execute(
            """
            CREATE TABLE conversation_history (
                transport_session_id TEXT NOT NULL,
                working_session_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                text TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
                request_id TEXT,
                credential_like INTEGER NOT NULL DEFAULT 0 CHECK (credential_like IN (0, 1)),
                PRIMARY KEY (transport_session_id, message_id)
            )
            """
        )
        self.connection.execute(
            """
            INSERT INTO conversation_history(
                transport_session_id, working_session_id, message_id, event_id,
                chat_id, sender_id, text, occurred_at, direction, request_id,
                credential_like
            )
            SELECT transport_session_id, working_session_id, message_id, event_id,
                   chat_id, sender_id, text, occurred_at, direction, request_id,
                   credential_like
            FROM conversation_history_legacy
            """
        )
        self.connection.execute("DROP TABLE conversation_history_legacy")
        self.connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS conversation_history_by_working_session
                ON conversation_history(working_session_id, occurred_at, transport_session_id, message_id);
            CREATE INDEX IF NOT EXISTS conversation_history_by_request
                ON conversation_history(request_id, occurred_at, transport_session_id, message_id);
            CREATE INDEX IF NOT EXISTS conversation_history_by_direction
                ON conversation_history(direction, occurred_at, transport_session_id, message_id);
            CREATE INDEX IF NOT EXISTS conversation_history_by_occurred_at
                ON conversation_history(occurred_at, transport_session_id, message_id);
            """
        )
        self.connection.commit()

    def _classify_and_index_conversation_history(self) -> None:
        rows = self.connection.execute(
            """
            SELECT transport_session_id, message_id, text, credential_like
            FROM conversation_history ORDER BY occurred_at, transport_session_id, message_id
            """
        ).fetchall()
        self.connection.execute("DELETE FROM conversation_history_fts")
        for row in rows:
            message = ConversationMessage(
                working_session_id="classification-only",
                transport_session_id=row["transport_session_id"],
                message_id=row["message_id"],
                event_id="classification-only",
                chat_id="classification-only",
                sender_id="classification-only",
                text=row["text"],
                occurred_at=datetime.now(UTC),
                credential_like=bool(row["credential_like"]),
            )
            self.connection.execute(
                """
                UPDATE conversation_history SET credential_like = ?
                WHERE transport_session_id = ? AND message_id = ?
                """,
                (
                    int(message.credential_like),
                    row["transport_session_id"],
                    row["message_id"],
                ),
            )
            self.connection.execute(
                """
                INSERT INTO conversation_history_fts(
                    transport_session_id, message_id, text
                ) VALUES (?, ?, ?)
                """,
                (row["transport_session_id"], row["message_id"], row["text"]),
            )
        self.connection.commit()

    @_locked_sqlite_state
    def close(self) -> None:
        self._deleted_archive = None
        if self._owns_connection:
            self.connection.close()


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


class InMemoryAuditBoundary:
    """Append-only redacted audit fake with deterministic failure injection."""

    def __init__(
        self, *, fail: bool = False, fail_on_append: int | None = None
    ) -> None:
        self._records: list[AuditEvidence] = []
        self._evidence_ids: set[str] = set()
        self.fail = fail
        self.fail_on_append = fail_on_append

    def append(self, evidence: AuditEvidence) -> None:
        self.append_batch((evidence,))

    def append_batch(self, evidence: Sequence[AuditEvidence]) -> None:
        records = tuple(evidence)
        if any(not isinstance(record, AuditEvidence) for record in records):
            raise TypeError("audit boundary accepts only AuditEvidence")
        identifiers = [record.evidence_id for record in records]
        if len(set(identifiers)) != len(identifiers) or any(
            identifier in self._evidence_ids for identifier in identifiers
        ):
            raise AuditWriteError("duplicate audit evidence identifier")
        if self.fail:
            raise AuditWriteError("controlled audit append failure")
        first_number = len(self._records) + 1
        last_number = first_number + len(records) - 1
        if (
            records
            and self.fail_on_append is not None
            and first_number <= self.fail_on_append <= last_number
        ):
            raise AuditWriteError("controlled audit append failure")
        self._records.extend(records)
        self._evidence_ids.update(identifiers)

    @property
    def records(self) -> _ReadOnlyAuditRecords:
        """Return a read-only snapshot retained for ticket01 compatibility."""

        return _ReadOnlyAuditRecords(self._records)

    def safe_view(
        self,
        query: AuditFilter | None = None,
        **filters: object,
    ) -> tuple[AuditEvidence, ...]:
        resolved = _resolve_audit_filter(query, filters)
        records = tuple(record for record in self._records if resolved.matches(record))
        return records[: resolved.limit] if resolved.limit is not None else records

    def inspect(
        self,
        query: AuditFilter | None = None,
        **filters: object,
    ) -> tuple[AuditEvidence, ...]:
        """Alias for the local administration safe inspection view."""

        return self.safe_view(query, **filters)

    def export_json(
        self,
        query: AuditFilter | None = None,
        **filters: object,
    ) -> str:
        return _export_audit_json(self.safe_view(query, **filters))

    def export(
        self,
        query: AuditFilter | None = None,
        **filters: object,
    ) -> str:
        """Export only the filtered redacted view as deterministic JSON."""

        return self.export_json(query, **filters)


class SQLiteAuditBoundary:
    """Append-only SQLite audit adapter with a safe local read surface.

    The append-only contract is protected at two local layers: SQLite triggers
    reject row updates/deletes even from another connection, and the adapter's
    connection authorizer rejects direct mutation attempts made through a
    connection shared with ticket01's state seam.
    """

    _AUDIT_COLUMNS: ClassVar[dict[str, tuple[str, int, int]]] = {
        "evidence_id": ("TEXT", 0, 1),
        "kind": ("TEXT", 1, 0),
        "occurred_at": ("TEXT", 1, 0),
        "event_id": ("TEXT", 0, 0),
        "request_id": ("TEXT", 0, 0),
        "message_id": ("TEXT", 0, 0),
        "operation_type": ("TEXT", 0, 0),
        "target_category": ("TEXT", 0, 0),
        "approval_decision": ("TEXT", 0, 0),
        "policy_decision": ("TEXT", 0, 0),
        "execution_status": ("TEXT", 0, 0),
        "outcome": ("TEXT", 1, 0),
        "actor": ("TEXT", 1, 0),
        "details_json": ("TEXT", 1, 0),
        "redacted": ("INTEGER", 1, 0),
    }
    _LEGACY_AUDIT_COLUMNS: ClassVar[dict[str, tuple[str, int, int]]] = {
        "evidence_id": ("TEXT", 0, 1),
        "kind": ("TEXT", 1, 0),
        "occurred_at": ("TEXT", 1, 0),
        "event_id": ("TEXT", 0, 0),
        "request_id": ("TEXT", 0, 0),
        "outcome": ("TEXT", 1, 0),
        "actor": ("TEXT", 1, 0),
        "details_json": ("TEXT", 1, 0),
        "redacted": ("INTEGER", 1, 0),
    }
    _AUDIT_TABLE_SQL_VARIANTS: ClassVar[frozenset[str]] = frozenset(
        {
            (
                "create table audit_evidence ( evidence_id text primary key, "
                "kind text not null, occurred_at text not null, event_id text, "
                "request_id text, message_id text, operation_type text, "
                "target_category text, approval_decision text, "
                "policy_decision text, execution_status text, outcome text not null, "
                "actor text not null, details_json text not null, "
                "redacted integer not null check (redacted = 1) )"
            ),
            (
                "create table audit_evidence ( evidence_id text primary key, "
                "kind text not null, occurred_at text not null, event_id text, "
                "request_id text, outcome text not null, actor text not null, "
                "details_json text not null, redacted integer not null "
                "check (redacted = 1) )"
            ),
        }
    )
    _AUDIT_TRIGGERS: ClassVar[dict[str, str]] = {
        "audit_evidence_no_update": (
            "create trigger audit_evidence_no_update before update on audit_evidence "
            "begin select raise(abort, 'audit evidence is append-only'); end"
        ),
        "audit_evidence_no_delete": (
            "create trigger audit_evidence_no_delete before delete on audit_evidence "
            "begin select raise(abort, 'audit evidence is append-only'); end"
        ),
    }

    def __init__(self, database: str | Path | sqlite3.Connection = ":memory:") -> None:
        if isinstance(database, sqlite3.Connection) and database.in_transaction:
            raise AuditWriteError(
                "caller-owned SQLite connection has an uncommitted transaction"
            )
        self._owns_connection = not isinstance(database, sqlite3.Connection)
        self._connection = (
            database
            if isinstance(database, sqlite3.Connection)
            else sqlite3.connect(str(database), check_same_thread=False)
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._append_transaction_active = False
        try:
            self._connection.execute("PRAGMA recursive_triggers = ON")
            self._ensure_canonical_audit_table()
            self._connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS audit_evidence_occurred_at
                    ON audit_evidence(occurred_at, evidence_id);
                CREATE INDEX IF NOT EXISTS audit_evidence_request_id
                    ON audit_evidence(request_id, evidence_id);
                CREATE INDEX IF NOT EXISTS audit_evidence_operation_type
                    ON audit_evidence(operation_type, evidence_id);
                CREATE INDEX IF NOT EXISTS audit_evidence_target_category
                    ON audit_evidence(target_category, evidence_id);
                CREATE INDEX IF NOT EXISTS audit_evidence_approval_decision
                    ON audit_evidence(approval_decision, evidence_id);
                CREATE INDEX IF NOT EXISTS audit_evidence_policy_decision
                    ON audit_evidence(policy_decision, evidence_id);
                CREATE INDEX IF NOT EXISTS audit_evidence_execution_status
                    ON audit_evidence(execution_status, evidence_id);
                CREATE INDEX IF NOT EXISTS audit_evidence_outcome
                    ON audit_evidence(outcome, evidence_id);
                CREATE TRIGGER IF NOT EXISTS audit_evidence_no_update
                    BEFORE UPDATE ON audit_evidence
                    BEGIN
                        SELECT RAISE(ABORT, 'audit evidence is append-only');
                    END;
                CREATE TRIGGER IF NOT EXISTS audit_evidence_no_delete
                    BEFORE DELETE ON audit_evidence
                    BEGIN
                        SELECT RAISE(ABORT, 'audit evidence is append-only');
                    END;
                """
            )
            self._connection.commit()
            self._connection.set_authorizer(self._authorize_sql)
        except sqlite3.Error as exc:
            self._connection.rollback()
            raise AuditWriteError("could not initialize SQLite audit") from exc

    def _ensure_canonical_audit_table(self) -> None:
        table = self._connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("audit_evidence",),
        ).fetchone()
        if table is None:
            self._connection.execute(
                """
                CREATE TABLE audit_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    event_id TEXT,
                    request_id TEXT,
                    message_id TEXT,
                    operation_type TEXT,
                    target_category TEXT,
                    approval_decision TEXT,
                    policy_decision TEXT,
                    execution_status TEXT,
                    outcome TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    redacted INTEGER NOT NULL CHECK (redacted = 1)
                )
                """
            )
            return

        columns = self._connection.execute(
            "PRAGMA table_info(audit_evidence)"
        ).fetchall()
        actual_columns = {
            row["name"]: (row["type"].upper(), row["notnull"], row["pk"])
            for row in columns
        }
        if actual_columns == self._AUDIT_COLUMNS:
            return
        if actual_columns != self._LEGACY_AUDIT_COLUMNS:
            # Leave unknown schemas for _assert_schema_integrity(), which keeps
            # unexpected or tampered layouts fail-closed on reads/appends.
            return

        normalized_sql = " ".join((table["sql"] or "").casefold().split())
        required_fragments = (
            "create table audit_evidence",
            "evidence_id text primary key",
            "kind text not null",
            "occurred_at text not null",
            "outcome text not null",
            "actor text not null",
            "details_json text not null",
            "redacted integer not null check (redacted = 1)",
        )
        if "on conflict" in normalized_sql or any(
            fragment not in normalized_sql for fragment in required_fragments
        ):
            raise AuditWriteError("unsupported legacy SQLite audit schema")
        self._rebuild_legacy_audit_table()

    def _rebuild_legacy_audit_table(self) -> None:
        migration_table = "audit_evidence_ticket03_migration"
        try:
            existing = self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (migration_table,),
            ).fetchone()
            if existing is not None:
                raise AuditWriteError("SQLite audit migration table already exists")
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(
                f"""
                CREATE TABLE {migration_table} (
                    evidence_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    event_id TEXT,
                    request_id TEXT,
                    message_id TEXT,
                    operation_type TEXT,
                    target_category TEXT,
                    approval_decision TEXT,
                    policy_decision TEXT,
                    execution_status TEXT,
                    outcome TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    redacted INTEGER NOT NULL CHECK (redacted = 1)
                )
                """
            )
            self._connection.execute(
                f"""
                INSERT INTO {migration_table}(
                    evidence_id, kind, occurred_at, event_id, request_id,
                    message_id, operation_type, target_category,
                    approval_decision, policy_decision, execution_status,
                    outcome, actor, details_json, redacted
                )
                SELECT evidence_id, kind, occurred_at, event_id, request_id,
                       NULL, NULL, NULL, NULL, NULL, NULL,
                       outcome, actor, details_json, redacted
                FROM audit_evidence
                ORDER BY rowid
                """
            )
            self._connection.execute("DROP TABLE audit_evidence")
            self._connection.execute(
                """
                CREATE TABLE audit_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    event_id TEXT,
                    request_id TEXT,
                    message_id TEXT,
                    operation_type TEXT,
                    target_category TEXT,
                    approval_decision TEXT,
                    policy_decision TEXT,
                    execution_status TEXT,
                    outcome TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    redacted INTEGER NOT NULL CHECK (redacted = 1)
                )
                """
            )
            self._connection.execute(
                f"""
                INSERT INTO audit_evidence(
                    evidence_id, kind, occurred_at, event_id, request_id,
                    message_id, operation_type, target_category,
                    approval_decision, policy_decision, execution_status,
                    outcome, actor, details_json, redacted
                )
                SELECT evidence_id, kind, occurred_at, event_id, request_id,
                       message_id, operation_type, target_category,
                       approval_decision, policy_decision, execution_status,
                       outcome, actor, details_json, redacted
                FROM {migration_table}
                ORDER BY rowid
                """
            )
            self._connection.execute(f"DROP TABLE {migration_table}")
            self._connection.commit()
        except AuditWriteError:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise
        except sqlite3.Error as exc:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise AuditWriteError("could not migrate legacy SQLite audit") from exc

    def append(self, evidence: AuditEvidence) -> None:
        self.append_batch((evidence,))

    def append_batch(self, evidence: Sequence[AuditEvidence]) -> None:
        with self._lock:
            self._append_batch_locked(evidence)

    def _append_batch_locked(self, evidence: Sequence[AuditEvidence]) -> None:
        records = tuple(evidence)
        if any(not isinstance(record, AuditEvidence) for record in records):
            raise TypeError("audit boundary accepts only AuditEvidence")
        if not records:
            return
        if not self._owns_connection and self._connection.in_transaction:
            raise AuditWriteError(
                "caller-owned SQLite connection has an uncommitted transaction"
            )
        transaction_started = False
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            transaction_started = True
            self._append_batch_in_transaction(records)
            self._connection.commit()
        except AuditWriteError:
            if transaction_started:
                self._connection.rollback()
            raise
        except sqlite3.Error as exc:
            if transaction_started or self._owns_connection:
                self._connection.rollback()
            raise AuditWriteError("could not append SQLite audit evidence") from exc
        finally:
            self._append_transaction_active = False

    def _append_batch_in_transaction(
        self,
        evidence: Sequence[AuditEvidence],
    ) -> None:
        """Append into a caller-owned transaction without committing it."""

        records = tuple(evidence)
        if any(not isinstance(record, AuditEvidence) for record in records):
            raise TypeError("audit boundary accepts only AuditEvidence")
        if not records:
            return
        identifiers = [record.evidence_id for record in records]
        if len(set(identifiers)) != len(identifiers):
            raise AuditWriteError("duplicate audit evidence identifier")

        self._append_transaction_active = True
        try:
            self._assert_schema_integrity()
            placeholders = ",".join("?" for _ in identifiers)
            existing = self._connection.execute(
                f"SELECT evidence_id FROM audit_evidence WHERE evidence_id IN ({placeholders})",
                identifiers,
            ).fetchone()
            if existing is not None:
                raise AuditWriteError("duplicate audit evidence identifier")
            before = self._connection.execute(
                "SELECT COUNT(*) AS count FROM audit_evidence"
            ).fetchone()["count"]
            for record in records:
                cursor = self._connection.execute(
                    """
                    INSERT INTO audit_evidence(
                        evidence_id, kind, occurred_at, event_id, request_id,
                        message_id, operation_type, target_category, approval_decision,
                        policy_decision, execution_status,
                        outcome, actor, details_json, redacted
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        record.evidence_id,
                        record.kind,
                        ensure_utc(record.occurred_at).isoformat(),
                        record.event_id,
                        record.request_id,
                        record.message_id,
                        record.operation_type,
                        record.target_category,
                        record.approval_decision,
                        record.policy_decision,
                        record.execution_status,
                        record.outcome,
                        record.actor,
                        json.dumps(
                            dict(record.details),
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                    ),
                )
                if cursor.rowcount != 1:
                    raise AuditWriteError(
                        "SQLite audit append did not retain exactly one evidence row"
                    )
            after = self._connection.execute(
                "SELECT COUNT(*) AS count FROM audit_evidence"
            ).fetchone()["count"]
            if after - before != len(records):
                raise AuditWriteError(
                    "SQLite audit batch did not retain exactly one row per evidence"
                )
        except AuditWriteError:
            raise
        except sqlite3.Error as exc:
            raise AuditWriteError("could not append SQLite audit evidence") from exc
        finally:
            self._append_transaction_active = False

    @property
    def records(self) -> tuple[AuditEvidence, ...]:
        return self.safe_view()

    def safe_view(
        self,
        query: AuditFilter | None = None,
        **filters: object,
    ) -> tuple[AuditEvidence, ...]:
        with self._lock:
            return self._safe_view_locked(query, **filters)

    def _safe_view_locked(
        self,
        query: AuditFilter | None = None,
        **filters: object,
    ) -> tuple[AuditEvidence, ...]:
        resolved = _resolve_audit_filter(query, filters)
        self._assert_schema_integrity()
        try:
            rows = self._select_rows(resolved)
        except sqlite3.Error as exc:
            raise AuditWriteError("could not read SQLite audit evidence") from exc
        try:
            records = tuple(self._evidence_from_row(row) for row in rows)
            records = tuple(record for record in records if resolved.matches(record))
            return records[: resolved.limit] if resolved.limit is not None else records
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AuditWriteError(
                "stored audit evidence is not safe to inspect"
            ) from exc

    def inspect(
        self,
        query: AuditFilter | None = None,
        **filters: object,
    ) -> tuple[AuditEvidence, ...]:
        """Alias for the local administration safe inspection view."""

        return self.safe_view(query, **filters)

    def export_json(
        self,
        query: AuditFilter | None = None,
        **filters: object,
    ) -> str:
        return _export_audit_json(self.safe_view(query, **filters))

    def export(
        self,
        query: AuditFilter | None = None,
        **filters: object,
    ) -> str:
        """Export only the filtered redacted view as deterministic JSON."""

        return self.export_json(query, **filters)

    def _assert_schema_integrity(self) -> None:
        try:
            if (
                not self._owns_connection
                and self._connection.in_transaction
                and not self._append_transaction_active
            ):
                raise AuditWriteError(
                    "caller-owned SQLite connection has an uncommitted transaction"
                )
            table = self._connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                ("audit_evidence",),
            ).fetchone()
            columns = self._connection.execute(
                "PRAGMA table_info(audit_evidence)"
            ).fetchall()
            rows = self._connection.execute(
                """
                SELECT name, sql FROM sqlite_master
                WHERE type = 'trigger' AND tbl_name = 'audit_evidence'
                """
            ).fetchall()
        except AuditWriteError:
            raise
        except sqlite3.Error as exc:
            raise AuditWriteError("could not inspect SQLite audit schema") from exc

        actual_columns = {
            row["name"]: (row["type"].upper(), row["notnull"], row["pk"])
            for row in columns
        }
        normalized_table_sql = " ".join(
            (table["sql"] if table is not None and table["sql"] else "")
            .casefold()
            .split()
        )
        if (
            table is None
            or actual_columns != self._AUDIT_COLUMNS
            or normalized_table_sql not in self._AUDIT_TABLE_SQL_VARIANTS
        ):
            raise AuditWriteError("SQLite audit schema integrity check failed")

        actual_triggers = {
            row["name"]: " ".join((row["sql"] or "").casefold().split()) for row in rows
        }
        if actual_triggers != self._AUDIT_TRIGGERS:
            raise AuditWriteError("SQLite audit schema integrity check failed")

    def _authorize_sql(
        self,
        action: int,
        first_argument: str | None,
        second_argument: str | None,
        _database_name: str | None,
        _trigger_name: str | None,
    ) -> int:
        audit_table = (
            first_argument == "audit_evidence" or second_argument == "audit_evidence"
        )
        audit_trigger = (first_argument or "").startswith("audit_evidence_") or (
            second_argument or ""
        ).startswith("audit_evidence_")
        if (
            audit_table
            and action == sqlite3.SQLITE_INSERT
            and not self._append_transaction_active
            or audit_table
            and action
            in (
                sqlite3.SQLITE_UPDATE,
                sqlite3.SQLITE_DELETE,
                sqlite3.SQLITE_DROP_TABLE,
                sqlite3.SQLITE_ALTER_TABLE,
            )
            or audit_trigger
            and action == sqlite3.SQLITE_DROP_TRIGGER
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def _select_rows(self, query: AuditFilter) -> list[sqlite3.Row]:
        conditions: list[str] = []
        parameters: list[object] = []
        if query.start_at is not None:
            conditions.append("occurred_at >= ?")
            parameters.append(query.start_at.isoformat())
        if query.end_at is not None:
            conditions.append("occurred_at < ?")
            parameters.append(query.end_at.isoformat())
        if query.on_date is not None:
            conditions.append("substr(occurred_at, 1, 10) = ?")
            parameters.append(query.on_date.isoformat())
        for column, value in (
            ("request_id", query.request_id),
            ("operation_type", query.operation_type),
            ("target_category", query.target_category),
            ("approval_decision", query.approval_decision),
            ("policy_decision", query.policy_decision),
            ("execution_status", query.execution_status),
            ("outcome", query.outcome),
        ):
            if value is not None:
                conditions.append(f"{column} = ?")
                parameters.append(value)
        statement = "SELECT * FROM audit_evidence"
        if conditions:
            statement += " WHERE " + " AND ".join(conditions)
        statement += " ORDER BY rowid"
        if query.limit is not None:
            statement += " LIMIT ?"
            parameters.append(query.limit)
        return self._connection.execute(statement, parameters).fetchall()

    @staticmethod
    def _evidence_from_row(row: sqlite3.Row) -> AuditEvidence:
        return AuditEvidence(
            evidence_id=row["evidence_id"],
            kind=row["kind"],
            occurred_at=datetime.fromisoformat(row["occurred_at"]),
            event_id=row["event_id"],
            request_id=row["request_id"],
            outcome=row["outcome"],
            actor=row["actor"],
            details=json.loads(row["details_json"]),
            redacted=bool(row["redacted"]),
            message_id=row["message_id"],
            operation_type=row["operation_type"],
            target_category=row["target_category"],
            approval_decision=row["approval_decision"],
            policy_decision=row["policy_decision"],
            execution_status=row["execution_status"],
        )

    def close(self) -> None:
        with self._lock:
            if self._owns_connection:
                self._connection.close()


class ControlledOrchestrationAdapter:
    """Deterministic orchestration fake; it cannot authorize or send anything."""

    def __init__(
        self,
        *,
        response_text: str = "Controlled orchestration completed the request.",
        failure: str | None = None,
        response_factory: Callable[[OrchestrationRequest], str] | None = None,
        proposal_factory: Callable[[OrchestrationRequest], FrozenActionProposal]
        | None = None,
    ) -> None:
        if not response_text.strip():
            raise ValueError("response_text must be non-blank")
        self.response_text = response_text
        self.failure = failure
        self.response_factory = response_factory
        self.proposal_factory = proposal_factory
        self.calls: list[OrchestrationRequest] = []

    def run(self, request: OrchestrationRequest) -> OrchestrationResult:
        self.calls.append(request)
        if self.failure is not None:
            raise OrchestrationAdapterError(self.failure)
        reply_text = (
            self.response_factory(request)
            if self.response_factory is not None
            else self.response_text
        )
        return OrchestrationResult(
            request_id=request.state.request_id,
            outcome="completed",
            reply_text=reply_text,
            adapter="controlled",
            proposal=(
                self.proposal_factory(request)
                if self.proposal_factory is not None
                else None
            ),
        )


class _ControlledActionDispatch:
    """Prepared controlled action with the same cancellation barrier as workers."""

    def __init__(
        self, owner: ControlledActionDispatcher, action: FrozenActionProposal
    ) -> None:
        self._owner = owner
        self._action = action
        self._lock = threading.RLock()
        self._started = False
        self._cancelled = False

    def run(self) -> None:
        with self._lock:
            if self._cancelled:
                self._owner._forget(self._action.action_id, self)
                raise ActionDispatcherError("action was cancelled before dispatch")
            self._started = True
        try:
            self._owner._dispatch(self._action)
        finally:
            self._owner._forget(self._action.action_id, self)

    def cancel(self) -> ActionCancellationResult:
        with self._lock:
            if not self._started:
                self._cancelled = True
                result = ActionCancellationResult(ActionCancellationStatus.NOT_STARTED)
                self._owner._forget(self._action.action_id, self)
                return result
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)


class ControlledActionDispatcher:
    """Controlled action edge with an explicit prepared/cancellable lifecycle."""

    def __init__(
        self, *, failure: str | None = None, failure_may_have_dispatched: bool = False
    ) -> None:
        self.failure = failure
        self.failure_may_have_dispatched = failure_may_have_dispatched
        self.dispatched: list[FrozenActionProposal] = []
        self._lock = threading.RLock()
        self._prepared: dict[str, _ControlledActionDispatch] = {}

    def prepare(self, action: FrozenActionProposal) -> _ControlledActionDispatch:
        handle = _ControlledActionDispatch(self, action)
        with self._lock:
            if action.action_id in self._prepared:
                raise ActionDispatcherError(
                    f"action {action.action_id} is already prepared",
                    may_have_dispatched=True,
                )
            self._prepared[action.action_id] = handle
        return handle

    def dispatch(self, action: FrozenActionProposal) -> None:
        """Compatibility helper for direct controlled-adapter callers."""

        self.prepare(action).run()

    def cancel(self, *, action_id: str) -> ActionCancellationResult:
        with self._lock:
            handle = self._prepared.get(action_id)
        if handle is None:
            # The prepared handle may have forgotten itself after the external
            # operation returned but before the control plane persisted its
            # terminal state. Absence is therefore not proof of non-start.
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
        return handle.cancel()

    def _dispatch(self, action: FrozenActionProposal) -> None:
        if self.failure is not None:
            raise ActionDispatcherError(
                self.failure, may_have_dispatched=self.failure_may_have_dispatched
            )
        self.dispatched.append(action)

    def _forget(self, action_id: str, handle: _ControlledActionDispatch) -> None:
        with self._lock:
            if self._prepared.get(action_id) is handle:
                del self._prepared[action_id]


class ControlledOutboundConnector:
    """Closed fake connector with a fixed destination.

    The capability broker owns the audit admission gate immediately before
    calling this connector.  The retained audit/clock/ID constructor inputs
    keep the ticket01 controlled-adapter shape source-compatible.
    """

    def __init__(
        self,
        *,
        operator_id: str,
        session_id: str,
        audit: AuditBoundary,
        clock: Clock,
        ids: IdGenerator,
        failure: str | None = None,
    ) -> None:
        self.operator_id = operator_id
        self.session_id = session_id
        self.audit = audit
        self.clock = clock
        self.ids = ids
        self.failure = failure
        self.sent: list[OutboundReply] = []

    def send(self, reply: OutboundReply) -> OutboundDelivery:
        self.preflight(reply)
        if self.failure is not None:
            raise OutboundConnectorError(self.failure)

        self.sent.append(reply)
        return OutboundDelivery(outbound_id=self.ids.new_id("outbound"), accepted=True)

    def preflight(self, reply: OutboundReply) -> None:
        """Validate the deterministic send without performing it.

        The broker uses this contract to append the complete outbound audit
        admission before calling ``send``.  A connector whose send can fail
        after preflight must expose that uncertainty instead of implementing
        this method as a best-effort check.
        """

        if reply.session_id != self.session_id:
            raise OutboundConnectorError("reply session is not configured")
        if reply.recipient_id != self.operator_id:
            raise OutboundConnectorError("reply recipient is not configured")
        if reply.request_id not in reply.body:
            raise OutboundConnectorError("reply is missing request correlation")
        if self.failure is not None:
            raise OutboundConnectorError(self.failure)


def replace_request(request: RequestState, **changes: object) -> RequestState:
    """Typed helper kept in the adapter module for small state transitions."""

    return replace(request, **changes)

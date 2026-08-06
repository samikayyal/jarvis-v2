"""Controlled local adapters used by the ticket01/ticket03 seam.

No class in this module opens a network connection.  SQLite is used for the
durable local state/audit test boundary; the orchestration and outbound
implementations are deterministic controlled fakes with the same typed ports
that production adapters will later implement.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

from .models import (
    AuditEvidence,
    AuditFilter,
    ConversationMessage,
    FrozenActionProposal,
    HistorySelection,
    IngressAdmissionResult,
    IngressClaim,
    OrchestrationRequest,
    OrchestrationResult,
    OutboundDelivery,
    OutboundReply,
    RequestState,
    ensure_utc,
)
from .ports import (
    ActionDispatcherError,
    AuditBoundary,
    AuditWriteError,
    Clock,
    IdGenerator,
    OrchestrationAdapterError,
    OutboundConnectorError,
    StateStoreError,
)
from .sessions import ModelAvailability


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


class InMemoryDurableStateStore:
    """A failure-controllable state port for narrow unit tests."""

    def __init__(self) -> None:
        self.claims: dict[tuple[str, str], IngressClaim] = {}
        self.conversation_messages: dict[tuple[str, str], ConversationMessage] = {}
        self.requests: dict[str, RequestState] = {}
        self.fail_claim = False
        self.fail_conversation = False
        self.fail_save = False
        self.fail_update = False
        self._lock = threading.RLock()

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
            key = (message.transport_session_id, message.message_id)
            if key in self.conversation_messages:
                raise StateStoreError("conversation message identifier already exists")
            self.conversation_messages[key] = message

    def search_conversation_messages(
        self,
        *,
        text: str | None = None,
        working_session_id: str | None = None,
        request_id: str | None = None,
        direction: str | None = None,
        message_ids: tuple[str, ...] = (),
        limit: int = 50,
    ) -> tuple[ConversationMessage, ...]:
        return _filter_conversation_messages(
            self.list_conversation_messages(),
            text=text,
            working_session_id=working_session_id,
            request_id=request_id,
            direction=direction,
            message_ids=message_ids,
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

    def has_ingress_claim(self, *, session_id: str, message_id: str) -> bool:
        with self._lock:
            if self.fail_claim:
                raise StateStoreError("controlled ingress claim failure")
            return (session_id, message_id) in self.claims

    def release_ingress_claim(self, *, session_id: str, message_id: str) -> bool:
        with self._lock:
            key = (session_id, message_id)
            released = self.claims.pop(key, None) is not None
            self.conversation_messages.pop(key, None)
            return released

    def save_request(self, request: RequestState) -> None:
        with self._lock:
            if self.fail_save:
                raise StateStoreError("controlled request save failure")
            if request.request_id in self.requests:
                raise StateStoreError("request identifier already exists")
            self.requests[request.request_id] = request

    def update_request(self, request: RequestState) -> None:
        with self._lock:
            if self.fail_update:
                raise StateStoreError("controlled request update failure")
            if request.request_id not in self.requests:
                raise StateStoreError("request identifier does not exist")
            self.requests[request.request_id] = request

    def delete_request(self, request_id: str) -> bool:
        with self._lock:
            return self.requests.pop(request_id, None) is not None

    def get_request(self, request_id: str) -> RequestState | None:
        with self._lock:
            return self.requests.get(request_id)

    def list_requests(self) -> tuple[RequestState, ...]:
        with self._lock:
            return tuple(self.requests.values())

    def list_ingress_claims(self) -> tuple[IngressClaim, ...]:
        with self._lock:
            return tuple(self.claims.values())


class SQLiteDurableStateStore:
    """Small SQLite-backed durable state adapter for the primary seam."""

    def __init__(self, database: str | Path | sqlite3.Connection = ":memory:") -> None:
        self._owns_connection = not isinstance(database, sqlite3.Connection)
        self.connection = (
            database
            if isinstance(database, sqlite3.Connection)
            else sqlite3.connect(str(database))
        )
        self.connection.row_factory = sqlite3.Row
        self._conversation_has_legacy_session = False
        try:
            self.connection.executescript(
                """
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
                """
            )
            self.connection.commit()

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
        except sqlite3.Error as exc:
            raise StateStoreError("could not initialize SQLite state") from exc

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
        if not conversation_message.credential_like:
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

    def get_request(self, request_id: str) -> RequestState | None:
        try:
            row = self.connection.execute(
                "SELECT * FROM request_state WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StateStoreError("could not read request state") from exc
        return _request_from_row(row) if row else None

    def list_requests(self) -> tuple[RequestState, ...]:
        try:
            rows = self.connection.execute(
                "SELECT * FROM request_state ORDER BY created_at, request_id"
            ).fetchall()
        except sqlite3.Error as exc:
            raise StateStoreError("could not list request state") from exc
        return tuple(_request_from_row(row) for row in rows)

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

    def search_conversation_messages(
        self,
        *,
        text: str | None = None,
        working_session_id: str | None = None,
        request_id: str | None = None,
        direction: str | None = None,
        message_ids: tuple[str, ...] = (),
        limit: int = 50,
    ) -> tuple[ConversationMessage, ...]:
        # The local FTS index accelerates non-secret retrieval. Secret-bearing
        # records intentionally do not enter that index, so exact inspection
        # and deterministic search additionally check retained rows directly.
        indexed_keys = set(self._fts_candidates(text)) if text is not None else set()
        matches = _filter_conversation_messages(
            self.list_conversation_messages(),
            text=text,
            working_session_id=working_session_id,
            request_id=request_id,
            direction=direction,
            message_ids=message_ids,
            limit=limit,
        )
        if text is None:
            return matches
        return tuple(
            message
            for message in matches
            if message.credential_like
            or (message.transport_session_id, message.message_id) in indexed_keys
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
        matches = self.search_conversation_messages(
            text=text,
            limit=_MAX_HISTORY_RESULTS,
        )
        return HistorySelection(
            tuple(
                message
                for message in matches
                if message.working_session_id != excluding_working_session_id
                and not message.credential_like
            )[:limit]
        )

    def _fts_candidates(self, text: str) -> tuple[tuple[str, str], ...]:
        query = _fts_query(text)
        if not query:
            return ()
        try:
            rows = self.connection.execute(
                """
                SELECT transport_session_id, message_id
                FROM conversation_history_fts
                WHERE conversation_history_fts MATCH ?
                """,
                (query,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise StateStoreError("could not search local conversation index") from exc
        return tuple((row[0], row[1]) for row in rows)

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
            if not message.credential_like:
                self.connection.execute(
                    """
                    INSERT INTO conversation_history_fts(
                        transport_session_id, message_id, text
                    ) VALUES (?, ?, ?)
                    """,
                    (row["transport_session_id"], row["message_id"], row["text"]),
                )
        self.connection.commit()

    def close(self) -> None:
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


def _filter_conversation_messages(
    messages: tuple[ConversationMessage, ...],
    *,
    text: str | None = None,
    working_session_id: str | None = None,
    request_id: str | None = None,
    direction: str | None = None,
    message_ids: tuple[str, ...] = (),
    limit: int = _MAX_HISTORY_RESULTS,
) -> tuple[ConversationMessage, ...]:
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
        not isinstance(message_id, str) or not message_id for message_id in message_ids
    ):
        raise ValueError("history message selectors must be non-blank strings")

    terms = _history_search_terms(text or "")
    selected_ids = set(message_ids)
    results = tuple(
        message
        for message in messages
        if (
            working_session_id is None
            or message.working_session_id == working_session_id
        )
        and (request_id is None or message.request_id == request_id)
        and (direction is None or message.direction == direction)
        and (not selected_ids or message.message_id in selected_ids)
        and (not terms or any(term in message.text.casefold() for term in terms))
    )
    return results[:limit]


def _select_history_for_context(
    messages: tuple[ConversationMessage, ...],
    *,
    text: str,
    excluding_working_session_id: str,
    limit: int,
) -> HistorySelection:
    matches = _filter_conversation_messages(
        messages,
        text=text,
        limit=_MAX_HISTORY_RESULTS,
    )
    return HistorySelection(
        tuple(
            message
            for message in matches
            if message.working_session_id != excluding_working_session_id
            and not message.credential_like
        )[:limit]
    )


def _export_conversation_messages(messages: tuple[ConversationMessage, ...]) -> str:
    return json.dumps(
        [
            {
                "conversation_id": message.working_session_id,
                "direction": message.direction,
                "event_id": message.event_id,
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


def _fts_query(text: str) -> str:
    return " OR ".join(f'"{term}"' for term in _history_search_terms(text))


def _history_search_terms(text: str) -> tuple[str, ...]:
    return tuple(
        term.casefold().strip(".,:;!?()[]{}\"'")
        for term in text.split()
        if len(term.strip(".,:;!?()[]{}\"'")) > 2
        and term.casefold().strip(".,:;!?()[]{}\"'") not in _HISTORY_SEARCH_STOPWORDS
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
            else sqlite3.connect(str(database))
        )
        self._connection.row_factory = sqlite3.Row
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


class ControlledActionDispatcher:
    """Controlled action edge that records exactly the frozen proposal passed in."""

    def __init__(
        self, *, failure: str | None = None, failure_may_have_dispatched: bool = False
    ) -> None:
        self.failure = failure
        self.failure_may_have_dispatched = failure_may_have_dispatched
        self.dispatched: list[FrozenActionProposal] = []

    def dispatch(self, action: FrozenActionProposal) -> None:
        if self.failure is not None:
            raise ActionDispatcherError(
                self.failure, may_have_dispatched=self.failure_may_have_dispatched
            )
        self.dispatched.append(action)


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

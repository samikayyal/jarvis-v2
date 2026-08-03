"""Controlled local adapters used by the ticket01 seam.

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
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import (
    AuditEvidence,
    ConversationMessage,
    IngressClaim,
    OrchestrationRequest,
    OrchestrationResult,
    OutboundReply,
    RequestState,
    ensure_utc,
)
from .ports import (
    AuditBoundary,
    AuditWriteError,
    Clock,
    IdGenerator,
    OrchestrationAdapterError,
    OutboundConnectorError,
    StateStoreError,
)


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

    def claim_ingress(
        self,
        *,
        session_id: str,
        message_id: str,
        event_id: str,
        claimed_at: datetime,
        conversation_message: ConversationMessage | None = None,
        disposition: str = "pending_audit",
    ) -> bool:
        with self._lock:
            if self.fail_claim:
                raise StateStoreError("controlled ingress claim failure")
            key = (session_id, message_id)
            if key in self.claims:
                return False
            if conversation_message is not None:
                if self.fail_conversation:
                    raise StateStoreError("controlled conversation write failure")
                if (
                    conversation_message.session_id,
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
            return tuple(self.conversation_messages.values())

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
        try:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS ingress_claims (
                    session_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    disposition TEXT NOT NULL DEFAULT 'pending_audit',
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
                    reply_id TEXT,
                    outcome TEXT,
                    error_code TEXT
                );
                CREATE TABLE IF NOT EXISTS conversation_history (
                    session_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    direction TEXT NOT NULL CHECK (direction = 'inbound'),
                    PRIMARY KEY (session_id, message_id)
                );
                """
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
        except sqlite3.Error as exc:
            raise StateStoreError("could not initialize SQLite state") from exc

    def claim_ingress(
        self,
        *,
        session_id: str,
        message_id: str,
        event_id: str,
        claimed_at: datetime,
        conversation_message: ConversationMessage | None = None,
        disposition: str = "pending_audit",
    ) -> bool:
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
                    conversation_message.session_id,
                    conversation_message.message_id,
                ) != (session_id, message_id):
                    self.connection.rollback()
                    raise StateStoreError(
                        "conversation message key does not match claim"
                    )
                self.connection.execute(
                    """
                    INSERT INTO conversation_history(
                        session_id, message_id, event_id, chat_id, sender_id,
                        text, occurred_at, direction
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        conversation_message.session_id,
                        conversation_message.message_id,
                        conversation_message.event_id,
                        conversation_message.chat_id,
                        conversation_message.sender_id,
                        conversation_message.text,
                        ensure_utc(conversation_message.occurred_at).isoformat(),
                        conversation_message.direction,
                    ),
                )
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

    def save_request(self, request: RequestState) -> None:
        try:
            self.connection.execute(
                """
                INSERT INTO request_state(
                    request_id, event_id, message_id, operator_id, session_id,
                    chat_id, created_at, updated_at, status, phase,
                    reply_id, outcome, error_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    phase = ?, reply_id = ?, outcome = ?, error_code = ?
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
                SELECT session_id, message_id, event_id, chat_id, sender_id,
                       text, occurred_at, direction
                FROM conversation_history
                ORDER BY occurred_at, session_id, message_id
                """
            ).fetchall()
        except sqlite3.Error as exc:
            raise StateStoreError("could not list conversation history") from exc
        return tuple(
            ConversationMessage(
                session_id=row["session_id"],
                message_id=row["message_id"],
                event_id=row["event_id"],
                chat_id=row["chat_id"],
                sender_id=row["sender_id"],
                text=row["text"],
                occurred_at=datetime.fromisoformat(row["occurred_at"]),
                direction=row["direction"],
            )
            for row in rows
        )

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
        request.reply_id,
        request.outcome,
        request.error_code,
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
        reply_id=row["reply_id"],
        outcome=row["outcome"],
        error_code=row["error_code"],
    )


class InMemoryAuditBoundary:
    """Append-only redacted audit fake with deterministic failure injection."""

    def __init__(
        self, *, fail: bool = False, fail_on_append: int | None = None
    ) -> None:
        self.records: list[AuditEvidence] = []
        self.fail = fail
        self.fail_on_append = fail_on_append

    def append(self, evidence: AuditEvidence) -> None:
        next_number = len(self.records) + 1
        if self.fail or (
            self.fail_on_append is not None and next_number == self.fail_on_append
        ):
            raise AuditWriteError("controlled audit append failure")
        self.records.append(evidence)


class SQLiteAuditBoundary:
    """Append-only SQLite audit adapter sharing a local connection when desired."""

    def __init__(self, database: str | Path | sqlite3.Connection = ":memory:") -> None:
        self._owns_connection = not isinstance(database, sqlite3.Connection)
        self.connection = (
            database
            if isinstance(database, sqlite3.Connection)
            else sqlite3.connect(str(database))
        )
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    event_id TEXT,
                    request_id TEXT,
                    outcome TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    redacted INTEGER NOT NULL CHECK (redacted = 1)
                )
                """
            )
            self.connection.commit()
        except sqlite3.Error as exc:
            raise AuditWriteError("could not initialize SQLite audit") from exc

    def append(self, evidence: AuditEvidence) -> None:
        try:
            self.connection.execute(
                """
                INSERT INTO audit_evidence(
                    evidence_id, kind, occurred_at, event_id, request_id,
                    outcome, actor, details_json, redacted
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    evidence.evidence_id,
                    evidence.kind,
                    ensure_utc(evidence.occurred_at).isoformat(),
                    evidence.event_id,
                    evidence.request_id,
                    evidence.outcome,
                    evidence.actor,
                    json.dumps(
                        dict(evidence.details),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ),
            )
            self.connection.commit()
        except sqlite3.Error as exc:
            raise AuditWriteError("could not append SQLite audit evidence") from exc

    @property
    def records(self) -> tuple[AuditEvidence, ...]:
        try:
            rows = self.connection.execute(
                "SELECT * FROM audit_evidence ORDER BY rowid"
            ).fetchall()
        except sqlite3.Error as exc:
            raise AuditWriteError("could not read SQLite audit evidence") from exc
        return tuple(
            AuditEvidence(
                evidence_id=row["evidence_id"],
                kind=row["kind"],
                occurred_at=datetime.fromisoformat(row["occurred_at"]),
                event_id=row["event_id"],
                request_id=row["request_id"],
                outcome=row["outcome"],
                actor=row["actor"],
                details=json.loads(row["details_json"]),
                redacted=bool(row["redacted"]),
            )
            for row in rows
        )

    def close(self) -> None:
        if self._owns_connection:
            self.connection.close()


class ControlledOrchestrationAdapter:
    """Deterministic orchestration fake; it cannot authorize or send anything."""

    def __init__(
        self,
        *,
        response_text: str = "Controlled orchestration completed the request.",
        failure: str | None = None,
        response_factory: Callable[[OrchestrationRequest], str] | None = None,
    ) -> None:
        if not response_text.strip():
            raise ValueError("response_text must be non-blank")
        self.response_text = response_text
        self.failure = failure
        self.response_factory = response_factory
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
        )


class ControlledOutboundConnector:
    """Closed fake connector with an audit gate and fixed destination."""

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

    def send(self, reply: OutboundReply) -> None:
        if reply.session_id != self.session_id:
            raise OutboundConnectorError("reply session is not configured")
        if reply.recipient_id != self.operator_id:
            raise OutboundConnectorError("reply recipient is not configured")
        if reply.request_id not in reply.body:
            raise OutboundConnectorError("reply is missing request correlation")

        self._audit(
            kind="outbound_attempt",
            reply=reply,
            outcome="attempted",
            details={
                "channel": "controlled_outbound",
                "destination": "configured_operator",
            },
        )
        if self.failure is not None:
            self._audit(
                kind="outbound_result",
                reply=reply,
                outcome="failed",
                details={"channel": "controlled_outbound", "result": "failed"},
            )
            raise OutboundConnectorError(self.failure)

        self.sent.append(reply)
        try:
            self._audit(
                kind="outbound_result",
                reply=reply,
                outcome="accepted",
                details={"channel": "controlled_outbound", "result": "accepted"},
            )
        except AuditWriteError as exc:
            raise OutboundConnectorError(
                "outbound audit result was not recorded",
                may_have_sent=True,
            ) from exc

    def _audit(
        self,
        *,
        kind: str,
        reply: OutboundReply,
        outcome: str,
        details: dict[str, str],
    ) -> None:
        evidence = AuditEvidence(
            evidence_id=self.ids.new_id("audit"),
            kind=kind,
            occurred_at=self.clock.now(),
            request_id=reply.request_id,
            outcome=outcome,
            actor="controlled_outbound",
            details=details,
        )
        self.audit.append(evidence)


def replace_request(request: RequestState, **changes: object) -> RequestState:
    """Typed helper kept in the adapter module for small state transitions."""

    return replace(request, **changes)

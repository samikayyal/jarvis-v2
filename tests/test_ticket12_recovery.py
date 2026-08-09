"""Ticket 12 restart and durable ambiguous-outcome acceptance seam."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from test_support import build_receiver_components

from jarvis_control_plane import (
    AuditWriteError,
    ConversationMessage,
    InboundMessage,
    InMemoryAuditBoundary,
    InMemoryDurableStateStore,
    OutboundAttemptStatus,
    RequestState,
    SignedInboundEvent,
    SQLiteDurableStateStore,
)
from jarvis_control_plane.models import OutboundDelivery
from jarvis_control_plane.ports import OutboundConnectorError
from jarvis_control_plane.sessions import InMemoryWorkingSessionStore

NOW = datetime(2026, 8, 9, 8, 0, tzinfo=UTC)
OPERATOR = "operator-001"
TRANSPORT_SESSION = "openwa-internal-session"


@pytest.mark.parametrize(
    ("before_restart", "after_restart"),
    [
        (OutboundAttemptStatus.UNATTEMPTED, OutboundAttemptStatus.NOT_STARTED),
        (OutboundAttemptStatus.ATTEMPTED, OutboundAttemptStatus.UNKNOWN),
        (OutboundAttemptStatus.CONFIRMED, OutboundAttemptStatus.CONFIRMED),
        (OutboundAttemptStatus.UNKNOWN, OutboundAttemptStatus.UNKNOWN),
    ],
)
def test_restart_reconciles_outbound_attempts_without_resending(
    before_restart: OutboundAttemptStatus,
    after_restart: OutboundAttemptStatus,
) -> None:
    state = InMemoryDurableStateStore()
    components = _components(state=state)
    message = _outbound_message(message_id=f"reply-{before_restart.value}")
    state.reserve_outbound_conversation_message(message)
    if before_restart is not OutboundAttemptStatus.UNATTEMPTED:
        state.mark_outbound_conversation_attempted(
            transport_session_id=message.transport_session_id,
            message_id=message.message_id,
            attempted_at=NOW + timedelta(seconds=1),
        )
    if before_restart is OutboundAttemptStatus.CONFIRMED:
        state.accept_reserved_outbound_conversation_message(
            transport_session_id=message.transport_session_id,
            message_id=message.message_id,
            terminal_at=NOW + timedelta(seconds=2),
        )
    elif before_restart is OutboundAttemptStatus.UNKNOWN:
        state.reconcile_outbound_conversation_attempts(
            interrupted_at=NOW + timedelta(seconds=2)
        )

    restarted = _components(
        state=state,
        working_sessions=components.broker.working_sessions,
    )

    record = state.list_outbound_conversation_attempts()[-1]
    assert record.status is after_restart
    assert record.message is None
    assert restarted.outbound.sent == []


def test_restart_interrupts_only_nonterminal_requests_and_records_safe_evidence() -> (
    None
):
    state = InMemoryDurableStateStore()
    components = _components(state=state)
    active = _request("request-active", status="accepted", phase="orchestration")
    completed = _request(
        "request-completed",
        status="completed",
        phase="completed",
        outcome="reply_sent",
    )
    state.save_request(active)
    state.save_request(completed)

    restarted = _components(
        state=state,
        working_sessions=components.broker.working_sessions,
    )

    interrupted = state.get_request(active.request_id)
    assert interrupted is not None
    assert interrupted.status == "interrupted"
    assert interrupted.phase == "interrupted"
    assert interrupted.outcome == "interrupted"
    assert interrupted.error_code == "service_restart"
    assert state.get_request(completed.request_id) == completed
    assert restarted.outbound.sent == []
    restart_records = [
        record for record in restarted.audit.records if record.kind == "service_restart"
    ]
    assert restart_records
    assert restart_records[-1].details == {
        "interrupted_requests": "1",
        "outbound_not_started": "0",
        "outbound_unknown": "0",
    }


def test_restart_audit_failure_does_not_mutate_recovery_state() -> None:
    state = InMemoryDurableStateStore()
    audit = InMemoryAuditBoundary()
    initial = _components(state=state, audit=audit)
    active = _request("request-audit-gated", status="accepted", phase="orchestration")
    message = _outbound_message(message_id="reply-audit-gated")
    state.save_request(active)
    state.reserve_outbound_conversation_message(message)
    state.mark_outbound_conversation_attempted(
        transport_session_id=message.transport_session_id,
        message_id=message.message_id,
        attempted_at=NOW + timedelta(seconds=1),
    )
    before_session = initial.broker.working_sessions.load()
    before_requests = state.list_requests()
    before_attempts = state.list_outbound_conversation_attempts()
    audit.fail = True

    with pytest.raises(AuditWriteError, match="controlled audit append failure"):
        _components(
            state=state,
            audit=audit,
            working_sessions=initial.broker.working_sessions,
        )

    assert initial.broker.working_sessions.load() == before_session
    assert state.list_requests() == before_requests
    assert state.list_outbound_conversation_attempts() == before_attempts
    assert audit.records == []

    audit.fail = False
    restarted = _components(
        state=state,
        audit=audit,
        working_sessions=initial.broker.working_sessions,
    )
    interrupted = state.get_request(active.request_id)
    assert interrupted is not None
    assert interrupted.status == "interrupted"
    attempt = state.list_outbound_conversation_attempts()[0]
    assert attempt.status is OutboundAttemptStatus.UNKNOWN
    assert attempt.message is None
    restart_records = [
        record for record in restarted.audit.records if record.kind == "service_restart"
    ]
    assert restart_records[-1].details == {
        "interrupted_requests": "1",
        "outbound_not_started": "0",
        "outbound_unknown": "1",
    }


def test_restart_audit_failure_does_not_create_missing_working_session() -> None:
    state = InMemoryDurableStateStore()
    active = _request("request-no-session", status="accepted", phase="orchestration")
    state.save_request(active)
    audit = InMemoryAuditBoundary(fail=True)
    working_sessions = InMemoryWorkingSessionStore()
    before_requests = state.list_requests()

    with pytest.raises(AuditWriteError, match="controlled audit append failure"):
        _components(
            state=state,
            audit=audit,
            working_sessions=working_sessions,
        )

    assert working_sessions.load() is None
    assert state.list_requests() == before_requests
    assert audit.records == []

    audit.fail = False
    recovered = _components(
        state=state,
        audit=audit,
        working_sessions=working_sessions,
    )
    assert working_sessions.load() is not None
    assert state.get_request(active.request_id).status == "interrupted"
    assert any(record.kind == "service_restart" for record in recovered.audit.records)


@pytest.mark.parametrize(
    ("may_have_sent", "expected_status", "expected_disposition"),
    [
        (False, OutboundAttemptStatus.NOT_STARTED, "failed"),
        (True, OutboundAttemptStatus.UNKNOWN, "unknown"),
    ],
)
def test_normal_outbound_failure_terminalizes_private_attempt(
    may_have_sent: bool,
    expected_status: OutboundAttemptStatus,
    expected_disposition: str,
) -> None:
    components = _components(state=InMemoryDurableStateStore())

    def fail_after_admission(_reply: object) -> object:
        raise OutboundConnectorError(
            "controlled outbound failure", may_have_sent=may_have_sent
        )

    components.outbound.send = fail_after_admission  # type: ignore[method-assign]
    result = components.receiver.receive(
        SignedInboundEvent.from_message(
            InboundMessage(
                event_type="message.received",
                session_id=TRANSPORT_SESSION,
                event_id=f"event-failure-{may_have_sent}",
                message_id=f"message-failure-{may_have_sent}",
                sender_id=OPERATOR,
                chat_id=OPERATOR,
                chat_type="direct",
                message_type="text",
                from_me=False,
                text="summarize the controlled result",
            ),
            components.config.signing_secret,
        )
    )

    assert result.disposition == expected_disposition
    attempts = components.state.list_outbound_conversation_attempts()
    assert len(attempts) == 1
    assert attempts[0].status is expected_status, result.reason
    assert attempts[0].message is None
    assert attempts[0].terminal_at is not None
    assert components.outbound.sent == []


def test_normal_outbound_confirmation_persists_gateway_message_id() -> None:
    components = _components(state=InMemoryDurableStateStore())

    def send_with_gateway_id(reply: object) -> OutboundDelivery:
        components.outbound.preflight(reply)
        components.outbound.sent.append(reply)  # type: ignore[arg-type]
        return OutboundDelivery(outbound_id="openwa-message-001", accepted=True)

    components.outbound.send = send_with_gateway_id  # type: ignore[method-assign]
    result = components.receiver.receive(
        SignedInboundEvent.from_message(
            InboundMessage(
                event_type="message.received",
                session_id=TRANSPORT_SESSION,
                event_id="event-gateway-id",
                message_id="message-gateway-id",
                sender_id=OPERATOR,
                chat_id=OPERATOR,
                chat_type="direct",
                message_type="text",
                from_me=False,
                text="summarize the controlled result",
            ),
            components.config.signing_secret,
        )
    )

    assert result.disposition == "completed"
    attempts = components.state.list_outbound_conversation_attempts()
    assert len(attempts) == 1
    assert attempts[0].status is OutboundAttemptStatus.CONFIRMED
    assert attempts[0].outbound_id == "openwa-message-001"
    assert attempts[0].message is None


def test_sqlite_terminal_transition_removes_payload_and_persists_gateway_id(
    tmp_path,
) -> None:
    state = SQLiteDurableStateStore(tmp_path / "terminal-outbound.sqlite3")
    message = _outbound_message(message_id="reply-sqlite-confirmed")
    state.reserve_outbound_conversation_message(message)
    state.mark_outbound_conversation_attempted(
        transport_session_id=message.transport_session_id,
        message_id=message.message_id,
        attempted_at=NOW + timedelta(seconds=1),
    )
    state.terminalize_outbound_conversation_attempt(
        transport_session_id=message.transport_session_id,
        message_id=message.message_id,
        status=OutboundAttemptStatus.CONFIRMED,
        terminal_at=NOW + timedelta(seconds=2),
        outbound_id="openwa-sqlite-message-001",
    )

    record = state.list_outbound_conversation_attempts()[0]
    assert record.status is OutboundAttemptStatus.CONFIRMED
    assert record.outbound_id == "openwa-sqlite-message-001"
    assert record.message is None
    assert state.list_conversation_messages()[0].message_id == message.message_id
    state.close()


@pytest.mark.parametrize("outbound_id", [None, "openwa-message-recovered"])
def test_restart_preserves_confirmed_attempt_with_or_without_gateway_id(
    outbound_id: str | None,
) -> None:
    state = InMemoryDurableStateStore()
    initial = _components(state=state)
    message = _outbound_message(message_id=f"reply-confirmed-{outbound_id}")
    state.reserve_outbound_conversation_message(message)
    state.mark_outbound_conversation_attempted(
        transport_session_id=message.transport_session_id,
        message_id=message.message_id,
        attempted_at=NOW + timedelta(seconds=1),
    )
    state.terminalize_outbound_conversation_attempt(
        transport_session_id=message.transport_session_id,
        message_id=message.message_id,
        status=OutboundAttemptStatus.CONFIRMED,
        terminal_at=NOW + timedelta(seconds=2),
        outbound_id=outbound_id,
    )

    _components(state=state, working_sessions=initial.broker.working_sessions)

    attempt = state.list_outbound_conversation_attempts()[0]
    assert attempt.status is OutboundAttemptStatus.CONFIRMED
    assert attempt.outbound_id == outbound_id
    assert attempt.message is None


def test_sqlite_restart_persists_unknown_outcome_and_removes_private_payload(
    tmp_path,
) -> None:
    database = tmp_path / "ticket12-state.sqlite3"
    state = SQLiteDurableStateStore(database)
    request = _request("request-sqlite", status="replying", phase="outbound")
    message = _outbound_message(message_id="reply-sqlite")
    state.save_request(request)
    state.reserve_outbound_conversation_message(message)
    state.mark_outbound_conversation_attempted(
        transport_session_id=message.transport_session_id,
        message_id=message.message_id,
        attempted_at=NOW + timedelta(seconds=1),
    )
    state.close()

    reopened = SQLiteDurableStateStore(database)
    restarted = _components(state=reopened)

    record = reopened.list_outbound_conversation_attempts()[-1]
    assert record.status is OutboundAttemptStatus.UNKNOWN
    assert record.message is None
    interrupted = reopened.get_request(request.request_id)
    assert interrupted is not None
    assert interrupted.status == "interrupted"
    assert restarted.outbound.sent == []

    restarted_again = _components(
        state=reopened,
        working_sessions=restarted.broker.working_sessions,
    )
    repeated = reopened.list_outbound_conversation_attempts()[-1]
    assert repeated == record
    restart_records = [
        item for item in restarted_again.audit.records if item.kind == "service_restart"
    ]
    assert restart_records[-1].details == {
        "interrupted_requests": "0",
        "outbound_not_started": "0",
        "outbound_unknown": "0",
    }
    reopened.close()


def test_signed_request_records_gateway_confirmation_before_completion() -> None:
    state = InMemoryDurableStateStore()
    components = _components(state=state)

    result = components.receiver.receive(
        SignedInboundEvent.from_message(
            InboundMessage(
                event_type="message.received",
                session_id=TRANSPORT_SESSION,
                event_id="event-confirmed",
                message_id="message-confirmed",
                sender_id=OPERATOR,
                chat_id=OPERATOR,
                chat_type="direct",
                message_type="text",
                from_me=False,
                text="summarize the controlled result",
            ),
            components.config.signing_secret,
        )
    )

    assert result.disposition == "completed"
    attempts = state.list_outbound_conversation_attempts()
    assert len(attempts) == 1
    assert attempts[0].status is OutboundAttemptStatus.CONFIRMED
    assert attempts[0].message is None


def test_pre_ticket12_sqlite_outbox_is_migrated_unknown_without_delivery(
    tmp_path,
) -> None:
    database = tmp_path / "legacy-ticket20-state.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE outbound_conversation_outbox (
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
        """
    )
    connection.execute(
        """
        INSERT INTO outbound_conversation_outbox(
            transport_session_id, message_id, working_session_id, event_id,
            chat_id, sender_id, text, occurred_at, request_id, credential_like
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            TRANSPORT_SESSION,
            "legacy-reply",
            "legacy-working-session",
            "legacy-event",
            OPERATOR,
            "jarvis",
            "legacy private outbound body",
            NOW.isoformat(),
            "legacy-request",
            0,
        ),
    )
    connection.commit()
    connection.close()

    state = SQLiteDurableStateStore(database)
    restarted = _components(state=state)

    attempts = state.list_outbound_conversation_attempts()
    assert len(attempts) == 1
    assert attempts[0].status is OutboundAttemptStatus.UNKNOWN
    assert attempts[0].message is None
    assert restarted.outbound.sent == []
    state.close()


def _components(
    *,
    state: object,
    audit: object | None = None,
    working_sessions: object | None = None,
):
    return build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=b"ticket12-recovery-secret",
        now=NOW,
        id_prefix="ticket12",
        state=state,
        audit=audit,
        working_sessions=working_sessions,
    )


def _request(
    request_id: str,
    *,
    status: str,
    phase: str,
    outcome: str | None = None,
) -> RequestState:
    return RequestState(
        request_id=request_id,
        event_id=f"event-{request_id}",
        message_id=f"message-{request_id}",
        operator_id=OPERATOR,
        session_id=TRANSPORT_SESSION,
        chat_id=OPERATOR,
        created_at=NOW,
        updated_at=NOW,
        status=status,
        phase=phase,
        outcome=outcome,
    )


def _outbound_message(*, message_id: str) -> ConversationMessage:
    return ConversationMessage(
        working_session_id="working-session",
        transport_session_id=TRANSPORT_SESSION,
        message_id=message_id,
        event_id=f"event-{message_id}",
        chat_id=OPERATOR,
        sender_id="jarvis",
        text="bounded reply body",
        occurred_at=NOW,
        direction="outbound",
        request_id=f"request-{message_id}",
    )

"""Ticket 12 outbound-attempt recovery tests."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from jarvis_control_plane import (
    InboundMessage,
    InMemoryDurableStateStore,
    OutboundAttemptStatus,
    SignedInboundEvent,
    SQLiteDurableStateStore,
)
from jarvis_control_plane.models import OutboundDelivery
from jarvis_control_plane.ports import OutboundConnectorError

from .helpers import (
    NOW,
    OPERATOR,
    TRANSPORT_SESSION,
    _components,
    _outbound_message,
    _request,
)


@pytest.mark.parametrize(
    ("may_have_sent", "expected_status", "expected_disposition"),
    [
        (False, OutboundAttemptStatus.UNKNOWN, "failed"),
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


@pytest.mark.parametrize(
    "message_text",
    ["summarize the controlled result", "/status"],
    ids=["ordinary", "control"],
)
@pytest.mark.parametrize(
    "delivery",
    [None, SimpleNamespace(accepted=True, outbound_id="duck-typed-id")],
    ids=["missing-delivery", "invalid-delivery-object"],
)
def test_invalid_outbound_delivery_is_unknown_and_not_retried(
    message_text: str,
    delivery: object,
) -> None:
    components = _components(state=InMemoryDurableStateStore())
    send_calls: list[object] = []

    def send_with_invalid_delivery(reply: object) -> object:
        components.outbound.preflight(reply)
        send_calls.append(reply)
        components.outbound.sent.append(reply)  # type: ignore[arg-type]
        return delivery

    components.outbound.send = send_with_invalid_delivery  # type: ignore[method-assign]
    result = components.receiver.receive(
        SignedInboundEvent.from_message(
            InboundMessage(
                event_type="message.received",
                session_id=TRANSPORT_SESSION,
                event_id=f"event-invalid-delivery-{len(send_calls)}",
                message_id=f"message-invalid-delivery-{len(send_calls)}",
                sender_id=OPERATOR,
                chat_id=OPERATOR,
                chat_type="direct",
                message_type="text",
                from_me=False,
                text=message_text,
            ),
            components.config.signing_secret,
        )
    )

    assert result.disposition == "unknown", result.reason
    assert len(send_calls) == 1
    assert components.state.outbound_outbox == {}
    assert components.state.search_conversation_messages(direction="outbound") == ()
    attempts = components.state.list_outbound_conversation_attempts()
    assert len(attempts) == 1
    assert attempts[0].status is OutboundAttemptStatus.UNKNOWN
    assert attempts[0].message is None
    assert attempts[0].outbound_id is None


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

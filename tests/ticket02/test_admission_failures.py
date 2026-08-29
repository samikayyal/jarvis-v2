# ruff: noqa: F401, I001, RUF100 -- split modules retain ticket context.
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest
from test_support import build_receiver_components

from jarvis_control_plane import (
    ControlledOrchestrationAdapter,
    ControlledOutboundConnector,
    ControlPlaneConfig,
    DeterministicIdGenerator,
    DiagnosticTraceRecorder,
    FixedClock,
    InboundMessage,
    InMemoryAuditBoundary,
    InMemoryDiagnosticTraceStore,
    InMemoryDurableStateStore,
    SignedInboundEvent,
    SignedMessageReceiver,
    SQLiteAuditBoundary,
    SQLiteDurableStateStore,
    sign_body,
)

SECRET = b"ticket02-test-secret"
OPERATOR = "operator.test"
SESSION = "session.test"
NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
_TRACE_STORE: InMemoryDiagnosticTraceStore | None = None
_TRACE_WRITER: object | None = None


def make_trace(
    clock: FixedClock, ids: DeterministicIdGenerator
) -> DiagnosticTraceRecorder:
    global _TRACE_STORE, _TRACE_WRITER
    if _TRACE_WRITER is None:
        _TRACE_STORE = InMemoryDiagnosticTraceStore()
        _TRACE_WRITER = _TRACE_STORE.writer()
    return DiagnosticTraceRecorder(writer=_TRACE_WRITER, clock=clock, ids=ids)


@pytest.fixture(scope="module", autouse=True)
def close_module_trace_store():
    global _TRACE_STORE, _TRACE_WRITER
    yield
    if _TRACE_STORE is not None:
        _TRACE_STORE._close_writer_service()
    _TRACE_WRITER = None
    _TRACE_STORE = None


def make_message(**changes: object) -> InboundMessage:
    values: dict[str, object] = {
        "event_type": "message.received",
        "session_id": SESSION,
        "event_id": "event-001",
        "message_id": "message-001",
        "sender_id": OPERATOR,
        "chat_id": OPERATOR,
        "chat_type": "direct",
        "message_type": "text",
        "from_me": False,
        "text": "Retain this exact authorized text",
    }
    values.update(changes)
    return InboundMessage(**values)  # type: ignore[arg-type]


def make_components(
    *,
    state: object | None = None,
    audit: object | None = None,
    working_session_id: str | None = None,
    ids: DeterministicIdGenerator | None = None,
) -> tuple[
    ControlPlaneConfig,
    object,
    object,
    ControlledOrchestrationAdapter,
    ControlledOutboundConnector,
    SignedMessageReceiver,
]:
    clock = FixedClock(NOW)
    ids = ids or DeterministicIdGenerator("ticket02")
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=SESSION,
        signing_secret=SECRET,
        now=NOW,
        id_prefix="ticket02",
        state=state,  # type: ignore[arg-type]
        audit=audit,  # type: ignore[arg-type]
        working_session_id=working_session_id,
        clock=clock,
        ids=ids,
        trace=make_trace(clock, ids),
    )
    return (
        components.config,
        components.state,
        components.audit,
        components.orchestration,
        components.outbound,
        components.receiver,
    )


def make_event(config: ControlPlaneConfig, **changes: object) -> SignedInboundEvent:
    return SignedInboundEvent.from_message(
        make_message(**changes), config.signing_secret
    )


def make_raw_event(config: ControlPlaneConfig, body: bytes) -> SignedInboundEvent:
    return SignedInboundEvent(
        raw_body=body,
        signature=sign_body(body, config.signing_secret),
    )


def test_ingress_claim_and_history_are_atomic_when_state_write_fails() -> None:
    state = InMemoryDurableStateStore()
    state.fail_conversation = True
    config, _, audit, orchestration, outbound, receiver = make_components(state=state)

    result = receiver.receive(make_event(config))

    assert result.status_code == 503
    assert result.disposition == "state_unavailable"
    assert state.list_ingress_claims() == ()
    assert state.list_conversation_messages() == ()
    assert state.list_requests() == ()
    assert audit.records == []
    assert orchestration.calls == []
    assert outbound.sent == []

    state.fail_conversation = False
    retry = receiver.receive(make_event(config))

    assert retry.status_code == 202
    assert retry.disposition == "completed"
    assert len(state.list_ingress_claims()) == 1
    assert state.list_ingress_claims()[0].disposition == "dispatched"
    assert len(state.list_conversation_messages()) == 2
    assert len(state.list_requests()) == 1
    assert len(orchestration.calls) == 1
    assert len(outbound.sent) == 1


def test_audit_unavailable_keeps_claimed_history_but_blocks_assistant_work() -> None:
    state = InMemoryDurableStateStore()
    audit = InMemoryAuditBoundary(fail_on_append=1)
    config, _, _, orchestration, outbound, receiver = make_components(
        state=state,
        audit=audit,
    )

    result = receiver.receive(make_event(config))

    assert result.status_code == 202
    assert result.disposition == "audit_blocked"
    assert state.list_ingress_claims()
    assert len(state.list_conversation_messages()) == 1
    assert state.list_requests() == ()
    assert audit.records == []
    assert orchestration.calls == []
    assert outbound.sent == []


def test_audit_blocked_disposition_survives_sqlite_reconstruction_and_replay(
    tmp_path,
) -> None:
    database = tmp_path / "ticket02-audit-blocked.sqlite3"
    state = SQLiteDurableStateStore(database)
    audit = InMemoryAuditBoundary(fail_on_append=1)
    config, _, _, orchestration, outbound, receiver = make_components(
        state=state,
        audit=audit,
    )
    event = make_event(config)

    try:
        result = receiver.receive(event)
    finally:
        state.close()

    assert result.status_code == 202
    assert result.disposition == "audit_blocked"
    assert orchestration.calls == []
    assert outbound.sent == []

    reconstructed_state = SQLiteDurableStateStore(database)
    reconstructed_audit = InMemoryAuditBoundary()
    try:
        claims = reconstructed_state.list_ingress_claims()
        history = reconstructed_state.list_conversation_messages()
        assert len(claims) == 1
        assert claims[0].disposition == "audit_blocked"
        assert len(history) == 1
        assert reconstructed_state.list_requests() == ()

        _, _, _, replay_orchestration, replay_outbound, replay_receiver = (
            make_components(
                state=reconstructed_state,
                audit=reconstructed_audit,
            )
        )
        replay = replay_receiver.receive(event)

        assert replay.status_code == 204
        assert replay.disposition == "duplicate"
        assert replay_orchestration.calls == []
        assert replay_outbound.sent == []
    finally:
        reconstructed_state.close()


def test_audit_blocked_update_failure_keeps_claim_non_eligible() -> None:
    state = InMemoryDurableStateStore()
    state.fail_update = True
    audit = InMemoryAuditBoundary(fail_on_append=1)
    config, _, _, orchestration, outbound, receiver = make_components(
        state=state,
        audit=audit,
    )

    result = receiver.receive(make_event(config))

    assert result.status_code == 503
    assert result.disposition == "state_unavailable"
    assert state.list_ingress_claims() == ()
    assert state.list_conversation_messages() == ()
    assert state.list_requests() == ()
    assert audit.records == []
    assert orchestration.calls == []
    assert outbound.sent == []
    state.fail_update = False
    replay = receiver.receive(make_event(config))
    assert replay.status_code == 202
    assert replay.disposition == "audit_blocked"
    assert state.list_ingress_claims()[0].disposition == "audit_blocked"
    assert len(state.list_conversation_messages()) == 1
    assert orchestration.calls == []
    assert outbound.sent == []

    final_replay = receiver.receive(make_event(config))
    assert final_replay.status_code == 204
    assert final_replay.disposition == "duplicate"


def test_admitted_update_failure_keeps_claim_non_eligible() -> None:
    state = InMemoryDurableStateStore()
    state.fail_update = True
    config, _, audit, orchestration, outbound, receiver = make_components(state=state)

    result = receiver.receive(make_event(config))

    assert result.status_code == 503
    assert result.disposition == "state_unavailable"
    assert state.list_ingress_claims() == ()
    assert state.list_conversation_messages() == ()
    assert state.list_requests() == ()
    assert audit.records == []
    assert orchestration.calls == []
    assert outbound.sent == []

    state.fail_update = False
    replay = receiver.receive(make_event(config))
    assert replay.status_code == 202
    assert replay.disposition == "completed"
    assert len(state.list_ingress_claims()) == 1
    assert state.list_ingress_claims()[0].disposition == "dispatched"
    assert len(state.list_conversation_messages()) == 2
    assert len(state.list_requests()) == 1
    assert len(orchestration.calls) == 1
    assert len(outbound.sent) == 1


def test_claim_write_failure_returns_503_and_retry_processes_once() -> None:
    state = InMemoryDurableStateStore()
    state.fail_claim = True
    config, _, audit, orchestration, outbound, receiver = make_components(state=state)

    first = receiver.receive(make_event(config))

    assert first.status_code == 503
    assert first.disposition == "state_unavailable"
    assert state.list_ingress_claims() == ()
    assert state.list_conversation_messages() == ()
    assert audit.records == []

    state.fail_claim = False
    retry = receiver.receive(make_event(config))

    assert retry.status_code == 202
    assert retry.disposition == "completed"
    assert len(state.list_ingress_claims()) == 1
    assert state.list_ingress_claims()[0].disposition == "dispatched"
    assert len(state.list_conversation_messages()) == 2
    assert len(state.list_requests()) == 1
    assert len(orchestration.calls) == 1
    assert len(outbound.sent) == 1


def test_rejected_event_claim_failure_returns_503_for_gateway_retry() -> None:
    state = InMemoryDurableStateStore()
    state.fail_claim = True
    config, _, audit, orchestration, outbound, receiver = make_components(state=state)

    result = receiver.receive(make_event(config, sender_id="other.operator"))

    assert result.status_code == 503
    assert result.disposition == "state_unavailable"
    assert state.list_ingress_claims() == ()
    assert state.list_conversation_messages() == ()
    assert audit.records == []
    assert orchestration.calls == []
    assert outbound.sent == []


def test_oversized_raw_body_is_413_without_signature_or_state_work() -> None:
    _, state, audit, orchestration, outbound, receiver = make_components()
    event = SignedInboundEvent(raw_body=b"x" * (128 * 1024 + 1))

    result = receiver.receive(event)

    assert result.status_code == 413
    assert result.disposition == "payload_too_large"
    assert state.list_ingress_claims() == ()
    assert state.list_conversation_messages() == ()
    assert audit.records == []
    assert orchestration.calls == []
    assert outbound.sent == []


def test_text_limit_is_fixed_at_4096_characters() -> None:
    with pytest.raises(ValueError, match="fixed at 4096"):
        ControlPlaneConfig(
            operator_id=OPERATOR,
            session_id=SESSION,
            signing_secret=SECRET,
            max_text_length=4097,
        )


def test_rejected_event_is_safely_discarded_when_audit_is_unavailable() -> None:
    audit = InMemoryAuditBoundary(fail_on_append=1)
    config, state, _, orchestration, outbound, receiver = make_components(audit=audit)

    result = receiver.receive(make_event(config, sender_id="other.operator"))

    assert result.status_code == 204
    assert result.disposition == "rejected"
    assert result.reason == "unauthorized_operator"
    assert state.list_ingress_claims() == ()
    assert state.list_conversation_messages() == ()
    assert audit.records == []
    assert orchestration.calls == []
    assert outbound.sent == []


def test_history_separates_working_session_from_transport_session() -> None:
    state = InMemoryDurableStateStore()
    ids = DeterministicIdGenerator("ticket02-sessions")
    audit_one = InMemoryAuditBoundary()
    config_one, _, _, _, _, receiver_one = make_components(
        state=state,
        audit=audit_one,
        working_session_id="working-session-001",
        ids=ids,
    )
    first = receiver_one.receive(make_event(config_one, message_id="message-001"))

    audit_two = InMemoryAuditBoundary()
    config_two, _, _, _, _, receiver_two = make_components(
        state=state,
        audit=audit_two,
        working_session_id="working-session-002",
        ids=ids,
    )
    second = receiver_two.receive(
        make_event(
            config_two,
            event_id="event-002",
            message_id="message-002",
            text="A new working session under the same transport session",
        )
    )

    assert first.disposition == "completed"
    assert second.disposition == "completed"
    history = state.list_conversation_messages()
    assert [message.working_session_id for message in history] == [
        "working-session-001",
        "working-session-002",
        "working-session-001",
        "working-session-002",
    ]
    assert [message.transport_session_id for message in history] == [
        SESSION,
        SESSION,
        SESSION,
        SESSION,
    ]
    assert [message.session_id for message in history] == [
        SESSION,
        SESSION,
        SESSION,
        SESSION,
    ]


def test_invalid_signature_is_rejected_before_malformed_body_parsing() -> None:
    _, state, audit, orchestration, outbound, receiver = make_components()
    body = b"not-json"
    event = SignedInboundEvent(raw_body=body, signature="0" * 64)

    result = receiver.receive(event)

    assert result.status_code == 401
    assert result.disposition == "unauthenticated"
    assert state.list_ingress_claims() == ()
    assert state.list_conversation_messages() == ()
    assert state.list_requests() == ()
    assert audit.records == []
    assert orchestration.calls == []
    assert outbound.sent == []


def test_missing_signature_is_rejected_before_parsing() -> None:
    _, state, audit, orchestration, outbound, receiver = make_components()
    event = SignedInboundEvent(raw_body=b"not-json")

    result = receiver.receive(event)

    assert result.status_code == 401
    assert result.disposition == "unauthenticated"
    assert state.list_ingress_claims() == ()
    assert state.list_conversation_messages() == ()
    assert state.list_requests() == ()
    assert audit.records == []
    assert orchestration.calls == []
    assert outbound.sent == []

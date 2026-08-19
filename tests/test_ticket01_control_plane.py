from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from test_support import build_receiver_components

from jarvis_control_plane import (
    AuditEvidence,
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
    OutboundAttemptStatus,
    OutboundConnectorError,
    OutboundReply,
    SignedInboundEvent,
    SignedMessageReceiver,
    SQLiteAuditBoundary,
    SQLiteDurableStateStore,
)

SECRET = b"ticket01-test-secret"
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
        "text": "Check the controlled path",
    }
    values.update(changes)
    return InboundMessage(**values)  # type: ignore[arg-type]


def make_event(config: ControlPlaneConfig, **changes: object) -> SignedInboundEvent:
    return SignedInboundEvent.from_message(
        make_message(**changes), config.signing_secret
    )


def make_components(
    *,
    state: object | None = None,
    audit: object | None = None,
    orchestration: ControlledOrchestrationAdapter | None = None,
    clock: FixedClock | None = None,
    ids: DeterministicIdGenerator | None = None,
) -> tuple[
    ControlPlaneConfig,
    object,
    object,
    ControlledOrchestrationAdapter,
    ControlledOutboundConnector,
    SignedMessageReceiver,
]:
    clock = clock or FixedClock(NOW)
    ids = ids or DeterministicIdGenerator("seam")
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=SESSION,
        signing_secret=SECRET,
        now=NOW,
        id_prefix="seam",
        state=state,  # type: ignore[arg-type]
        audit=audit,  # type: ignore[arg-type]
        clock=clock,
        ids=ids,
        orchestration=orchestration,
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


def test_primary_seam_persists_state_and_audit_and_sends_one_correlated_reply() -> None:
    connection = sqlite3.connect(":memory:")
    state = SQLiteDurableStateStore(connection)
    audit = SQLiteAuditBoundary(connection)
    config, _, _, orchestration, outbound, receiver = make_components(
        state=state,
        audit=audit,
    )

    result = receiver.receive(make_event(config))

    assert result.status_code == 202
    assert result.disposition == "completed"
    assert result.reply is not None
    assert result.request is not None
    assert result.request.status == "completed"
    assert result.request_id in result.reply.body
    assert len(outbound.sent) == 1
    assert outbound.sent[0] == result.reply
    assert len(orchestration.calls) == 1
    assert orchestration.calls[0].text == "Check the controlled path"

    requests = state.list_requests()
    assert len(requests) == 1
    assert requests[0].request_id == result.request_id
    assert requests[0].reply_id == result.reply.reply_id
    assert len(state.list_ingress_claims()) == 1

    records = audit.records
    assert {record.kind for record in records} == {
        "inbound_admitted",
        "request_accepted",
        "orchestration_result",
        "outbound_attempt",
        "outbound_result",
        "outbound_completion",
        "request_lifecycle",
    }
    assert all(record.redacted for record in records)
    assert all(
        "Check the controlled path" not in str(record.details) for record in records
    )
    assert all(SECRET.decode() not in str(record.details) for record in records)
    assert sum(record.kind == "outbound_result" for record in records) == 2


def test_replay_is_deduplicated_before_broker_and_outbound() -> None:
    config, state, audit, orchestration, outbound, receiver = make_components()
    event = make_event(config)

    first = receiver.receive(event)
    audit_count_after_first = len(audit.records)
    second = receiver.receive(event)

    assert first.reply is not None
    assert second.status_code == 204
    assert second.disposition == "duplicate"
    assert second.reply is None
    assert len(state.list_requests()) == 1
    assert len(orchestration.calls) == 1
    assert len(outbound.sent) == 1
    assert len(audit.records) == audit_count_after_first


def test_invalid_signature_never_enters_state_or_audit() -> None:
    config, state, audit, _, outbound, receiver = make_components()
    event = make_event(config)
    tampered = replace(event, signature="0" * 64)

    result = receiver.receive(tampered)

    assert result.status_code == 401
    assert result.disposition == "unauthenticated"
    assert state.list_ingress_claims() == ()
    assert state.list_requests() == ()
    assert audit.records == []
    assert outbound.sent == []


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("sender_id", "another.operator", "unauthorized_operator"),
        ("chat_type", "group", "not_direct_message"),
        ("message_type", "image", "unsupported_message_type"),
        ("from_me", True, "self_message"),
        ("text", "   ", "blank_text"),
    ],
)
def test_signed_but_non_admissible_events_are_rejected_without_work(
    field: str,
    value: object,
    reason: str,
) -> None:
    config, state, audit, orchestration, outbound, receiver = make_components()

    result = receiver.receive(make_event(config, **{field: value}))

    assert result.status_code == 204
    assert result.disposition == "rejected"
    assert result.reason == reason
    assert len(state.list_ingress_claims()) == 1
    assert state.list_ingress_claims()[0].disposition == "rejected"
    assert state.list_requests() == ()
    assert orchestration.calls == []
    assert outbound.sent == []
    assert len(audit.records) == 1
    assert audit.records[0].kind == "inbound_rejected"
    assert audit.records[0].details == {"reason": reason}


def test_audit_failure_at_outbound_gate_blocks_the_controlled_send() -> None:
    audit = InMemoryAuditBoundary(fail_on_append=4)
    config, state, _, orchestration, outbound, receiver = make_components(audit=audit)

    result = receiver.receive(make_event(config))

    assert result.status_code == 202
    assert result.disposition == "failed"
    assert result.reply is None
    assert result.request is not None
    assert result.request.status == "accepted"
    assert result.request.error_code is None
    assert len(orchestration.calls) == 1
    assert outbound.sent == []
    assert len(state.list_requests()) == 1
    assert [record.kind for record in audit.records] == [
        "inbound_admitted",
        "request_accepted",
        "orchestration_result",
    ]


def test_state_failure_at_replying_gate_does_not_send_and_is_not_claimed_complete() -> (
    None
):
    state = InMemoryDurableStateStore()

    def fail_replying_transition(_: object) -> str:
        state.fail_update = True
        return "Controlled orchestration completed the request."

    orchestration = ControlledOrchestrationAdapter(
        response_factory=fail_replying_transition
    )
    config, _, audit, orchestration, outbound, receiver = make_components(
        state=state,
        orchestration=orchestration,
    )

    result = receiver.receive(make_event(config))

    assert result.status_code == 202
    assert result.disposition == "failed"
    assert result.reply is None
    assert result.request is not None
    assert result.request.status == "accepted"
    assert result.request.outcome is None
    assert result.reason is not None
    assert "outbound was not sent" in result.reason
    assert state.list_requests() == (result.request,)
    assert len(orchestration.calls) == 1
    assert outbound.sent == []
    assert not any(record.kind == "outbound_attempt" for record in audit.records)
    assert any(
        record.kind == "outbound_completion" and record.outcome == "not_sent"
        for record in audit.records
    )


def test_state_failure_after_outbound_acceptance_returns_unknown_without_durable_completion() -> (
    None
):
    state = InMemoryDurableStateStore()
    config, _, _, _orchestration, outbound, receiver = make_components(state=state)
    original_send = outbound.send

    def send_then_fail_completion(reply: OutboundReply) -> None:
        original_send(reply)
        state.fail_update = True

    outbound.send = send_then_fail_completion  # type: ignore[method-assign]

    result = receiver.receive(make_event(config))

    assert result.status_code == 202
    assert result.disposition == "unknown"
    assert result.reply is not None
    assert result.request is not None
    assert result.request.status == "replying"
    assert result.reason is not None
    assert "durable completion state" in result.reason
    assert outbound.sent == [result.reply]

    durable_request = state.list_requests()
    assert len(durable_request) == 1
    assert durable_request[0] == result.request
    assert durable_request[0].status == "replying"
    assert durable_request[0].reply_id == result.reply.reply_id
    assert durable_request[0].outcome == "replying"
    assert all(request.status != "completed" for request in durable_request)


def test_outbound_audit_batch_failure_blocks_before_send() -> None:
    audit = InMemoryAuditBoundary(fail_on_append=5)
    config, state, _, _, outbound, receiver = make_components(audit=audit)

    result = receiver.receive(make_event(config))

    assert result.status_code == 202
    assert result.disposition == "failed"
    assert result.reply is None
    assert result.request is not None
    assert result.request.status == "replying"
    assert result.request.outcome == "replying"
    assert outbound.sent == []
    assert state.list_requests() == (result.request,)


def test_orchestration_failure_is_durable_and_sends_sanitized_outbound_reply() -> None:
    orchestration = ControlledOrchestrationAdapter(failure="controlled planner failure")
    config, state, audit, _, outbound, receiver = make_components(
        orchestration=orchestration,
    )

    result = receiver.receive(make_event(config))

    assert result.status_code == 202
    assert result.disposition == "failed"
    assert result.reply is not None
    assert len(outbound.sent) == 1
    assert "controlled planner failure" not in result.reply.body
    assert "controlled planner failure" not in outbound.sent[0].body
    assert "could not complete" in result.reply.body
    assert result.request is not None
    assert result.request.status == "failed"
    assert result.request.outcome == "orchestration_failed"
    assert len(state.list_requests()) == 1
    assert any(
        record.kind == "orchestration_result" and record.outcome == "failed"
        for record in audit.records
    )


def test_ambiguous_orchestration_failure_reply_is_durable_and_not_retried() -> None:
    class AmbiguousOutbound(ControlledOutboundConnector):
        def send(self, reply: OutboundReply):
            self.sent.append(reply)
            raise OutboundConnectorError(
                "gateway outcome was unknown", may_have_sent=True
            )

    orchestration = ControlledOrchestrationAdapter(failure="private planner cause")
    config, state, audit, _, outbound, receiver = make_components(
        orchestration=orchestration,
    )
    ambiguous = AmbiguousOutbound(
        operator_id=config.operator_id,
        session_id=config.session_id,
        audit=audit,
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("ambiguous"),
    )
    receiver.broker.outbound = ambiguous

    result = receiver.receive(make_event(config))

    assert result.disposition == "unknown"
    assert result.request is not None
    assert result.request.status == "failed"
    assert result.reply is not None
    assert "private planner cause" not in result.reply.body
    assert ambiguous.sent == [result.reply]
    attempts = state.list_outbound_conversation_attempts()
    assert len(attempts) == 1
    assert attempts[0].status == OutboundAttemptStatus.UNKNOWN
    assert outbound.sent == []


def test_file_backed_sqlite_reconstructs_state_audit_and_replay(tmp_path) -> None:
    database = tmp_path / "ticket01.sqlite3"
    connection = sqlite3.connect(database)
    first_state = SQLiteDurableStateStore(connection)
    first_audit = SQLiteAuditBoundary(connection)
    config, _, _, first_orchestration, first_outbound, first_receiver = make_components(
        state=first_state,
        audit=first_audit,
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("first"),
    )
    event = make_event(config)

    try:
        first = first_receiver.receive(event)
    finally:
        first_state.close()
        first_audit.close()
        connection.close()

    assert first.status_code == 202
    assert first.disposition == "completed"
    assert first.request is not None
    assert first.reply is not None
    assert len(first_orchestration.calls) == 1
    assert first_outbound.sent == [first.reply]

    reconstructed_state = SQLiteDurableStateStore(database)
    reconstructed_audit = SQLiteAuditBoundary(database)
    try:
        _, _, _, replay_orchestration, replay_outbound, replay_receiver = (
            make_components(
                state=reconstructed_state,
                audit=reconstructed_audit,
                clock=FixedClock(NOW),
                ids=DeterministicIdGenerator("replay"),
            )
        )

        requests = reconstructed_state.list_requests()
        claims = reconstructed_state.list_ingress_claims()
        records = reconstructed_audit.records

        assert len(requests) == 1
        assert requests[0].request_id == first.request_id
        assert requests[0].status == "completed"
        assert requests[0].reply_id == first.reply.reply_id
        assert len(claims) == 1
        assert claims[0].session_id == SESSION
        assert claims[0].message_id == "message-001"
        assert claims[0].event_id == "event-001"
        assert records
        assert all(record.redacted for record in records)
        assert all(
            "Check the controlled path" not in str(record.details) for record in records
        )
        assert all(SECRET.decode() not in str(record.details) for record in records)

        replay = replay_receiver.receive(event)

        assert replay.status_code == 204
        assert replay.disposition == "duplicate"
        assert replay.reply is None
        assert len(reconstructed_state.list_requests()) == 1
        assert len(reconstructed_state.list_ingress_claims()) == 1
        assert replay_orchestration.calls == []
        assert replay_outbound.sent == []
    finally:
        reconstructed_state.close()
        reconstructed_audit.close()


def test_state_failure_returns_service_unavailable_before_broker() -> None:
    state = InMemoryDurableStateStore()
    state.fail_claim = True
    config, _, audit, orchestration, outbound, receiver = make_components(state=state)

    result = receiver.receive(make_event(config))

    assert result.status_code == 503
    assert result.disposition == "state_unavailable"
    assert state.list_requests() == ()
    assert audit.records == []
    assert orchestration.calls == []
    assert outbound.sent == []


def test_audit_evidence_rejects_raw_content_fields() -> None:
    with pytest.raises(ValueError, match="forbidden raw-content"):
        AuditEvidence(
            evidence_id="audit-1",
            kind="invalid",
            occurred_at=NOW,
            details={"text": "do not retain this"},
        )

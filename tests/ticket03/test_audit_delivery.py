# ruff: noqa: F401, I001, RUF100 -- split modules retain ticket context.
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from jarvis_control_plane import (
    AuditEvidence,
    AuditFilter,
    AuditWriteError,
    ControlledOrchestrationAdapter,
    ControlPlaneConfig,
    DeterministicCapabilityBroker,
    DeterministicIdGenerator,
    DiagnosticTraceRecorder,
    FixedClock,
    FixedModelAvailabilityProvider,
    InboundMessage,
    InMemoryAuditBoundary,
    InMemoryDiagnosticTraceStore,
    InMemoryDurableStateStore,
    ModelAvailability,
    OutboundConnectorError,
    SignedInboundEvent,
    SignedMessageReceiver,
    SQLiteAuditBoundary,
)
from jarvis_control_plane.models import OutboundDelivery

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
SECRET = b"ticket03-test-secret"
OPERATOR = "operator.test"
SESSION = "session.test"
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


def make_evidence(
    evidence_id: str,
    occurred_at: datetime,
    **changes: object,
) -> AuditEvidence:
    values: dict[str, object] = {
        "evidence_id": evidence_id,
        "kind": "action_outcome",
        "occurred_at": occurred_at,
        "event_id": "event-001",
        "request_id": "request-001",
        "message_id": "message-001",
        "operation_type": "gmail_send",
        "target_category": "google_gmail",
        "approval_decision": "approved",
        "policy_decision": "allowed",
        "execution_status": "accepted",
        "outcome": "accepted",
        "actor": "capability_broker",
        "details": {"channel": "controlled", "command": "--token=not-retained"},
    }
    values.update(changes)
    return AuditEvidence(**values)  # type: ignore[arg-type]


def make_event(
    config: ControlPlaneConfig,
    *,
    event_id: str = "event-001",
    message_id: str = "message-001",
) -> SignedInboundEvent:
    message = InboundMessage(
        event_type="message.received",
        session_id=SESSION,
        event_id=event_id,
        message_id=message_id,
        sender_id=OPERATOR,
        chat_id=OPERATOR,
        chat_type="direct",
        message_type="text",
        from_me=False,
        text="run the controlled request",
    )
    return SignedInboundEvent.from_message(message, config.signing_secret)


def test_safe_filter_dimensions_and_export_are_deterministic() -> None:
    audit = InMemoryAuditBoundary()
    first = make_evidence("audit-001", NOW)
    second = make_evidence(
        "audit-002",
        NOW + timedelta(days=1),
        request_id="request-002",
        operation_type="calendar_update",
        target_category="google_calendar",
        approval_decision="rejected",
        policy_decision="denied",
        execution_status="not_started",
        outcome="rejected",
    )
    audit.append(first)
    audit.append(second)

    query = AuditFilter(
        start_at=NOW,
        end_at=NOW + timedelta(hours=1),
        request_id="request-001",
        operation_type="gmail_send",
        target_category="google_gmail",
        approval_decision="approved",
        policy_decision="allowed",
        execution_status="accepted",
        outcome="accepted",
    )

    assert audit.safe_view(query) == (first,)
    assert audit.inspect(on_date=date(2026, 8, 5)) == (second,)

    exported = json.loads(audit.export_json(query))
    assert exported == [first.as_safe_mapping()]
    assert "not-retained" not in audit.export_json()
    assert exported[0]["details"]["command"] == "[redacted]"
    assert exported[0]["redacted"] is True


def test_in_memory_audit_snapshot_and_duplicate_ids_are_append_only() -> None:
    audit = InMemoryAuditBoundary()
    evidence = make_evidence("audit-001", NOW)
    audit.append(evidence)

    with pytest.raises(AttributeError):
        audit.records.clear()  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        audit.records[0].details["channel"] = "changed"  # type: ignore[index]
    with pytest.raises(AuditWriteError, match="duplicate"):
        audit.append(evidence)

    assert audit.records == [evidence]


def test_sqlite_audit_rejects_update_delete_and_round_trips_safe_fields() -> None:
    connection = sqlite3.connect(":memory:")
    audit = SQLiteAuditBoundary(connection)
    evidence = make_evidence("audit-001", NOW)
    audit.append(evidence)

    with pytest.raises(sqlite3.DatabaseError):
        connection.execute("UPDATE audit_evidence SET outcome = 'tampered'")
    with pytest.raises(sqlite3.DatabaseError):
        connection.execute("DELETE FROM audit_evidence")

    assert audit.records == (evidence,)
    assert audit.safe_view(request_id="request-001") == (evidence,)
    assert json.loads(audit.export(request_id="request-001")) == [
        evidence.as_safe_mapping()
    ]
    audit.close()


def test_unsafe_audit_content_is_rejected_or_redacted_and_bounded() -> None:
    with pytest.raises(ValueError, match="forbidden raw-content"):
        AuditEvidence(
            evidence_id="audit-001",
            kind="invalid",
            occurred_at=NOW,
            details={"text": "operator message body"},
        )
    with pytest.raises(ValueError, match="unsupported detail field"):
        AuditEvidence(
            evidence_id="audit-002",
            kind="invalid",
            occurred_at=NOW,
            details={"nested": {"secret": "value"}},  # type: ignore[dict-item]
        )
    with pytest.raises(ValueError, match="too long"):
        AuditEvidence(
            evidence_id="audit-003",
            kind="invalid",
            occurred_at=NOW,
            details={"reason": "x" * 257},
        )

    redacted = make_evidence(
        "audit-004",
        NOW,
        details={"reason": "authorization=super-secret-value"},
    )
    assert redacted.details == {"reason": "[redacted]"}


def test_audit_event_top_level_fields_cannot_contradict_details() -> None:
    with pytest.raises(ValueError, match="does not match|contradictory"):
        AuditEvidence(
            evidence_id="audit-005",
            kind="outbound_result",
            occurred_at=NOW,
            operation_type="outbound_message",
            target_category="operator_conversation",
            actor="controlled_outbound",
            outcome="accepted",
            execution_status="completed",
            details={
                "channel": "controlled_outbound",
                "result": "pending",
            },
        )
    with pytest.raises(ValueError, match="contradictory"):
        AuditEvidence(
            evidence_id="audit-006",
            kind="outbound_completion",
            occurred_at=NOW,
            operation_type="outbound_message",
            target_category="operator_conversation",
            actor="controlled_outbound",
            outcome="reply_sent",
            execution_status="accepted",
            details={"result": "outbound_failed"},
        )
    with pytest.raises(ValueError, match="duplicate detail"):
        AuditEvidence(
            evidence_id="audit-007",
            kind="outbound_result",
            occurred_at=NOW,
            operation_type="outbound_message",
            target_category="operator_conversation",
            actor="controlled_outbound",
            outcome="pending",
            execution_status="pending",
            details={
                "channel": "controlled_outbound",
                "result": "pending",
                "Result": "pending",
            },
        )
    with pytest.raises(ValueError, match="contradictory"):
        AuditEvidence(
            evidence_id="audit-008",
            kind="outbound_result",
            occurred_at=NOW,
            operation_type="outbound_message",
            target_category="operator_conversation",
            actor="controlled_outbound",
            outcome="accepted",
            execution_status="accepted",
            details={"channel": "controlled_outbound", "Result": "pending"},
        )
    with pytest.raises(ValueError, match="does not match"):
        AuditEvidence(
            evidence_id="audit-009",
            kind="request_lifecycle",
            occurred_at=NOW,
            operation_type="request_lifecycle",
            target_category="control_plane",
            actor="control_plane",
            outcome="completed",
            execution_status="replying",
            details={"phase": "outbound", "status": "replying"},
        )


class PlainOutbound:
    def __init__(self) -> None:
        self.sent = []

    def preflight(self, reply: object) -> None:
        del reply

    def send(self, reply: object) -> OutboundDelivery:
        self.sent.append(reply)
        return OutboundDelivery(
            outbound_id=f"plain-outbound-{len(self.sent)}", accepted=True
        )


def test_audit_append_failure_prevents_plain_outbound_dispatch() -> None:
    config = ControlPlaneConfig(
        operator_id=OPERATOR,
        session_id=SESSION,
        signing_secret=SECRET,
    )
    state = InMemoryDurableStateStore()
    audit = InMemoryAuditBoundary(fail_on_append=4)
    outbound = PlainOutbound()
    clock = FixedClock(NOW)
    ids = DeterministicIdGenerator("ticket03")
    trace = make_trace(clock, ids)
    broker = DeterministicCapabilityBroker(
        config=config,
        state=state,
        audit=audit,
        orchestration=ControlledOrchestrationAdapter(),
        outbound=outbound,  # type: ignore[arg-type]
        clock=clock,
        ids=ids,
        trace=trace,
        model_availability_provider=FixedModelAvailabilityProvider(ModelAvailability()),
    )
    receiver = SignedMessageReceiver(
        config=config,
        state=state,
        audit=audit,
        broker=broker,
        clock=clock,
        ids=ids,
    )

    result = receiver.receive(make_event(config))

    assert result.disposition == "failed"
    assert result.reply is None
    assert outbound.sent == []
    assert result.request is not None
    assert result.request.status == "accepted"
    assert result.request.error_code is None


def test_outbound_audit_admission_failure_at_terminal_slot_prevents_dispatch() -> None:
    config = ControlPlaneConfig(
        operator_id=OPERATOR,
        session_id=SESSION,
        signing_secret=SECRET,
    )
    state = InMemoryDurableStateStore()
    audit = InMemoryAuditBoundary(fail_on_append=7)
    outbound = PlainOutbound()
    clock = FixedClock(NOW)
    ids = DeterministicIdGenerator("ticket03-terminal-slot")
    trace = make_trace(clock, ids)
    broker = DeterministicCapabilityBroker(
        config=config,
        state=state,
        audit=audit,
        orchestration=ControlledOrchestrationAdapter(),
        outbound=outbound,  # type: ignore[arg-type]
        clock=clock,
        ids=ids,
        trace=trace,
        model_availability_provider=FixedModelAvailabilityProvider(ModelAvailability()),
    )
    receiver = SignedMessageReceiver(
        config=config,
        state=state,
        audit=audit,
        broker=broker,
        clock=clock,
        ids=ids,
    )

    result = receiver.receive(make_event(config))

    assert result.disposition == "failed"
    assert result.reply is None
    assert outbound.sent == []
    assert not any(
        record.kind == "outbound_result" and record.outcome == "accepted"
        for record in audit.records
    )


def test_post_dispatch_audit_observation_failure_is_reconcilable_unknown() -> None:
    config = ControlPlaneConfig(
        operator_id=OPERATOR,
        session_id=SESSION,
        signing_secret=SECRET,
    )
    state = InMemoryDurableStateStore()
    audit = InMemoryAuditBoundary(fail_on_append=8)
    outbound = PlainOutbound()
    clock = FixedClock(NOW)
    ids = DeterministicIdGenerator("ticket03-post-dispatch")
    trace = make_trace(clock, ids)
    broker = DeterministicCapabilityBroker(
        config=config,
        state=state,
        audit=audit,
        orchestration=ControlledOrchestrationAdapter(),
        outbound=outbound,  # type: ignore[arg-type]
        clock=clock,
        ids=ids,
        trace=trace,
        model_availability_provider=FixedModelAvailabilityProvider(ModelAvailability()),
    )
    receiver = SignedMessageReceiver(
        config=config,
        state=state,
        audit=audit,
        broker=broker,
        clock=clock,
        ids=ids,
    )

    result = receiver.receive(make_event(config))

    assert result.disposition == "unknown"
    assert result.request is not None
    assert result.request.status == "unknown"
    assert result.request.outcome == "outbound_unknown"
    assert outbound.sent == [result.reply]
    assert any(
        record.kind == "outbound_completion"
        and record.outcome == "pending"
        and record.execution_status == "pending"
        for record in audit.records
    )
    assert not any(
        record.kind == "outbound_result" and record.outcome == "accepted"
        for record in audit.records
    )


def test_ambiguous_outbound_failure_never_records_successful_completion() -> None:
    config = ControlPlaneConfig(
        operator_id=OPERATOR,
        session_id=SESSION,
        signing_secret=SECRET,
    )
    state = InMemoryDurableStateStore()
    audit = InMemoryAuditBoundary()
    clock = FixedClock(NOW)
    ids = DeterministicIdGenerator("ticket03")
    outbound = PlainOutbound()
    trace = make_trace(clock, ids)

    def send_ambiguously(reply: object) -> None:
        outbound.sent.append(reply)
        raise OutboundConnectorError("gateway outcome was unknown", may_have_sent=True)

    outbound.send = send_ambiguously
    broker = DeterministicCapabilityBroker(
        config=config,
        state=state,
        audit=audit,
        orchestration=ControlledOrchestrationAdapter(),
        outbound=outbound,  # type: ignore[arg-type]
        clock=clock,
        ids=ids,
        trace=trace,
        model_availability_provider=FixedModelAvailabilityProvider(ModelAvailability()),
    )
    receiver = SignedMessageReceiver(
        config=config,
        state=state,
        audit=audit,
        broker=broker,
        clock=clock,
        ids=ids,
    )

    result = receiver.receive(make_event(config))

    assert result.disposition == "unknown"
    assert result.request is not None
    assert result.request.status == "unknown"
    assert result.request.outcome == "outbound_unknown"
    assert outbound.sent == [result.reply]
    assert any(
        record.kind == "outbound_result" and record.outcome == "pending"
        for record in audit.records
    )
    assert any(
        record.kind == "outbound_result"
        and record.outcome == "unknown"
        and record.execution_status == "unknown"
        for record in audit.records
    )
    assert any(
        record.kind == "request_lifecycle" and record.execution_status == "unknown"
        for record in audit.records
    )
    assert not any(
        record.kind == "request_lifecycle" and record.execution_status == "completed"
        for record in audit.records
    )
    assert not any(
        record.kind == "outbound_result" and record.outcome == "accepted"
        for record in audit.records
    )

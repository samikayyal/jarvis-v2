from __future__ import annotations

from datetime import UTC, datetime, timedelta

from test_support import build_receiver_components

from jarvis_control_plane import (
    AuditWriteError,
    ControlledActionDispatcher,
    ControlledOrchestrationAdapter,
    FrozenActionProposal,
    InboundMessage,
    InMemoryAuditBoundary,
    SignedInboundEvent,
)
from jarvis_control_plane.sessions import (
    DispatchStatus,
    InMemoryWorkingSessionStore,
    SessionStoreError,
    approve_pending_action,
    mark_action_dispatch_attempted,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
OPERATOR = "operator.test"
TRANSPORT_SESSION = "session.test"
SECRET = b"ticket07-test-secret"


def make_event(text: str, *, suffix: str) -> SignedInboundEvent:
    return SignedInboundEvent.from_message(
        InboundMessage(
            event_type="message.received",
            session_id=TRANSPORT_SESSION,
            event_id=f"event-{suffix}",
            message_id=f"message-{suffix}",
            sender_id=OPERATOR,
            chat_id=OPERATOR,
            chat_type="direct",
            message_type="text",
            from_me=False,
            text=text,
        ),
        SECRET,
    )


def make_components() -> tuple[object, ControlledActionDispatcher]:
    return make_components_with_store()


class TerminalWriteFailingStore(InMemoryWorkingSessionStore):
    def compare_and_set(
        self, expected: object, updated: object, **kwargs: object
    ) -> None:
        if any(
            record.status is DispatchStatus.COMPLETED
            for record in updated.action_outbox
        ):
            raise SessionStoreError("terminal state write failed")
        super().compare_and_set(expected, updated, **kwargs)


class PendingActionAuditFailure(InMemoryAuditBoundary):
    def append(self, evidence: object) -> None:
        if evidence.kind == "pending_action":
            raise AuditWriteError("pending-action audit unavailable")
        super().append(evidence)


def make_components_with_store(
    working_sessions: InMemoryWorkingSessionStore | None = None,
    audit: InMemoryAuditBoundary | None = None,
) -> tuple[object, ControlledActionDispatcher]:
    dispatcher = ControlledActionDispatcher()
    orchestration = ControlledOrchestrationAdapter(
        proposal_factory=lambda request: FrozenActionProposal.create(
            action_id="action-001",
            request_id=request.state.request_id,
            kind="calendar_update",
            preview="Update the calendar event to 10:00.",
            payload={"event_id": "event-1", "start": "2026-08-05T10:00:00Z"},
        )
    )
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=SECRET,
        now=NOW,
        id_prefix="ticket07",
        orchestration=orchestration,
        action_dispatcher=dispatcher,
        working_sessions=working_sessions,
        audit=audit,
    )
    return components, dispatcher


def install_proposal(components: object) -> FrozenActionProposal:
    accepted = components.receiver.receive(
        make_event("prepare the change", suffix="01")
    )
    assert accepted.disposition == "pending_action", accepted.reason
    action = components.broker.current_pending_action
    assert action is not None
    return FrozenActionProposal(
        action_id=action.action_id,
        request_id=action.request_id,
        kind=action.kind,
        preview=action.preview or action.summary,
        payload=action.payload,
        digest=action.digest,
    )


def test_exact_approval_dispatches_the_frozen_payload_only_once() -> None:
    components, dispatcher = make_components()
    proposal = install_proposal(components)

    approved = components.receiver.receive(make_event("  YES ", suffix="02"))

    assert approved.disposition == "action_dispatched"
    assert [item.action_id for item in dispatcher.dispatched] == [proposal.action_id]
    assert dispatcher.dispatched[0].digest == proposal.digest
    assert dispatcher.dispatched[0].payload == proposal.payload
    assert components.broker.current_pending_action is None
    record = components.broker.working_sessions.load().action_outbox[-1]
    assert record.status is DispatchStatus.COMPLETED
    assert record.payload is None
    assert record.preview is None

    replay = components.receiver.receive(make_event("yes", suffix="03"))
    assert replay.disposition != "action_dispatched"
    assert len(dispatcher.dispatched) == 1


def test_rejection_and_altered_approval_never_dispatch() -> None:
    components, dispatcher = make_components()
    install_proposal(components)

    altered = components.receiver.receive(
        make_event("yes, but change the time", suffix="02")
    )
    assert altered.disposition == "pending_blocked"
    assert dispatcher.dispatched == []

    rejected = components.receiver.receive(make_event("4", suffix="03"))
    assert rejected.disposition == "action_rejected"
    assert dispatcher.dispatched == []
    assert components.broker.current_pending_action is None


def test_dispatch_failure_closes_the_outbox_without_replaying_the_action() -> None:
    components, dispatcher = make_components()
    dispatcher.failure = "controlled dispatcher failure"
    install_proposal(components)

    failed = components.receiver.receive(make_event("yes", suffix="02"))

    assert failed.disposition == "action_dispatch_failed"
    record = components.broker.working_sessions.load().action_outbox[-1]
    assert record.status is DispatchStatus.FAILED
    assert record.payload is None
    assert dispatcher.dispatched == []


def test_ambiguous_dispatch_failure_is_recorded_as_unknown_without_a_retry() -> None:
    components, dispatcher = make_components()
    dispatcher.failure = "connection dropped after dispatch"
    dispatcher.failure_may_have_dispatched = True
    install_proposal(components)

    unknown = components.receiver.receive(make_event("yes", suffix="02"))

    assert unknown.disposition == "action_dispatch_unknown"
    record = components.broker.working_sessions.load().action_outbox[-1]
    assert record.status is DispatchStatus.UNKNOWN
    assert record.payload is None


def test_terminal_persistence_failure_returns_unknown_and_leaves_attempt_for_reconciliation() -> (
    None
):
    components, dispatcher = make_components_with_store(TerminalWriteFailingStore())
    install_proposal(components)

    unknown = components.receiver.receive(make_event("yes", suffix="02"))

    assert unknown.disposition == "action_dispatch_unknown"
    assert len(dispatcher.dispatched) == 1
    record = components.broker.working_sessions.load().action_outbox[-1]
    assert record.status is DispatchStatus.ATTEMPTED


def test_audit_admission_failure_never_leaves_a_dispatchable_action() -> None:
    components, dispatcher = make_components_with_store(
        audit=PendingActionAuditFailure()
    )

    result = components.receiver.receive(make_event("prepare the change", suffix="01"))

    assert result.disposition == "failed"
    session = components.broker.working_sessions.load()
    assert session.pending_action is None
    assert not any(record.is_live for record in session.action_outbox)
    assert dispatcher.dispatched == []


def test_restart_closes_unattempted_and_ambiguous_outbox_records_without_dispatch() -> (
    None
):
    components, dispatcher = make_components()
    install_proposal(components)
    session = components.broker.working_sessions.load()
    approved = approve_pending_action(session, now=components.clock)
    components.broker.working_sessions.compare_and_set(session, approved.state)

    restarted = _restart(components, dispatcher)
    record = restarted.working_sessions.load().action_outbox[-1]
    assert record.status is DispatchStatus.NOT_STARTED
    assert record.payload is None
    assert dispatcher.dispatched == []

    components, dispatcher = make_components()
    install_proposal(components)
    session = components.broker.working_sessions.load()
    approved = approve_pending_action(session, now=components.clock)
    components.broker.working_sessions.compare_and_set(session, approved.state)
    attempted = mark_action_dispatch_attempted(
        approved.state, action_id="action-001", now=components.clock
    )
    components.broker.working_sessions.compare_and_set(approved.state, attempted.state)

    restarted = _restart(components, dispatcher)
    record = restarted.working_sessions.load().action_outbox[-1]
    assert record.status is DispatchStatus.UNKNOWN
    assert record.payload is None
    assert dispatcher.dispatched == []


def test_expiry_cancellation_and_restart_invalidate_without_dispatch() -> None:
    components, dispatcher = make_components()
    install_proposal(components)
    components.clock.current = NOW + timedelta(minutes=10)

    expired = components.receiver.receive(make_event("yes", suffix="02"))
    assert expired.disposition == "pending_expired"
    assert dispatcher.dispatched == []

    components, dispatcher = make_components()
    install_proposal(components)
    cancelled = components.receiver.receive(make_event("/cancel", suffix="03"))
    assert cancelled.disposition == "cancelled"
    assert dispatcher.dispatched == []

    components, dispatcher = make_components()
    install_proposal(components)
    restarted = _restart(components, dispatcher)
    assert restarted.current_pending_action is None
    assert dispatcher.dispatched == []


def _restart(components: object, dispatcher: ControlledActionDispatcher) -> object:
    return type(components.broker)(
        config=components.config,
        state=components.state,
        audit=components.audit,
        orchestration=components.orchestration,
        outbound=components.outbound,
        action_dispatcher=dispatcher,
        clock=components.clock,
        ids=components.ids,
        trace=components.trace,
        model_availability_provider=components.provider,
        working_sessions=components.broker.working_sessions,
    )

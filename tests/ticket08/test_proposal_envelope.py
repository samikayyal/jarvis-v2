from __future__ import annotations

from test_support import build_receiver_components
from ticket07.test_pending_actions import NOW, OPERATOR, SECRET, TRANSPORT_SESSION

from jarvis_control_plane import (
    ControlledActionDispatcher,
    ControlledOrchestrationAdapter,
    FrozenActionProposal,
    InboundMessage,
    InMemoryAuditBoundary,
    SignedInboundEvent,
)
from jarvis_control_plane.ports import AuditWriteError, OutboundConnectorError


def make_event(text: str, *, suffix: str) -> SignedInboundEvent:
    return SignedInboundEvent.from_message(
        InboundMessage(
            event_type="message.received",
            session_id=TRANSPORT_SESSION,
            event_id=f"event-envelope-{suffix}",
            message_id=f"message-envelope-{suffix}",
            sender_id=OPERATOR,
            chat_id=OPERATOR,
            chat_type="direct",
            message_type="text",
            from_me=False,
            text=text,
        ),
        SECRET,
    )


def make_components(
    *, preview: str, outbound: object | None = None, audit: object | None = None
) -> tuple[object, ControlledActionDispatcher]:
    dispatcher = ControlledActionDispatcher()
    orchestration = ControlledOrchestrationAdapter(
        proposal_factory=lambda request: FrozenActionProposal.create(
            action_id="action-envelope-001",
            request_id=request.state.request_id,
            kind="calendar_update",
            preview=preview,
            payload={"event_id": "event-1", "start": "2026-08-05T10:00:00Z"},
        )
    )
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=SECRET,
        now=NOW,
        id_prefix="ticket08",
        orchestration=orchestration,
        action_dispatcher=dispatcher,
        audit=audit,
    )
    if outbound is not None:
        components.broker.outbound = outbound
    return components, dispatcher


def test_oversized_proposal_is_delivered_as_one_bounded_digest_bound_sequence() -> None:
    components, dispatcher = make_components(preview="Change:\n" + "x" * 6_500)

    presented = components.receiver.receive(make_event("prepare change", suffix="01"))

    assert presented.disposition == "pending_action", presented.reason
    action = components.broker.current_pending_action
    assert action is not None
    assert action.presentation_status.value == "presented"
    assert len(action.presentation_fragments) == 3
    sent = components.outbound.sent
    assert len(sent) == 4
    assert all(len(reply.body) <= 4_096 for reply in sent)
    for number, reply in enumerate(sent[:-1], start=1):
        assert action.action_id in reply.body
        assert action.digest in reply.body
        assert f"part {number}/3" in reply.body
    assert action.action_id in sent[-1].body
    assert action.digest in sent[-1].body
    assert "All proposal fragments were presented" in sent[-1].body

    approved = components.receiver.receive(make_event("1", suffix="02"))
    assert approved.disposition == "action_dispatched"
    assert [item.action_id for item in dispatcher.dispatched] == [action.action_id]


class FailOnSecondPresentationSend:
    def __init__(self, wrapped: object) -> None:
        self.wrapped = wrapped
        self.calls = 0

    def preflight(self, reply: object) -> None:
        self.wrapped.preflight(reply)

    def send(self, reply: object) -> object:
        self.calls += 1
        if self.calls == 2:
            raise OutboundConnectorError(
                "gateway outcome is ambiguous", may_have_sent=True
            )
        return self.wrapped.send(reply)


class ProposalSendAuditFailure(InMemoryAuditBoundary):
    def append_batch(self, evidence: object) -> None:
        if any(item.kind == "outbound_attempt" for item in evidence):
            raise AuditWriteError("proposal outbound audit is unavailable")
        super().append_batch(evidence)


def test_ambiguous_fragment_outcome_invalidates_the_action_without_retry() -> None:
    seed, _ = make_components(preview="unused")
    outbound = FailOnSecondPresentationSend(seed.outbound)
    components, dispatcher = make_components(
        preview="Change:\n" + "x" * 6_500, outbound=outbound
    )

    failed = components.receiver.receive(make_event("prepare change", suffix="01"))

    assert failed.disposition == "failed"
    assert components.broker.current_pending_action is None
    assert components.broker.working_sessions.load().active_request is None
    assert outbound.calls == 2
    assert dispatcher.dispatched == []


def test_proposal_fragment_never_sends_when_its_audit_admission_fails() -> None:
    audit = ProposalSendAuditFailure()
    components, dispatcher = make_components(
        preview="Change:\n" + "x" * 6_500, audit=audit
    )

    failed = components.receiver.receive(make_event("prepare change", suffix="audit"))

    assert failed.disposition == "failed"
    assert components.outbound.sent == []
    assert components.broker.current_pending_action is None
    assert dispatcher.dispatched == []

from __future__ import annotations

import base64
from email import message_from_bytes
from email.policy import SMTP

import pytest

from jarvis_control_plane import (
    ActionDispatcherError,
    AuditWriteError,
    ControlledGmailWriteProvider,
    DeterministicIdGenerator,
    DiagnosticTraceRecorder,
    FixedClock,
    GmailDeliveryResult,
    GmailNewSendRequest,
    GmailReplyRequest,
    GoogleHttpResponse,
    GoogleRefreshTokenExchanger,
    InMemoryAuditBoundary,
    InMemoryDiagnosticTraceStore,
    TraceReservation,
    TraceWriteError,
    create_gmail_new_send_proposal,
    gmail_write_request_from_proposal,
)
from jarvis_control_plane.acceptance_failpoints import (
    ReviewedPostDispatchFailpoint,
    ReviewedPostDispatchFailpointSpec,
)
from jarvis_control_plane.gmail_writes import _encode_rfc822

from .helpers import (
    NOW,
    _components,
    _dispatcher,
    _event,
    _proposal,
)


def test_gmail_new_send_and_reply_use_distinct_typed_action_contracts() -> None:
    new_send = gmail_write_request_from_proposal(_proposal())
    reply = gmail_write_request_from_proposal(_proposal(reply=True))

    assert isinstance(new_send, GmailNewSendRequest)
    assert isinstance(reply, GmailReplyRequest)
    assert new_send.operation == "gmail_send"
    assert reply.operation == "gmail_reply"
    assert new_send.threading == "new_message"
    assert reply.threading == "gmail_threaded_reply"

    with pytest.raises(TypeError):
        create_gmail_new_send_proposal(
            action_id="gmail-action-invalid",
            request_id="request-invalid",
            to=("recipient@example.com",),
            subject="Subject",
            body="Body",
            mime_type="text/plain",
            source_message_id="source-001",  # type: ignore[call-arg]
        )


def test_shared_google_refresh_exchange_owns_wire_request_and_timeout_bound() -> None:
    class Transport:
        def request(self, **kwargs: object) -> GoogleHttpResponse:
            assert kwargs["method"] == "POST"
            assert kwargs["url"] == "https://oauth2.googleapis.com/token"
            assert kwargs["timeout_seconds"] == 5.0
            return GoogleHttpResponse(
                status_code=200,
                headers={"Content-Type": "application/json"},
                body=b'{"access_token":"shared-access-token"}',
            )

    result = GoogleRefreshTokenExchanger(
        client_id="client-id",
        client_secret="client-secret",
        transport=Transport(),
    ).exchange("refresh-token")

    assert result.access_token == "shared-access-token"
    assert result.request.form == {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "grant_type": "refresh_token",
        "refresh_token": "refresh-token",
    }
    assert b"grant_type=refresh_token" in result.request.body


def test_new_send_freezes_every_delivery_field_and_dispatches_that_exact_message() -> (
    None
):
    provider = ControlledGmailWriteProvider(
        result=GmailDeliveryResult(message_id="sent-001", thread_id="thread-new")
    )
    proposal = _proposal()
    components = _components(proposal, _dispatcher(provider))

    pending = components.receiver.receive(_event("send the email", suffix="01"))
    approved = components.receiver.receive(_event("yes", suffix="02"))

    assert pending.disposition == "pending_action"
    assert approved.disposition == "action_dispatched"
    assert "To: recipient@example.com" in proposal.preview
    assert "Cc: copy@example.com" in proposal.preview
    assert "Bcc: blind@example.com" in proposal.preview
    assert "MIME: text/plain" in proposal.preview
    assert "Please review the attached plan." in proposal.preview
    assert len(provider.calls) == 1
    acknowledgements = [
        reply
        for reply in components.outbound.sent
        if "completed successfully" in reply.body
    ]
    assert len(acknowledgements) == 1
    assert "no retry" in acknowledgements[0].body.lower()
    sent = provider.calls[0]
    assert sent.operation == "gmail_send"
    assert sent.message.to == ("recipient@example.com",)
    assert sent.message.cc == ("copy@example.com",)
    assert sent.message.bcc == ("blind@example.com",)
    assert sent.message.subject == "Quarterly check-in"
    assert sent.message.body == "Hello\n\nPlease review the attached plan."
    assert sent.message.mime_type == "text/plain"


def test_approved_message_round_trips_from_proposal_through_rfc822() -> None:
    request = gmail_write_request_from_proposal(_proposal(reply=True))

    encoded = _encode_rfc822(request)
    raw = base64.urlsafe_b64decode(encoded + "===")
    message = message_from_bytes(raw, policy=SMTP)
    body = message.get_content().replace("\r\n", "\n")

    assert message["To"] == ", ".join(request.message.to)
    assert message["Cc"] == ", ".join(request.message.cc)
    assert message["Bcc"] == ", ".join(request.message.bcc)
    assert message["Subject"] == request.message.subject
    assert message.get_content_type() == request.message.mime_type
    assert body.removesuffix("\n") == request.message.body
    assert message["In-Reply-To"] == request.in_reply_to
    assert message["References"] == " ".join(request.references)
    assert request.thread_id == request.source_thread_id


def test_exact_gmail_rejection_sends_one_terminal_ack_without_provider_dispatch() -> (
    None
):
    provider = ControlledGmailWriteProvider()
    components = _components(_proposal(), _dispatcher(provider))

    pending = components.receiver.receive(_event("send the email", suffix="reject-01"))
    rejected = components.receiver.receive(_event("no", suffix="reject-02"))

    assert pending.disposition == "pending_action"
    assert rejected.disposition == "action_rejected"
    assert provider.calls == []
    acknowledgements = [
        reply
        for reply in components.outbound.sent
        if "rejected before dispatch" in reply.body
    ]
    assert len(acknowledgements) == 1
    assert "no retry" in acknowledgements[0].body.lower()


def test_gmail_post_dispatch_failpoint_is_unknown_and_replay_free() -> None:
    provider = ControlledGmailWriteProvider(
        result=GmailDeliveryResult(message_id="sent-failpoint", thread_id="thread-new")
    )
    failpoint = ReviewedPostDispatchFailpoint(
        ReviewedPostDispatchFailpointSpec(
            service="gmail",
            operation="gmail_send",
            action_id="gmail-action-001",
            review_id="ticket18-gmail-unknown",
        )
    )

    components = _components(
        _proposal(),
        _dispatcher(provider, acceptance_failpoint=failpoint),
        action_id="gmail-action-001",
    )

    components.receiver.receive(_event("send", suffix="failpoint-01"))
    unknown = components.receiver.receive(_event("yes", suffix="failpoint-02"))
    replay = components.receiver.receive(_event("yes", suffix="failpoint-03"))

    assert unknown.disposition == "action_dispatch_unknown"
    assert replay.disposition != "action_dispatched"
    assert failpoint.consumed is True
    assert len(provider.calls) == 1
    acknowledgements = [
        reply
        for reply in components.outbound.sent
        if "unknown provider outcome" in reply.body
    ]
    assert len(acknowledgements) == 1
    assert "no retry" in acknowledgements[0].body.lower()


def test_gmail_failpoint_does_not_swallow_terminal_audit_failure() -> None:
    class UnknownOutcomeAuditFailure(InMemoryAuditBoundary):
        def append(self, evidence: object) -> None:
            if evidence.kind == "gmail_write" and evidence.outcome == "unknown":
                raise AuditWriteError("unknown outcome audit unavailable")
            super().append(evidence)  # type: ignore[arg-type]

    provider = ControlledGmailWriteProvider(
        result=GmailDeliveryResult(
            message_id="sent-audit-failure", thread_id="thread-new"
        )
    )
    failpoint = ReviewedPostDispatchFailpoint(
        ReviewedPostDispatchFailpointSpec(
            service="gmail",
            operation="gmail_send",
            action_id="gmail-action-001",
            review_id="ticket31-gmail-audit-failure",
        )
    )
    dispatcher = _dispatcher(
        provider,
        audit=UnknownOutcomeAuditFailure(),
        acceptance_failpoint=failpoint,
    )

    with pytest.raises(ActionDispatcherError) as caught:
        dispatcher.dispatch(dispatcher.bind_proposal(_proposal()))

    assert caught.value.may_have_dispatched is True
    assert str(caught.value) == "Gmail terminal audit evidence is unavailable"
    assert isinstance(caught.value.__cause__, AuditWriteError)
    assert len(provider.calls) == 1


def test_gmail_failpoint_terminal_audit_failure_closes_unknown_without_success_ack() -> (
    None
):
    class UnknownOutcomeAuditFailure(InMemoryAuditBoundary):
        def append(self, evidence: object) -> None:
            if evidence.kind == "gmail_write" and evidence.outcome == "unknown":
                raise AuditWriteError("unknown outcome audit unavailable")
            super().append(evidence)  # type: ignore[arg-type]

    provider = ControlledGmailWriteProvider(
        result=GmailDeliveryResult(
            message_id="sent-audit-failure", thread_id="thread-new"
        )
    )
    failpoint = ReviewedPostDispatchFailpoint(
        ReviewedPostDispatchFailpointSpec(
            service="gmail",
            operation="gmail_send",
            action_id="gmail-action-001",
            review_id="ticket31-gmail-audit-failure-broker",
        )
    )
    audit = UnknownOutcomeAuditFailure()
    components = _components(
        _proposal(),
        _dispatcher(
            provider,
            audit=audit,
            acceptance_failpoint=failpoint,
        ),
        audit=audit,
        action_id="gmail-action-001",
    )

    pending = components.receiver.receive(_event("send", suffix="audit-failure-01"))
    result = components.receiver.receive(_event("yes", suffix="audit-failure-02"))

    assert pending.disposition == "pending_action"
    assert result.disposition == "action_dispatch_unknown"
    assert result.reason == "Gmail terminal audit evidence is unavailable"
    assert failpoint.consumed is True
    assert len(provider.calls) == 1
    session = components.broker.working_sessions.load()
    assert session is not None
    record = next(
        item for item in session.action_outbox if item.action_id == "gmail-action-001"
    )
    assert record.status.value == "unknown"
    assert not any(
        "completed successfully" in reply.body for reply in components.outbound.sent
    )
    assert (
        sum(
            "unknown provider outcome" in reply.body
            for reply in components.outbound.sent
        )
        == 1
    )


def test_trace_capacity_failure_is_definite_not_started_for_gmail_dispatch() -> None:
    trace_store = InMemoryDiagnosticTraceStore()
    writer = trace_store.writer()
    trace = DiagnosticTraceRecorder(
        writer=writer,
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("ticket18-capacity-trace"),
    )
    provider = ControlledGmailWriteProvider()
    components = _components(
        _proposal(),
        _dispatcher(provider, trace=trace),
        trace=trace,
    )
    blockers = []
    try:
        pending = components.receiver.receive(_event("send", suffix="capacity-pending"))
        assert pending.disposition == "pending_action"
        while (available := trace_store.available_bytes) > 0:
            blockers.append(
                writer.reserve(
                    request_id=f"ticket18-capacity-blocker-{len(blockers)}",
                    reservation_bytes=min(
                        available, trace_store.limits.reservation_bytes
                    ),
                )
            )

        result = components.receiver.receive(_event("yes", suffix="capacity-approve"))

        assert result.disposition == "action_dispatch_failed"
        assert "not attempted" in (result.reason or "")
        assert provider.calls == []
        session = components.broker.working_sessions.load()
        assert session is not None
        assert session.pending_action is None
        assert len(session.action_outbox) == 1
        assert session.action_outbox[0].status.value == "failed"
    finally:
        for blocker in blockers:
            writer.release(blocker)
        trace_store._close_writer_service()


def test_trace_persistence_failure_after_provider_start_remains_unknown() -> None:
    class AppendFailureWriter:
        def __init__(self) -> None:
            self.owner = object()

        def reserve(
            self, *, request_id: str, reservation_bytes: int | None = None
        ) -> TraceReservation:
            return TraceReservation(
                reservation_id="ticket18-persistence-failure",
                request_id=request_id,
                reserved_bytes=16 * 1024 * 1024,
                _owner=self.owner,
            )

        def append(self, _trace: object, _reservation: TraceReservation) -> None:
            raise TraceWriteError(
                "controlled trace persistence failure",
                operation_started=True,
            )

        def release(self, _reservation: TraceReservation) -> None:
            return

    trace = DiagnosticTraceRecorder(
        writer=AppendFailureWriter(),
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("ticket18-persistence-trace"),
    )
    provider = ControlledGmailWriteProvider()
    connector = _dispatcher(provider, trace=trace)

    with pytest.raises(ActionDispatcherError) as caught:
        connector.dispatch(connector.bind_proposal(_proposal()))

    assert caught.value.may_have_dispatched is True
    assert provider.calls != []

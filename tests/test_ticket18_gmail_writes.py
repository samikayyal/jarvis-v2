from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_support import build_receiver_components

from jarvis_control_plane import (
    ActionDispatcherError,
    ControlledGmailWriteProvider,
    DeterministicIdGenerator,
    DiagnosticTraceRecorder,
    FixedClock,
    FrozenActionProposal,
    GmailApiWriteProvider,
    GmailSendProviderResult,
    GmailWriteConnector,
    GoogleReadHttpResponse,
    InboundMessage,
    InMemoryAuditBoundary,
    InMemoryDiagnosticTraceStore,
    InMemoryGoogleCredentialStore,
    OAuthCredentialRecord,
    OrchestrationRequest,
    RequestState,
    SignedInboundEvent,
    create_gmail_send_proposal,
)
from jarvis_control_plane.orchestration import (
    AgentsSdkOrchestrationAdapter,
    AgentsSdkPlan,
    AgentsSdkProposal,
)

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
IDENTITY = "google-subject-123"
OPERATOR = "operator.test"
TRANSPORT_SESSION = "session.test"
SECRET = b"ticket18-test-secret"


def _event(text: str, *, suffix: str) -> SignedInboundEvent:
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


def _trace() -> DiagnosticTraceRecorder:
    return DiagnosticTraceRecorder(
        writer=InMemoryDiagnosticTraceStore().writer(),
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("ticket18-trace"),
    )


def _dispatcher(
    provider: ControlledGmailWriteProvider,
    *,
    audit: InMemoryAuditBoundary | None = None,
) -> GmailWriteConnector:
    return GmailWriteConnector(
        configured_identity=IDENTITY,
        credential_store=InMemoryGoogleCredentialStore(
            OAuthCredentialRecord(
                subject=IDENTITY,
                granted_scopes=frozenset(
                    {"https://www.googleapis.com/auth/gmail.send"}
                ),
                refresh_token="controlled-refresh-token",
            )
        ),
        provider=provider,
        audit=audit or InMemoryAuditBoundary(),
        trace=_trace(),
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("ticket18-gmail"),
    )


def _proposal(*, reply: bool = False) -> FrozenActionProposal:
    fields: dict[str, object] = {
        "to": ("recipient@example.com",),
        "cc": ("copy@example.com",),
        "bcc": ("blind@example.com",),
        "subject": "Quarterly check-in",
        "body": "Hello\n\nPlease review the attached plan.",
        "mime_type": "text/plain",
    }
    if reply:
        fields.update(
            {
                "source_message_id": "source-001",
                "source_thread_id": "thread-001",
                "in_reply_to": "<source-001@example.com>",
                "references": ("<root@example.com>", "<source-001@example.com>"),
            }
        )
    return create_gmail_send_proposal(
        action_id="gmail-action-001",
        request_id="request-001",
        **fields,
    )


def _components(
    proposal: FrozenActionProposal, dispatcher: GmailWriteConnector
) -> object:
    from jarvis_control_plane import ControlledOrchestrationAdapter

    return build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=SECRET,
        now=NOW,
        id_prefix="ticket18",
        orchestration=ControlledOrchestrationAdapter(
            proposal_factory=lambda request: FrozenActionProposal.create(
                action_id=f"{request.state.request_id}:gmail",
                request_id=request.state.request_id,
                kind=proposal.kind,
                preview=proposal.preview,
                payload=json.loads(proposal.payload),
            )
        ),
        action_dispatcher=dispatcher,  # type: ignore[arg-type]
    )


def test_new_send_freezes_every_delivery_field_and_dispatches_that_exact_message() -> (
    None
):
    provider = ControlledGmailWriteProvider(
        result=GmailSendProviderResult(message_id="sent-001", thread_id="thread-new")
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
    sent = provider.calls[0]
    assert sent.operation == "gmail_send"
    assert sent.to == ("recipient@example.com",)
    assert sent.cc == ("copy@example.com",)
    assert sent.bcc == ("blind@example.com",)
    assert sent.subject == "Quarterly check-in"
    assert sent.body == "Hello\n\nPlease review the attached plan."
    assert sent.mime_type == "text/plain"


def test_typed_reply_freezes_source_thread_headers_and_requires_returned_thread_match() -> (
    None
):
    provider = ControlledGmailWriteProvider(
        result=GmailSendProviderResult(message_id="sent-002", thread_id="thread-001")
    )
    proposal = _proposal(reply=True)
    components = _components(proposal, _dispatcher(provider))

    components.receiver.receive(_event("reply", suffix="01"))
    approved = components.receiver.receive(_event("YES", suffix="02"))

    assert approved.disposition == "action_dispatched"
    assert "Source message: source-001" in proposal.preview
    assert "Source thread: thread-001" in proposal.preview
    assert "In-Reply-To: <source-001@example.com>" in proposal.preview
    assert "References: <root@example.com> <source-001@example.com>" in proposal.preview
    sent = provider.calls[0]
    assert sent.operation == "gmail_reply"
    assert sent.thread_id == "thread-001"
    assert sent.source_message_id == "source-001"
    assert sent.in_reply_to == "<source-001@example.com>"
    assert sent.references == ("<root@example.com>", "<source-001@example.com>")


@pytest.mark.parametrize("approval", ("yes, but use a new recipient", "yes"))
def test_altered_or_expired_approval_never_dispatches_a_gmail_replacement(
    approval: str,
) -> None:
    provider = ControlledGmailWriteProvider()
    components = _components(_proposal(), _dispatcher(provider))
    components.receiver.receive(_event("send", suffix="01"))
    if approval == "yes":
        components.clock.current = NOW + timedelta(minutes=10)

    result = components.receiver.receive(_event(approval, suffix="02"))

    assert result.disposition in {"pending_blocked", "pending_expired"}
    assert provider.calls == []


def test_mismatched_thread_is_unknown_and_a_replay_never_sends_a_replacement() -> None:
    provider = ControlledGmailWriteProvider(
        result=GmailSendProviderResult(message_id="sent-003", thread_id="wrong-thread")
    )
    components = _components(_proposal(reply=True), _dispatcher(provider))
    components.receiver.receive(_event("reply", suffix="01"))

    mismatched = components.receiver.receive(_event("yes", suffix="02"))
    replay = components.receiver.receive(_event("yes", suffix="03"))

    assert mismatched.disposition == "action_dispatch_unknown"
    assert replay.disposition != "action_dispatched"
    assert len(provider.calls) == 1


def test_terminal_audit_failure_after_a_send_is_reported_unknown_without_retry() -> (
    None
):
    class CompletedWriteAuditFailure(InMemoryAuditBoundary):
        def append(self, evidence: object) -> None:
            if evidence.kind == "gmail_write" and evidence.outcome == "completed":
                raise RuntimeError("terminal audit unavailable")
            super().append(evidence)  # type: ignore[arg-type]

    provider = ControlledGmailWriteProvider()
    dispatcher = _dispatcher(provider, audit=CompletedWriteAuditFailure())

    with pytest.raises(ActionDispatcherError) as caught:
        dispatcher.dispatch(_proposal())

    assert caught.value.may_have_dispatched is True
    assert len(provider.calls) == 1


def test_live_provider_posts_only_raw_frozen_rfc822_message_and_thread_id() -> None:
    class Transport:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def request(self, **kwargs: object) -> GoogleReadHttpResponse:
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return GoogleReadHttpResponse(
                    status_code=200,
                    headers={"Content-Type": "application/json"},
                    body=b'{"access_token":"live-token"}',
                )
            return GoogleReadHttpResponse(
                status_code=200,
                headers={"Content-Type": "application/json"},
                body=json.dumps({"id": "sent-004", "threadId": "thread-001"}).encode(),
            )

    transport = Transport()
    provider = GmailApiWriteProvider(
        client_id="client-id", client_secret="client-secret", transport=transport
    )
    dispatcher = _dispatcher(provider)  # type: ignore[arg-type]

    dispatcher.dispatch(_proposal(reply=True))

    assert len(transport.calls) == 2
    send = transport.calls[1]
    assert send["method"] == "POST"
    assert str(send["url"]).endswith("/gmail/v1/users/me/messages/send")
    encoded = json.loads(bytes(send["body"] or b"").decode())["raw"]
    raw = __import__("base64").urlsafe_b64decode(encoded + "===").decode()
    assert "To: recipient@example.com" in raw
    assert "In-Reply-To: <source-001@example.com>" in raw
    assert "References: <root@example.com> <source-001@example.com>" in raw
    assert '"threadId":"thread-001"' in bytes(send["body"] or b"").decode()


def test_orchestration_rebuilds_a_canonical_gmail_preview() -> None:
    class Reasoning:
        def __init__(self, **values: object) -> None:
            self.values = values

    class Settings:
        def __init__(self, **values: object) -> None:
            self.values = values

    class RunConfig:
        def __init__(self, **values: object) -> None:
            self.values = values

    plan = AgentsSdkPlan(
        reply_text="The exact Gmail action is ready.",
        execution_host="ubuntu",
        host_reason_code="default_ubuntu",
        proposal=AgentsSdkProposal(
            kind="gmail_send",
            preview="untrusted model prose is never the frozen preview",
            payload={
                "to": ["recipient@example.com"],
                "cc": [],
                "bcc": [],
                "subject": "Quarterly check-in",
                "body": "Please review.",
                "mime_type": "text/plain",
            },
        ),
    )

    result = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **_kwargs: object(),
        run_sync=lambda *_args, **_kwargs: SimpleNamespace(final_output=plan),
        model_settings_factory=Settings,
        reasoning_factory=Reasoning,
        run_config_factory=RunConfig,
    ).run(
        OrchestrationRequest(
            state=RequestState(
                request_id="request-001",
                event_id="event-001",
                message_id="message-001",
                operator_id=OPERATOR,
                session_id="working-session-001",
                chat_id=OPERATOR,
                created_at=NOW,
                updated_at=NOW,
                status="accepted",
                phase="orchestration",
            ),
            text="send this email",
        )
    )

    assert result.proposal is not None
    assert result.proposal.kind == "gmail_send"
    assert result.proposal.preview.startswith(
        "Gmail new send\nTo: recipient@example.com"
    )
    assert "untrusted model prose" not in result.proposal.preview

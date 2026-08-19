from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email import message_from_bytes
from email.policy import SMTP
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from test_support import build_receiver_components

from jarvis_control_plane import (
    GMAIL_SEND_SCOPE,
    ActionDispatcher,
    ActionDispatcherError,
    AuditWriteError,
    ControlledActionDispatcher,
    ControlledGmailWriteProvider,
    ControlledGoogleOAuthProvider,
    DeterministicIdGenerator,
    DiagnosticTraceRecorder,
    FixedClock,
    FrozenActionProposal,
    GmailApiWriteProvider,
    GmailDeliveryResult,
    GmailNewSendRequest,
    GmailReplyRequest,
    GmailWriteConnector,
    GoogleConnectionState,
    GoogleHttpResponse,
    GoogleOAuthLifecycle,
    GoogleRefreshTokenExchanger,
    InboundMessage,
    InMemoryAuditBoundary,
    InMemoryDiagnosticTraceStore,
    InMemoryGoogleCredentialStore,
    InMemoryGoogleOAuthStateStore,
    OAuthCredentialRecord,
    OAuthGrant,
    OrchestrationRequest,
    RequestState,
    RoutedActionDispatcher,
    SignedInboundEvent,
    TraceReservation,
    TraceWriteError,
    create_gmail_new_send_proposal,
    create_gmail_reply_proposal,
    gmail_write_request_from_proposal,
)
from jarvis_control_plane.gmail_writes import _encode_rfc822
from jarvis_control_plane.manual_admin import _open_manual_trace_boundary
from jarvis_control_plane.orchestration import (
    AgentsSdkOrchestrationAdapter,
    AgentsSdkPlan,
    AgentsSdkProposal,
)
from jarvis_control_plane.sessions import ReadinessState

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
    connection_state: object | None = None,
    trace: DiagnosticTraceRecorder | None = None,
    post_dispatch_failpoint: object | None = None,
) -> GmailWriteConnector:
    connection_state = connection_state or (
        lambda: GoogleConnectionState(
            connected=True,
            generation=1,
            granted_scopes=frozenset({"https://www.googleapis.com/auth/gmail.send"}),
        )
    )
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
        trace=trace or _trace(),
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("ticket18-gmail"),
        connection_state=connection_state,  # type: ignore[arg-type]
        post_dispatch_failpoint=post_dispatch_failpoint,  # type: ignore[arg-type]
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
    factory = create_gmail_reply_proposal if reply else create_gmail_new_send_proposal
    return factory(action_id="gmail-action-001", request_id="request-001", **fields)


def _terminal_proposal() -> FrozenActionProposal:
    return FrozenActionProposal.create(
        action_id="terminal-action-001",
        request_id="request-001",
        kind="terminal",
        preview="Run the exact terminal action.",
        payload={
            "host": "ubuntu",
            "executable": "/usr/bin/touch",
            "arguments": ["/workspace/output.txt"],
            "cwd": "/workspace",
        },
    )


def _components(
    proposal: FrozenActionProposal,
    dispatcher: ActionDispatcher,
    *,
    audit: InMemoryAuditBoundary | None = None,
    trace: DiagnosticTraceRecorder | None = None,
) -> object:
    from jarvis_control_plane import ControlledOrchestrationAdapter

    return build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=SECRET,
        now=NOW,
        id_prefix="ticket18",
        audit=audit,
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
        action_lifecycle=dispatcher,  # type: ignore[arg-type]
        trace=trace,
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
    failpoint_calls: list[str] = []

    def failpoint(operation: str) -> None:
        failpoint_calls.append(operation)
        raise RuntimeError("controlled post-dispatch fault")

    components = _components(
        _proposal(),
        _dispatcher(provider, post_dispatch_failpoint=failpoint),
    )

    components.receiver.receive(_event("send", suffix="failpoint-01"))
    unknown = components.receiver.receive(_event("yes", suffix="failpoint-02"))
    replay = components.receiver.receive(_event("yes", suffix="failpoint-03"))

    assert unknown.disposition == "action_dispatch_unknown"
    assert replay.disposition != "action_dispatched"
    assert failpoint_calls == ["gmail_send"]
    assert len(provider.calls) == 1
    acknowledgements = [
        reply
        for reply in components.outbound.sent
        if "unknown provider outcome" in reply.body
    ]
    assert len(acknowledgements) == 1
    assert "no retry" in acknowledgements[0].body.lower()


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


def test_typed_reply_freezes_source_thread_headers_and_requires_returned_thread_match() -> (
    None
):
    provider = ControlledGmailWriteProvider(
        result=GmailDeliveryResult(message_id="sent-002", thread_id="thread-001")
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
        result=GmailDeliveryResult(message_id="sent-003", thread_id="wrong-thread")
    )
    components = _components(_proposal(reply=True), _dispatcher(provider))
    components.receiver.receive(_event("reply", suffix="01"))

    mismatched = components.receiver.receive(_event("yes", suffix="02"))
    replay = components.receiver.receive(_event("yes", suffix="03"))

    assert mismatched.disposition == "action_dispatch_unknown"
    assert replay.disposition != "action_dispatched"
    assert len(provider.calls) == 1


def test_reconnected_google_generation_invalidates_a_frozen_gmail_action() -> None:
    connection = InMemoryGoogleOAuthStateStore()
    connected = connection.set_connection(
        connected=True,
        granted_scopes=frozenset({"https://www.googleapis.com/auth/gmail.send"}),
    )
    provider = ControlledGmailWriteProvider()
    components = _components(
        _proposal(),
        _dispatcher(provider, connection_state=connection.get_connection),
    )

    pending = components.receiver.receive(_event("send", suffix="01"))
    action = components.broker.current_pending_action
    assert pending.disposition == "pending_action"
    assert action is not None
    assert json.loads(action.payload or "{}") == {
        "bcc": ["blind@example.com"],
        "body": "Hello\n\nPlease review the attached plan.",
        "cc": ["copy@example.com"],
        "connection_generation": connected.generation,
        "google_subject": IDENTITY,
        "mime_type": "text/plain",
        "subject": "Quarterly check-in",
        "threading": "new_message",
        "to": ["recipient@example.com"],
    }
    connection.set_connection(
        connected=True,
        granted_scopes=frozenset({"https://www.googleapis.com/auth/gmail.send"}),
    )

    stale = components.receiver.receive(_event("yes", suffix="02"))

    assert stale.disposition == "action_invalidated"
    assert provider.calls == []


def test_reconnect_cannot_replace_the_credential_between_binding_and_gmail_send() -> (
    None
):
    replaced = Event()
    release_replacement = Event()

    class PausingCredentialStore(InMemoryGoogleCredentialStore):
        def replace(self, credential: OAuthCredentialRecord) -> None:
            super().replace(credential)
            replaced.set()
            if not release_replacement.wait(timeout=5):
                raise AssertionError("credential replacement was not released")

    credentials = PausingCredentialStore(
        OAuthCredentialRecord(
            subject=IDENTITY,
            granted_scopes=frozenset({GMAIL_SEND_SCOPE}),
            refresh_token="credential-a",
        )
    )
    connection = InMemoryGoogleOAuthStateStore()
    connection.set_connection(
        connected=True, granted_scopes=frozenset({GMAIL_SEND_SCOPE})
    )
    audit = InMemoryAuditBoundary()
    trace_store = InMemoryDiagnosticTraceStore()
    trace = DiagnosticTraceRecorder(
        writer=trace_store.writer(),
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("ticket18-race-trace"),
    )
    oauth = GoogleOAuthLifecycle(
        configured_identity=IDENTITY,
        state_store=connection,
        credential_store=credentials,
        provider=ControlledGoogleOAuthProvider(
            grant=OAuthGrant(
                subject=IDENTITY,
                granted_scopes=frozenset({GMAIL_SEND_SCOPE}),
                access_token="access-b",
                refresh_token="credential-b",
            )
        ),
        audit=audit,
        trace=trace,
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("ticket18-race-oauth"),
        state_factory=lambda: "ticket18-race-state",
    )
    provider = ControlledGmailWriteProvider()
    gmail = GmailWriteConnector(
        configured_identity=IDENTITY,
        credential_store=credentials,
        provider=provider,
        audit=audit,
        trace=trace,
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("ticket18-race-gmail"),
        connection_binding=oauth.connection_binding,
    )
    router = RoutedActionDispatcher(
        terminal=ControlledActionDispatcher(),
        gmail=gmail,
        gmail_lifecycle=gmail,
    )
    components = _components(_proposal(), router)
    pending = components.receiver.receive(_event("send", suffix="race-pending"))
    authorization = oauth.start_authorization(
        operation_id="ticket18-race-reconnect",
        requested_scopes=(GMAIL_SEND_SCOPE,),
    )

    callback_result: dict[str, object] = {}
    approval_result: dict[str, object] = {}

    def reconnect() -> None:
        callback_result["response"] = oauth.handle_callback(
            method="GET",
            query={"state": authorization.state, "code": "code-b"},
        )

    def approve() -> None:
        approval_result["result"] = components.receiver.receive(
            _event("yes", suffix="race-approval")
        )

    callback_thread = Thread(target=reconnect)
    approval_thread: Thread | None = None
    try:
        assert pending.disposition == "pending_action"
        callback_thread.start()
        assert replaced.wait(timeout=5)
        approval_thread = Thread(target=approve)
        approval_thread.start()
        approval_thread.join(timeout=0.2)
        assert approval_thread.is_alive()
        assert provider.calls == []
    finally:
        release_replacement.set()
        callback_thread.join(timeout=5)
        if approval_thread is not None:
            approval_thread.join(timeout=5)
        trace_store._close_writer_service()

    assert not callback_thread.is_alive()
    assert approval_thread is not None and not approval_thread.is_alive()
    assert callback_result["response"].status_code == 204  # type: ignore[union-attr]
    assert approval_result["result"].disposition == "action_invalidated"  # type: ignore[union-attr]
    assert connection.get_connection().generation == 2
    assert credentials.current is not None
    assert credentials.current.refresh_token == "credential-b"
    assert provider.calls == []


def test_manual_trace_retains_complete_credential_bearing_gmail_provider_exchange() -> (
    None
):
    class Transport:
        def __init__(self) -> None:
            self.responses = [
                GoogleHttpResponse(
                    status_code=200,
                    headers={"Content-Type": "application/json"},
                    body=b'{"access_token":"controlled-access-token"}',
                ),
                GoogleHttpResponse(
                    status_code=200,
                    headers={"Content-Type": "application/json"},
                    body=b'{"id":"sent-traced","threadId":"thread-new"}',
                ),
            ]

        def request(self, **_kwargs: object) -> GoogleHttpResponse:
            return self.responses.pop(0)

    trace_store = InMemoryDiagnosticTraceStore()
    trace = DiagnosticTraceRecorder(
        writer=trace_store.writer(),
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("ticket18-trace-evidence"),
    )
    connection = InMemoryGoogleOAuthStateStore()
    connection.set_connection(
        connected=True,
        granted_scopes=frozenset({"https://www.googleapis.com/auth/gmail.send"}),
    )
    connector = GmailWriteConnector(
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
        provider=GmailApiWriteProvider(
            client_id="controlled-client-id",
            client_secret="controlled-client-secret",
            transport=Transport(),
        ),
        audit=InMemoryAuditBoundary(),
        trace=trace,
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("ticket18-gmail-evidence"),
        connection_state=connection.get_connection,
    )

    connector.dispatch(connector.bind_proposal(_proposal()))

    trace_payload = str(
        _open_manual_trace_boundary(trace_store)
        .list_traces(operation_type="gmail_write_connector")[0]
        .to_mapping()
    )
    assert "controlled-refresh-token" in trace_payload
    assert "controlled-access-token" in trace_payload
    assert "controlled-client-secret" in trace_payload
    assert "sent-traced" in trace_payload


def test_manual_trace_retains_gmail_provider_error_evidence() -> None:
    class Transport:
        def __init__(self) -> None:
            self.responses = [
                GoogleHttpResponse(
                    status_code=200,
                    headers={"Content-Type": "application/json"},
                    body=b'{"access_token":"controlled-access-token"}',
                ),
                GoogleHttpResponse(
                    status_code=503,
                    headers={"Content-Type": "application/json"},
                    body=b'{"error":"controlled-provider-failure"}',
                ),
            ]

        def request(self, **_kwargs: object) -> GoogleHttpResponse:
            return self.responses.pop(0)

    trace_store = InMemoryDiagnosticTraceStore()
    trace = DiagnosticTraceRecorder(
        writer=trace_store.writer(),
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("ticket18-trace-failure"),
    )
    connection = InMemoryGoogleOAuthStateStore()
    connection.set_connection(
        connected=True,
        granted_scopes=frozenset({"https://www.googleapis.com/auth/gmail.send"}),
    )
    connector = GmailWriteConnector(
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
        provider=GmailApiWriteProvider(
            client_id="controlled-client-id",
            client_secret="controlled-client-secret",
            transport=Transport(),
        ),
        audit=InMemoryAuditBoundary(),
        trace=trace,
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("ticket18-gmail-failure"),
        connection_state=connection.get_connection,
    )

    with pytest.raises(ActionDispatcherError) as caught:
        connector.dispatch(connector.bind_proposal(_proposal()))

    assert caught.value.may_have_dispatched is True
    trace_payload = str(
        _open_manual_trace_boundary(trace_store)
        .list_traces(operation_type="gmail_write_connector")[0]
        .to_mapping()
    )
    assert "controlled-refresh-token" in trace_payload
    assert "controlled-access-token" in trace_payload
    assert "controlled-client-secret" in trace_payload
    assert "controlled-provider-failure" in trace_payload


def test_invalid_grant_cleanup_audit_failure_is_bounded_and_closes_the_outbox() -> None:
    class RefreshInvalidationAuditFailure(InMemoryAuditBoundary):
        def append(self, evidence: object) -> None:
            if getattr(evidence, "kind", None) == "google_oauth_refresh_invalidated":
                raise AuditWriteError("refresh invalidation audit unavailable")
            super().append(evidence)  # type: ignore[arg-type]

    class InvalidGrantTransport:
        def __init__(self) -> None:
            self.calls = 0

        def request(self, **_kwargs: object) -> GoogleHttpResponse:
            self.calls += 1
            return GoogleHttpResponse(
                status_code=400,
                headers={"Content-Type": "application/json"},
                body=b'{"error":"invalid_grant"}',
            )

    credentials = InMemoryGoogleCredentialStore(
        OAuthCredentialRecord(
            subject=IDENTITY,
            granted_scopes=frozenset({GMAIL_SEND_SCOPE}),
            refresh_token="refresh-token",
        )
    )
    connection = InMemoryGoogleOAuthStateStore()
    connection.set_connection(
        connected=True, granted_scopes=frozenset({GMAIL_SEND_SCOPE})
    )
    audit = RefreshInvalidationAuditFailure()
    trace_store = InMemoryDiagnosticTraceStore()
    trace = DiagnosticTraceRecorder(
        writer=trace_store.writer(),
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("ticket18-invalid-grant-trace"),
    )
    oauth = GoogleOAuthLifecycle(
        configured_identity=IDENTITY,
        state_store=connection,
        credential_store=credentials,
        provider=ControlledGoogleOAuthProvider(
            grant=OAuthGrant(
                subject=IDENTITY,
                granted_scopes=frozenset({GMAIL_SEND_SCOPE}),
                access_token="access-token",
                refresh_token="refresh-token",
            )
        ),
        audit=audit,
        trace=trace,
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("ticket18-invalid-grant-oauth"),
    )
    transport = InvalidGrantTransport()
    gmail = GmailWriteConnector(
        configured_identity=IDENTITY,
        credential_store=credentials,
        provider=GmailApiWriteProvider(
            client_id="client-id",
            client_secret="client-secret",
            transport=transport,
        ),
        audit=audit,
        trace=trace,
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("ticket18-invalid-grant-gmail"),
        connection_binding=oauth.connection_binding,
        on_invalid_grant=lambda: oauth.handle_refresh_failure("invalid_grant"),
    )
    router = RoutedActionDispatcher(
        terminal=ControlledActionDispatcher(),
        gmail=gmail,
        gmail_lifecycle=gmail,
    )
    components = _components(_proposal(), router, audit=audit)

    assert components.receiver.receive(
        _event("send", suffix="invalid-grant-01")
    ).disposition == ("pending_action")
    result = components.receiver.receive(_event("yes", suffix="invalid-grant-02"))

    assert result.status_code == 202
    assert result.disposition == "action_dispatch_failed"
    assert transport.calls == 1
    assert credentials.current is None
    assert not connection.get_connection().connected
    assert components.broker.current_pending_action is None
    session = components.broker.working_sessions.load()
    assert session is not None
    assert len(session.action_outbox) == 1
    assert session.action_outbox[0].status.value == "failed"
    assert session.action_outbox[0].payload is None
    trace_store._close_writer_service()


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
        dispatcher.dispatch(dispatcher.bind_proposal(_proposal()))

    assert caught.value.may_have_dispatched is True
    assert len(provider.calls) == 1


def test_live_provider_posts_only_raw_frozen_rfc822_message_and_thread_id() -> None:
    class Transport:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def request(self, **kwargs: object) -> GoogleHttpResponse:
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return GoogleHttpResponse(
                    status_code=200,
                    headers={"Content-Type": "application/json"},
                    body=b'{"access_token":"live-token"}',
                )
            return GoogleHttpResponse(
                status_code=200,
                headers={"Content-Type": "application/json"},
                body=json.dumps({"id": "sent-004", "threadId": "thread-001"}).encode(),
            )

    transport = Transport()
    provider = GmailApiWriteProvider(
        client_id="client-id", client_secret="client-secret", transport=transport
    )
    dispatcher = _dispatcher(provider)  # type: ignore[arg-type]

    dispatcher.dispatch(dispatcher.bind_proposal(_proposal(reply=True)))

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
    assert result.reply_text == "The exact Gmail action is ready."
    assert result.execution_host is None
    assert result.host_reason_code is None


def test_orchestration_normalizes_redundant_model_gmail_fields() -> None:
    plan = AgentsSdkPlan(
        reply_text="The exact Gmail action is ready.",
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
                "attachments": [],
                "threading": "new_message",
            },
        ),
    )

    result = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **_kwargs: object(),
        run_sync=lambda *_args, **_kwargs: SimpleNamespace(final_output=plan),
        model_settings_factory=lambda **values: values,
        reasoning_factory=lambda **values: values,
        run_config_factory=lambda **values: values,
    ).run(
        OrchestrationRequest(
            state=RequestState(
                request_id="request-model-shape",
                event_id="event-model-shape",
                message_id="message-model-shape",
                operator_id=OPERATOR,
                session_id="working-session-model-shape",
                chat_id=OPERATOR,
                created_at=NOW,
                updated_at=NOW,
                status="accepted",
                phase="orchestration",
            ),
            text="prepare this plain-text email",
        )
    )

    assert result.proposal is not None
    request = gmail_write_request_from_proposal(result.proposal)
    assert isinstance(request, GmailNewSendRequest)
    payload = json.loads(result.proposal.payload)
    assert set(payload) == {
        "to",
        "cc",
        "bcc",
        "subject",
        "body",
        "mime_type",
        "threading",
    }
    assert payload["threading"] == "new_message"


def test_routed_action_surface_freezes_terminal_and_gmail_proposals() -> None:
    terminal_dispatcher = ControlledActionDispatcher()
    gmail_provider = ControlledGmailWriteProvider(
        result=GmailDeliveryResult(message_id="sent-routed", thread_id="thread-new")
    )
    terminal_gmail = _dispatcher(gmail_provider)

    terminal_components = _components(
        _terminal_proposal(),
        RoutedActionDispatcher(
            terminal=terminal_dispatcher,
            gmail=terminal_gmail,
            gmail_lifecycle=terminal_gmail,
        ),
    )
    terminal_pending = terminal_components.receiver.receive(
        _event("run the terminal action", suffix="routed-terminal-01")
    )
    terminal_session = terminal_components.broker.working_sessions.load()
    assert terminal_session is not None
    terminal_components.broker.working_sessions.compare_and_set(
        terminal_session,
        replace(
            terminal_session,
            readiness=ReadinessState(ubuntu="ready", windows="unavailable"),
        ),
    )
    terminal_approved = terminal_components.receiver.receive(
        _event("yes", suffix="routed-terminal-02")
    )

    gmail_components = _components(
        _proposal(),
        RoutedActionDispatcher(
            terminal=ControlledActionDispatcher(),
            gmail=terminal_gmail,
            gmail_lifecycle=terminal_gmail,
        ),
    )
    gmail_pending = gmail_components.receiver.receive(
        _event("send the email", suffix="routed-gmail-01")
    )
    gmail_approved = gmail_components.receiver.receive(
        _event("yes", suffix="routed-gmail-02")
    )

    assert terminal_pending.disposition == "pending_action"
    assert terminal_approved.disposition == "action_dispatched"
    assert len(terminal_dispatcher.dispatched) == 1
    assert gmail_pending.disposition == "pending_action"
    assert gmail_approved.disposition == "action_dispatched"
    assert len(gmail_provider.calls) == 1


@pytest.mark.parametrize(
    ("connection_state", "case"),
    [
        pytest.param(
            lambda: GoogleConnectionState(
                connected=False,
                generation=1,
                granted_scopes=frozenset(),
            ),
            "disconnected",
        ),
        pytest.param(
            lambda: GoogleConnectionState(
                connected=True,
                generation=1,
                granted_scopes=frozenset(
                    {"https://www.googleapis.com/auth/calendar.events"}
                ),
            ),
            "missing-gmail-send",
        ),
        pytest.param(
            lambda: (_ for _ in ()).throw(RuntimeError("connection store unavailable")),
            "connection-state-unavailable",
        ),
    ],
)
def test_gmail_binding_failures_are_bounded_and_close_the_active_request(
    connection_state: object, case: str
) -> None:
    components = _components(
        _proposal(),
        _dispatcher(
            ControlledGmailWriteProvider(),
            connection_state=connection_state,
        ),
    )

    result = components.receiver.receive(_event("send", suffix=f"binding-{case}"))

    assert result.status_code == 202
    assert result.disposition == "failed"
    assert result.request is not None
    session = components.broker.working_sessions.load()
    assert session is not None
    assert session.active_request is None
    assert session.pending_action is None

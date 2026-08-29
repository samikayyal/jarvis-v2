from __future__ import annotations

import json
from datetime import timedelta
from threading import (
    Event,
    Thread,
)

import pytest

from jarvis_control_plane import (
    GMAIL_SEND_SCOPE,
    ControlledActionDispatcher,
    ControlledGmailWriteProvider,
    ControlledGoogleOAuthProvider,
    DeterministicIdGenerator,
    DiagnosticTraceRecorder,
    FixedClock,
    GmailDeliveryResult,
    GmailWriteConnector,
    GoogleOAuthLifecycle,
    InMemoryAuditBoundary,
    InMemoryDiagnosticTraceStore,
    InMemoryGoogleCredentialStore,
    InMemoryGoogleOAuthStateStore,
    OAuthCredentialRecord,
    OAuthGrant,
    RoutedActionDispatcher,
)

from .helpers import (
    IDENTITY,
    NOW,
    _components,
    _dispatcher,
    _event,
    _proposal,
)


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

from __future__ import annotations

import pytest

from jarvis_control_plane import (
    GMAIL_SEND_SCOPE,
    ActionDispatcherError,
    AuditWriteError,
    ControlledActionDispatcher,
    ControlledGmailWriteProvider,
    ControlledGoogleOAuthProvider,
    DeterministicIdGenerator,
    DiagnosticTraceRecorder,
    FixedClock,
    GmailApiWriteProvider,
    GmailWriteConnector,
    GoogleHttpResponse,
    GoogleOAuthLifecycle,
    InMemoryAuditBoundary,
    InMemoryDiagnosticTraceStore,
    InMemoryGoogleCredentialStore,
    InMemoryGoogleOAuthStateStore,
    OAuthCredentialRecord,
    OAuthGrant,
    RoutedActionDispatcher,
)
from jarvis_control_plane.manual_admin import _open_manual_trace_boundary

from .helpers import (
    IDENTITY,
    NOW,
    _components,
    _dispatcher,
    _event,
    _proposal,
)


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

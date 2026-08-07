"""Ticket 16 state-bound Google OAuth lifecycle contract tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jarvis_control_plane import (
    ControlledGoogleOAuthProvider,
    DeterministicIdGenerator,
    FileGoogleCredentialStore,
    FixedClock,
    GoogleOAuthError,
    GoogleOAuthLifecycle,
    InMemoryAuditBoundary,
    InMemoryDiagnosticTraceStore,
    InMemoryGoogleCredentialStore,
    InMemoryGoogleOAuthStateStore,
    OAuthCredentialRecord,
    OAuthGrant,
    SQLiteGoogleOAuthStateStore,
    TraceWriteError,
)
from jarvis_control_plane.manual_admin import _open_manual_trace_boundary
from jarvis_control_plane.traces import DiagnosticTraceRecorder

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
IDENTITY = "google-subject-123"
READ_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
)


def build_lifecycle(
    *,
    provider: ControlledGoogleOAuthProvider | None = None,
    credentials: InMemoryGoogleCredentialStore | None = None,
    clock: FixedClock | None = None,
    state_store: InMemoryGoogleOAuthStateStore
    | SQLiteGoogleOAuthStateStore
    | None = None,
    audit: InMemoryAuditBoundary | None = None,
    trace: DiagnosticTraceRecorder | None = None,
):
    clock = clock or FixedClock(NOW)
    ids = DeterministicIdGenerator("ticket16")
    trace_store = InMemoryDiagnosticTraceStore()
    lifecycle = GoogleOAuthLifecycle(
        configured_identity=IDENTITY,
        state_store=state_store or InMemoryGoogleOAuthStateStore(),
        credential_store=credentials or InMemoryGoogleCredentialStore(),
        provider=provider
        or ControlledGoogleOAuthProvider(
            grant=OAuthGrant(
                subject=IDENTITY,
                granted_scopes=frozenset(READ_SCOPES),
                access_token="controlled-access-token",
                refresh_token="controlled-refresh-token",
            )
        ),
        audit=audit or InMemoryAuditBoundary(),
        trace=trace
        or DiagnosticTraceRecorder(
            writer=trace_store.writer(),
            clock=clock,
            ids=ids,
        ),
        clock=clock,
        ids=ids,
        state_factory=lambda: "state-ticket16",
    )
    return lifecycle, trace_store


def test_callback_accepts_only_get_and_documented_fields_without_a_body() -> None:
    lifecycle, trace_store = build_lifecycle()
    try:
        authorization = lifecycle.start_authorization(
            operation_id="connect-google", requested_scopes=READ_SCOPES
        )

        wrong_method = lifecycle.handle_callback(
            method="POST", query={"state": authorization.state, "code": "code-1"}
        )
        unexpected_field = lifecycle.handle_callback(
            method="GET",
            query={
                "state": authorization.state,
                "code": "code-1",
                "jarvis_command": "send mail",
            },
        )

        assert wrong_method.status_code == 405
        assert unexpected_field.status_code == 400
        assert wrong_method.body == unexpected_field.body == b""
        assert wrong_method.headers["Cache-Control"] == "no-store"
        assert wrong_method.headers["Referrer-Policy"] == "no-referrer"
    finally:
        trace_store._close_writer_service()


def test_callback_consumes_state_once_and_replaces_connector_credential() -> None:
    credentials = InMemoryGoogleCredentialStore()
    lifecycle, trace_store = build_lifecycle(credentials=credentials)
    try:
        authorization = lifecycle.start_authorization(
            operation_id="connect-google", requested_scopes=READ_SCOPES
        )

        accepted = lifecycle.handle_callback(
            method="GET", query={"state": authorization.state, "code": "code-1"}
        )
        replay = lifecycle.handle_callback(
            method="GET", query={"state": authorization.state, "code": "code-1"}
        )

        assert accepted.status_code == 204
        assert accepted.body == b""
        assert replay.status_code == 400
        assert credentials.current == OAuthCredentialRecord(
            subject=IDENTITY,
            granted_scopes=frozenset(READ_SCOPES),
            refresh_token="controlled-refresh-token",
            connection_generation=1,
        )
        assert lifecycle.connection.connected
        assert lifecycle.connection.generation == 1
    finally:
        trace_store._close_writer_service()


def test_manual_trace_retains_complete_oauth_exchange_and_revocation_payloads() -> None:
    credentials = InMemoryGoogleCredentialStore()
    lifecycle, trace_store = build_lifecycle(credentials=credentials)
    try:
        authorization = lifecycle.start_authorization(
            operation_id="connect-google", requested_scopes=READ_SCOPES
        )

        assert (
            lifecycle.handle_callback(
                method="GET", query={"state": authorization.state, "code": "code-1"}
            ).status_code
            == 204
        )
        lifecycle.disconnect()

        manual = _open_manual_trace_boundary(trace_store)
        exchange = manual.list_traces(request_id="connect-google")[0]
        revocation = manual.list_traces(request_id="google-oauth-disconnect")[0]
        exchange_payload = str(exchange.to_mapping())
        revocation_payload = str(revocation.to_mapping())
        assert "code-1" in exchange_payload
        assert "controlled-access-token" in exchange_payload
        assert "controlled-refresh-token" in exchange_payload
        assert "controlled-refresh-token" in revocation_payload
    finally:
        trace_store._close_writer_service()


def test_expired_state_is_not_exchanged() -> None:
    provider = ControlledGoogleOAuthProvider(
        grant=OAuthGrant(
            subject=IDENTITY,
            granted_scopes=frozenset(READ_SCOPES),
            access_token="controlled-access-token",
            refresh_token="controlled-refresh-token",
        )
    )
    clock = FixedClock(NOW)
    lifecycle, trace_store = build_lifecycle(provider=provider, clock=clock)
    try:
        authorization = lifecycle.start_authorization(
            operation_id="connect-google", requested_scopes=READ_SCOPES
        )
        clock.advance(minutes=11)

        response = lifecycle.handle_callback(
            method="GET", query={"state": authorization.state, "code": "code-1"}
        )

        assert response.status_code == 400
        assert provider.exchange_calls == []
    finally:
        trace_store._close_writer_service()


def test_sqlite_state_is_single_use_across_restart_and_never_stores_credentials(
    tmp_path,
) -> None:
    database = tmp_path / "oauth-state.sqlite"
    original_store = SQLiteGoogleOAuthStateStore(database)
    lifecycle, first_trace_store = build_lifecycle(state_store=original_store)
    authorization = lifecycle.start_authorization(
        operation_id="connect-google", requested_scopes=READ_SCOPES
    )
    first_trace_store._close_writer_service()
    original_store.close()

    resumed_store = SQLiteGoogleOAuthStateStore(database)
    credentials = InMemoryGoogleCredentialStore()
    resumed, second_trace_store = build_lifecycle(
        state_store=resumed_store, credentials=credentials
    )
    try:
        accepted = resumed.handle_callback(
            method="GET", query={"state": authorization.state, "code": "code-1"}
        )

        assert accepted.status_code == 204
        assert resumed.connection.connected
        persisted = database.read_bytes()
        assert b"controlled-access-token" not in persisted
        assert b"controlled-refresh-token" not in persisted
    finally:
        second_trace_store._close_writer_service()
        resumed_store.close()


def test_audit_admission_failure_blocks_code_exchange() -> None:
    provider = ControlledGoogleOAuthProvider(
        grant=OAuthGrant(
            subject=IDENTITY,
            granted_scopes=frozenset(READ_SCOPES),
            access_token="controlled-access-token",
            refresh_token="controlled-refresh-token",
        )
    )
    credentials = InMemoryGoogleCredentialStore()
    lifecycle, trace_store = build_lifecycle(
        provider=provider,
        credentials=credentials,
        audit=InMemoryAuditBoundary(fail_on_append=2),
    )
    try:
        authorization = lifecycle.start_authorization(
            operation_id="connect-google", requested_scopes=READ_SCOPES
        )

        response = lifecycle.handle_callback(
            method="GET", query={"state": authorization.state, "code": "code-1"}
        )

        assert response.status_code == 503
        assert provider.exchange_calls == []
        assert credentials.current is None
    finally:
        trace_store._close_writer_service()


def test_trace_write_failure_after_oauth_rejection_returns_degraded_response() -> None:
    class FailingAppendWriter:
        def __init__(self, writer: object) -> None:
            self._writer = writer

        def reserve(self, **kwargs: object):
            return self._writer.reserve(**kwargs)  # type: ignore[attr-defined]

        def append(self, *_: object) -> None:
            raise TraceWriteError(
                "controlled trace append failure", operation_started=True
            )

        def release(self, reservation: object) -> None:
            self._writer.release(reservation)  # type: ignore[attr-defined]

    provider = ControlledGoogleOAuthProvider(
        grant=OAuthGrant(
            subject=IDENTITY,
            granted_scopes=frozenset(READ_SCOPES),
            access_token="controlled-access-token",
            refresh_token="controlled-refresh-token",
        ),
        exchange_failure="wrong_identity",
    )
    failing_store = InMemoryDiagnosticTraceStore()
    trace = DiagnosticTraceRecorder(
        writer=FailingAppendWriter(failing_store.writer()),
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("ticket16-failing-trace"),
    )
    lifecycle, unused_trace_store = build_lifecycle(provider=provider, trace=trace)
    try:
        authorization = lifecycle.start_authorization(
            operation_id="connect-google", requested_scopes=READ_SCOPES
        )

        response = lifecycle.handle_callback(
            method="GET", query={"state": authorization.state, "code": "code-1"}
        )

        assert response.status_code == 503
        assert response.body == b""
        assert len(provider.exchange_calls) == 1
    finally:
        unused_trace_store._close_writer_service()
        failing_store._close_writer_service()


def test_callback_state_store_failure_is_content_free_and_never_exchanges_code() -> (
    None
):
    class UnavailableStateStore(InMemoryGoogleOAuthStateStore):
        def consume(self, *, state: str, now: datetime):
            raise GoogleOAuthError("state_store_unavailable")

    provider = ControlledGoogleOAuthProvider(
        grant=OAuthGrant(
            subject=IDENTITY,
            granted_scopes=frozenset(READ_SCOPES),
            access_token="controlled-access-token",
            refresh_token="controlled-refresh-token",
        )
    )
    lifecycle, trace_store = build_lifecycle(
        provider=provider, state_store=UnavailableStateStore()
    )
    try:
        authorization = lifecycle.start_authorization(
            operation_id="connect-google", requested_scopes=READ_SCOPES
        )

        response = lifecycle.handle_callback(
            method="GET", query={"state": authorization.state, "code": "code-1"}
        )

        assert response.status_code == 503
        assert response.body == b""
        assert provider.exchange_calls == []
    finally:
        trace_store._close_writer_service()


@pytest.mark.parametrize(
    "grant",
    (
        OAuthGrant(
            subject="wrong-google-subject",
            granted_scopes=frozenset(READ_SCOPES),
            access_token="controlled-access-token",
            refresh_token="controlled-refresh-token",
        ),
        OAuthGrant(
            subject=IDENTITY,
            granted_scopes=frozenset({READ_SCOPES[0]}),
            access_token="controlled-access-token",
            refresh_token="controlled-refresh-token",
        ),
    ),
)
def test_wrong_identity_or_missing_scope_never_replaces_existing_credential(
    grant: OAuthGrant,
) -> None:
    existing = OAuthCredentialRecord(
        subject=IDENTITY,
        granted_scopes=frozenset(READ_SCOPES),
        refresh_token="existing-controlled-refresh-token",
    )
    credentials = InMemoryGoogleCredentialStore(existing)
    lifecycle, trace_store = build_lifecycle(
        provider=ControlledGoogleOAuthProvider(grant=grant), credentials=credentials
    )
    try:
        authorization = lifecycle.start_authorization(
            operation_id="reconnect-google", requested_scopes=READ_SCOPES
        )

        response = lifecycle.handle_callback(
            method="GET", query={"state": authorization.state, "code": "code-1"}
        )

        assert response.status_code == 400
        assert response.body == b""
        assert credentials.current == existing
        assert not lifecycle.connection.connected
    finally:
        trace_store._close_writer_service()


def test_invalid_grant_and_explicit_disconnect_delete_local_credential() -> None:
    credentials = InMemoryGoogleCredentialStore(
        OAuthCredentialRecord(
            subject=IDENTITY,
            granted_scopes=frozenset(READ_SCOPES),
            refresh_token="controlled-refresh-token",
        )
    )
    provider = ControlledGoogleOAuthProvider(
        grant=OAuthGrant(
            subject=IDENTITY,
            granted_scopes=frozenset(READ_SCOPES),
            access_token="controlled-access-token",
            refresh_token="controlled-refresh-token",
        )
    )
    lifecycle, trace_store = build_lifecycle(provider=provider, credentials=credentials)
    try:
        lifecycle.handle_refresh_failure("invalid_grant")
        assert credentials.current is None
        assert not lifecycle.connection.connected

        credentials.replace(
            OAuthCredentialRecord(
                subject=IDENTITY,
                granted_scopes=frozenset(READ_SCOPES),
                refresh_token="controlled-refresh-token",
            )
        )
        lifecycle.disconnect()

        assert credentials.current is None
        assert provider.revoke_calls == ["controlled-refresh-token"]
        assert lifecycle.connection.generation == 2
    finally:
        trace_store._close_writer_service()


def test_file_credential_replacement_is_atomic_when_replace_fails(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileGoogleCredentialStore(tmp_path / "google-credentials")
    original = OAuthCredentialRecord(
        subject=IDENTITY,
        granted_scopes=frozenset(READ_SCOPES),
        refresh_token="original-controlled-refresh-token",
    )
    replacement = OAuthCredentialRecord(
        subject=IDENTITY,
        granted_scopes=frozenset(READ_SCOPES),
        refresh_token="replacement-controlled-refresh-token",
    )
    store.replace(original)

    def fail_replace(*_: object) -> None:
        raise OSError("controlled replace failure")

    monkeypatch.setattr("jarvis_control_plane.google_oauth.os.replace", fail_replace)

    with pytest.raises(OSError, match="controlled replace failure"):
        store.replace(replacement)

    assert store.current == original
    assert list((tmp_path / "google-credentials").glob("*.tmp")) == []

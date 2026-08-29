# ruff: noqa: F401, I001, RUF100 -- split modules retain ticket context.
"""Ticket 16 state-bound Google OAuth lifecycle contract tests."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from jarvis_control_plane import (
    AuditWriteError,
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
from jarvis_control_plane.google_oauth import GoogleLiveOAuthProvider
from jarvis_control_plane.manual_admin import _open_manual_trace_boundary
from jarvis_control_plane.traces import DiagnosticTraceRecorder

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
IDENTITY = "google-subject-123"
READ_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
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
    state_factory=None,
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
        state_factory=state_factory or (lambda: "state-ticket16"),
    )
    return lifecycle, trace_store


def test_callback_accepts_only_get_and_documented_fields_without_a_body() -> None:
    lifecycle, trace_store = build_lifecycle()
    try:
        authorization = lifecycle.start_authorization(
            operation_id="connect-google", requested_scopes=(*READ_SCOPES, "openid")
        )
        assert "openid" in authorization.requested_scopes
        url = GoogleLiveOAuthProvider(
            client_id="client-id",
            client_secret="client-secret",
            redirect_uri="https://oauth.jarvis.invalid/callback",
        ).authorization_url(authorization)
        query = parse_qs(urlsplit(url).query)
        assert "openid" in query["scope"][0].split()
        assert query["state"] == [authorization.state]

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


def test_callback_accepts_google_authorization_response_metadata() -> None:
    credentials = InMemoryGoogleCredentialStore()
    lifecycle, trace_store = build_lifecycle(credentials=credentials)
    try:
        authorization = lifecycle.start_authorization(
            operation_id="connect-google-live-shape", requested_scopes=READ_SCOPES
        )

        accepted = lifecycle.handle_callback(
            method="GET",
            query={
                "state": authorization.state,
                "code": "code-1",
                "scope": " ".join(sorted(authorization.requested_scopes)),
                "iss": "https://accounts.google.com",
                "authuser": "0",
                "prompt": "consent",
            },
        )

        assert accepted.status_code == 204
        assert lifecycle.connection.connected
        assert credentials.current is not None
    finally:
        trace_store._close_writer_service()


def test_callback_rejects_invalid_google_response_metadata_without_consuming_state() -> (
    None
):
    lifecycle, trace_store = build_lifecycle()
    try:
        authorization = lifecycle.start_authorization(
            operation_id="connect-google-invalid-metadata",
            requested_scopes=READ_SCOPES,
        )
        base = {
            "state": authorization.state,
            "code": "code-1",
            "scope": " ".join(sorted(authorization.requested_scopes)),
            "iss": "https://accounts.google.com",
            "authuser": "0",
            "prompt": "consent",
        }

        assert (
            lifecycle.handle_callback(
                method="GET", query={**base, "iss": "https://issuer.invalid"}
            ).status_code
            == 400
        )
        assert (
            lifecycle.handle_callback(
                method="GET", query={**base, "authuser": "-1"}
            ).status_code
            == 400
        )
        assert (
            lifecycle.handle_callback(
                method="GET", query={**base, "prompt": "select_account"}
            ).status_code
            == 400
        )
        assert lifecycle.handle_callback(method="GET", query=base).status_code == 204
    finally:
        trace_store._close_writer_service()


def test_calendar_scope_is_outside_the_v1_authorization_surface() -> None:
    calendar_write = "https://www.googleapis.com/auth/calendar.events"
    state = InMemoryGoogleOAuthStateStore()
    state.set_connection(
        connected=True,
        granted_scopes=frozenset({*READ_SCOPES, "openid"}),
    )
    lifecycle, trace_store = build_lifecycle(state_store=state)
    try:
        with pytest.raises(
            ValueError, match="outside the exact Google connector allowlist"
        ):
            lifecycle.start_authorization(
                operation_id="enable-calendar-write",
                requested_scopes=(*READ_SCOPES, "openid", calendar_write),
            )
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

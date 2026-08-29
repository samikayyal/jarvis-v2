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
        lifecycle.handle_refresh_failure("invalid_grant", connection_generation=0)
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


def test_old_invalid_grant_cannot_invalidate_a_newer_google_connection() -> None:
    old = OAuthCredentialRecord(
        subject=IDENTITY,
        granted_scopes=frozenset(READ_SCOPES),
        refresh_token="old-refresh-token",
        connection_generation=1,
    )
    credentials = InMemoryGoogleCredentialStore(old)
    state_store = InMemoryGoogleOAuthStateStore()
    state_store.set_connection(connected=True, granted_scopes=frozenset(READ_SCOPES))
    lifecycle, trace_store = build_lifecycle(
        credentials=credentials, state_store=state_store
    )
    try:
        newer_state = state_store.set_connection(
            connected=True, granted_scopes=frozenset(READ_SCOPES)
        )
        newer = OAuthCredentialRecord(
            subject=IDENTITY,
            granted_scopes=frozenset(READ_SCOPES),
            refresh_token="new-refresh-token",
            connection_generation=newer_state.generation,
        )
        credentials.replace(newer)

        lifecycle.handle_refresh_failure(
            "invalid_grant", connection_generation=old.connection_generation
        )

        assert credentials.current == newer
        assert lifecycle.connection == newer_state
    finally:
        trace_store._close_writer_service()


def test_callback_audit_failure_cannot_invalidate_a_newer_callback_connection() -> None:
    import threading

    class FailFirstCompletionAudit(InMemoryAuditBoundary):
        def __init__(self) -> None:
            super().__init__()
            self.completion_started = threading.Event()
            self.release_completion = threading.Event()

        def append(self, evidence):  # type: ignore[no-untyped-def]
            if (
                evidence.kind == "google_oauth_code_exchange_completed"
                and evidence.request_id == "callback-old"
            ):
                self.completion_started.set()
                assert self.release_completion.wait(timeout=2)
                raise AuditWriteError("controlled completion audit failure")
            return super().append(evidence)

    class GrantsByCode(ControlledGoogleOAuthProvider):
        def exchange_code(self, *, code: str, requested_scopes: frozenset[str]):
            self.grant = OAuthGrant(
                subject=IDENTITY,
                granted_scopes=frozenset(READ_SCOPES),
                access_token=f"access-{code}",
                refresh_token=f"refresh-{code}",
            )
            return super().exchange_code(code=code, requested_scopes=requested_scopes)

    audit = FailFirstCompletionAudit()
    provider = GrantsByCode(
        grant=OAuthGrant(
            subject=IDENTITY,
            granted_scopes=frozenset(READ_SCOPES),
            access_token="unused-access",
            refresh_token="unused-refresh",
        )
    )
    credentials = InMemoryGoogleCredentialStore()
    state_store = InMemoryGoogleOAuthStateStore()
    lifecycle, trace_store = build_lifecycle(
        provider=provider,
        credentials=credentials,
        state_store=state_store,
        audit=audit,
        state_factory=iter(("state-old", "state-new")).__next__,
    )
    try:
        old_authorization = lifecycle.start_authorization(
            operation_id="callback-old", requested_scopes=READ_SCOPES
        )
        new_authorization = lifecycle.start_authorization(
            operation_id="callback-new", requested_scopes=READ_SCOPES
        )
        old_result: list[object] = []

        def run_old_callback() -> None:
            old_result.append(
                lifecycle.handle_callback(
                    method="GET",
                    query={"state": old_authorization.state, "code": "old"},
                )
            )

        old_thread = threading.Thread(target=run_old_callback)
        old_thread.start()
        assert audit.completion_started.wait(timeout=2)

        new_response = lifecycle.handle_callback(
            method="GET",
            query={"state": new_authorization.state, "code": "new"},
        )
        audit.release_completion.set()
        old_thread.join(timeout=2)

        assert new_response.status_code == 204
        assert old_result[0].status_code == 503
        assert credentials.current is not None
        assert credentials.current.refresh_token == "refresh-new"
        assert lifecycle.connection.connected
        assert lifecycle.connection.generation == 2
    finally:
        audit.release_completion.set()
        trace_store._close_writer_service()

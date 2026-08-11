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


def test_incremental_authorization_retains_existing_scopes_and_adds_one_write_scope() -> (
    None
):
    gmail_send = "https://www.googleapis.com/auth/gmail.send"
    calendar_write = "https://www.googleapis.com/auth/calendar.events"
    state = InMemoryGoogleOAuthStateStore()
    state.set_connection(
        connected=True,
        granted_scopes=frozenset({*READ_SCOPES, "openid", gmail_send}),
    )
    lifecycle, trace_store = build_lifecycle(state_store=state)
    try:
        authorization = lifecycle.start_authorization(
            operation_id="enable-calendar-write",
            requested_scopes=(*READ_SCOPES, "openid", calendar_write),
        )

        assert authorization.requested_scopes == frozenset(
            {*READ_SCOPES, "openid", gmail_send, calendar_write}
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


def test_file_credential_store_reads_legacy_records_and_commits_generation_with_token(
    tmp_path,
) -> None:
    directory = tmp_path / "google-credentials"
    directory.mkdir()
    path = directory / "google-oauth.json"
    legacy = {
        "granted_scopes": sorted(READ_SCOPES),
        "refresh_token": "legacy-refresh-token",
        "subject": IDENTITY,
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")
    store = FileGoogleCredentialStore(directory)

    assert store.current == OAuthCredentialRecord(
        subject=IDENTITY,
        granted_scopes=frozenset(READ_SCOPES),
        refresh_token="legacy-refresh-token",
        connection_generation=0,
    )

    replacement = OAuthCredentialRecord(
        subject=IDENTITY,
        granted_scopes=frozenset(READ_SCOPES),
        refresh_token="new-refresh-token",
        connection_generation=7,
    )
    store.replace(replacement)

    stored_payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(stored_payload) == {
        "granted_scopes",
        "refresh_token",
        "subject",
    }
    # This is the exact reader contract from the prior pinned release.  The
    # generation sidecar is intentionally invisible to that reader.
    prior_release_record = json.loads(path.read_text(encoding="utf-8"))
    assert prior_release_record == {
        "granted_scopes": sorted(READ_SCOPES),
        "refresh_token": "new-refresh-token",
        "subject": IDENTITY,
    }
    metadata = json.loads((directory / "google-oauth.json.meta").read_text())
    assert metadata["schema"] == "google_oauth_credential_metadata_v2"
    assert any(record["connection_generation"] == 7 for record in metadata["records"])
    assert FileGoogleCredentialStore(directory).current == replacement


def test_file_credential_generation_remains_bound_when_a_second_rename_would_fail(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "google-credentials"
    store = FileGoogleCredentialStore(directory)
    original = OAuthCredentialRecord(
        subject=IDENTITY,
        granted_scopes=frozenset(READ_SCOPES),
        refresh_token="original-refresh-token",
        connection_generation=1,
    )
    replacement = OAuthCredentialRecord(
        subject=IDENTITY,
        granted_scopes=frozenset(READ_SCOPES),
        refresh_token="replacement-refresh-token",
        connection_generation=2,
    )
    store.replace(original)

    real_replace = os.replace
    calls = 0

    def fail_if_second_rename(source: object, target: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("controlled second rename failure")
        real_replace(source, target)

    monkeypatch.setattr(
        "jarvis_control_plane.google_oauth.os.replace", fail_if_second_rename
    )

    with pytest.raises(OSError, match="controlled second rename failure"):
        store.replace(replacement)

    # The sidecar was published with both generations, but the old primary
    # record remained visible when its rename failed.  The current reader must
    # therefore continue to resolve the original token and generation.
    assert calls == 2
    assert store.current == original

    state_store = InMemoryGoogleOAuthStateStore()
    current_connection = state_store.set_connection(
        connected=True, granted_scopes=frozenset(READ_SCOPES)
    )
    lifecycle, trace_store = build_lifecycle(credentials=store, state_store=state_store)
    try:
        invalidated = lifecycle.handle_refresh_failure(
            "invalid_grant", connection_generation=current_connection.generation
        )
        assert not invalidated.connected
    finally:
        trace_store._close_writer_service()
    assert store.current is None


def test_file_credential_store_migrates_embedded_generation_before_rollback(
    tmp_path,
) -> None:
    directory = tmp_path / "google-credentials"
    directory.mkdir()
    path = directory / "google-oauth.json"
    path.write_text(
        json.dumps(
            {
                "connection_generation": 4,
                "granted_scopes": sorted(READ_SCOPES),
                "refresh_token": "embedded-generation-token",
                "subject": IDENTITY,
            }
        ),
        encoding="utf-8",
    )

    store = FileGoogleCredentialStore(directory)

    assert store.current == OAuthCredentialRecord(
        subject=IDENTITY,
        granted_scopes=frozenset(READ_SCOPES),
        refresh_token="embedded-generation-token",
        connection_generation=4,
    )
    assert set(json.loads(path.read_text(encoding="utf-8"))) == {
        "granted_scopes",
        "refresh_token",
        "subject",
    }

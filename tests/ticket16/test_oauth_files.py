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

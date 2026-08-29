"""Google connector contract tests for Ticket 27."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jarvis_control_plane.service_protocol import ServiceProtocolError
from jarvis_control_plane.service_runtime import (
    SERVICE_ROLES,
    _RemoteGoogleReads,
    _service_access,
)


def test_google_administration_is_authenticated_and_not_model_accessible() -> None:
    identities, allowlists = _service_access(
        "google_connector", SERVICE_ROLES["google_connector"]
    )

    assert identities == (
        "jarvis-broker",
        "jarvis-orchestration",
        "jarvis-oauth-callback",
    )
    assert "current_connection_generation" in allowlists["jarvis-orchestration"]
    assert {"current", "start_authorization", "disconnect"} <= set(
        allowlists["jarvis-broker"]
    )
    assert "start_authorization" not in allowlists["jarvis-orchestration"]
    assert allowlists["jarvis-oauth-callback"] == ("oauth_callback",)


@pytest.mark.parametrize("generation", (0, 1, 42))
def test_remote_google_reads_exposes_a_strict_connection_generation(
    generation: int,
) -> None:
    calls: list[str] = []

    class Client:
        def call(self, operation: str, **_kwargs: object) -> int:
            calls.append(operation)
            return generation

    reads = _RemoteGoogleReads(Client())

    assert reads.current_connection_generation() == generation
    assert calls == ["current_connection_generation"]


@pytest.mark.parametrize("invalid", (True, False, -1, "1", None))
def test_remote_google_reads_rejects_invalid_connection_generation(
    invalid: object,
) -> None:
    class Client:
        def call(self, _operation: str, **_kwargs: object) -> object:
            return invalid

    with pytest.raises(ServiceProtocolError, match="invalid connection generation"):
        _RemoteGoogleReads(Client()).current_connection_generation()


def test_fresh_google_authorization_requests_only_identity_and_read_scopes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import jarvis_control_plane.service_runtime as runtime

    captured: dict[str, object] = {}

    class Client:
        def call(self, operation: str, **kwargs: object) -> str:
            captured.update(operation=operation, **kwargs)
            return "https://accounts.google.com/o/oauth2/v2/auth?state=test"

    monkeypatch.setenv("JARVIS_SERVICE_IDENTITY", "jarvis-broker")
    monkeypatch.setattr(runtime, "_load_configuration", lambda _path: {})
    monkeypatch.setattr(runtime, "_client", lambda *_args, **_kwargs: Client())

    assert runtime.main(["google-authorize", "--operation-id", "connect-01"]) == 0

    requested_scopes = set(captured["requested_scopes"])
    assert captured["operation"] == "start_authorization"
    assert captured["operation_id"] == "connect-01"
    assert "openid" in requested_scopes
    assert "https://www.googleapis.com/auth/gmail.readonly" in requested_scopes
    assert not any("/auth/calendar" in scope for scope in requested_scopes)
    assert "https://www.googleapis.com/auth/drive.readonly" in requested_scopes
    assert "https://www.googleapis.com/auth/gmail.send" not in requested_scopes
    assert "https://www.googleapis.com/auth/calendar.events" not in requested_scopes
    assert capsys.readouterr().out.startswith("https://accounts.google.com/")


@pytest.mark.parametrize(
    ("access", "expected_scope", "excluded_scope"),
    (
        (
            "gmail-send",
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/calendar.events",
        ),
    ),
)
def test_google_administration_requests_only_the_named_incremental_write_scope(
    monkeypatch: pytest.MonkeyPatch,
    access: str,
    expected_scope: str,
    excluded_scope: str,
) -> None:
    import jarvis_control_plane.service_runtime as runtime

    captured: dict[str, object] = {}

    class Client:
        def call(self, operation: str, **kwargs: object) -> str:
            captured.update(operation=operation, **kwargs)
            return "https://accounts.google.com/o/oauth2/v2/auth?state=test"

    monkeypatch.setenv("JARVIS_SERVICE_IDENTITY", "jarvis-broker")
    monkeypatch.setattr(runtime, "_load_configuration", lambda _path: {})
    monkeypatch.setattr(runtime, "_client", lambda *_args, **_kwargs: Client())

    assert (
        runtime.main(
            [
                "google-authorize",
                "--operation-id",
                f"enable-{access}",
                "--access",
                access,
            ]
        )
        == 0
    )

    requested_scopes = set(captured["requested_scopes"])
    assert expected_scope in requested_scopes
    assert excluded_scope not in requested_scopes


def test_google_startup_does_not_revive_a_persisted_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jarvis_control_plane.service_runtime as runtime

    class StateStore:
        def __init__(self, _database: str) -> None:
            self.set_calls: list[object] = []

        def get_connection(self) -> object:
            return SimpleNamespace(connected=False)

        def set_connection(self, **kwargs: object) -> None:
            self.set_calls.append(kwargs)

    state = StateStore("unused")
    credential_store = SimpleNamespace(
        current=SimpleNamespace(granted_scopes=frozenset({"scope"})),
        delete_calls=0,
    )

    def delete_credential() -> None:
        credential_store.delete_calls += 1

    credential_store.delete = delete_credential
    monkeypatch.setattr(
        runtime,
        "_credential_json",
        lambda _path: {"client_id": "id", "client_secret": "secret"},
    )
    monkeypatch.setattr(
        runtime,
        "FileGoogleCredentialStore",
        lambda _path: credential_store,
    )
    monkeypatch.setattr(runtime, "SQLiteGoogleOAuthStateStore", lambda _path: state)
    monkeypatch.setattr(
        runtime,
        "_service_trace",
        lambda *_args, **_kwargs: (object(), object(), object()),
    )
    monkeypatch.setattr(runtime, "_client", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(runtime, "RemoteAuditBoundary", lambda _client: object())
    monkeypatch.setattr(
        runtime,
        "GoogleLiveOAuthProvider",
        lambda **_kwargs: SimpleNamespace(authorization_url=lambda _value: "url"),
    )
    monkeypatch.setattr(
        runtime,
        "GoogleOAuthLifecycle",
        lambda **_kwargs: SimpleNamespace(
            connection_binding=object(),
            start_authorization=lambda **_kwargs: object(),
            handle_callback=lambda **_kwargs: object(),
            disconnect=lambda: object(),
            handle_refresh_failure=lambda *_args, **_kwargs: object(),
        ),
    )

    class Reads:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __getattr__(self, _name: str):
            return lambda **_kwargs: object()

    monkeypatch.setattr(runtime, "GoogleReadConnector", Reads)
    monkeypatch.setattr(runtime, "GoogleApiReadProvider", lambda **_kwargs: object())
    monkeypatch.setattr(runtime, "GmailWriteConnector", lambda **_kwargs: object())
    monkeypatch.setattr(runtime, "GmailApiWriteProvider", lambda **_kwargs: object())
    monkeypatch.setattr(
        runtime,
        "OwnedActionService",
        lambda _owner: SimpleNamespace(
            operations=lambda: {
                name: (lambda: None)
                for name in SERVICE_ROLES["google_connector"].operations
                if name.startswith("action_")
            }
        ),
    )

    operations = runtime._google_operations(
        {
            "deployment": {
                "google_subject": "subject-01",
                "oauth_callback_url": "https://oauth.jarvis.invalid/callback",
            },
            "resource_bounds": {"minimum_free_disk_gib": 2},
            "timeouts": {
                "read_connector_seconds": 20,
                "side_effect_connector_seconds": 30,
                "terminal_seconds": 120,
                "active_request_seconds": 480,
            },
        }
    )

    assert set(operations) == set(SERVICE_ROLES["google_connector"].operations)
    assert state.set_calls == []
    assert credential_store.delete_calls == 1

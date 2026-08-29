from __future__ import annotations

from types import SimpleNamespace

import jarvis_control_plane.service_runtime as runtime
from jarvis_control_plane.acceptance_failpoints import ReviewedPostDispatchFailpoint


def _config(*, service: str, operation: str) -> dict[str, object]:
    return {
        "deployment": {
            "google_subject": "operator@example.test",
            "oauth_callback_url": "https://oauth.example.test/callback",
        },
        "acceptance_failpoint": {
            "enabled": True,
            "service": service,
            "operation": operation,
            "action_id": f"ticket31-{service}-unknown",
            "review_id": f"ticket31-{service}-review",
        },
    }


def test_google_composition_routes_only_the_reviewed_service_failpoint(monkeypatch) -> None:
    captured: dict[str, list[dict[str, object]]] = {"gmail": [], "calendar": []}

    class CredentialStore:
        current = None

    class StateStore:
        def get_connection(self) -> object:
            return SimpleNamespace(connected=True)

    class ReadConnector:
        def __getattr__(self, _name: str):
            return lambda **_kwargs: object()

    monkeypatch.setattr(
        runtime,
        "_credential_json",
        lambda _path: {"client_id": "client", "client_secret": "secret"},
    )
    monkeypatch.setattr(runtime, "FileGoogleCredentialStore", lambda _path: CredentialStore())
    monkeypatch.setattr(runtime, "SQLiteGoogleOAuthStateStore", lambda _path: StateStore())
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
    monkeypatch.setattr(runtime, "GoogleReadConnector", lambda **_kwargs: ReadConnector())
    monkeypatch.setattr(runtime, "GoogleApiReadProvider", lambda **_kwargs: object())
    monkeypatch.setattr(
        runtime,
        "GmailWriteConnector",
        lambda **kwargs: (captured["gmail"].append(kwargs) or object()),
    )
    monkeypatch.setattr(runtime, "GmailApiWriteProvider", lambda **_kwargs: object())
    monkeypatch.setattr(
        runtime,
        "CalendarActionDispatcher",
        lambda **kwargs: (captured["calendar"].append(kwargs) or object()),
    )
    monkeypatch.setattr(
        runtime, "GoogleApiCalendarWriteProvider", lambda **_kwargs: object()
    )
    monkeypatch.setattr(
        runtime,
        "OwnedActionService",
        lambda _owner: SimpleNamespace(operations=dict),
    )

    runtime._google_operations(_config(service="calendar", operation="insert"))

    calendar_failpoint = captured["calendar"][-1]["acceptance_failpoint"]
    assert isinstance(calendar_failpoint, ReviewedPostDispatchFailpoint)
    assert calendar_failpoint.spec.service == "calendar"
    assert captured["gmail"][-1]["acceptance_failpoint"] is None

    runtime._google_operations(_config(service="gmail", operation="gmail_send"))

    gmail_failpoint = captured["gmail"][-1]["acceptance_failpoint"]
    assert isinstance(gmail_failpoint, ReviewedPostDispatchFailpoint)
    assert gmail_failpoint.spec.service == "gmail"
    assert captured["calendar"][-1]["acceptance_failpoint"] is None

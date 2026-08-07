from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest

from jarvis_control_plane import (
    CALENDAR_WRITE_SCOPE,
    CalendarActionDispatcher,
    CalendarWriteProposal,
    CalendarWriteRequest,
    ControlledGoogleCalendarWriteProvider,
    DeterministicIdGenerator,
    DiagnosticTraceRecorder,
    FixedClock,
    GoogleApiCalendarWriteProvider,
    GoogleCalendarHttpResponse,
    InMemoryDiagnosticTraceStore,
    InMemoryGoogleCredentialStore,
    InMemoryGoogleOAuthStateStore,
    OAuthCredentialRecord,
)
from jarvis_control_plane.google_calendar import GoogleCalendarWriteProviderError
from jarvis_control_plane.ports import ActionDispatcherError

IDENTITY = "operator@example.test"
NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


def event() -> dict[str, object]:
    return {
        "summary": "Design review",
        "start": {"dateTime": "2026-08-10T10:00:00Z"},
        "end": {"dateTime": "2026-08-10T11:00:00Z"},
        "attendees": [{"email": "guest@example.test"}],
        "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=2"],
        "reminders": {"useDefault": False, "overrides": []},
        "visibility": "private",
    }


class ControlledCalendarTransport:
    def __init__(self, responses: list[GoogleCalendarHttpResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> GoogleCalendarHttpResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        return self.responses.pop(0)


def response(status_code: int, payload: object) -> GoogleCalendarHttpResponse:
    return GoogleCalendarHttpResponse(
        status_code=status_code,
        headers={"Content-Type": "application/json"},
        body=json.dumps(payload).encode("utf-8"),
    )


def request(operation: str = "update") -> CalendarWriteRequest:
    state = InMemoryGoogleOAuthStateStore()
    connection = state.set_connection(
        connected=True, granted_scopes=frozenset({CALENDAR_WRITE_SCOPE})
    )
    proposal_factory = getattr(CalendarWriteProposal, operation)
    kwargs: dict[str, object] = {
        "action_id": "calendar-http-action",
        "request_id": "calendar-http-request",
        "calendar_id": "primary",
        "complete_event": event(),
        "notification": "all",
        "connection_generation": connection.generation,
    }
    if operation != "insert":
        kwargs.update({"event_id": "event-1", "etag": '"etag-1"'})
    if operation == "patch":
        kwargs["reviewed_patch"] = {"summary": "Design review"}
    return CalendarWriteRequest.from_proposal(proposal_factory(**kwargs))


def credential() -> OAuthCredentialRecord:
    return OAuthCredentialRecord(
        subject=IDENTITY,
        granted_scopes=frozenset({CALENDAR_WRITE_SCOPE}),
        refresh_token="refresh-token",
        connection_generation=1,
    )


def test_live_provider_uses_only_exact_update_method_etag_and_notification() -> None:
    returned = {"id": "event-1", **event()}
    transport = ControlledCalendarTransport(
        [response(200, {"access_token": "access-token"}), response(200, returned)]
    )
    provider = GoogleApiCalendarWriteProvider(
        client_id="client-id", client_secret="client-secret", transport=transport
    )

    result = provider.write(request=request(), credential=credential())

    assert result.event == returned
    assert [call["method"] for call in transport.calls] == ["POST", "PUT"]
    write = transport.calls[1]
    parsed = urlparse(write["url"])
    assert parsed.path.endswith("/calendars/primary/events/event-1")
    assert parse_qs(parsed.query) == {"sendUpdates": ["all"]}
    assert write["headers"]["If-Match"] == '"etag-1"'
    assert json.loads(write["body"]) == event()


def test_precondition_failure_is_known_but_malformed_success_is_ambiguous() -> None:
    provider = GoogleApiCalendarWriteProvider(
        client_id="client-id",
        client_secret="client-secret",
        transport=ControlledCalendarTransport(
            [response(200, {"access_token": "access-token"}), response(412, {})]
        ),
    )
    with pytest.raises(GoogleCalendarWriteProviderError) as rejected:
        provider.write(request=request(), credential=credential())
    assert rejected.value.code == "concurrent_change"
    assert rejected.value.may_have_dispatched is False

    provider = GoogleApiCalendarWriteProvider(
        client_id="client-id",
        client_secret="client-secret",
        transport=ControlledCalendarTransport(
            [
                response(200, {"access_token": "access-token"}),
                GoogleCalendarHttpResponse(200, {}, b"not-json"),
            ]
        ),
    )
    with pytest.raises(GoogleCalendarWriteProviderError) as ambiguous:
        provider.write(request=request(), credential=credential())
    assert ambiguous.value.may_have_dispatched is True


def test_trace_admission_blocks_calendar_call_before_the_provider() -> None:
    state = InMemoryGoogleOAuthStateStore()
    connection = state.set_connection(
        connected=True, granted_scopes=frozenset({CALENDAR_WRITE_SCOPE})
    )
    provider = ControlledGoogleCalendarWriteProvider()
    dispatcher = CalendarActionDispatcher(
        configured_identity=IDENTITY,
        connection_state=state,
        credential_store=InMemoryGoogleCredentialStore(
            OAuthCredentialRecord(
                subject=IDENTITY,
                granted_scopes=frozenset({CALENDAR_WRITE_SCOPE}),
                refresh_token="refresh-token",
                connection_generation=connection.generation,
            )
        ),
        provider=provider,
        trace=DiagnosticTraceRecorder(
            writer=InMemoryDiagnosticTraceStore().writer(),
            clock=FixedClock(NOW),
            ids=DeterministicIdGenerator("trace-admission"),
            reservation_bytes=1,
        ),
    )
    proposal = CalendarWriteProposal.insert(
        action_id="calendar-trace-action",
        request_id="calendar-trace-request",
        calendar_id="primary",
        complete_event=event(),
        notification="none",
        connection_generation=connection.generation,
    )

    with pytest.raises(ActionDispatcherError, match="trace admission"):
        dispatcher.dispatch(proposal)

    assert provider.calls == []


def test_dispatch_lease_prevents_reconnection_from_interleaving_with_provider_call() -> (
    None
):
    state = InMemoryGoogleOAuthStateStore()
    connection = state.set_connection(
        connected=True, granted_scopes=frozenset({CALENDAR_WRITE_SCOPE})
    )
    entered = threading.Event()
    release = threading.Event()

    class BlockingProvider(ControlledGoogleCalendarWriteProvider):
        def write(self, **kwargs: object):
            entered.set()
            assert release.wait(timeout=2)
            return super().write(**kwargs)

    provider = BlockingProvider()
    dispatcher = CalendarActionDispatcher(
        configured_identity=IDENTITY,
        connection_state=state,
        credential_store=InMemoryGoogleCredentialStore(
            OAuthCredentialRecord(
                subject=IDENTITY,
                granted_scopes=frozenset({CALENDAR_WRITE_SCOPE}),
                refresh_token="refresh-token",
                connection_generation=connection.generation,
            )
        ),
        provider=provider,
        trace=DiagnosticTraceRecorder(
            writer=InMemoryDiagnosticTraceStore().writer(),
            clock=FixedClock(NOW),
            ids=DeterministicIdGenerator("dispatch-lease"),
        ),
    )
    proposal = CalendarWriteProposal.insert(
        action_id="calendar-lease-action",
        request_id="calendar-lease-request",
        calendar_id="primary",
        complete_event=event(),
        notification="none",
        connection_generation=connection.generation,
    )
    dispatch = threading.Thread(target=dispatcher.dispatch, args=(proposal,))
    dispatch.start()
    assert entered.wait(timeout=2)

    reconnect = threading.Thread(target=lambda: state.set_connection(connected=False))
    reconnect.start()
    reconnect.join(timeout=0.1)
    assert reconnect.is_alive()

    release.set()
    dispatch.join(timeout=2)
    reconnect.join(timeout=2)
    assert not dispatch.is_alive()
    assert not reconnect.is_alive()
    assert len(provider.calls) == 1

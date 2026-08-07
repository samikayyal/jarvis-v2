from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from test_support import build_receiver_components

from jarvis_control_plane import (
    CALENDAR_WRITE_SCOPE,
    CalendarActionDispatcher,
    CalendarWriteProposal,
    ControlledGoogleCalendarWriteProvider,
    ControlledOrchestrationAdapter,
    DeterministicIdGenerator,
    DiagnosticTraceRecorder,
    FixedClock,
    InboundMessage,
    InMemoryDiagnosticTraceStore,
    InMemoryGoogleCredentialStore,
    InMemoryGoogleOAuthStateStore,
    OAuthCredentialRecord,
    SignedInboundEvent,
)
from jarvis_control_plane.ports import ActionDispatcherError

IDENTITY = "operator@example.test"
NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


def event(*, summary: str = "Design review") -> dict[str, object]:
    return {
        "summary": summary,
        "start": {"dateTime": "2026-08-10T10:00:00Z"},
        "end": {"dateTime": "2026-08-10T11:00:00Z"},
        "attendees": [{"email": "guest@example.test"}],
        "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=2"],
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "email", "minutes": 30}],
        },
        "visibility": "private",
    }


def connected_dispatcher() -> tuple[
    CalendarActionDispatcher,
    ControlledGoogleCalendarWriteProvider,
    InMemoryGoogleOAuthStateStore,
]:
    state = InMemoryGoogleOAuthStateStore()
    state.set_connection(
        connected=True, granted_scopes=frozenset({CALENDAR_WRITE_SCOPE})
    )
    connection = state.get_connection()
    credentials = InMemoryGoogleCredentialStore(
        OAuthCredentialRecord(
            subject=IDENTITY,
            granted_scopes=frozenset({CALENDAR_WRITE_SCOPE}),
            refresh_token="controlled-refresh-token",
            connection_generation=connection.generation,
        )
    )
    provider = ControlledGoogleCalendarWriteProvider()
    return (
        CalendarActionDispatcher(
            configured_identity=IDENTITY,
            connection_state=state,
            credential_store=credentials,
            provider=provider,
            trace=DiagnosticTraceRecorder(
                writer=InMemoryDiagnosticTraceStore().writer(),
                clock=FixedClock(NOW),
                ids=DeterministicIdGenerator("ticket19-calendar"),
            ),
        ),
        provider,
        state,
    )


def test_update_freezes_complete_event_and_dispatches_the_stored_operation_once() -> (
    None
):
    dispatcher, provider, state = connected_dispatcher()
    proposal = CalendarWriteProposal.update(
        action_id="calendar-action-1",
        request_id="request-1",
        calendar_id="primary",
        event_id="event-1",
        complete_event=event(),
        etag='"etag-1"',
        notification="all",
        connection_generation=state.get_connection().generation,
    )

    dispatcher.dispatch(proposal)

    assert len(provider.calls) == 1
    request, credential = provider.calls[0]
    assert request.operation == "update"
    assert request.calendar_id == "primary"
    assert request.event_id == "event-1"
    assert request.complete_event == event()
    assert request.etag == '"etag-1"'
    assert request.notification == "all"
    assert credential.subject == IDENTITY
    assert json.loads(proposal.payload)["complete_event"] == event()


def test_stale_generation_or_tampered_proposal_never_reaches_calendar() -> None:
    dispatcher, provider, state = connected_dispatcher()
    proposal = CalendarWriteProposal.insert(
        action_id="calendar-action-2",
        request_id="request-2",
        calendar_id="primary",
        complete_event=event(),
        notification="none",
        connection_generation=state.get_connection().generation,
    )
    state.set_connection(connected=False)

    with pytest.raises(ActionDispatcherError, match="stale"):
        dispatcher.dispatch(proposal)

    assert provider.calls == []

    with pytest.raises(ValueError, match="digest"):
        proposal.__class__(
            action_id=proposal.action_id,
            request_id=proposal.request_id,
            kind=proposal.kind,
            preview=proposal.preview,
            payload=proposal.payload.replace("primary", "other", 1),
            digest=proposal.digest,
        )
    assert provider.calls == []


def test_reviewed_patch_requires_complete_array_values_and_never_retries_ambiguous_outcomes() -> (
    None
):
    dispatcher, provider, state = connected_dispatcher()
    with pytest.raises(ValueError, match="complete event"):
        CalendarWriteProposal.patch(
            action_id="calendar-action-3",
            request_id="request-3",
            calendar_id="primary",
            event_id="event-3",
            complete_event=event(),
            reviewed_patch={"attendees": [{"email": "different@example.test"}]},
            etag='"etag-3"',
            notification="externalOnly",
            connection_generation=state.get_connection().generation,
        )

    proposal = CalendarWriteProposal.patch(
        action_id="calendar-action-4",
        request_id="request-4",
        calendar_id="primary",
        event_id="event-4",
        complete_event=event(),
        reviewed_patch={"summary": "Changed", "attendees": event()["attendees"]},
        etag='"etag-4"',
        notification="externalOnly",
        connection_generation=state.get_connection().generation,
    )
    provider.failure = ("connection dropped after request", True)

    with pytest.raises(ActionDispatcherError, match="connection dropped") as error:
        dispatcher.dispatch(proposal)

    assert error.value.may_have_dispatched is True
    assert len(provider.calls) == 1


def test_exact_broker_approval_dispatches_a_calendar_proposal_once() -> None:
    dispatcher, provider, state = connected_dispatcher()
    orchestration = ControlledOrchestrationAdapter(
        proposal_factory=lambda request: CalendarWriteProposal.insert(
            action_id="calendar-action-broker",
            request_id=request.state.request_id,
            calendar_id="primary",
            complete_event=event(),
            notification="none",
            connection_generation=state.get_connection().generation,
        )
    )
    components = build_receiver_components(
        operator_id="operator.test",
        transport_session_id="session.test",
        signing_secret=b"ticket19-secret",
        now=NOW,
        id_prefix="ticket19",
        orchestration=orchestration,
        action_dispatcher=dispatcher,
    )

    def receive(text: str, suffix: str):
        return components.receiver.receive(
            SignedInboundEvent.from_message(
                InboundMessage(
                    event_type="message.received",
                    session_id="session.test",
                    event_id=f"event-{suffix}",
                    message_id=f"message-{suffix}",
                    sender_id="operator.test",
                    chat_id="operator.test",
                    chat_type="direct",
                    message_type="text",
                    from_me=False,
                    text=text,
                ),
                b"ticket19-secret",
            )
        )

    assert receive("create it", "1").disposition == "pending_action"
    assert receive("yes", "2").disposition == "action_dispatched"
    assert receive("yes", "3").disposition != "action_dispatched"
    assert len(provider.calls) == 1

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from test_support import build_receiver_components

from jarvis_control_plane import (
    CALENDAR_WRITE_SCOPE,
    AuditWriteError,
    CalendarActionDispatcher,
    CalendarEventSnapshot,
    CalendarWriteProposal,
    CalendarWriteRequest,
    ControlledGoogleCalendarWriteProvider,
    ControlledGoogleOAuthProvider,
    ControlledOrchestrationAdapter,
    DeterministicIdGenerator,
    DiagnosticTraceRecorder,
    FixedClock,
    GoogleOAuthLifecycle,
    InboundMessage,
    InMemoryAuditBoundary,
    InMemoryDiagnosticTraceStore,
    InMemoryGoogleCredentialStore,
    InMemoryGoogleOAuthStateStore,
    OAuthCredentialRecord,
    OAuthGrant,
    SignedInboundEvent,
)
from jarvis_control_plane.google_calendar import GoogleCalendarWriteProviderError
from jarvis_control_plane.ports import ActionDispatcherError
from jarvis_control_plane.sessions import DispatchStatus

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
    snapshot = CalendarEventSnapshot(event=event(), etag='"etag-1"')
    proposal = CalendarWriteProposal.update(
        action_id="calendar-action-1",
        request_id="request-1",
        calendar_id="primary",
        event_id="event-1",
        snapshot=snapshot,
        changes={},
        notification="all",
        connection_generation=state.get_connection().generation,
    )

    dispatcher.dispatch(proposal)

    assert len(provider.calls) == 1
    request, credential = provider.calls[0]
    assert request.operation == "update"
    assert request.calendar_id == "primary"
    assert request.event_id == "event-1"
    assert request.complete_event == snapshot.event
    assert request.etag == '"etag-1"'
    assert request.notification == "all"
    assert credential.subject == IDENTITY
    assert json.loads(proposal.payload)["complete_event"] == snapshot.event


def test_calendar_connector_binds_and_revalidates_its_current_generation() -> None:
    dispatcher, _provider, state = connected_dispatcher()
    proposal = CalendarWriteProposal.insert(
        action_id="calendar-bound-generation",
        request_id="request-bound-generation",
        calendar_id="primary",
        complete_event=event(),
        notification="none",
        connection_generation=999,
    )

    bound = dispatcher.bind_proposal(proposal)

    assert CalendarWriteRequest.from_proposal(bound).connection_generation == (
        state.get_connection().generation
    )
    dispatcher.validate_pending_action(bound)
    state.set_connection(connected=False)
    with pytest.raises(ActionDispatcherError, match="stale"):
        dispatcher.validate_pending_action(bound)


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
        CalendarWriteRequest(
            operation="patch",
            calendar_id="primary",
            event_id="event-3",
            complete_event=event(),
            reviewed_patch={"summary": "Changed"},
            etag='"etag-3"',
            notification="externalOnly",
            connection_generation=state.get_connection().generation,
        )

    proposal = CalendarWriteProposal.patch(
        action_id="calendar-action-4",
        request_id="request-4",
        calendar_id="primary",
        event_id="event-4",
        snapshot=CalendarEventSnapshot(event=event(), etag='"etag-4"'),
        reviewed_patch={
            "summary": "Changed",
            "attendees": event()["attendees"],
        },
        notification="externalOnly",
        connection_generation=state.get_connection().generation,
    )
    provider.failure = ("connection dropped after request", True)

    with pytest.raises(ActionDispatcherError, match="connection dropped") as error:
        dispatcher.dispatch(proposal)

    assert error.value.may_have_dispatched is True
    assert len(provider.calls) == 1


def test_complete_event_requires_explicit_material_calendar_fields() -> None:
    _, _, state = connected_dispatcher()
    sparse_event = {
        "summary": "Design review",
        "start": {"dateTime": "2026-08-10T10:00:00Z"},
        "end": {"dateTime": "2026-08-10T11:00:00Z"},
    }

    with pytest.raises(ValueError, match="attendees"):
        CalendarWriteProposal.insert(
            action_id="calendar-sparse",
            request_id="request-sparse",
            calendar_id="primary",
            complete_event=sparse_event,
            notification="none",
            connection_generation=state.get_connection().generation,
        )


def test_insert_freezes_client_identity_and_insert_only_event_type() -> None:
    _, _, state = connected_dispatcher()
    proposal = CalendarWriteProposal.insert(
        action_id="calendar-special-insert",
        request_id="request-special-insert",
        calendar_id="primary",
        event_id="eventid",
        complete_event={**event(), "eventType": "focusTime"},
        notification="none",
        connection_generation=state.get_connection().generation,
    )

    request = CalendarWriteRequest.from_proposal(proposal)

    assert request.event_id == "eventid"
    assert request.complete_event["id"] == "eventid"
    assert request.complete_event["eventType"] == "focusTime"

    current = {**event(), "id": "eventid", "eventType": "focusTime"}
    snapshot = CalendarEventSnapshot(event=current, etag='"etag-special"')
    unchanged = CalendarWriteProposal.update(
        action_id="calendar-special-update",
        request_id="request-special-update",
        calendar_id="primary",
        event_id="eventid",
        snapshot=snapshot,
        changes={},
        notification="none",
        connection_generation=state.get_connection().generation,
    )
    assert json.loads(unchanged.payload)["complete_event"]["eventType"] == "focusTime"

    with pytest.raises(ValueError, match="cannot be changed"):
        CalendarWriteProposal.update(
            action_id="calendar-special-update-invalid",
            request_id="request-special-update-invalid",
            calendar_id="primary",
            event_id="eventid",
            snapshot=snapshot,
            changes={"eventType": "outOfOffice"},
            notification="none",
            connection_generation=state.get_connection().generation,
        )


def test_update_from_snapshot_derives_one_complete_result_and_preserves_material_fields() -> (
    None
):
    _, _, state = connected_dispatcher()
    current = event(summary="Existing summary")
    proposal = CalendarWriteProposal.update_from_snapshot(
        action_id="calendar-snapshot",
        request_id="request-snapshot",
        calendar_id="primary",
        event_id="event-snapshot",
        snapshot=CalendarEventSnapshot(event=current, etag='"etag-snapshot"'),
        changes={"summary": "Changed summary"},
        notification="all",
        connection_generation=state.get_connection().generation,
    )

    payload = json.loads(proposal.payload)
    assert payload["complete_event"]["summary"] == "Changed summary"
    assert (
        payload["complete_event"]["attendees"][0]["email"]
        == current["attendees"][0]["email"]
    )
    assert payload["complete_event"]["attendees"][0]["optional"] is False
    assert payload["complete_event"]["recurrence"] == current["recurrence"]
    assert payload["complete_event"]["reminders"] == current["reminders"]
    assert payload["complete_event"]["visibility"] == current["visibility"]
    assert payload["etag"] == '"etag-snapshot"'


def test_calendar_writes_reject_cancelled_status_for_insert_update_and_patch() -> None:
    _, _, state = connected_dispatcher()
    cancelled = {**event(), "status": "cancelled"}

    with pytest.raises(ValueError, match="cancelled"):
        CalendarWriteProposal.insert(
            action_id="calendar-cancelled-insert",
            request_id="request-cancelled-insert",
            calendar_id="primary",
            complete_event=cancelled,
            notification="none",
            connection_generation=state.get_connection().generation,
        )

    snapshot = CalendarEventSnapshot(event=event(), etag='"etag-cancelled"')
    with pytest.raises(ValueError, match="cancelled"):
        CalendarWriteProposal.update(
            action_id="calendar-cancelled-update",
            request_id="request-cancelled-update",
            calendar_id="primary",
            event_id="event-cancelled",
            snapshot=snapshot,
            changes={"status": "cancelled"},
            notification="none",
            connection_generation=state.get_connection().generation,
        )

    with pytest.raises(ValueError, match="cancelled"):
        CalendarWriteProposal.patch(
            action_id="calendar-cancelled-patch",
            request_id="request-cancelled-patch",
            calendar_id="primary",
            event_id="event-cancelled",
            snapshot=snapshot,
            reviewed_patch={"status": "cancelled"},
            notification="none",
            connection_generation=state.get_connection().generation,
        )


def test_full_snapshot_preserves_writable_google_event_state_for_put() -> None:
    _, _, state = connected_dispatcher()
    current = {
        **event(summary="Existing summary"),
        "id": "event-full",
        "etag": '"etag-full"',
        "conferenceData": {
            "createRequest": {"requestId": "conference-request"},
            "entryPoints": [{"entryPointType": "video", "uri": "https://meet.test"}],
        },
        "extendedProperties": {
            "private": {"workflow": "jarvis"},
            "shared": {"team": "calendar"},
        },
        "guestsCanInviteOthers": False,
        "guestsCanModify": True,
        "guestsCanSeeOtherGuests": False,
        "transparency": "transparent",
        "sequence": 7,
        "source": {"title": "Planner", "url": "https://planner.test"},
        "attachments": [
            {
                "fileId": "file-1",
                "fileUrl": "https://drive.test/file-1",
                "title": "Agenda",
                "mimeType": "text/plain",
            }
        ],
        # These are returned by a complete GET but must not become PUT fields.
        "organizer": {"email": "organizer@example.test"},
        "updated": "2026-08-07T11:00:00Z",
    }

    proposal = CalendarWriteProposal.update_from_snapshot(
        action_id="calendar-full-snapshot",
        request_id="request-full-snapshot",
        calendar_id="primary",
        event_id="event-full",
        snapshot=CalendarEventSnapshot(event=current, etag='"etag-full"'),
        changes={"summary": "Changed summary"},
        notification="all",
        connection_generation=state.get_connection().generation,
    )

    complete_event = json.loads(proposal.payload)["complete_event"]
    assert complete_event["summary"] == "Changed summary"
    for field in (
        "conferenceData",
        "extendedProperties",
        "guestsCanInviteOthers",
        "guestsCanModify",
        "guestsCanSeeOtherGuests",
        "transparency",
        "sequence",
        "source",
        "attachments",
    ):
        assert complete_event[field] == current[field]
    assert "organizer" not in complete_event
    assert "updated" not in complete_event


def test_fetched_snapshot_makes_google_omissions_explicit() -> None:
    snapshot = CalendarEventSnapshot(
        event={
            "summary": "No optional fields returned",
            "start": {"dateTime": "2026-08-10T10:00:00Z"},
            "end": {"dateTime": "2026-08-10T11:00:00Z"},
        },
        etag='"etag-defaults"',
    )

    assert snapshot.event["attendees"] == []
    assert snapshot.event["recurrence"] == []
    assert snapshot.event["visibility"] == "default"
    assert snapshot.event["reminders"] == {
        "useDefault": True,
        "overrides": [],
    }


def test_snapshot_binds_identity_and_excludes_server_only_fields_from_writable_result() -> (
    None
):
    _, _, state = connected_dispatcher()
    current = {**event(), "id": "event-bound", "etag": '"etag-bound"'}
    snapshot = CalendarEventSnapshot(event=current, etag='"etag-bound"')

    with pytest.raises(ValueError, match="identity"):
        CalendarWriteProposal.update_from_snapshot(
            action_id="calendar-wrong-event",
            request_id="request-wrong-event",
            calendar_id="primary",
            event_id="different-event",
            snapshot=snapshot,
            changes={},
            notification="none",
            connection_generation=state.get_connection().generation,
        )

    proposal = CalendarWriteProposal.update_from_snapshot(
        action_id="calendar-bound-event",
        request_id="request-bound-event",
        calendar_id="primary",
        event_id="event-bound",
        snapshot=snapshot,
        changes={},
        notification="none",
        connection_generation=state.get_connection().generation,
    )
    writable_event = json.loads(proposal.payload)["complete_event"]
    assert "id" not in writable_event
    assert "etag" not in writable_event


def test_calendar_invalid_grant_cleanup_receives_the_failed_generation() -> None:
    dispatcher, provider, state = connected_dispatcher()
    failed_generation = state.get_connection().generation
    received: list[int] = []

    class InvalidGrantProvider(ControlledGoogleCalendarWriteProvider):
        def write(self, **kwargs: object):
            self.calls.append((kwargs["request"], kwargs["credential"]))  # type: ignore[arg-type]
            raise GoogleCalendarWriteProviderError("invalid_grant")

    def on_invalid_grant(connection_generation: int) -> None:
        received.append(connection_generation)

    provider = InvalidGrantProvider()
    dispatcher = CalendarActionDispatcher(
        configured_identity=IDENTITY,
        connection_state=state,
        credential_store=InMemoryGoogleCredentialStore(
            OAuthCredentialRecord(
                subject=IDENTITY,
                granted_scopes=frozenset({CALENDAR_WRITE_SCOPE}),
                refresh_token="controlled-refresh-token",
                connection_generation=failed_generation,
            )
        ),
        provider=provider,
        trace=DiagnosticTraceRecorder(
            writer=InMemoryDiagnosticTraceStore().writer(),
            clock=FixedClock(NOW),
            ids=DeterministicIdGenerator("ticket19-invalid-grant"),
        ),
        on_invalid_grant=on_invalid_grant,
    )
    proposal = CalendarWriteProposal.insert(
        action_id="calendar-invalid-grant",
        request_id="request-invalid-grant",
        calendar_id="primary",
        complete_event=event(),
        notification="none",
        connection_generation=failed_generation,
    )

    with pytest.raises(ActionDispatcherError, match="invalid_grant"):
        dispatcher.dispatch(proposal)

    assert received == [failed_generation]


def test_calendar_invalid_grant_audit_failure_preserves_known_no_dispatch_outcome() -> (
    None
):
    _, _, state = connected_dispatcher()
    failed_generation = state.get_connection().generation

    class InvalidGrantProvider(ControlledGoogleCalendarWriteProvider):
        def write(self, **kwargs: object):
            self.calls.append((kwargs["request"], kwargs["credential"]))  # type: ignore[arg-type]
            raise GoogleCalendarWriteProviderError("invalid_grant")

    provider = InvalidGrantProvider()

    def on_invalid_grant(connection_generation: int) -> None:
        assert connection_generation == failed_generation
        raise AuditWriteError("controlled invalidation audit failure")

    dispatcher = CalendarActionDispatcher(
        configured_identity=IDENTITY,
        connection_state=state,
        credential_store=InMemoryGoogleCredentialStore(
            OAuthCredentialRecord(
                subject=IDENTITY,
                granted_scopes=frozenset({CALENDAR_WRITE_SCOPE}),
                refresh_token="controlled-refresh-token",
                connection_generation=failed_generation,
            )
        ),
        provider=provider,
        trace=DiagnosticTraceRecorder(
            writer=InMemoryDiagnosticTraceStore().writer(),
            clock=FixedClock(NOW),
            ids=DeterministicIdGenerator("ticket19-invalid-grant-audit"),
        ),
        on_invalid_grant=on_invalid_grant,
    )
    proposal = CalendarWriteProposal.insert(
        action_id="calendar-invalid-grant-audit",
        request_id="request-invalid-grant-audit",
        calendar_id="primary",
        complete_event=event(),
        notification="none",
        connection_generation=failed_generation,
    )
    with pytest.raises(ActionDispatcherError, match="invalidation failed") as error:
        dispatcher.dispatch(proposal)

    assert error.value.may_have_dispatched is False


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


def test_broker_closes_invalid_grant_action_when_invalidation_audit_fails() -> None:
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
    oauth_trace_store = InMemoryDiagnosticTraceStore()
    oauth_lifecycle = GoogleOAuthLifecycle(
        configured_identity=IDENTITY,
        state_store=state,
        credential_store=credentials,
        provider=ControlledGoogleOAuthProvider(
            grant=OAuthGrant(
                subject=IDENTITY,
                granted_scopes=frozenset({CALENDAR_WRITE_SCOPE}),
                access_token="controlled-access-token",
                refresh_token="controlled-refresh-token",
            )
        ),
        audit=InMemoryAuditBoundary(fail=True),
        trace=DiagnosticTraceRecorder(
            writer=oauth_trace_store.writer(),
            clock=FixedClock(NOW),
            ids=DeterministicIdGenerator("ticket19-oauth"),
        ),
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("ticket19-oauth"),
    )

    class InvalidGrantProvider(ControlledGoogleCalendarWriteProvider):
        def write(self, **kwargs: object):
            self.calls.append((kwargs["request"], kwargs["credential"]))  # type: ignore[arg-type]
            raise GoogleCalendarWriteProviderError("invalid_grant")

    provider = InvalidGrantProvider()
    calendar_trace_store = InMemoryDiagnosticTraceStore()
    dispatcher = CalendarActionDispatcher(
        configured_identity=IDENTITY,
        connection_state=state,
        credential_store=credentials,
        provider=provider,
        trace=DiagnosticTraceRecorder(
            writer=calendar_trace_store.writer(),
            clock=FixedClock(NOW),
            ids=DeterministicIdGenerator("ticket19-calendar-broker"),
        ),
        on_invalid_grant=lambda connection_generation: (
            oauth_lifecycle.handle_refresh_failure(
                "invalid_grant", connection_generation=connection_generation
            )
        ),
    )
    orchestration = ControlledOrchestrationAdapter(
        proposal_factory=lambda request: CalendarWriteProposal.insert(
            action_id="calendar-action-broker-audit-failure",
            request_id=request.state.request_id,
            calendar_id="primary",
            complete_event=event(),
            notification="none",
            connection_generation=connection.generation,
        )
    )
    components = build_receiver_components(
        operator_id="operator.test",
        transport_session_id="session.test",
        signing_secret=b"ticket19-secret-audit-failure",
        now=NOW,
        id_prefix="ticket19-audit-failure",
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
                b"ticket19-secret-audit-failure",
            )
        )

    try:
        assert receive("create it", "1").disposition == "pending_action"
        result = receive("yes", "2")

        assert result.disposition == "action_dispatch_failed"
        assert credentials.current is None
        assert not state.get_connection().connected
        session = components.broker.working_sessions.load()
        assert session is not None
        assert session.pending_action is None
        assert session.active_request is None
        record = next(
            record
            for record in session.action_outbox
            if record.action_id == "calendar-action-broker-audit-failure"
        )
        assert record.status is DispatchStatus.FAILED
        assert record.payload is None
        assert record.preview is None
        assert len(provider.calls) == 1
    finally:
        oauth_trace_store._close_writer_service()
        calendar_trace_store._close_writer_service()

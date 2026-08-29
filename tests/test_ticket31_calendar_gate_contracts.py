from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from test_support import build_receiver_components

from jarvis_control_plane import (
    CALENDAR_WRITE_SCOPE,
    CalendarActionDispatcher,
    ControlledGoogleCalendarWriteProvider,
    DeterministicIdGenerator,
    DiagnosticTraceRecorder,
    FixedClock,
    InMemoryDiagnosticTraceStore,
    InMemoryGoogleCredentialStore,
    InMemoryGoogleOAuthStateStore,
    OAuthCredentialRecord,
)
from jarvis_control_plane.google_calendar import (
    CalendarWriteRequest,
    GoogleCalendarWriteProviderResult,
)
from jarvis_control_plane.models import InboundMessage, SignedInboundEvent
from jarvis_control_plane.orchestration import (
    AgentsSdkOrchestrationAdapter,
    AgentsSdkPlan,
    AgentsSdkProposal,
)

IDENTITY = "operator@example.test"
NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
SIGNING_SECRET = b"ticket31-calendar-contract-secret"
SECONDARY_CALENDAR_ID = "secondary-calendar"
CREATE_REQUEST = "Create the labeled Ticket 31 event in the secondary calendar."


class _FakeReasoning:
    def __init__(self, *, effort: str) -> None:
        self.effort = effort


class _FakeModelSettings:
    def __init__(self, **values: object) -> None:
        self.reasoning = values["reasoning"]
        self.parallel_tool_calls = values["parallel_tool_calls"]
        self.store = values["store"]


class _FakeRunConfig:
    def __init__(self, **values: object) -> None:
        self.values = values


def _complete_event() -> dict[str, object]:
    """Return the complete material event that the model must propose."""

    return {
        "summary": "[JARVIS T31 ACCEPTANCE] Calendar contract",
        "description": "Bounded Calendar approval contract test.",
        "location": "Amman",
        "start": {
            "dateTime": "2026-08-25T14:00:00+03:00",
            "timeZone": "Asia/Amman",
        },
        "end": {
            "dateTime": "2026-08-25T15:00:00+03:00",
            "timeZone": "Asia/Amman",
        },
        "attendees": [],
        "recurrence": [],
        "reminders": {"useDefault": False, "overrides": []},
        "visibility": "private",
    }


class _AuthoritativeCalendarProvider(ControlledGoogleCalendarWriteProvider):
    """Controlled provider with an external-system-shaped event collection."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[str, dict[str, object]]] = []

    def write(
        self, *, request: CalendarWriteRequest, credential: OAuthCredentialRecord
    ) -> GoogleCalendarWriteProviderResult:
        result = super().write(request=request, credential=credential)
        event = dict(result.event)
        event["id"] = f"event-{len(self.events) + 1}"
        self.events.append((request.calendar_id, event))
        return GoogleCalendarWriteProviderResult(event=event)


class _CalendarHarness:
    """Production-shaped model -> broker -> Calendar connector test seam."""

    def __init__(self, *, proposal_responses: int | None = None) -> None:
        self.state = InMemoryGoogleOAuthStateStore()
        self.state.set_connection(
            connected=True, granted_scopes=frozenset({CALENDAR_WRITE_SCOPE})
        )
        connection = self.state.get_connection()
        self.credentials = InMemoryGoogleCredentialStore(
            OAuthCredentialRecord(
                subject=IDENTITY,
                granted_scopes=frozenset({CALENDAR_WRITE_SCOPE}),
                refresh_token="controlled-refresh-token",
                connection_generation=connection.generation,
            )
        )
        self.provider = _AuthoritativeCalendarProvider()
        self.connector_trace_store = InMemoryDiagnosticTraceStore()
        self.dispatcher = CalendarActionDispatcher(
            configured_identity=IDENTITY,
            connection_state=self.state,
            credential_store=self.credentials,
            provider=self.provider,
            trace=DiagnosticTraceRecorder(
                writer=self.connector_trace_store.writer(),
                clock=FixedClock(NOW),
                ids=DeterministicIdGenerator("ticket31-calendar-connector"),
            ),
        )
        self.run_calls = 0

        def run_sync(_agent: object, _text: str, **_kwargs: object) -> object:
            self.run_calls += 1
            if proposal_responses is not None and self.run_calls > proposal_responses:
                return SimpleNamespace(
                    final_output=AgentsSdkPlan(
                        reply_text="No new Calendar action was requested."
                    )
                )
            return SimpleNamespace(
                final_output=AgentsSdkPlan(
                    reply_text="I prepared the exact Calendar event for approval.",
                    proposal=AgentsSdkProposal(
                        kind="calendar_insert",
                        preview="Create the exact labeled Ticket 31 event.",
                        payload={
                            "calendar_id": SECONDARY_CALENDAR_ID,
                            "complete_event": _complete_event(),
                            "notification": "none",
                        },
                    ),
                )
            )

        orchestration = AgentsSdkOrchestrationAdapter(
            agent_factory=lambda **kwargs: SimpleNamespace(**kwargs),
            run_sync=run_sync,
            model_settings_factory=_FakeModelSettings,
            reasoning_factory=_FakeReasoning,
            run_config_factory=_FakeRunConfig,
        )
        self.components = build_receiver_components(
            operator_id="operator.test",
            transport_session_id="session.test",
            signing_secret=SIGNING_SECRET,
            now=NOW,
            id_prefix="ticket31-calendar-contract",
            orchestration=orchestration,  # type: ignore[arg-type]
            action_dispatcher=self.dispatcher,
            # The deployed graph gives the same connector ownership to both
            # action dispatch and the bind/revalidate lifecycle.
            action_lifecycle=self.dispatcher,
        )

    def receive(self, text: str, suffix: str):
        return self.components.receiver.receive(
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
                SIGNING_SECRET,
            )
        )

    def close(self) -> None:
        if self.components.trace_store is not None:
            self.components.trace_store._close_writer_service()
        self.connector_trace_store._close_writer_service()


def _pending_payload(harness: _CalendarHarness) -> dict[str, object]:
    session = harness.components.broker.working_sessions.load()
    assert session is not None
    assert session.pending_action is not None
    return json.loads(session.pending_action.payload)


def test_ticket31_calendar_exact_approval_freezes_complete_secondary_calendar_event() -> (
    None
):
    harness = _CalendarHarness()
    try:
        prepared = harness.receive(CREATE_REQUEST, "prepare")

        assert prepared.disposition == "pending_action"
        payload = _pending_payload(harness)
        assert payload["calendar_id"] == SECONDARY_CALENDAR_ID
        assert payload["notification"] == "none"
        complete_event = payload["complete_event"]
        assert isinstance(complete_event, dict)
        assert {
            "summary",
            "description",
            "location",
            "start",
            "end",
            "attendees",
            "recurrence",
            "visibility",
            "reminders",
        } <= set(complete_event)
        assert complete_event["attendees"] == []
        assert complete_event["recurrence"] == []
        assert complete_event["reminders"] == {
            "useDefault": False,
            "overrides": [],
        }
        assert harness.provider.calls == []

        approved = harness.receive("yes", "approve")

        assert approved.disposition == "action_dispatched"
        assert len(harness.provider.calls) == 1
        request, credential = harness.provider.calls[0]
        assert request.calendar_id == SECONDARY_CALENDAR_ID
        assert request.complete_event == complete_event
        assert request.notification == "none"
        assert credential.subject == IDENTITY
        assert credential.connection_generation == request.connection_generation
        assert len(harness.provider.events) == 1
        assert harness.provider.events[0][0] == SECONDARY_CALENDAR_ID
        assert {
            key: value
            for key, value in harness.provider.events[0][1].items()
            if key != "id"
        } == complete_event
    finally:
        harness.close()


def test_ticket31_calendar_old_approval_replay_does_not_create_a_second_event() -> None:
    harness = _CalendarHarness(proposal_responses=1)
    try:
        assert (
            harness.receive(CREATE_REQUEST, "prepare").disposition == "pending_action"
        )
        assert harness.receive("yes", "approve").disposition == "action_dispatched"

        replay = harness.receive("yes", "replay")

        assert replay.disposition != "action_dispatched"
        assert harness.run_calls == 2
        assert len(harness.provider.calls) == 1
        assert len(harness.provider.events) == 1
        assert harness.provider.calls[0][0].calendar_id == SECONDARY_CALENDAR_ID
        assert harness.provider.events[0][0] == SECONDARY_CALENDAR_ID
        session = harness.components.broker.working_sessions.load()
        assert session is not None
        assert session.active_request is None
        assert session.pending_action is None
    finally:
        harness.close()


def test_ticket31_calendar_stale_generation_rejects_old_proposal_before_write() -> None:
    harness = _CalendarHarness()
    try:
        assert (
            harness.receive(CREATE_REQUEST, "prepare").disposition == "pending_action"
        )
        old_generation = harness.state.get_connection().generation
        payload = _pending_payload(harness)
        assert payload["calendar_id"] == SECONDARY_CALENDAR_ID
        assert payload["connection_generation"] == old_generation

        harness.state.set_connection(connected=False)
        reconnected = harness.state.set_connection(
            connected=True, granted_scopes=frozenset({CALENDAR_WRITE_SCOPE})
        )
        assert reconnected.generation > old_generation

        stale = harness.receive("yes", "stale-approve")

        assert stale.disposition == "action_invalidated"
        assert "connection changed" in stale.reason
        assert harness.provider.calls == []
        assert harness.provider.events == []
        session = harness.components.broker.working_sessions.load()
        assert session is not None
        assert session.active_request is None
        assert session.pending_action is None
    finally:
        harness.close()

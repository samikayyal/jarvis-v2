from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from jarvis_control_plane import (
    ControlledActionDispatcher,
    ControlledGmailWriteProvider,
    GmailDeliveryResult,
    GmailNewSendRequest,
    GoogleConnectionState,
    OrchestrationRequest,
    RequestState,
    RoutedActionDispatcher,
    WorkerExecutionResult,
    gmail_write_request_from_proposal,
)
from jarvis_control_plane.orchestration import (
    AgentsSdkOrchestrationAdapter,
    AgentsSdkPlan,
    AgentsSdkProposal,
)
from jarvis_control_plane.sessions import ReadinessState

from .helpers import (
    NOW,
    OPERATOR,
    _components,
    _dispatcher,
    _event,
    _proposal,
    _terminal_proposal,
)


def test_orchestration_rebuilds_a_canonical_gmail_preview() -> None:
    class Reasoning:
        def __init__(self, **values: object) -> None:
            self.values = values

    class Settings:
        def __init__(self, **values: object) -> None:
            self.values = values

    class RunConfig:
        def __init__(self, **values: object) -> None:
            self.values = values

    plan = AgentsSdkPlan(
        reply_text="The exact Gmail action is ready.",
        proposal=AgentsSdkProposal(
            kind="gmail_send",
            preview="untrusted model prose is never the frozen preview",
            payload={
                "to": ["recipient@example.com"],
                "cc": [],
                "bcc": [],
                "subject": "Quarterly check-in",
                "body": "Please review.",
                "mime_type": "text/plain",
            },
        ),
    )

    result = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **_kwargs: object(),
        run_sync=lambda *_args, **_kwargs: SimpleNamespace(final_output=plan),
        model_settings_factory=Settings,
        reasoning_factory=Reasoning,
        run_config_factory=RunConfig,
    ).run(
        OrchestrationRequest(
            state=RequestState(
                request_id="request-001",
                event_id="event-001",
                message_id="message-001",
                operator_id=OPERATOR,
                session_id="working-session-001",
                chat_id=OPERATOR,
                created_at=NOW,
                updated_at=NOW,
                status="accepted",
                phase="orchestration",
            ),
            text="send this email",
        )
    )

    assert result.proposal is not None
    assert result.proposal.kind == "gmail_send"
    assert result.proposal.preview.startswith(
        "Gmail new send\nTo: recipient@example.com"
    )
    assert "untrusted model prose" not in result.proposal.preview
    assert result.reply_text == "The exact Gmail action is ready."
    assert result.execution_host is None
    assert result.host_reason_code is None


def test_orchestration_normalizes_redundant_model_gmail_fields() -> None:
    plan = AgentsSdkPlan(
        reply_text="The exact Gmail action is ready.",
        proposal=AgentsSdkProposal(
            kind="gmail_send",
            preview="untrusted model prose is never the frozen preview",
            payload={
                "to": ["recipient@example.com"],
                "cc": [],
                "bcc": [],
                "subject": "Quarterly check-in",
                "body": "Please review.",
                "mime_type": "text/plain",
                "attachments": [],
                "threading": "new_message",
            },
        ),
    )

    result = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **_kwargs: object(),
        run_sync=lambda *_args, **_kwargs: SimpleNamespace(final_output=plan),
        model_settings_factory=lambda **values: values,
        reasoning_factory=lambda **values: values,
        run_config_factory=lambda **values: values,
    ).run(
        OrchestrationRequest(
            state=RequestState(
                request_id="request-model-shape",
                event_id="event-model-shape",
                message_id="message-model-shape",
                operator_id=OPERATOR,
                session_id="working-session-model-shape",
                chat_id=OPERATOR,
                created_at=NOW,
                updated_at=NOW,
                status="accepted",
                phase="orchestration",
            ),
            text="prepare this plain-text email",
        )
    )

    assert result.proposal is not None
    request = gmail_write_request_from_proposal(result.proposal)
    assert isinstance(request, GmailNewSendRequest)
    payload = json.loads(result.proposal.payload)
    assert set(payload) == {
        "to",
        "cc",
        "bcc",
        "subject",
        "body",
        "mime_type",
        "threading",
    }
    assert payload["threading"] == "new_message"


def test_routed_action_surface_freezes_terminal_and_gmail_proposals() -> None:
    terminal_dispatcher = ControlledActionDispatcher()
    gmail_provider = ControlledGmailWriteProvider(
        result=GmailDeliveryResult(message_id="sent-routed", thread_id="thread-new")
    )
    terminal_gmail = _dispatcher(gmail_provider)

    terminal_components = _components(
        _terminal_proposal(),
        RoutedActionDispatcher(
            terminal=terminal_dispatcher,
            gmail=terminal_gmail,
            gmail_lifecycle=terminal_gmail,
        ),
    )
    terminal_pending = terminal_components.receiver.receive(
        _event("run the terminal action", suffix="routed-terminal-01")
    )
    terminal_session = terminal_components.broker.working_sessions.load()
    assert terminal_session is not None
    terminal_components.broker.working_sessions.compare_and_set(
        terminal_session,
        replace(
            terminal_session,
            readiness=ReadinessState(ubuntu="ready", windows="unavailable"),
        ),
    )
    terminal_approved = terminal_components.receiver.receive(
        _event("yes", suffix="routed-terminal-02")
    )

    gmail_components = _components(
        _proposal(),
        RoutedActionDispatcher(
            terminal=ControlledActionDispatcher(),
            gmail=terminal_gmail,
            gmail_lifecycle=terminal_gmail,
        ),
    )
    gmail_pending = gmail_components.receiver.receive(
        _event("send the email", suffix="routed-gmail-01")
    )
    gmail_approved = gmail_components.receiver.receive(
        _event("yes", suffix="routed-gmail-02")
    )

    assert terminal_pending.disposition == "pending_action"
    assert terminal_approved.disposition == "action_dispatched"
    assert len(terminal_dispatcher.dispatched) == 1
    assert gmail_pending.disposition == "pending_action"
    assert gmail_approved.disposition == "action_dispatched"
    assert len(gmail_provider.calls) == 1


def test_controlled_terminal_dispatch_returns_typed_result_only_for_terminal_actions() -> (
    None
):
    terminal_result = ControlledActionDispatcher().dispatch(_terminal_proposal())

    assert isinstance(terminal_result, WorkerExecutionResult)
    assert terminal_result.status.value == "completed"
    assert terminal_result.process_tree_stopped is True
    assert ControlledActionDispatcher().dispatch(_proposal()) is None


@pytest.mark.parametrize(
    ("connection_state", "case"),
    [
        pytest.param(
            lambda: GoogleConnectionState(
                connected=False,
                generation=1,
                granted_scopes=frozenset(),
            ),
            "disconnected",
        ),
        pytest.param(
            lambda: GoogleConnectionState(
                connected=True,
                generation=1,
                granted_scopes=frozenset(
                    {"https://www.googleapis.com/auth/calendar.events"}
                ),
            ),
            "missing-gmail-send",
        ),
        pytest.param(
            lambda: (_ for _ in ()).throw(RuntimeError("connection store unavailable")),
            "connection-state-unavailable",
        ),
    ],
)
def test_gmail_binding_failures_are_bounded_and_close_the_active_request(
    connection_state: object, case: str
) -> None:
    components = _components(
        _proposal(),
        _dispatcher(
            ControlledGmailWriteProvider(),
            connection_state=connection_state,
        ),
    )

    result = components.receiver.receive(_event("send", suffix=f"binding-{case}"))

    assert result.status_code == 202
    assert result.disposition == "failed"
    assert result.request is not None
    session = components.broker.working_sessions.load()
    assert session is not None
    assert session.active_request is None
    assert session.pending_action is None

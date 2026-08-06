from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from test_support import build_receiver_components

from jarvis_control_plane import (
    ControlledOrchestrationAdapter,
    ConversationMessage,
    InboundMessage,
    SignedInboundEvent,
    SQLiteDurableStateStore,
)

NOW = datetime(2026, 8, 6, 9, 0, tzinfo=UTC)
OPERATOR = "operator.test"
TRANSPORT_SESSION = "session.test"


def _message(
    *,
    message_id: str,
    text: str,
    direction: str = "inbound",
    working_session_id: str = "conversation-001",
    request_id: str | None = None,
    occurred_at: datetime = NOW,
) -> ConversationMessage:
    return ConversationMessage(
        working_session_id=working_session_id,
        transport_session_id=TRANSPORT_SESSION,
        message_id=message_id,
        event_id=f"event-{message_id}",
        chat_id=OPERATOR,
        sender_id=OPERATOR if direction == "inbound" else "jarvis",
        text=text,
        occurred_at=occurred_at,
        direction=direction,
        request_id=request_id,
    )


def test_sqlite_history_searches_filters_inspects_and_exports_exact_content() -> None:
    connection = sqlite3.connect(":memory:")
    state = SQLiteDurableStateStore(connection)
    try:
        state.append_conversation_message(
            _message(message_id="in-001", text="Find the release plan")
        )
        state.append_conversation_message(
            _message(
                message_id="out-001",
                direction="outbound",
                request_id="request-001",
                text="The release plan is ready.",
                occurred_at=NOW + timedelta(seconds=1),
            )
        )

        matches = state.search_conversation_messages(
            text="release",
            working_session_id="conversation-001",
            limit=10,
        )

        assert [message.message_id for message in matches] == ["in-001", "out-001"]
        assert matches[1].request_id == "request-001"
        assert matches[1].direction == "outbound"
        assert state.search_conversation_messages(
            request_id="request-001", direction="outbound"
        ) == (matches[1],)

        exported = json.loads(
            state.export_conversation_messages(message_ids=("out-001",))
        )
        assert exported == [
            {
                "conversation_id": "conversation-001",
                "direction": "outbound",
                "event_id": "event-out-001",
                "message_id": "out-001",
                "occurred_at": "2026-08-06T09:00:01+00:00",
                "request_id": "request-001",
                "sender_id": "jarvis",
                "text": "The release plan is ready.",
                "transport_session_id": TRANSPORT_SESSION,
            }
        ]
    finally:
        state.close()


def test_automatic_history_selection_excludes_credential_like_content_but_exact_selection_keeps_it_searchable() -> (
    None
):
    state = SQLiteDurableStateStore()
    try:
        state.append_conversation_message(
            _message(
                message_id="safe-001",
                working_session_id="conversation-earlier",
                text="The payroll report is due Friday.",
            )
        )
        state.append_conversation_message(
            _message(
                message_id="secret-001",
                working_session_id="conversation-earlier",
                text="API key sk-proj-very-secret-value is for the payroll report.",
                occurred_at=NOW + timedelta(seconds=1),
            )
        )

        automatic = state.select_history_for_context(
            text="What is due for the payroll report?",
            excluding_working_session_id="conversation-current",
            limit=10,
        )
        exact = state.search_conversation_messages(message_ids=("secret-001",))

        assert [message.message_id for message in automatic.messages] == ["safe-001"]
        assert automatic.provenance_disclosure == (
            "History used: conversation conversation-earlier, message safe-001 at "
            "2026-08-06T09:00:00+00:00."
        )
        assert (
            exact[0].text
            == "API key sk-proj-very-secret-value is for the payroll report."
        )
        assert exact[0].credential_like is True
    finally:
        state.close()


def test_broker_reuses_only_safe_prior_history_discloses_it_and_retains_final_outbound_text() -> (
    None
):
    state = SQLiteDurableStateStore()
    state.append_conversation_message(
        _message(
            message_id="prior-001",
            working_session_id="conversation-earlier",
            text="The deployment window is Thursday.",
        )
    )
    state.append_conversation_message(
        _message(
            message_id="prior-secret-001",
            working_session_id="conversation-earlier",
            text="password=do-not-send is related to deployment.",
            occurred_at=NOW + timedelta(seconds=1),
        )
    )
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=b"ticket20-test-secret",
        now=NOW + timedelta(minutes=1),
        id_prefix="ticket20",
        state=state,
        working_session_id="conversation-current",
    )
    event = SignedInboundEvent.from_message(
        InboundMessage(
            event_type="message.received",
            session_id=TRANSPORT_SESSION,
            event_id="event-current",
            message_id="in-current",
            sender_id=OPERATOR,
            chat_id=OPERATOR,
            chat_type="direct",
            message_type="text",
            from_me=False,
            text="When is the deployment window?",
        ),
        components.config.signing_secret,
    )

    result = components.receiver.receive(event)

    assert result.disposition == "completed"
    assert [item.message_id for item in components.orchestration.calls[0].history] == [
        "prior-001"
    ]
    assert "do-not-send" not in components.orchestration.calls[0].history[0].text
    assert result.reply is not None
    assert (
        "History used: conversation conversation-earlier, message prior-001"
        in result.reply.body
    )
    outbound = state.search_conversation_messages(
        request_id=result.request_id,
        direction="outbound",
    )
    assert len(outbound) == 1
    assert outbound[0].text == result.reply.body
    assert outbound[0].working_session_id == "conversation-current"


def test_history_provenance_survives_the_bounded_informational_reply() -> None:
    state = SQLiteDurableStateStore()
    state.append_conversation_message(
        _message(
            message_id="prior-bound-001",
            working_session_id="conversation-earlier",
            text="The deployment window is Thursday.",
        )
    )
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=b"ticket20-test-secret",
        now=NOW + timedelta(minutes=1),
        id_prefix="ticket20-bound",
        state=state,
        working_session_id="conversation-current",
        orchestration=ControlledOrchestrationAdapter(response_text="x" * 5_000),
    )
    event = SignedInboundEvent.from_message(
        InboundMessage(
            event_type="message.received",
            session_id=TRANSPORT_SESSION,
            event_id="event-bound",
            message_id="in-bound",
            sender_id=OPERATOR,
            chat_id=OPERATOR,
            chat_type="direct",
            message_type="text",
            from_me=False,
            text="When is the deployment window?",
        ),
        components.config.signing_secret,
    )

    result = components.receiver.receive(event)

    assert result.reply is not None
    assert len(result.reply.body) == 4_096
    assert result.reply.body.startswith(
        "History used: conversation conversation-earlier, message prior-bound-001"
    )
    assert "[truncated]" in result.reply.body

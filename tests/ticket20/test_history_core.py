# ruff: noqa: F401, I001, RUF100 -- split modules retain ticket context.
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from test_support import build_receiver_components

from jarvis_control_plane import (
    ControlledOrchestrationAdapter,
    ConversationMessage,
    InboundMessage,
    InMemoryDurableStateStore,
    OutboundAttemptStatus,
    SignedInboundEvent,
    SQLiteDurableStateStore,
)
from jarvis_control_plane.ports import OutboundConnectorError

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
            state.export_conversation_messages(history_ids=(matches[1].history_id,))
        )
        assert exported == [
            {
                "conversation_id": "conversation-001",
                "direction": "outbound",
                "event_id": "event-out-001",
                "history_id": matches[1].history_id,
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
        exact = state.search_conversation_messages(
            history_ids=(
                _message(message_id="secret-001", text="placeholder").history_id,
            )
        )

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


@pytest.mark.parametrize(
    "text",
    (
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature",
        "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC",
        "refresh_token=1//0g-realistic-oauth-refresh-token",
        "AWS access key AKIAIOSFODNN7EXAMPLE belongs to the deployer",
        "webhook_secret=whsec_9AaBbCcDdEeFfGgHhIiJjKk",
    ),
)
def test_credential_class_material_is_never_automatic_model_context(text: str) -> None:
    state = SQLiteDurableStateStore()
    try:
        state.append_conversation_message(
            _message(
                message_id="credential-001",
                working_session_id="conversation-earlier",
                text=text,
            )
        )

        selected = state.select_history_for_context(
            text="deployer realistic oauth",
            excluding_working_session_id="conversation-current",
            limit=10,
        )
        exact = state.search_conversation_messages(
            history_ids=(
                _message(message_id="credential-001", text="placeholder").history_id,
            )
        )

        assert selected.messages == ()
        assert exact[0].credential_like is True
    finally:
        state.close()


@pytest.mark.parametrize(
    "store_type", (InMemoryDurableStateStore, SQLiteDurableStateStore)
)
def test_history_search_contract_is_identical_for_stopwords_and_unicode(
    store_type: object,
) -> None:
    state = store_type()  # type: ignore[operator]
    try:
        state.append_conversation_message(  # type: ignore[union-attr]
            _message(message_id="safe-001", text="Café release plan is ready.")
        )
        state.append_conversation_message(  # type: ignore[union-attr]
            _message(message_id="secret-001", text="password=hidden release plan")
        )

        assert state.search_conversation_messages(text="what is it") == ()  # type: ignore[union-attr]
        assert [
            message.message_id
            for message in state.search_conversation_messages(text="CAFÉ")  # type: ignore[union-attr]
        ] == ["safe-001"]
        assert [
            message.message_id
            for message in state.search_conversation_messages(text="release!")  # type: ignore[union-attr]
        ] == ["safe-001", "secret-001"]
    finally:
        close = getattr(state, "close", None)
        if callable(close):
            close()


def test_authorized_operator_can_search_then_exactly_inspect_credential_history() -> (
    None
):
    state = SQLiteDurableStateStore()
    secret = _message(
        message_id="secret-001",
        working_session_id="conversation-earlier",
        text="Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature",
    )
    state.append_conversation_message(secret)
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=b"ticket20-history-command-secret",
        now=NOW,
        id_prefix="ticket20-history-command",
        state=state,
        working_session_id="conversation-current",
    )

    def receive(text: str, suffix: str):
        return components.receiver.receive(
            SignedInboundEvent.from_message(
                InboundMessage(
                    event_type="message.received",
                    session_id=TRANSPORT_SESSION,
                    event_id=f"history-event-{suffix}",
                    message_id=f"history-message-{suffix}",
                    sender_id=OPERATOR,
                    chat_id=OPERATOR,
                    chat_type="direct",
                    message_type="text",
                    from_me=False,
                    text=text,
                ),
                components.config.signing_secret,
            )
        )

    try:
        searched = receive("/history search bearer", "search")
        assert searched.disposition == "history_search", searched.reason
        assert searched.reply is not None
        assert "secret-001" in searched.reply.body
        assert "eyJhbGci" not in searched.reply.body

        inspected = receive(f"/history inspect {secret.history_id}", "inspect")
        assert inspected.disposition == "history_inspect"
        assert inspected.reply is not None
        assert "eyJhbGciOiJIUzI1NiJ9" in inspected.reply.body
        assert "Conversation-history export part 1/1" in inspected.reply.body
    finally:
        state.close()


def test_outbound_history_write_failure_blocks_connector_before_dispatch() -> None:
    state = InMemoryDurableStateStore()

    def fail_history_before_reply(_request: object) -> str:
        state.fail_conversation = True
        return "This reply must not reach the connector."

    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=b"ticket20-pre-dispatch-secret",
        now=NOW,
        id_prefix="ticket20-pre-dispatch",
        state=state,
        orchestration=ControlledOrchestrationAdapter(
            response_factory=fail_history_before_reply
        ),
    )
    event = SignedInboundEvent.from_message(
        InboundMessage(
            event_type="message.received",
            session_id=TRANSPORT_SESSION,
            event_id="outbound-history-event",
            message_id="outbound-history-message",
            sender_id=OPERATOR,
            chat_id=OPERATOR,
            chat_type="direct",
            message_type="text",
            from_me=False,
            text="send a reply",
        ),
        components.config.signing_secret,
    )

    result = components.receiver.receive(event)

    assert result.disposition == "failed"
    assert components.outbound.sent == []
    assert [message.message_id for message in state.list_conversation_messages()] == [
        "outbound-history-message"
    ]


def test_failed_outbound_attempt_is_terminal_and_not_searchable() -> None:
    state = InMemoryDurableStateStore()
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=b"ticket20-failed-outbound-secret",
        now=NOW,
        id_prefix="ticket20-failed-outbound",
        state=state,
    )

    def fail_after_preflight(_reply: object) -> object:
        raise OutboundConnectorError("controlled gateway failure")

    components.outbound.send = fail_after_preflight  # type: ignore[method-assign]
    event = SignedInboundEvent.from_message(
        InboundMessage(
            event_type="message.received",
            session_id=TRANSPORT_SESSION,
            event_id="failed-outbound-event",
            message_id="failed-outbound-message",
            sender_id=OPERATOR,
            chat_id=OPERATOR,
            chat_type="direct",
            message_type="text",
            from_me=False,
            text="send a reply",
        ),
        components.config.signing_secret,
    )

    result = components.receiver.receive(event)

    assert result.disposition == "failed"
    assert state.search_conversation_messages(direction="outbound") == ()
    assert state.outbound_outbox == {}
    attempts = state.list_outbound_conversation_attempts()
    assert len(attempts) == 1
    assert attempts[0].status is OutboundAttemptStatus.UNKNOWN
    assert attempts[0].message is None
    assert attempts[0].terminal_at is not None


def test_history_selector_is_stable_and_exact_across_transport_sessions() -> None:
    state = SQLiteDurableStateStore()
    try:
        selected = _message(message_id="shared-message", text="first record")
        duplicate = replace(
            selected,
            transport_session_id="other.transport.session",
            working_session_id="conversation-other",
            text="other record",
        )
        state.append_conversation_message(selected)
        state.append_conversation_message(duplicate)

        matches = state.search_conversation_messages(
            history_ids=(selected.history_id,), limit=1
        )
        exported = json.loads(
            state.export_conversation_messages(history_ids=(selected.history_id,))
        )

        assert matches == (selected,)
        assert exported[0]["history_id"] == selected.history_id
        assert exported[0]["text"] == "first record"
    finally:
        state.close()

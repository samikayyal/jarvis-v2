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


@pytest.mark.parametrize(
    "store_type", (InMemoryDurableStateStore, SQLiteDurableStateStore)
)
def test_context_candidate_limit_is_applied_after_safety_exclusions(
    store_type: object,
) -> None:
    state = store_type()  # type: ignore[operator]
    try:
        for number in range(50):
            state.append_conversation_message(  # type: ignore[union-attr]
                _message(
                    message_id=f"excluded-{number}",
                    text="release plan",
                    working_session_id="conversation-current",
                    occurred_at=NOW + timedelta(seconds=number),
                )
            )
        safe = _message(
            message_id="safe-after-exclusions",
            text="release plan",
            working_session_id="conversation-prior",
            occurred_at=NOW + timedelta(minutes=2),
        )
        state.append_conversation_message(safe)  # type: ignore[union-attr]

        selected = state.select_history_for_context(  # type: ignore[union-attr]
            text="release plan",
            excluding_working_session_id="conversation-current",
            limit=1,
        )

        assert selected.messages == (safe,)
    finally:
        close = getattr(state, "close", None)
        if callable(close):
            close()


def test_history_command_preserves_failed_delivery_disposition() -> None:
    state = SQLiteDurableStateStore()
    state.append_conversation_message(_message(message_id="prior", text="release plan"))
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=b"ticket20-history-delivery-secret",
        now=NOW,
        id_prefix="ticket20-history-delivery",
        state=state,
    )
    components.outbound.failure = "controlled gateway failure"
    event = SignedInboundEvent.from_message(
        InboundMessage(
            event_type="message.received",
            session_id=TRANSPORT_SESSION,
            event_id="history-delivery-event",
            message_id="history-delivery-message",
            sender_id=OPERATOR,
            chat_id=OPERATOR,
            chat_type="direct",
            message_type="text",
            from_me=False,
            text="/history search release",
        ),
        components.config.signing_secret,
    )
    try:
        result = components.receiver.receive(event)

        assert result.disposition == "failed"
    finally:
        state.close()


def test_sqlite_history_search_uses_indexed_candidates_without_listing_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = SQLiteDurableStateStore()
    try:
        state.append_conversation_message(
            _message(message_id="indexed", text="find the release plan")
        )

        def archive_must_not_be_loaded() -> tuple[ConversationMessage, ...]:
            raise AssertionError("SQLite history search must query bounded candidates")

        monkeypatch.setattr(
            state, "list_conversation_messages", archive_must_not_be_loaded
        )

        matches = state.search_conversation_messages(text="release", limit=1)

        assert [message.message_id for message in matches] == ["indexed"]
    finally:
        state.close()


def test_multipart_history_export_preserves_second_fragment_failure() -> None:
    state = SQLiteDurableStateStore()
    record = _message(message_id="large-export", text="x" * 8_000)
    state.append_conversation_message(record)
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=b"ticket20-multipart-delivery-secret",
        now=NOW,
        id_prefix="ticket20-multipart-delivery",
        state=state,
    )
    original_send = components.outbound.send
    calls = 0

    def fail_second_fragment(reply: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OutboundConnectorError("controlled second-fragment failure")
        return original_send(reply)  # type: ignore[arg-type]

    components.outbound.send = fail_second_fragment  # type: ignore[method-assign]
    event = SignedInboundEvent.from_message(
        InboundMessage(
            event_type="message.received",
            session_id=TRANSPORT_SESSION,
            event_id="multipart-history-event",
            message_id="multipart-history-message",
            sender_id=OPERATOR,
            chat_id=OPERATOR,
            chat_type="direct",
            message_type="text",
            from_me=False,
            text=f"/history export {record.history_id}",
        ),
        components.config.signing_secret,
    )
    try:
        result = components.receiver.receive(event)

        assert calls == 2
        assert result.disposition == "failed"
        assert len(components.outbound.sent) == 1
    finally:
        state.close()

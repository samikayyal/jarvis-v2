from __future__ import annotations

import pytest
from test_support import build_receiver_components

from jarvis_control_plane import (
    InboundMessage,
    InMemoryDurableStateStore,
    SignedInboundEvent,
)

from .helpers import (
    NOW,
    OPERATOR,
    TRANSPORT_SESSION,
    _AmbiguousDeletionStateStore,
    _FailDeletionArchiveStateStore,
    _FailDeletionResultAudit,
    _FailDeletionStateStore,
    _message,
)


def test_history_delete_command_freezes_exact_preview_and_dispatches_once() -> None:
    state = InMemoryDurableStateStore()
    selected = _message(message_id="delete-me", text="remove this exact record")
    outside = _message(message_id="keep-me", text="leave this record alone")
    state.append_conversation_message(selected)
    state.append_conversation_message(outside)
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=b"ticket21-delete-secret",
        now=NOW,
        id_prefix="ticket21-delete",
        state=state,
        working_session_id="conversation-current",
    )

    def receive(text: str, suffix: str):
        return components.receiver.receive(
            SignedInboundEvent.from_message(
                InboundMessage(
                    event_type="message.received",
                    session_id=TRANSPORT_SESSION,
                    event_id=f"event-{suffix}",
                    message_id=f"message-{suffix}",
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
        pending = receive(f"/history delete message {selected.history_id}", "preview")
        action = components.broker.current_pending_action

        assert pending.disposition == "pending_action", pending.reason
        assert action is not None
        assert action.kind == "conversation_history_delete"
        assert selected.history_id in action.preview
        assert "Messages: 1" in action.preview
        assert state.search_conversation_messages(  # type: ignore[union-attr]
            history_ids=(selected.history_id,)
        ) == (selected,)

        approved = receive("1", "approve")
        replay = receive("1", "replay")

        assert approved.disposition == "action_dispatched", approved.reason
        assert replay.disposition != "action_dispatched"
        assert (
            state.search_conversation_messages(  # type: ignore[union-attr]
                history_ids=(selected.history_id,)
            )
            == ()
        )
        assert [item.history_id for item in state.list_conversation_tombstones()] == [
            selected.history_id
        ]
        assert state.search_conversation_messages(  # type: ignore[union-attr]
            history_ids=(outside.history_id,)
        ) == (outside,)
    finally:
        close = getattr(state, "close", None)
        if callable(close):
            close()


@pytest.mark.parametrize(
    ("store_type", "expected_disposition", "expected_outcome", "deleted"),
    (
        (InMemoryDurableStateStore, "action_dispatched", "completed", True),
        (_FailDeletionStateStore, "action_dispatch_failed", "failed", False),
        (
            _FailDeletionArchiveStateStore,
            "action_dispatch_failed",
            "failed",
            False,
        ),
        (_AmbiguousDeletionStateStore, "action_dispatch_unknown", "unknown", True),
    ),
)
def test_deletion_records_a_terminal_redacted_audit_result(
    store_type: object,
    expected_disposition: str,
    expected_outcome: str,
    deleted: bool,
) -> None:
    state = store_type()  # type: ignore[operator]
    selected = _message(
        message_id=f"audit-terminal-{expected_outcome}", text="delete me"
    )
    state.append_conversation_message(selected)  # type: ignore[union-attr]
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=b"ticket21-terminal-audit-secret",
        now=NOW,
        id_prefix=f"ticket21-terminal-{expected_outcome}",
        state=state,
    )

    def receive(text: str, suffix: str):
        return components.receiver.receive(
            SignedInboundEvent.from_message(
                InboundMessage(
                    event_type="message.received",
                    session_id=TRANSPORT_SESSION,
                    event_id=f"event-{suffix}",
                    message_id=f"message-{suffix}",
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
        pending = receive(
            f"/history delete message {selected.history_id}", "terminal-preview"
        )
        result = receive("1", "terminal-approve")

        assert pending.disposition == "pending_action", pending.reason
        assert result.disposition == expected_disposition, result.reason
        terminal = [
            record
            for record in components.audit.records
            if record.kind == "conversation_history_deletion_result"
        ]
        assert len(terminal) == 1
        assert terminal[0].outcome == expected_outcome
        assert terminal[0].execution_status == expected_outcome
        assert terminal[0].details["result"] == expected_outcome
        assert (
            state.search_conversation_messages(  # type: ignore[union-attr]
                history_ids=(selected.history_id,)
            )
            == ()
        ) is deleted
    finally:
        close = getattr(state, "close", None)
        if callable(close):
            close()


def test_terminal_deletion_audit_failure_closes_action_as_unknown() -> None:
    state = InMemoryDurableStateStore()
    selected = _message(message_id="audit-result-failure", text="must be removed")
    state.append_conversation_message(selected)
    audit = _FailDeletionResultAudit()
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=b"ticket21-terminal-audit-failure-secret",
        now=NOW,
        id_prefix="ticket21-terminal-audit-failure",
        state=state,
        audit=audit,
    )

    def receive(text: str, suffix: str):
        return components.receiver.receive(
            SignedInboundEvent.from_message(
                InboundMessage(
                    event_type="message.received",
                    session_id=TRANSPORT_SESSION,
                    event_id=f"event-{suffix}",
                    message_id=f"message-{suffix}",
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
        assert (
            receive(
                f"/history delete message {selected.history_id}",
                "audit-failure-preview",
            ).disposition
            == "pending_action"
        )
        result = receive("1", "audit-failure-approve")

        assert result.disposition == "action_dispatch_unknown", result.reason
        assert (
            state.search_conversation_messages(history_ids=(selected.history_id,)) == ()
        )
        assert not any(
            record.kind == "conversation_history_deletion_result"
            for record in audit.records
        )
        current = components.broker._current_working_session()
        action = next(
            record
            for record in current.action_outbox
            if record.kind == "conversation_history_delete"
        )
        assert action.status.value == "unknown"
        assert components.broker.current_pending_action is None
        replay = receive("1", "audit-failure-replay")
        assert replay.disposition != "action_dispatched"
    finally:
        close = getattr(state, "close", None)
        if callable(close):
            close()

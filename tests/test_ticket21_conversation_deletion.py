from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest
from test_support import build_receiver_components

from jarvis_control_plane import (
    AuditEvidence,
    AuditWriteError,
    ConversationDeletionPreview,
    ConversationDeletionScope,
    ConversationMessage,
    InboundMessage,
    InMemoryAuditBoundary,
    InMemoryDeletedConversationArchive,
    InMemoryDurableStateStore,
    SignedInboundEvent,
    SQLiteDurableStateStore,
    StateStoreError,
)
from jarvis_control_plane.control_grammar import MessageKind, parse_control
from jarvis_control_plane.manual_admin import open_sqlite_deleted_conversation_archive

NOW = datetime(2026, 8, 8, 9, 0, tzinfo=UTC)
OPERATOR = "operator.test"
TRANSPORT_SESSION = "session.test"


class _FailDeletionAttemptAudit(InMemoryAuditBoundary):
    def append(self, evidence: AuditEvidence) -> None:
        if evidence.kind == "conversation_history_deletion_attempt":
            raise AuditWriteError("controlled deletion-attempt audit failure")
        super().append(evidence)


def _message(
    *,
    message_id: str,
    text: str,
    working_session_id: str = "conversation-001",
    direction: str = "inbound",
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
    )


@pytest.mark.parametrize(
    "command",
    (
        "/history delete",
        "/history delete message",
        "/history delete date 2026-08-08",
    ),
)
def test_incomplete_history_delete_is_deterministically_malformed(command: str) -> None:
    parsed = parse_control(command)

    assert parsed.kind is MessageKind.MALFORMED_COMMAND


@pytest.mark.parametrize(
    "store_type", (InMemoryDurableStateStore, SQLiteDurableStateStore)
)
def test_deletion_scopes_produce_stable_exact_previews(store_type: object) -> None:
    state = store_type()  # type: ignore[operator]
    messages = (
        _message(message_id="message-a", text="keep outside the selected scope"),
        _message(
            message_id="message-b",
            text="delete this exact message",
            occurred_at=NOW + timedelta(days=1),
        ),
        _message(
            message_id="message-c",
            text="delete the whole other conversation",
            working_session_id="conversation-002",
            occurred_at=NOW + timedelta(days=2),
        ),
    )
    try:
        for message in messages:
            state.append_conversation_message(message)  # type: ignore[union-attr]

        exact = state.preview_conversation_deletion(  # type: ignore[union-attr]
            ConversationDeletionScope.message(messages[1].history_id)
        )
        exact_again = state.preview_conversation_deletion(  # type: ignore[union-attr]
            ConversationDeletionScope.message(messages[1].history_id)
        )
        conversation = state.preview_conversation_deletion(  # type: ignore[union-attr]
            ConversationDeletionScope.conversation("conversation-002")
        )
        date_range = state.preview_conversation_deletion(  # type: ignore[union-attr]
            ConversationDeletionScope.date_range(date(2026, 8, 9), date(2026, 8, 9))
        )

        assert exact.history_ids == (messages[1].history_id,)
        assert exact.messages == (messages[1],)
        assert exact.content_digest == exact_again.content_digest
        assert conversation.history_ids == (messages[2].history_id,)
        assert date_range.history_ids == (messages[1].history_id,)
        assert date_range.messages[0].text == "delete this exact message"
    finally:
        close = getattr(state, "close", None)
        if callable(close):
            close()


def test_deletion_preview_rejects_records_outside_declared_scope() -> None:
    state = InMemoryDurableStateStore()
    message = _message(message_id="scope-boundary", text="scope-boundary")
    state.append_conversation_message(message)
    preview = state.preview_conversation_deletion(
        ConversationDeletionScope.message(message.history_id)
    )

    with pytest.raises(ValueError, match="outside its scope"):
        ConversationDeletionPreview(
            scope=ConversationDeletionScope.conversation("conversation-999"),
            messages=preview.messages,
            content_digest=preview.content_digest,
        )


@pytest.mark.parametrize(
    "store_type", (InMemoryDurableStateStore, SQLiteDurableStateStore)
)
def test_confirmed_deletion_moves_exact_content_and_leaves_only_tombstones(
    store_type: object,
    tmp_path,
) -> None:
    archive = InMemoryDeletedConversationArchive()
    admin_archive = None
    if store_type is SQLiteDurableStateStore:
        database = tmp_path / "jarvis.sqlite3"
        archive_path = tmp_path / "admin-only" / "deleted.sqlite3"
        archive_path.parent.mkdir()
        state = store_type(  # type: ignore[operator]
            database=database,
            deleted_database=archive_path,
        )
        admin_archive = open_sqlite_deleted_conversation_archive(archive_path)
    else:
        state = store_type(deleted_archive=archive)  # type: ignore[operator]
    selected = _message(message_id="delete-me", text="verbatim retained content")
    outside = _message(message_id="keep-me", text="still accessible")
    try:
        state.append_conversation_message(selected)  # type: ignore[union-attr]
        state.append_conversation_message(outside)  # type: ignore[union-attr]
        preview = state.preview_conversation_deletion(  # type: ignore[union-attr]
            ConversationDeletionScope.message(selected.history_id)
        )

        tombstones = state.delete_conversation_history(  # type: ignore[union-attr]
            preview,
            deletion_id="deletion-001",
            deleted_at=NOW + timedelta(minutes=1),
        )

        assert [item.history_id for item in tombstones] == [selected.history_id]
        assert not hasattr(tombstones[0], "text")
        assert [item.message_id for item in state.list_conversation_messages()] == [  # type: ignore[union-attr]
            "keep-me"
        ]
        assert (
            state.search_conversation_messages(  # type: ignore[union-attr]
                history_ids=(selected.history_id,)
            )
            == ()
        )
        assert (
            state.select_history_for_context(  # type: ignore[union-attr]
                text="verbatim retained content",
                excluding_working_session_id="conversation-current",
            ).messages
            == ()
        )
        assert (
            json.loads(  # type: ignore[union-attr]
                state.export_conversation_messages(history_ids=(selected.history_id,))
            )
            == []
        )
        assert state.list_conversation_tombstones() == tombstones  # type: ignore[union-attr]

        if isinstance(state, SQLiteDurableStateStore):
            assert [
                name for _, name, *_ in state.connection.execute("PRAGMA database_list")
            ] == ["main"]
            with pytest.raises(sqlite3.OperationalError, match="no such table"):
                state.connection.execute(
                    "SELECT text FROM deleted_conversation_archive.deleted_messages"
                ).fetchall()
            assert admin_archive is not None
            assert [record.message.text for record in admin_archive.list_records()] == [
                selected.text
            ]
        else:
            assert [record.message.text for record in archive.read_records()] == [
                selected.text
            ]
    finally:
        if admin_archive is not None:
            admin_archive.close()
        close = getattr(state, "close", None)
        if callable(close):
            close()


def test_sqlite_deletion_requires_an_explicit_archive_boundary(tmp_path) -> None:
    database = tmp_path / "jarvis.sqlite3"
    state = SQLiteDurableStateStore(database)
    selected = _message(message_id="archive-required", text="must not be orphaned")
    state.append_conversation_message(selected)
    preview = state.preview_conversation_deletion(
        ConversationDeletionScope.message(selected.history_id)
    )

    try:
        with pytest.raises(StateStoreError, match="archive writer is not configured"):
            state.delete_conversation_history(
                preview,
                deletion_id="archive-required-deletion",
                deleted_at=NOW + timedelta(minutes=1),
            )
        assert state.search_conversation_messages(
            history_ids=(selected.history_id,)
        ) == (selected,)
        assert not (tmp_path / "jarvis.deleted.sqlite3").exists()
    finally:
        state.close()


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


def test_date_range_delete_is_frozen_before_later_messages_arrive() -> None:
    state = InMemoryDurableStateStore()
    first = _message(message_id="first", text="delete in range", occurred_at=NOW)
    state.append_conversation_message(first)
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=b"ticket21-range-secret",
        now=NOW,
        id_prefix="ticket21-range",
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
        pending = receive("/history delete date 2026-08-08 2026-08-08", "preview")
        assert pending.disposition == "pending_action", pending.reason

        later = receive("this later message must survive", "later")
        approved = receive("yes", "approve")

        assert later.disposition == "pending_blocked"
        assert approved.disposition == "action_dispatched", approved.reason
        assert (
            state.search_conversation_messages(  # type: ignore[union-attr]
                history_ids=(first.history_id,)
            )
            == ()
        )
        later_message = next(
            message
            for message in state.list_conversation_messages()  # type: ignore[union-attr]
            if message.message_id == "message-later"
        )
        assert later_message.text == "this later message must survive"
    finally:
        close = getattr(state, "close", None)
        if callable(close):
            close()


def test_audit_failure_before_deletion_keeps_accessible_content() -> None:
    state = InMemoryDurableStateStore()
    selected = _message(message_id="audit-gated", text="must remain accessible")
    state.append_conversation_message(selected)
    audit = _FailDeletionAttemptAudit()
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=b"ticket21-audit-secret",
        now=NOW,
        id_prefix="ticket21-audit",
        state=state,
        audit=audit,
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
        blocked = receive("yes", "approve")

        assert pending.disposition == "pending_action", pending.reason
        assert blocked.disposition == "audit_blocked", blocked.reason
        assert state.search_conversation_messages(  # type: ignore[union-attr]
            history_ids=(selected.history_id,)
        ) == (selected,)
        assert state.list_conversation_tombstones() == ()  # type: ignore[union-attr]
    finally:
        close = getattr(state, "close", None)
        if callable(close):
            close()


def test_sqlite_deleted_area_and_tombstones_survive_restart(tmp_path) -> None:
    database = tmp_path / "jarvis.sqlite3"
    deleted_database = tmp_path / "admin-only" / "deleted.sqlite3"
    deleted_database.parent.mkdir()
    selected = _message(message_id="persistent-delete", text="retained outside Jarvis")
    state = SQLiteDurableStateStore(
        database=database,
        deleted_database=deleted_database,
    )
    try:
        state.append_conversation_message(selected)
        preview = state.preview_conversation_deletion(
            ConversationDeletionScope.message(selected.history_id)
        )
        expected_tombstones = state.delete_conversation_history(
            preview,
            deletion_id="persistent-deletion-001",
            deleted_at=NOW + timedelta(minutes=2),
        )
    finally:
        state.close()

    reopened = SQLiteDurableStateStore(
        database=database,
        deleted_database=deleted_database,
    )
    try:
        assert reopened.list_conversation_messages() == ()
        assert reopened.list_conversation_tombstones() == expected_tombstones
        assert reopened.search_conversation_messages(text="retained outside") == ()
        assert json.loads(reopened.export_conversation_messages()) == []
        assert [
            name for _, name, *_ in reopened.connection.execute("PRAGMA database_list")
        ] == ["main"]
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            reopened.connection.execute(
                "SELECT text FROM deleted_conversation_archive.deleted_messages"
            ).fetchall()
        admin_archive = open_sqlite_deleted_conversation_archive(deleted_database)
        try:
            assert [record.message.text for record in admin_archive.list_records()] == [
                selected.text
            ]
        finally:
            admin_archive.close()
    finally:
        reopened.close()

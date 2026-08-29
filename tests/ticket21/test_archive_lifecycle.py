from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from multiprocessing.connection import Listener

import pytest
from test_support import build_receiver_components

from jarvis_control_plane import (
    ConversationDeletionScope,
    InboundMessage,
    InMemoryDurableStateStore,
    SignedInboundEvent,
    SQLiteDurableStateStore,
    StateStoreError,
    conversation_archive,
)
from jarvis_control_plane.conversation_archive import (
    start_sqlite_deleted_conversation_archive_service,
)
from jarvis_control_plane.manual_admin import open_sqlite_deleted_conversation_archive

from .helpers import (
    NOW,
    OPERATOR,
    TRANSPORT_SESSION,
    _FailDeletionAttemptAudit,
    _FailOnceCommitConnection,
    _message,
)


def test_conversation_deletion_chunks_archive_frames_larger_than_eight_mebibytes(
    tmp_path,
) -> None:
    deleted_database = tmp_path / "admin-only" / "deleted.sqlite3"
    deleted_database.parent.mkdir()
    messages = tuple(
        _message(
            message_id=f"large-archive-{index}",
            text="x" * 75_000,
            working_session_id="conversation-large-archive",
            occurred_at=NOW + timedelta(seconds=index),
        )
        for index in range(150)
    )
    archive_service = start_sqlite_deleted_conversation_archive_service(
        deleted_database
    )
    state = SQLiteDurableStateStore(
        deleted_archive=archive_service.writer,
    )
    try:
        for message in messages:
            state.append_conversation_message(message)
        preview = state.preview_conversation_deletion(
            ConversationDeletionScope.conversation("conversation-large-archive")
        )
        with pytest.raises(ValueError, match="frame"):
            conversation_archive._encode_archive_request(
                {
                    "operation": "archive",
                    "messages": preview.messages,
                    "deletion_id": "large-archive-single-frame",
                    "deleted_at": NOW,
                }
            )

        tombstones = state.delete_conversation_history(
            preview,
            deletion_id="large-archive-chunked",
            deleted_at=NOW + timedelta(minutes=1),
        )
        assert len(tombstones) == len(messages)
        assert state.list_conversation_messages() == ()
    finally:
        state.close()
        archive_service.close()

    admin_archive = open_sqlite_deleted_conversation_archive(deleted_database)
    try:
        records = admin_archive.list_records()
        assert len(records) == len(messages)
        assert records[0].message.text == messages[0].text
        assert records[-1].message.text == messages[-1].text
    finally:
        admin_archive.close()


def test_archive_chunk_sizing_does_not_encode_every_growing_candidate(
    monkeypatch,
) -> None:
    messages = tuple(
        _message(
            message_id=f"small-archive-{index}",
            text="small message",
            working_session_id="conversation-small-archive",
            occurred_at=NOW + timedelta(seconds=index),
        )
        for index in range(2_000)
    )
    original_encode = conversation_archive._encode_archive_request
    encode_calls = 0

    def counted_encode(request):
        nonlocal encode_calls
        encode_calls += 1
        return original_encode(request)

    monkeypatch.setattr(conversation_archive, "_encode_archive_request", counted_encode)
    chunks = tuple(
        conversation_archive._archive_message_chunks(
            messages,
            deletion_id="linear-small-archive",
        )
    )

    assert sum(len(chunk) for chunk in chunks) == len(messages)
    assert encode_calls == 0
    for chunk_index, chunk in enumerate(chunks):
        frame = original_encode(
            {
                "operation": "chunk",
                "deletion_id": "linear-small-archive",
                "chunk_index": chunk_index,
                "messages": chunk,
            }
        )
        assert len(frame) <= conversation_archive._MAX_ARCHIVE_FRAME_BYTES


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
    archive_service = start_sqlite_deleted_conversation_archive_service(
        deleted_database
    )
    state = SQLiteDurableStateStore(
        database=database,
        deleted_archive=archive_service.writer,
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
        archive_service.close()

    reopened_service = start_sqlite_deleted_conversation_archive_service(
        deleted_database
    )
    reopened = SQLiteDurableStateStore(
        database=database,
        deleted_archive=reopened_service.writer,
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
        reopened_service.close()


def test_production_archive_service_cleans_endpoint_on_sigterm(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    endpoint = tmp_path / "writer.sock"
    calls: list[str] = []
    installed_handler: list[object] = []

    class Listener:
        def accept(self) -> object:
            handler = installed_handler[0]
            assert callable(handler)
            handler(conversation_archive.signal.SIGTERM, None)
            raise AssertionError("SIGTERM handler must stop the service")

        def close(self) -> None:
            calls.append("closed")

    def create_listener(_endpoint: object, _authkey: bytes) -> Listener:
        endpoint.touch()
        return Listener()

    def install_handler(_signal: object, handler: object) -> object:
        installed_handler.append(handler)
        return conversation_archive.signal.SIG_DFL

    def remove_endpoint(_endpoint: object) -> None:
        endpoint.unlink(missing_ok=True)
        calls.append("removed")

    monkeypatch.setattr(
        conversation_archive, "_create_archive_listener", create_listener
    )
    monkeypatch.setattr(
        conversation_archive, "_remove_archive_endpoint", remove_endpoint
    )
    monkeypatch.setattr(conversation_archive.signal, "signal", install_handler)

    with pytest.raises(SystemExit):
        conversation_archive.serve_sqlite_deleted_conversation_archive(
            tmp_path / "archive.sqlite3", endpoint, authkey=b"a" * 32
        )

    assert calls == ["closed", "removed"]
    assert not endpoint.exists()


def test_sqlite_deletion_adopts_archive_after_live_commit_failure(tmp_path) -> None:
    database = tmp_path / "jarvis.sqlite3"
    deleted_database = tmp_path / "admin-only" / "deleted.sqlite3"
    deleted_database.parent.mkdir()
    archive_service = start_sqlite_deleted_conversation_archive_service(
        deleted_database
    )
    connection = sqlite3.connect(database, factory=_FailOnceCommitConnection)
    state = SQLiteDurableStateStore(
        connection,
        deleted_archive=archive_service.writer,
    )
    selected = _message(message_id="retry-delete", text="retryable retained content")
    admin_archive = open_sqlite_deleted_conversation_archive(deleted_database)
    try:
        state.append_conversation_message(selected)
        preview = state.preview_conversation_deletion(
            ConversationDeletionScope.message(selected.history_id)
        )
        connection.fail_next_commit = True
        with pytest.raises(
            StateStoreError, match="could not delete conversation history"
        ):
            state.delete_conversation_history(
                preview,
                deletion_id="failed-deletion-attempt",
                deleted_at=NOW + timedelta(minutes=3),
            )

        retry_preview = state.preview_conversation_deletion(
            ConversationDeletionScope.message(selected.history_id)
        )
        tombstones = state.delete_conversation_history(
            retry_preview,
            deletion_id="successful-deletion-retry",
            deleted_at=NOW + timedelta(minutes=4),
        )

        assert [tombstone.deletion_id for tombstone in tombstones] == [
            "successful-deletion-retry"
        ]
        assert state.list_conversation_messages() == ()
        records = admin_archive.list_records()
        assert len(records) == 1
        assert records[0].message == selected
        assert records[0].deletion_id == "successful-deletion-retry"
        assert records[0].deleted_at == NOW + timedelta(minutes=4)
    finally:
        admin_archive.close()
        state.close()
        connection.close()
        archive_service.close()

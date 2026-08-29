from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import (
    date,
    timedelta,
)

import pytest
from test_support import build_receiver_components

from jarvis_control_plane import (
    ConversationDeletionPreview,
    ConversationDeletionScope,
    InboundMessage,
    InMemoryDeletedConversationArchive,
    InMemoryDurableStateStore,
    SignedInboundEvent,
    SQLiteDurableStateStore,
    StateStoreError,
)
from jarvis_control_plane.conversation_archive import (
    start_sqlite_deleted_conversation_archive_service,
)
from jarvis_control_plane.manual_admin import open_sqlite_deleted_conversation_archive

from .helpers import (
    NOW,
    OPERATOR,
    TRANSPORT_SESSION,
    _message,
)


def test_current_conversation_delete_is_refused_until_session_ends() -> None:
    state = InMemoryDurableStateStore()
    current = _message(
        message_id="current-message",
        text="keep the active conversation accessible",
        working_session_id="conversation-current",
    )
    state.append_conversation_message(current)
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=b"ticket21-current-conversation-secret",
        now=NOW,
        id_prefix="ticket21-current-conversation",
        state=state,
        working_session_id="conversation-current",
    )

    result = components.receiver.receive(
        SignedInboundEvent.from_message(
            InboundMessage(
                event_type="message.received",
                session_id=TRANSPORT_SESSION,
                event_id="event-delete-current",
                message_id="message-delete-current",
                sender_id=OPERATOR,
                chat_id=OPERATOR,
                chat_type="direct",
                message_type="text",
                from_me=False,
                text="/history delete conversation conversation-current",
            ),
            components.config.signing_secret,
        )
    )

    assert result.disposition == "history_delete_current_refused"
    assert result.reply is not None
    assert "/new" in result.reply.body
    assert components.broker.current_pending_action is None
    assert state.list_conversation_tombstones() == ()
    assert state.search_conversation_messages(history_ids=(current.history_id,)) == (
        current,
    )


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


def test_sqlite_exact_deletion_preview_reads_only_the_requested_rows(
    monkeypatch,
) -> None:
    state = SQLiteDurableStateStore()
    messages = tuple(
        _message(
            message_id=f"large-history-{index}",
            text=f"retained history record {index}",
            occurred_at=NOW + timedelta(seconds=index),
        )
        for index in range(1_200)
    )
    try:
        for message in messages:
            state.append_conversation_message(message)

        monkeypatch.setattr(
            state,
            "list_conversation_messages",
            lambda: pytest.fail("exact deletion selection loaded all history"),
        )
        preview = state.preview_conversation_deletion(
            ConversationDeletionScope.message(messages[777].history_id)
        )

        assert preview.messages == (messages[777],)
    finally:
        state.close()


@pytest.mark.parametrize(
    "store_type", (InMemoryDurableStateStore, SQLiteDurableStateStore)
)
def test_confirmed_deletion_moves_exact_content_and_leaves_only_tombstones(
    store_type: object,
    tmp_path,
) -> None:
    archive = InMemoryDeletedConversationArchive()
    admin_archive = None
    archive_service = None
    if store_type is SQLiteDurableStateStore:
        database = tmp_path / "jarvis.sqlite3"
        archive_path = tmp_path / "admin-only" / "deleted.sqlite3"
        archive_path.parent.mkdir()
        archive_service = start_sqlite_deleted_conversation_archive_service(
            archive_path
        )
        state = store_type(  # type: ignore[operator]
            database=database,
            deleted_archive=archive_service.writer,
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
        if archive_service is not None:
            archive_service.close()


def test_sqlite_state_cannot_receive_a_deleted_database_path_with_a_connection(
    tmp_path,
) -> None:
    database = tmp_path / "jarvis.sqlite3"
    connection = sqlite3.connect(database)
    try:
        with pytest.raises(TypeError, match="deleted_database"):
            SQLiteDurableStateStore(connection, deleted_database=database)  # type: ignore[call-arg]
    finally:
        connection.close()


@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() != 0,
    reason="requires POSIX root privileges to exercise a second filesystem identity",
)
def test_posix_archive_read_requires_the_administrative_identity(tmp_path) -> None:
    import pwd

    nobody = pwd.getpwnam("nobody")
    if nobody.pw_uid == os.geteuid():
        pytest.skip("the nobody account is the current test identity")

    archive_path = tmp_path / "admin-only" / "deleted.sqlite3"
    archive_path.parent.mkdir()
    service = start_sqlite_deleted_conversation_archive_service(archive_path)
    message = _message(message_id="permission-boundary", text="admin-only content")

    def drop_to_nobody() -> None:
        os.setgid(nobody.pw_gid)
        os.setuid(nobody.pw_uid)

    try:
        service.writer.archive(
            [message],
            deletion_id="permission-deletion",
            deleted_at=NOW + timedelta(minutes=1),
        )
        direct_read = subprocess.run(
            [
                sys.executable,
                "-c",
                """
import sqlite3
import sys

try:
    connection = sqlite3.connect(sys.argv[1])
    connection.execute("SELECT text FROM deleted_messages").fetchall()
except (PermissionError, sqlite3.OperationalError):
    raise SystemExit(0)
raise SystemExit(1)
""",
                str(archive_path),
            ],
            preexec_fn=drop_to_nobody,
            check=False,
        )
        assert direct_read.returncode == 0
        admin_archive = open_sqlite_deleted_conversation_archive(archive_path)
        try:
            assert [record.message.text for record in admin_archive.list_records()] == [
                message.text
            ]
        finally:
            admin_archive.close()
    finally:
        service.close()


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


def test_sqlite_conversation_delete_confirms_more_than_one_thousand_records(
    tmp_path,
) -> None:
    state = SQLiteDurableStateStore(
        tmp_path / "jarvis.sqlite3",
        deleted_archive=InMemoryDeletedConversationArchive(),
    )
    messages = tuple(
        _message(
            message_id=f"large-conversation-{index}",
            text=f"message {index}",
            working_session_id="conversation-large-delete",
            occurred_at=NOW + timedelta(seconds=index),
        )
        for index in range(1_201)
    )
    try:
        for message in messages:
            state.append_conversation_message(message)

        preview = state.preview_conversation_deletion(
            ConversationDeletionScope.conversation("conversation-large-delete")
        )
        assert preview.messages == messages

        tombstones = state.delete_conversation_history(
            preview,
            deletion_id="large-conversation-delete",
            deleted_at=NOW + timedelta(minutes=1),
        )

        assert len(tombstones) == len(messages)
        assert state.list_conversation_messages() == ()
        assert set(state.list_conversation_tombstones()) == set(tombstones)
    finally:
        state.close()

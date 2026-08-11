from __future__ import annotations

import json
import os
import pickle
import sqlite3
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from multiprocessing.connection import Listener
from threading import Event, RLock, Thread
from time import monotonic as time_monotonic
from time import sleep

import pytest
from test_support import build_receiver_components

from jarvis_control_plane import (
    AuditEvidence,
    AuditWriteError,
    ConversationDeletionPreview,
    ConversationDeletionScope,
    ConversationMessage,
    DeletedConversationArchiveError,
    InboundMessage,
    InMemoryAuditBoundary,
    InMemoryDeletedConversationArchive,
    InMemoryDurableStateStore,
    SignedInboundEvent,
    SQLiteDurableStateStore,
    StateStoreError,
    conversation_archive,
)
from jarvis_control_plane.control_grammar import MessageKind, parse_control
from jarvis_control_plane.conversation_archive import (
    SQLiteDeletedConversationArchiveWriter,
    start_sqlite_deleted_conversation_archive_service,
)
from jarvis_control_plane.manual_admin import open_sqlite_deleted_conversation_archive

NOW = datetime(2026, 8, 8, 9, 0, tzinfo=UTC)
OPERATOR = "operator.test"
TRANSPORT_SESSION = "session.test"


class _FailDeletionAttemptAudit(InMemoryAuditBoundary):
    def append(self, evidence: AuditEvidence) -> None:
        if evidence.kind == "conversation_history_deletion_attempt":
            raise AuditWriteError("controlled deletion-attempt audit failure")
        super().append(evidence)


class _FailOnceCommitConnection(sqlite3.Connection):
    fail_next_commit = False

    def commit(self) -> None:
        if self.fail_next_commit:
            self.fail_next_commit = False
            raise sqlite3.OperationalError("injected live-state commit failure")
        super().commit()


class _FailDeletionStateStore(InMemoryDurableStateStore):
    def delete_conversation_history(self, preview, *, deletion_id, deleted_at):
        raise StateStoreError("controlled deletion state failure")


class _FailDeletionArchive:
    def stage(
        self,
        messages,
        *,
        deletion_id,
        deleted_at,
        expected_count=None,
        expected_digest=None,
    ):
        raise DeletedConversationArchiveError("controlled archive failure")

    def finalize(self, *, deletion_id):
        raise DeletedConversationArchiveError("controlled archive failure")

    def abort(self, *, deletion_id):
        return None

    def archive(
        self,
        messages,
        *,
        deletion_id,
        deleted_at,
        expected_count=None,
        expected_digest=None,
    ):
        raise DeletedConversationArchiveError("controlled archive failure")

    def close(self):
        return None


class _TransactionObservingArchive:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._archive = InMemoryDeletedConversationArchive()
        self.stage_transaction_states: list[bool] = []
        self.finalize_transaction_states: list[bool] = []

    def stage(self, messages, **kwargs):
        self.stage_transaction_states.append(self._connection.in_transaction)
        self._archive.stage(messages, **kwargs)

    def finalize(self, *, deletion_id):
        self.finalize_transaction_states.append(self._connection.in_transaction)
        self._archive.finalize(deletion_id=deletion_id)

    def abort(self, *, deletion_id):
        self._archive.abort(deletion_id=deletion_id)

    def archive(self, messages, **kwargs):
        self._archive.archive(messages, **kwargs)

    def close(self):
        self._archive.close()


class _FailDeletionArchiveStateStore(InMemoryDurableStateStore):
    def __init__(self):
        super().__init__(deleted_archive=_FailDeletionArchive())


class _AmbiguousDeletionStateStore(InMemoryDurableStateStore):
    def delete_conversation_history(self, preview, *, deletion_id, deleted_at):
        super().delete_conversation_history(
            preview,
            deletion_id=deletion_id,
            deleted_at=deleted_at,
        )
        raise StateStoreError(
            "controlled post-deletion uncertainty",
            may_have_dispatched=True,
        )


class _FailDeletionResultAudit(InMemoryAuditBoundary):
    def append(self, evidence: AuditEvidence) -> None:
        if evidence.kind == "conversation_history_deletion_result":
            raise AuditWriteError("controlled deletion-result audit failure")
        super().append(evidence)


class _PickleExecutionProbe:
    """Would create a marker if an archive server unpickled this payload."""

    def __init__(self, marker: str) -> None:
        self.marker = marker

    def __reduce__(self):
        return os.system, (f'echo executed > "{self.marker}"',)


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


def test_archive_ipc_rejects_pickle_payload_without_executing_it(tmp_path) -> None:
    archive_service = start_sqlite_deleted_conversation_archive_service(
        tmp_path / "deleted.sqlite3"
    )
    marker = tmp_path / "pickle-executed.txt"
    writer = archive_service.writer
    try:
        writer._connection.send_bytes(  # type: ignore[attr-defined]
            pickle.dumps(_PickleExecutionProbe(str(marker)))
        )

        assert writer._connection.poll(2.0)  # type: ignore[attr-defined]
        response = json.loads(  # type: ignore[attr-defined]
            writer._connection.recv_bytes(  # type: ignore[attr-defined]
                conversation_archive._MAX_ARCHIVE_FRAME_BYTES
            ).decode("utf-8")
        )

        assert response["ok"] is False
        assert not marker.exists()
    finally:
        archive_service.close()


def test_archive_ipc_response_timeout_closes_writer_connection(
    tmp_path, monkeypatch
) -> None:
    authkey = b"ticket21-hanging-archive"
    endpoint = (
        rf"\\.\pipe\jarvis-ticket21-hanging-{os.urandom(8).hex()}"
        if os.name == "nt"
        else str(tmp_path / "hanging.sock")
    )
    listener_ready = Event()
    request_received = Event()
    release_server = Event()
    server_errors: list[BaseException] = []

    def serve_without_reply() -> None:
        listener = None
        connection = None
        try:
            listener = Listener(
                endpoint,
                family=conversation_archive._archive_ipc_family(),
                authkey=authkey,
            )
            listener_ready.set()
            connection = listener.accept()
            connection.send_bytes(b'{"ok":true}')
            connection.recv_bytes(conversation_archive._MAX_ARCHIVE_FRAME_BYTES)
            request_received.set()
            release_server.wait(2.0)
        except (EOFError, OSError):
            request_received.set()
        except (AssertionError, RuntimeError, TypeError, ValueError) as exc:
            server_errors.append(exc)
        finally:
            if connection is not None:
                connection.close()
            if listener is not None:
                listener.close()
            if os.name != "nt":
                (tmp_path / "hanging.sock").unlink(missing_ok=True)

    server_thread = Thread(target=serve_without_reply, daemon=True)
    server_thread.start()
    assert listener_ready.wait(2.0)
    monkeypatch.setattr(conversation_archive, "_ARCHIVE_RESPONSE_TIMEOUT_SECONDS", 0.2)

    writer = SQLiteDeletedConversationArchiveWriter(endpoint, authkey=authkey)
    try:
        with pytest.raises(DeletedConversationArchiveError, match="timed out"):
            writer.archive(
                (_message(message_id="timeout", text="archive request hangs"),),
                deletion_id="deletion-timeout",
                deleted_at=NOW,
            )
        assert request_received.wait(2.0)
        assert not writer._closed  # type: ignore[attr-defined]
        assert writer._connection is None  # type: ignore[attr-defined]
    finally:
        writer.close()
        release_server.set()
        server_thread.join(2.0)

    assert not server_thread.is_alive()
    assert server_errors == []


def test_archive_transfer_uses_one_deadline_across_all_frames(monkeypatch) -> None:
    writer = object.__new__(SQLiteDeletedConversationArchiveWriter)
    writer._lock = RLock()  # type: ignore[attr-defined]
    writer._closed = False  # type: ignore[attr-defined]
    writer._active_deletion_id = None  # type: ignore[attr-defined]
    timeouts: list[float] = []

    def record_request(request, *, timeout_seconds, reconnect=True):
        del request, reconnect
        timeouts.append(timeout_seconds)

    writer._request = record_request  # type: ignore[attr-defined]
    monkeypatch.setattr(conversation_archive, "_MAX_ARCHIVE_FRAME_BYTES", 500)
    messages = tuple(
        _message(message_id=f"deadline-{index}", text="x" * 100) for index in range(20)
    )

    writer.archive(messages, deletion_id="aggregate-deadline", deleted_at=NOW)

    assert len(timeouts) > 3
    assert all(earlier > later for earlier, later in pairwise(timeouts))


def test_archive_service_cleans_staged_batch_when_writer_disconnects(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "deleted.sqlite3"
    service = start_sqlite_deleted_conversation_archive_service(database)
    writer = service.writer
    original_receive_response = writer._receive_response
    receive_calls = 0

    def disconnect_after_first_chunk(*, timeout_seconds):
        nonlocal receive_calls
        response = original_receive_response(timeout_seconds=timeout_seconds)
        receive_calls += 1
        if receive_calls == 2:
            writer._close_connection()
            raise DeletedConversationArchiveError(
                "deleted archive service response timed out"
            )
        return response

    monkeypatch.setattr(writer, "_receive_response", disconnect_after_first_chunk)
    try:
        with pytest.raises(DeletedConversationArchiveError, match="timed out"):
            writer.archive(
                (_message(message_id="orphaned", text="must not be stranded"),),
                deletion_id="orphaned-batch",
                deleted_at=NOW,
            )

        deadline = time_monotonic() + 2.0
        counts = None
        while time_monotonic() < deadline:
            connection = sqlite3.connect(database)
            try:
                counts = connection.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM deleted_message_batches),
                        (SELECT COUNT(*) FROM deleted_message_batch_items),
                        (SELECT COUNT(*) FROM deleted_messages)
                    """
                ).fetchone()
            finally:
                connection.close()
            if counts == (0, 0, 0):
                break
            sleep(0.01)

        assert counts == (0, 0, 0)
    finally:
        service.close()


def test_closing_one_writer_does_not_stop_the_archive_service(tmp_path) -> None:
    database = tmp_path / "deleted.sqlite3"
    service = start_sqlite_deleted_conversation_archive_service(database)
    first_writer = service.writer
    second_writer = None
    message = _message(message_id="fresh-writer", text="service remains alive")
    try:
        first_writer.close()
        second_writer = SQLiteDeletedConversationArchiveWriter(
            service.endpoint,
            authkey=service._authkey,  # type: ignore[attr-defined]
        )
        second_writer.archive(
            (message,),
            deletion_id="fresh-writer-deletion",
            deleted_at=NOW,
        )
    finally:
        if second_writer is not None:
            second_writer.close()
        service.close()

    admin_archive = open_sqlite_deleted_conversation_archive(database)
    try:
        assert [record.message for record in admin_archive.list_records()] == [message]
    finally:
        admin_archive.close()


def test_archive_accepts_health_writer_while_broker_writer_remains_connected(
    tmp_path,
) -> None:
    database = tmp_path / "deleted.sqlite3"
    service = start_sqlite_deleted_conversation_archive_service(database)
    probe = None
    try:
        probe = SQLiteDeletedConversationArchiveWriter(
            service.endpoint,
            authkey=service._authkey,  # type: ignore[attr-defined]
        )
    finally:
        if probe is not None:
            probe.close()
        service.close()


def test_sqlite_deletion_stages_transfer_before_live_write_transaction(tmp_path):
    connection = sqlite3.connect(tmp_path / "jarvis.sqlite3")
    archive = _TransactionObservingArchive(connection)
    state = SQLiteDurableStateStore(connection, deleted_archive=archive)
    selected = _message(message_id="transaction-boundary", text="outside lock")
    try:
        state.append_conversation_message(selected)
        preview = state.preview_conversation_deletion(
            ConversationDeletionScope.message(selected.history_id)
        )

        state.delete_conversation_history(
            preview,
            deletion_id="transaction-boundary-deletion",
            deleted_at=NOW,
        )

        assert archive.stage_transaction_states == [False]
        assert archive.finalize_transaction_states == [True]
        assert state.list_conversation_messages() == ()
    finally:
        state.close()
        connection.close()


def test_archive_writer_recovers_after_timeout_for_a_later_transaction(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "deleted.sqlite3"
    archive_service = start_sqlite_deleted_conversation_archive_service(database)
    writer = archive_service.writer
    original_receive_response = writer._receive_response
    receive_calls = 0

    def timeout_first_response(*, timeout_seconds):
        nonlocal receive_calls
        receive_calls += 1
        if receive_calls == 1:
            writer._close_connection()
            raise DeletedConversationArchiveError(
                "deleted archive service response timed out"
            )
        return original_receive_response(timeout_seconds=timeout_seconds)

    monkeypatch.setattr(writer, "_receive_response", timeout_first_response)
    later_message = _message(message_id="recovered", text="second transaction")
    try:
        with pytest.raises(DeletedConversationArchiveError, match="timed out"):
            writer.archive(
                (_message(message_id="timeout-first", text="first transaction"),),
                deletion_id="deletion-timeout-first",
                deleted_at=NOW,
            )

        writer.archive(
            (later_message,),
            deletion_id="deletion-after-timeout",
            deleted_at=NOW + timedelta(minutes=1),
        )
    finally:
        archive_service.close()

    admin_archive = open_sqlite_deleted_conversation_archive(database)
    try:
        assert [record.message for record in admin_archive.list_records()] == [
            later_message
        ]
    finally:
        admin_archive.close()


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

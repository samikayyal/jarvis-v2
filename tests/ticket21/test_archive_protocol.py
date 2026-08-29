from __future__ import annotations

import json
import os
import pickle
import sqlite3
from datetime import timedelta
from itertools import pairwise
from multiprocessing.connection import Listener
from threading import (
    Event,
    RLock,
    Thread,
)
from time import monotonic as time_monotonic
from time import sleep

import pytest

from jarvis_control_plane import (
    ConversationDeletionScope,
    DeletedConversationArchiveError,
    SQLiteDurableStateStore,
    conversation_archive,
)
from jarvis_control_plane.control_grammar import (
    MessageKind,
    parse_control,
)
from jarvis_control_plane.conversation_archive import (
    SQLiteDeletedConversationArchiveWriter,
    start_sqlite_deleted_conversation_archive_service,
)
from jarvis_control_plane.manual_admin import open_sqlite_deleted_conversation_archive

from .helpers import (
    NOW,
    _message,
    _PickleExecutionProbe,
    _TransactionObservingArchive,
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

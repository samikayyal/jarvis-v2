"""Compatibility facade for the layered deleted-conversation archive.

The original module was the import and monkeypatch surface for the control
plane.  Keep that surface stable here while implementation lives under
``jarvis_control_plane.archive`` in records, wire, memory, SQLite, and
service layers.
"""

from __future__ import annotations

import os
import signal
import stat
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from multiprocessing.connection import Listener
from pathlib import Path
from typing import Any, NoReturn

from . import ports as _ports
from .archive import framing as _framing
from .archive import memory as _memory
from .archive import records as _records
from .archive import service as _service
from .archive import sqlite_storage as _storage
from .archive import wire as _wire
from .archive.records import DeletedConversationArchiveRecord
from .archive.sqlite_writer import (
    DEFAULT_ARCHIVE_CLOSE_TIMEOUT_SECONDS,
    DEFAULT_ARCHIVE_RESPONSE_TIMEOUT_SECONDS,
)
from .models import ConversationMessage, _conversation_message_digest
from .ports import DeletedConversationArchiveError

DeletedConversationArchiveWriter = _ports.DeletedConversationArchiveWriter
_StagedArchiveBatch = _records.StagedArchiveBatch


_ARCHIVE_RESPONSE_TIMEOUT_SECONDS = DEFAULT_ARCHIVE_RESPONSE_TIMEOUT_SECONDS
_ARCHIVE_CLOSE_TIMEOUT_SECONDS = DEFAULT_ARCHIVE_CLOSE_TIMEOUT_SECONDS
_MAX_ARCHIVE_FRAME_BYTES = _wire.DEFAULT_MAX_ARCHIVE_FRAME_BYTES
(
    _ARCHIVE_MESSAGE_FIELDS,
    _ARCHIVE_REQUEST_FIELDS,
    _ARCHIVE_BEGIN_REQUEST_FIELDS,
    _ARCHIVE_CHUNK_REQUEST_FIELDS,
    _ARCHIVE_COMMIT_REQUEST_FIELDS,
    _ARCHIVE_ABORT_REQUEST_FIELDS,
    _ARCHIVE_SUCCESS_RESPONSE_FIELDS,
    _ARCHIVE_FAILURE_RESPONSE_FIELDS,
) = (
    _wire.ARCHIVE_MESSAGE_FIELDS,
    _wire.ARCHIVE_REQUEST_FIELDS,
    _wire.ARCHIVE_BEGIN_REQUEST_FIELDS,
    _wire.ARCHIVE_CHUNK_REQUEST_FIELDS,
    _wire.ARCHIVE_COMMIT_REQUEST_FIELDS,
    _wire.ARCHIVE_ABORT_REQUEST_FIELDS,
    _wire.ARCHIVE_SUCCESS_RESPONSE_FIELDS,
    _wire.ARCHIVE_FAILURE_RESPONSE_FIELDS,
)
_ARCHIVE_SCHEMA = _storage.ARCHIVE_SCHEMA


def _terminate_archive_service(_signum: int, _frame: object) -> NoReturn:
    raise SystemExit


def _validate_archive_request(
    messages: Sequence[ConversationMessage],
    *,
    deletion_id: str,
    deleted_at: datetime,
    expected_count: int | None = None,
    expected_digest: str | None = None,
) -> tuple[tuple[ConversationMessage, ...], datetime]:
    return _records.validate_archive_request(
        messages,
        deletion_id=deletion_id,
        deleted_at=deleted_at,
        expected_count=expected_count,
        expected_digest=expected_digest,
    )


_validate_expected_count = _records.validate_expected_count
_validate_expected_digest = _records.validate_expected_digest


def _validate_batch_metadata(
    *,
    deletion_id: object,
    deleted_at: object,
    expected_count: object,
    expected_digest: object,
) -> tuple[str, datetime, int, str]:
    return _records.validate_batch_metadata(
        deletion_id=deletion_id,
        deleted_at=deleted_at,
        expected_count=expected_count,
        expected_digest=expected_digest,
    )


_archive_values = _records.archive_values
_archive_content_values = _records.archive_content_values
_archive_message_from_row = _records.archive_message_from_row
_archive_record_from_row = _records.archive_record_from_row


class InMemoryDeletedConversationArchive(_memory.InMemoryDeletedConversationArchive):
    """Compatibility subclass using facade-level validation seams."""

    def __init__(self) -> None:
        super().__init__(
            request_validator=lambda messages, **kwargs: _validate_archive_request(
                messages, **kwargs
            ),
            message_digest=lambda records: _conversation_message_digest(records),
        )


def _archive_ipc_family() -> str:
    return "AF_PIPE" if os.name == "nt" else "AF_UNIX"


_validate_archive_authkey = _service.validate_archive_authkey


_reject_json_constant = _framing._reject_json_constant
_reject_duplicate_json_keys = _framing._reject_duplicate_json_keys


def _encode_archive_frame(payload: Mapping[str, object]) -> bytes:
    return _wire.encode_archive_frame(
        payload,
        max_frame_bytes=_MAX_ARCHIVE_FRAME_BYTES,
    )


def _decode_archive_frame(frame: bytes) -> object:
    return _wire.decode_archive_frame(
        frame,
        max_frame_bytes=_MAX_ARCHIVE_FRAME_BYTES,
    )


_require_exact_mapping = _wire.require_exact_mapping
_archive_message_to_wire = _wire.archive_message_to_wire
_archive_message_from_wire = _wire.archive_message_from_wire


def _encode_archive_request(request: dict[str, Any]) -> bytes:
    return _wire.encode_archive_request(
        request,
        max_frame_bytes=_MAX_ARCHIVE_FRAME_BYTES,
    )


def _decode_archive_request(frame: bytes) -> dict[str, object]:
    return _wire.decode_archive_request(
        frame,
        max_frame_bytes=_MAX_ARCHIVE_FRAME_BYTES,
    )


def _archive_message_chunks(
    records: Sequence[ConversationMessage],
    *,
    deletion_id: str,
) -> Iterator[tuple[ConversationMessage, ...]]:
    return _wire.archive_message_chunks(
        records,
        deletion_id=deletion_id,
        max_frame_bytes=_MAX_ARCHIVE_FRAME_BYTES,
    )


def _encode_archive_response(*, ok: bool, message: str | None = None) -> bytes:
    return _wire.encode_archive_response(
        ok=ok,
        message=message,
        max_frame_bytes=_MAX_ARCHIVE_FRAME_BYTES,
    )


def _decode_archive_response(frame: bytes) -> dict[str, object]:
    return _wire.decode_archive_response(
        frame,
        max_frame_bytes=_MAX_ARCHIVE_FRAME_BYTES,
    )


def _send_archive_response(
    connection: Any,
    *,
    ok: bool,
    message: str | None = None,
) -> None:
    connection.send_bytes(_encode_archive_response(ok=ok, message=message))


_archive_endpoint_path = _service.archive_endpoint_path


def _create_archive_listener(endpoint: str | Path, authkey: bytes) -> Listener:
    family = _archive_ipc_family()
    if family == "AF_UNIX":
        endpoint_path = _archive_endpoint_path(endpoint)
        if endpoint_path.exists():
            raise DeletedConversationArchiveError(
                "deleted archive IPC endpoint already exists"
            )
        endpoint_path.parent.mkdir(parents=True, exist_ok=True)
        listener = Listener(
            str(endpoint_path),
            family=family,
            authkey=_validate_archive_authkey(authkey),
        )
        os.chmod(
            endpoint_path,
            stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP,
        )
        return listener
    return Listener(
        str(endpoint),
        family=family,
        authkey=_validate_archive_authkey(authkey),
    )


def _remove_archive_endpoint(endpoint: str | Path) -> None:
    if os.name != "nt":
        _archive_endpoint_path(endpoint).unlink(missing_ok=True)


_archive_database_path = _storage.archive_database_path
_insert_archive_records = _storage.insert_archive_records
_archive_batch = _storage.archive_batch
_begin_archive_batch = _storage.begin_archive_batch
_append_archive_batch_chunk = _storage.append_archive_batch_chunk
_commit_archive_batch = _storage.commit_archive_batch
_abort_archive_batch = _storage.abort_archive_batch
_abort_incomplete_archive_batches = _storage.abort_incomplete_archive_batches


def _serve_archive_connection(connection: Any, database: str | Path) -> None:
    _service._serve_archive_connection(
        connection,
        database,
        _wire.ArchiveWireCodec(_MAX_ARCHIVE_FRAME_BYTES),
    )


class SQLiteDeletedConversationArchiveWriter(
    _service.SQLiteDeletedConversationArchiveWriter
):
    """Compatibility writer retaining all historical monkeypatch points."""

    def __init__(self, endpoint: str | Path, *, authkey: bytes) -> None:
        super().__init__(endpoint, authkey=authkey)

    def _validate_authkey(self, authkey: bytes) -> bytes:
        return _validate_archive_authkey(authkey)

    def _frame_limit(self) -> int:
        return _MAX_ARCHIVE_FRAME_BYTES

    def _ipc_family(self) -> str:
        return _archive_ipc_family()

    def _encode_request(self, request: dict[str, Any]) -> bytes:
        return _encode_archive_request(request)

    def _decode_response(self, frame: bytes) -> dict[str, object]:
        return _decode_archive_response(frame)

    def _message_chunks(
        self,
        records: Sequence[ConversationMessage],
        *,
        deletion_id: str,
    ) -> Iterator[tuple[ConversationMessage, ...]]:
        return _archive_message_chunks(records, deletion_id=deletion_id)

    def _request_timeout(self) -> float:
        return _ARCHIVE_RESPONSE_TIMEOUT_SECONDS

    def _close_timeout(self) -> float:
        return _ARCHIVE_CLOSE_TIMEOUT_SECONDS

    def _validate_request(
        self,
        messages: Sequence[ConversationMessage],
        *,
        deletion_id: str,
        deleted_at: datetime,
        expected_count: int | None = None,
        expected_digest: str | None = None,
    ) -> tuple[tuple[ConversationMessage, ...], datetime]:
        return _validate_archive_request(
            messages,
            deletion_id=deletion_id,
            deleted_at=deleted_at,
            expected_count=expected_count,
            expected_digest=expected_digest,
        )

    def _digest(self, records: tuple[ConversationMessage, ...]) -> str:
        return _conversation_message_digest(records)


class SQLiteDeletedConversationArchiveService(
    _service.SQLiteDeletedConversationArchiveService
):
    """Compatibility launcher using the facade writer and lifecycle seams."""

    def __init__(
        self,
        database: str | Path,
        *,
        endpoint: str | Path | None = None,
        authkey: bytes | None = None,
    ) -> None:
        super().__init__(
            database,
            endpoint=endpoint,
            authkey=authkey,
            writer_factory=SQLiteDeletedConversationArchiveWriter,
            remove_endpoint=lambda value: _remove_archive_endpoint(value),
            frame_limit=_MAX_ARCHIVE_FRAME_BYTES,
            response_timeout_seconds=_ARCHIVE_RESPONSE_TIMEOUT_SECONDS,
            close_timeout_seconds=_ARCHIVE_CLOSE_TIMEOUT_SECONDS,
        )


def serve_sqlite_deleted_conversation_archive(
    database: str | Path,
    endpoint: str | Path,
    *,
    authkey: bytes,
) -> None:
    """Serve the archive from the independently supervised admin process."""

    _service.serve_archive(
        database,
        endpoint,
        authkey=authkey,
        create_listener=_create_archive_listener,
        remove_endpoint=_remove_archive_endpoint,
        signal_module=signal,
        terminate_handler=_terminate_archive_service,
        frame_limit=_MAX_ARCHIVE_FRAME_BYTES,
    )


def _archive_service_process_main(
    startup_connection: Any,
    database: str,
    endpoint: str,
    authkey: bytes,
) -> None:
    _service.archive_service_process_main(
        startup_connection,
        database,
        endpoint,
        authkey,
        _MAX_ARCHIVE_FRAME_BYTES,
    )


def start_sqlite_deleted_conversation_archive_service(
    database: str | Path,
    *,
    endpoint: str | Path | None = None,
    authkey: bytes | None = None,
) -> SQLiteDeletedConversationArchiveService:
    """Start the local test/development archive service launcher."""

    return SQLiteDeletedConversationArchiveService(
        database,
        endpoint=endpoint,
        authkey=authkey,
    )


__all__ = [
    "DeletedConversationArchiveRecord",
    "InMemoryDeletedConversationArchive",
    "SQLiteDeletedConversationArchiveService",
    "SQLiteDeletedConversationArchiveWriter",
    "serve_sqlite_deleted_conversation_archive",
    "start_sqlite_deleted_conversation_archive_service",
]

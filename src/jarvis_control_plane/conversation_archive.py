"""Write-only deleted-conversation archival capabilities.

The ordinary state store receives only a write capability for this boundary.
The archival SQLite connection belongs to a helper process, and the manual
administration module opens the read side independently.  Keeping those
capabilities separate prevents the live Jarvis state connection from
attaching, querying, or otherwise exposing deleted message bodies.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from multiprocessing import Pipe, Process
from multiprocessing.connection import Client, Listener
from pathlib import Path
from threading import RLock
from time import monotonic
from typing import Any, NoReturn

from .models import ConversationMessage, _conversation_message_digest, ensure_utc
from .ports import DeletedConversationArchiveError, DeletedConversationArchiveWriter

_ARCHIVE_RESPONSE_TIMEOUT_SECONDS = 30.0
_ARCHIVE_CLOSE_TIMEOUT_SECONDS = 2.0
_MAX_ARCHIVE_FRAME_BYTES = 8 * 1024 * 1024
_ARCHIVE_MESSAGE_FIELDS = frozenset(
    {
        "chat_id",
        "credential_like",
        "direction",
        "event_id",
        "message_id",
        "occurred_at",
        "request_id",
        "sender_id",
        "text",
        "transport_session_id",
        "working_session_id",
    }
)
_ARCHIVE_CLOSE_REQUEST_FIELDS = frozenset({"operation"})
_ARCHIVE_REQUEST_FIELDS = frozenset(
    {"operation", "messages", "deletion_id", "deleted_at"}
)
_ARCHIVE_BEGIN_REQUEST_FIELDS = frozenset(
    {"operation", "deletion_id", "deleted_at", "expected_count", "expected_digest"}
)
_ARCHIVE_CHUNK_REQUEST_FIELDS = frozenset(
    {"operation", "deletion_id", "chunk_index", "messages"}
)
_ARCHIVE_COMMIT_REQUEST_FIELDS = frozenset({"operation", "deletion_id"})
_ARCHIVE_ABORT_REQUEST_FIELDS = frozenset({"operation", "deletion_id"})
_ARCHIVE_SUCCESS_RESPONSE_FIELDS = frozenset({"ok"})
_ARCHIVE_FAILURE_RESPONSE_FIELDS = frozenset({"ok", "message"})
_ARCHIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS deleted_messages (
    transport_session_id TEXT NOT NULL,
    working_session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    text TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    request_id TEXT,
    credential_like INTEGER NOT NULL CHECK (credential_like IN (0, 1)),
    deletion_id TEXT NOT NULL,
    deleted_at TEXT NOT NULL,
    PRIMARY KEY (transport_session_id, message_id)
)
;
CREATE TABLE IF NOT EXISTS deleted_message_batches (
    deletion_id TEXT PRIMARY KEY,
    expected_count INTEGER NOT NULL CHECK (expected_count >= 0),
    expected_digest TEXT NOT NULL,
    deleted_at TEXT NOT NULL
)
;
CREATE TABLE IF NOT EXISTS deleted_message_batch_items (
    deletion_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    item_index INTEGER NOT NULL CHECK (item_index >= 0),
    transport_session_id TEXT NOT NULL,
    working_session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    text TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    request_id TEXT,
    credential_like INTEGER NOT NULL CHECK (credential_like IN (0, 1)),
    PRIMARY KEY (deletion_id, transport_session_id, message_id),
    UNIQUE (deletion_id, chunk_index, item_index)
)
"""


@dataclass(frozen=True, slots=True)
class DeletedConversationArchiveRecord:
    """One retained message visible only to manual administration."""

    message: ConversationMessage
    deletion_id: str
    deleted_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.message, ConversationMessage):
            raise TypeError("archive record message must be a ConversationMessage")
        if not isinstance(self.deletion_id, str) or not self.deletion_id.strip():
            raise ValueError("archive record deletion_id must be non-blank")
        object.__setattr__(self, "deleted_at", ensure_utc(self.deleted_at))


def _validate_archive_request(
    messages: Sequence[ConversationMessage],
    *,
    deletion_id: str,
    deleted_at: datetime,
    expected_count: int | None = None,
    expected_digest: str | None = None,
) -> tuple[tuple[ConversationMessage, ...], datetime]:
    if not isinstance(messages, Sequence):
        raise TypeError("deleted archive messages must be a sequence")
    records = tuple(messages)
    if any(not isinstance(message, ConversationMessage) for message in records):
        raise TypeError("deleted archive accepts only ConversationMessage values")
    if len(
        {(message.transport_session_id, message.message_id) for message in records}
    ) != len(records):
        raise ValueError("deleted archive request contains duplicate messages")
    if not isinstance(deletion_id, str) or not deletion_id.strip():
        raise ValueError("deletion_id must be non-blank")
    if expected_count is not None:
        _validate_expected_count(expected_count)
        if expected_count != len(records):
            raise ValueError("deleted archive message count does not match metadata")
    if expected_digest is not None:
        _validate_expected_digest(expected_digest)
        if expected_digest != _conversation_message_digest(records):
            raise ValueError("deleted archive message digest does not match metadata")
    return records, ensure_utc(deleted_at)


def _validate_expected_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(
            "deleted archive expected count must be a non-negative integer"
        )
    return value


def _validate_expected_digest(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("deleted archive expected digest must be a SHA-256 hex digest")
    return value


def _validate_batch_metadata(
    *,
    deletion_id: object,
    deleted_at: object,
    expected_count: object,
    expected_digest: object,
) -> tuple[str, datetime, int, str]:
    if not isinstance(deletion_id, str) or not deletion_id.strip():
        raise ValueError("deleted archive deletion_id must be non-blank")
    if not isinstance(deleted_at, datetime):
        raise TypeError("deleted archive deleted_at must be a datetime")
    count = _validate_expected_count(expected_count)
    digest = _validate_expected_digest(expected_digest)
    return deletion_id, ensure_utc(deleted_at), count, digest


def _archive_values(
    message: ConversationMessage,
    *,
    deletion_id: str,
    deleted_at: datetime,
) -> tuple[object, ...]:
    return (
        message.transport_session_id,
        message.working_session_id,
        message.message_id,
        message.event_id,
        message.chat_id,
        message.sender_id,
        message.text,
        message.occurred_at.isoformat(),
        message.direction,
        message.request_id,
        int(message.credential_like),
        deletion_id,
        deleted_at.isoformat(),
    )


def _archive_content_values(
    message: ConversationMessage,
) -> tuple[object, ...]:
    return (
        message.transport_session_id,
        message.working_session_id,
        message.message_id,
        message.event_id,
        message.chat_id,
        message.sender_id,
        message.text,
        message.occurred_at.isoformat(),
        message.direction,
        message.request_id,
        int(message.credential_like),
    )


def _archive_message_from_row(row: sqlite3.Row) -> ConversationMessage:
    return ConversationMessage(
        working_session_id=row["working_session_id"],
        transport_session_id=row["transport_session_id"],
        message_id=row["message_id"],
        event_id=row["event_id"],
        chat_id=row["chat_id"],
        sender_id=row["sender_id"],
        text=row["text"],
        occurred_at=datetime.fromisoformat(row["occurred_at"]),
        direction=row["direction"],
        request_id=row["request_id"],
        credential_like=bool(row["credential_like"]),
    )


def _archive_record_from_row(row: sqlite3.Row) -> DeletedConversationArchiveRecord:
    return DeletedConversationArchiveRecord(
        message=_archive_message_from_row(row),
        deletion_id=row["deletion_id"],
        deleted_at=datetime.fromisoformat(row["deleted_at"]),
    )


class InMemoryDeletedConversationArchive:
    """Test-only write boundary with a separate administration read surface."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], DeletedConversationArchiveRecord] = {}
        self._lock = RLock()

    def archive(
        self,
        messages: Sequence[ConversationMessage],
        *,
        deletion_id: str,
        deleted_at: datetime,
        expected_count: int | None = None,
        expected_digest: str | None = None,
    ) -> None:
        records, normalized_deleted_at = _validate_archive_request(
            messages,
            deletion_id=deletion_id,
            deleted_at=deleted_at,
            expected_count=expected_count,
            expected_digest=expected_digest,
        )
        with self._lock:
            for message in records:
                key = (message.transport_session_id, message.message_id)
                record = DeletedConversationArchiveRecord(
                    message=message,
                    deletion_id=deletion_id,
                    deleted_at=normalized_deleted_at,
                )
                existing = self._records.get(key)
                if existing is not None and existing.message != message:
                    raise DeletedConversationArchiveError(
                        "deleted archive record does not match a prior transfer"
                    )
                self._records[key] = record

    def read_records(self) -> tuple[DeletedConversationArchiveRecord, ...]:
        """Return records through the test's separate administration fixture."""

        with self._lock:
            return tuple(
                sorted(
                    self._records.values(),
                    key=lambda record: (
                        record.deleted_at,
                        record.message.transport_session_id,
                        record.message.message_id,
                    ),
                )
            )

    def close(self) -> None:
        return None


def _archive_ipc_family() -> str:
    return "AF_PIPE" if os.name == "nt" else "AF_UNIX"


def _validate_archive_authkey(authkey: bytes) -> bytes:
    if not isinstance(authkey, bytes) or not authkey:
        raise ValueError("deleted archive IPC authkey must be non-empty bytes")
    return authkey


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON constant {value!r} is not supported")


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _encode_archive_frame(payload: Mapping[str, object]) -> bytes:
    try:
        frame = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("deleted archive payload is not strict JSON") from exc
    if len(frame) > _MAX_ARCHIVE_FRAME_BYTES:
        raise ValueError("deleted archive IPC frame exceeds the fixed size limit")
    return frame


def _decode_archive_frame(frame: bytes) -> object:
    if not isinstance(frame, bytes):
        raise TypeError("deleted archive IPC frame must be bytes")
    if len(frame) > _MAX_ARCHIVE_FRAME_BYTES:
        raise ValueError("deleted archive IPC frame exceeds the fixed size limit")
    try:
        return json.loads(
            frame.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("deleted archive IPC frame is not valid strict JSON") from exc


def _require_exact_mapping(
    value: object,
    expected_fields: frozenset[str],
    description: str,
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise ValueError(f"{description} has an invalid schema")
    return value


def _archive_message_to_wire(message: ConversationMessage) -> dict[str, object]:
    return {
        "chat_id": message.chat_id,
        "credential_like": message.credential_like,
        "direction": message.direction,
        "event_id": message.event_id,
        "message_id": message.message_id,
        "occurred_at": message.occurred_at.isoformat(),
        "request_id": message.request_id,
        "sender_id": message.sender_id,
        "text": message.text,
        "transport_session_id": message.transport_session_id,
        "working_session_id": message.working_session_id,
    }


def _archive_message_from_wire(value: object) -> ConversationMessage:
    raw = _require_exact_mapping(value, _ARCHIVE_MESSAGE_FIELDS, "archive message")
    string_fields = (
        "chat_id",
        "direction",
        "event_id",
        "message_id",
        "occurred_at",
        "sender_id",
        "text",
        "transport_session_id",
        "working_session_id",
    )
    if any(not isinstance(raw[field], str) for field in string_fields):
        raise ValueError("archive message has a non-string field")
    credential_like = raw["credential_like"]
    if type(credential_like) is not bool:
        raise ValueError("archive message credential_like must be a boolean")
    request_id = raw["request_id"]
    if request_id is not None and not isinstance(request_id, str):
        raise ValueError("archive message request_id must be a string or null")
    try:
        message = ConversationMessage(
            working_session_id=raw["working_session_id"],
            transport_session_id=raw["transport_session_id"],
            message_id=raw["message_id"],
            event_id=raw["event_id"],
            chat_id=raw["chat_id"],
            sender_id=raw["sender_id"],
            text=raw["text"],
            occurred_at=datetime.fromisoformat(raw["occurred_at"]),
            direction=raw["direction"],
            request_id=request_id,
            credential_like=credential_like,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("archive message contains invalid domain fields") from exc
    if message.credential_like != credential_like:
        raise ValueError("archive message credential classification is inconsistent")
    return message


def _encode_archive_request(request: dict[str, Any]) -> bytes:
    operation = request.get("operation")
    if operation == "close":
        _require_exact_mapping(
            request, _ARCHIVE_CLOSE_REQUEST_FIELDS, "archive close request"
        )
        return _encode_archive_frame({"operation": "close"})
    if operation == "begin":
        _require_exact_mapping(
            request, _ARCHIVE_BEGIN_REQUEST_FIELDS, "archive begin request"
        )
        _, normalized_deleted_at, expected_count, expected_digest = (
            _validate_batch_metadata(
                deletion_id=request["deletion_id"],
                deleted_at=request["deleted_at"],
                expected_count=request["expected_count"],
                expected_digest=request["expected_digest"],
            )
        )
        return _encode_archive_frame(
            {
                "operation": "begin",
                "deletion_id": request["deletion_id"],
                "deleted_at": normalized_deleted_at.isoformat(),
                "expected_count": expected_count,
                "expected_digest": expected_digest,
            }
        )
    if operation == "chunk":
        _require_exact_mapping(
            request, _ARCHIVE_CHUNK_REQUEST_FIELDS, "archive chunk request"
        )
        chunk_index = request["chunk_index"]
        if isinstance(chunk_index, bool) or not isinstance(chunk_index, int):
            raise ValueError("archive chunk index must be a non-negative integer")
        if chunk_index < 0:
            raise ValueError("archive chunk index must be a non-negative integer")
        records, _ = _validate_archive_request(
            request["messages"],
            deletion_id=request["deletion_id"],
            deleted_at=datetime.now(UTC),
        )
        return _encode_archive_frame(
            {
                "operation": "chunk",
                "deletion_id": request["deletion_id"],
                "chunk_index": chunk_index,
                "messages": [_archive_message_to_wire(message) for message in records],
            }
        )
    if operation in {"commit", "abort"}:
        fields = (
            _ARCHIVE_COMMIT_REQUEST_FIELDS
            if operation == "commit"
            else _ARCHIVE_ABORT_REQUEST_FIELDS
        )
        _require_exact_mapping(request, fields, f"archive {operation} request")
        if (
            not isinstance(request["deletion_id"], str)
            or not request["deletion_id"].strip()
        ):
            raise ValueError("archive deletion_id must be non-blank")
        return _encode_archive_frame(
            {"operation": operation, "deletion_id": request["deletion_id"]}
        )
    if operation != "archive":
        raise ValueError("deleted archive request has an unsupported operation")
    _require_exact_mapping(request, _ARCHIVE_REQUEST_FIELDS, "archive request")
    records, normalized_deleted_at = _validate_archive_request(
        request["messages"],
        deletion_id=request["deletion_id"],
        deleted_at=request["deleted_at"],
    )
    return _encode_archive_frame(
        {
            "operation": "archive",
            "messages": [_archive_message_to_wire(message) for message in records],
            "deletion_id": request["deletion_id"],
            "deleted_at": normalized_deleted_at.isoformat(),
        }
    )


def _decode_archive_request(frame: bytes) -> dict[str, object]:
    payload = _decode_archive_frame(frame)
    if not isinstance(payload, dict):
        raise TypeError("deleted archive request must be a JSON object")
    operation = payload.get("operation")
    if operation == "close":
        _require_exact_mapping(
            payload, _ARCHIVE_CLOSE_REQUEST_FIELDS, "archive close request"
        )
        return {"operation": "close"}
    if operation == "begin":
        raw = _require_exact_mapping(
            payload, _ARCHIVE_BEGIN_REQUEST_FIELDS, "archive begin request"
        )
        deletion_id, deleted_at, expected_count, expected_digest = (
            _validate_batch_metadata(
                deletion_id=raw["deletion_id"],
                deleted_at=datetime.fromisoformat(str(raw["deleted_at"])),
                expected_count=raw["expected_count"],
                expected_digest=raw["expected_digest"],
            )
        )
        return {
            "operation": "begin",
            "deletion_id": deletion_id,
            "deleted_at": deleted_at,
            "expected_count": expected_count,
            "expected_digest": expected_digest,
        }
    if operation == "chunk":
        raw = _require_exact_mapping(
            payload, _ARCHIVE_CHUNK_REQUEST_FIELDS, "archive chunk request"
        )
        raw_messages = raw["messages"]
        if not isinstance(raw_messages, list):
            raise TypeError("archive chunk messages must be a JSON list")
        chunk_index = raw["chunk_index"]
        if isinstance(chunk_index, bool) or not isinstance(chunk_index, int):
            raise ValueError("archive chunk index must be a non-negative integer")
        if chunk_index < 0:
            raise ValueError("archive chunk index must be a non-negative integer")
        deletion_id = raw["deletion_id"]
        if not isinstance(deletion_id, str) or not deletion_id.strip():
            raise ValueError("archive chunk deletion_id must be non-blank")
        messages = tuple(_archive_message_from_wire(value) for value in raw_messages)
        _validate_archive_request(
            messages,
            deletion_id=deletion_id,
            deleted_at=datetime.now(UTC),
        )
        if not messages:
            raise ValueError("archive chunks must contain at least one message")
        return {
            "operation": "chunk",
            "deletion_id": deletion_id,
            "chunk_index": chunk_index,
            "messages": messages,
        }
    if operation in {"commit", "abort"}:
        fields = (
            _ARCHIVE_COMMIT_REQUEST_FIELDS
            if operation == "commit"
            else _ARCHIVE_ABORT_REQUEST_FIELDS
        )
        raw = _require_exact_mapping(payload, fields, f"archive {operation} request")
        deletion_id = raw["deletion_id"]
        if not isinstance(deletion_id, str) or not deletion_id.strip():
            raise ValueError("archive deletion_id must be non-blank")
        return {"operation": operation, "deletion_id": deletion_id}
    if operation != "archive":
        raise ValueError("deleted archive request has an unsupported operation")
    raw = _require_exact_mapping(payload, _ARCHIVE_REQUEST_FIELDS, "archive request")
    raw_messages = raw["messages"]
    if not isinstance(raw_messages, list):
        raise TypeError("archive request messages must be a JSON list")
    messages = tuple(_archive_message_from_wire(value) for value in raw_messages)
    deletion_id = raw["deletion_id"]
    deleted_at = raw["deleted_at"]
    if not isinstance(deletion_id, str) or not deletion_id.strip():
        raise ValueError("archive request deletion_id must be non-blank")
    if not isinstance(deleted_at, str):
        raise TypeError("archive request deleted_at must be an ISO timestamp")
    _, normalized_deleted_at = _validate_archive_request(
        messages,
        deletion_id=deletion_id,
        deleted_at=datetime.fromisoformat(deleted_at),
    )
    return {
        "operation": "archive",
        "messages": messages,
        "deletion_id": deletion_id,
        "deleted_at": normalized_deleted_at,
    }


def _archive_message_chunks(
    records: Sequence[ConversationMessage],
    *,
    deletion_id: str,
) -> Iterator[tuple[ConversationMessage, ...]]:
    """Yield complete-message chunks whose encoded IPC frames stay bounded."""

    current: list[ConversationMessage] = []
    for message in records:
        candidate = (*current, message)
        try:
            _encode_archive_request(
                {
                    "operation": "chunk",
                    "deletion_id": deletion_id,
                    "chunk_index": 0,
                    "messages": candidate,
                }
            )
        except ValueError as exc:
            if not current:
                raise DeletedConversationArchiveError(
                    "one deleted conversation message exceeds the archive frame limit"
                ) from exc
            yield tuple(current)
            current = [message]
            try:
                _encode_archive_request(
                    {
                        "operation": "chunk",
                        "deletion_id": deletion_id,
                        "chunk_index": 0,
                        "messages": current,
                    }
                )
            except ValueError as single_message_error:
                raise DeletedConversationArchiveError(
                    "one deleted conversation message exceeds the archive frame limit"
                ) from single_message_error
        else:
            current.append(message)
    if current:
        yield tuple(current)


def _encode_archive_response(*, ok: bool, message: str | None = None) -> bytes:
    if type(ok) is not bool:
        raise TypeError("archive response ok must be a boolean")
    if ok:
        if message is not None:
            raise ValueError("successful archive response cannot include a message")
        return _encode_archive_frame({"ok": True})
    if not isinstance(message, str) or not message:
        raise ValueError("failed archive response requires a message")
    return _encode_archive_frame({"ok": False, "message": message[:200]})


def _decode_archive_response(frame: bytes) -> dict[str, object]:
    payload = _decode_archive_frame(frame)
    if not isinstance(payload, dict) or "ok" not in payload:
        raise ValueError("deleted archive response has an invalid schema")
    ok = payload["ok"]
    if type(ok) is not bool:
        raise ValueError("deleted archive response ok must be a boolean")
    if ok:
        _require_exact_mapping(
            payload, _ARCHIVE_SUCCESS_RESPONSE_FIELDS, "successful archive response"
        )
        return {"ok": True}
    raw = _require_exact_mapping(
        payload, _ARCHIVE_FAILURE_RESPONSE_FIELDS, "failed archive response"
    )
    message = raw["message"]
    if not isinstance(message, str) or not message:
        raise ValueError("failed archive response message must be non-blank")
    return {"ok": False, "message": message[:200]}


def _send_archive_response(
    connection: Any, *, ok: bool, message: str | None = None
) -> None:
    connection.send_bytes(_encode_archive_response(ok=ok, message=message))


def _archive_endpoint_path(endpoint: str | Path) -> Path:
    if os.name == "nt":
        raise RuntimeError("Windows named-pipe endpoints do not have filesystem paths")
    return Path(endpoint).expanduser().resolve()


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
            endpoint_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP
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


def _archive_database_path(database: str | Path) -> Path:
    if str(database) == ":memory:":
        raise ValueError("deleted archive requires a durable database path")
    return Path(database).expanduser().resolve()


class SQLiteDeletedConversationArchiveWriter(DeletedConversationArchiveWriter):
    """Write-only client for an independently supervised archive service.

    The client receives only an IPC endpoint and authentication key.  It never
    receives the archive database path or a read-capable SQLite connection.
    Production must run :func:`serve_sqlite_deleted_conversation_archive`
    under the separate administrative storage identity, with the endpoint
    permissioned so ordinary Jarvis code can only submit archive writes.
    """

    def __init__(self, endpoint: str | Path, *, authkey: bytes) -> None:
        self._endpoint = str(endpoint)
        self._authkey = _validate_archive_authkey(authkey)
        try:
            self._connection = Client(
                self._endpoint,
                family=_archive_ipc_family(),
                authkey=self._authkey,
            )
        except (OSError, EOFError) as exc:
            raise DeletedConversationArchiveError(
                "deleted archive service is unavailable"
            ) from exc
        self._lock = RLock()
        self._closed = False
        try:
            response = self._receive_response(
                timeout_seconds=_ARCHIVE_RESPONSE_TIMEOUT_SECONDS
            )
        except DeletedConversationArchiveError as exc:
            self.close()
            raise DeletedConversationArchiveError(
                "deleted archive service did not become ready"
            ) from exc
        if not response["ok"]:
            message = str(response.get("message", "deleted archive service failed"))
            self.close()
            raise DeletedConversationArchiveError(message[:200])

    def archive(
        self,
        messages: Sequence[ConversationMessage],
        *,
        deletion_id: str,
        deleted_at: datetime,
        expected_count: int | None = None,
        expected_digest: str | None = None,
    ) -> None:
        records, normalized_deleted_at = _validate_archive_request(
            messages,
            deletion_id=deletion_id,
            deleted_at=deleted_at,
            expected_count=expected_count,
            expected_digest=expected_digest,
        )
        expected_count = len(records)
        expected_digest = expected_digest or _conversation_message_digest(records)
        with self._lock:
            try:
                self._request(
                    {
                        "operation": "begin",
                        "deletion_id": deletion_id,
                        "deleted_at": normalized_deleted_at,
                        "expected_count": expected_count,
                        "expected_digest": expected_digest,
                    },
                    timeout_seconds=_ARCHIVE_RESPONSE_TIMEOUT_SECONDS,
                )
                for chunk_index, chunk in enumerate(
                    _archive_message_chunks(records, deletion_id=deletion_id)
                ):
                    self._request(
                        {
                            "operation": "chunk",
                            "deletion_id": deletion_id,
                            "chunk_index": chunk_index,
                            "messages": chunk,
                        },
                        timeout_seconds=_ARCHIVE_RESPONSE_TIMEOUT_SECONDS,
                    )
                self._request(
                    {"operation": "commit", "deletion_id": deletion_id},
                    timeout_seconds=_ARCHIVE_RESPONSE_TIMEOUT_SECONDS,
                )
            except DeletedConversationArchiveError:
                try:
                    self._request(
                        {"operation": "abort", "deletion_id": deletion_id},
                        timeout_seconds=_ARCHIVE_CLOSE_TIMEOUT_SECONDS,
                    )
                except DeletedConversationArchiveError:
                    pass
                raise

    def _receive_response(self, *, timeout_seconds: float) -> dict[str, object]:
        deadline = monotonic() + timeout_seconds
        try:
            if not self._connection.poll(max(0.0, deadline - monotonic())):
                raise DeletedConversationArchiveError(
                    "deleted archive service response timed out"
                )
            return _decode_archive_response(
                self._connection.recv_bytes(_MAX_ARCHIVE_FRAME_BYTES)
            )
        except DeletedConversationArchiveError:
            self._close_connection()
            raise
        except (EOFError, OSError, TypeError, ValueError) as exc:
            self._close_connection()
            raise DeletedConversationArchiveError(
                "deleted archive service returned an invalid response"
            ) from exc

    def _request(self, request: dict[str, Any], *, timeout_seconds: float) -> None:
        if self._closed:
            raise DeletedConversationArchiveError(
                "deleted archive service is unavailable"
            )
        try:
            self._connection.send_bytes(_encode_archive_request(request))
            response = self._receive_response(timeout_seconds=timeout_seconds)
        except DeletedConversationArchiveError:
            raise
        except (EOFError, OSError, TypeError, ValueError) as exc:
            self._close_connection()
            raise DeletedConversationArchiveError(
                "deleted archive service is unavailable"
            ) from exc
        if not response["ok"]:
            message = str(response.get("message", "deleted archive transfer failed"))
            raise DeletedConversationArchiveError(message[:200])

    def _close_connection(self) -> None:
        if self._closed:
            return
        try:
            self._connection.close()
        except (OSError, ValueError):
            pass
        finally:
            self._closed = True

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._request(
                    {"operation": "close"},
                    timeout_seconds=_ARCHIVE_CLOSE_TIMEOUT_SECONDS,
                )
            except DeletedConversationArchiveError:
                pass
            finally:
                self._close_connection()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001 - finalizer must never raise
            return


def _insert_archive_records(
    archive_connection: sqlite3.Connection,
    records: Sequence[ConversationMessage],
    *,
    deletion_id: str,
    deleted_at: datetime,
) -> None:
    for message in records:
        values = _archive_values(
            message,
            deletion_id=deletion_id,
            deleted_at=deleted_at,
        )
        archive_connection.execute(
            """
            INSERT INTO deleted_messages(
                transport_session_id, working_session_id, message_id,
                event_id, chat_id, sender_id, text, occurred_at,
                direction, request_id, credential_like, deletion_id,
                deleted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(transport_session_id, message_id) DO NOTHING
            """,
            values,
        )
        existing = archive_connection.execute(
            """
            SELECT transport_session_id, working_session_id, message_id,
                   event_id, chat_id, sender_id, text, occurred_at,
                   direction, request_id, credential_like
            FROM deleted_messages
            WHERE transport_session_id = ? AND message_id = ?
            """,
            (message.transport_session_id, message.message_id),
        ).fetchone()
        if existing is None or tuple(existing) != _archive_content_values(message):
            raise DeletedConversationArchiveError(
                "deleted archive record does not match a prior transfer"
            )
        # The message body is immutable, but the deletion metadata belongs
        # to the successful live-state deletion attempt.  Updating it here
        # lets a fresh action adopt a prior archive after a live commit
        # failure without duplicating or rejecting the retained content.
        archive_connection.execute(
            """
            UPDATE deleted_messages
            SET deletion_id = ?, deleted_at = ?
            WHERE transport_session_id = ? AND message_id = ?
            """,
            (
                deletion_id,
                deleted_at.isoformat(),
                message.transport_session_id,
                message.message_id,
            ),
        )


def _archive_batch(
    archive_connection: sqlite3.Connection,
    records: Sequence[ConversationMessage],
    *,
    deletion_id: str,
    deleted_at: datetime,
) -> None:
    archive_connection.execute("BEGIN IMMEDIATE")
    try:
        _insert_archive_records(
            archive_connection,
            records,
            deletion_id=deletion_id,
            deleted_at=deleted_at,
        )
        archive_connection.commit()
    except Exception:
        archive_connection.rollback()
        raise


def _begin_archive_batch(
    archive_connection: sqlite3.Connection,
    *,
    deletion_id: str,
    deleted_at: datetime,
    expected_count: int,
    expected_digest: str,
) -> None:
    archive_connection.execute("BEGIN IMMEDIATE")
    try:
        _validate_batch_metadata(
            deletion_id=deletion_id,
            deleted_at=deleted_at,
            expected_count=expected_count,
            expected_digest=expected_digest,
        )
        archive_connection.execute(
            "DELETE FROM deleted_message_batch_items WHERE deletion_id = ?",
            (deletion_id,),
        )
        archive_connection.execute(
            "DELETE FROM deleted_message_batches WHERE deletion_id = ?",
            (deletion_id,),
        )
        archive_connection.execute(
            """
            INSERT INTO deleted_message_batches(
                deletion_id, expected_count, expected_digest, deleted_at
            ) VALUES (?, ?, ?, ?)
            """,
            (deletion_id, expected_count, expected_digest, deleted_at.isoformat()),
        )
        archive_connection.commit()
    except Exception:
        archive_connection.rollback()
        raise


def _append_archive_batch_chunk(
    archive_connection: sqlite3.Connection,
    records: Sequence[ConversationMessage],
    *,
    deletion_id: str,
    chunk_index: int,
) -> None:
    if not records:
        raise DeletedConversationArchiveError(
            "deleted archive chunks must contain at least one message"
        )
    archive_connection.execute("BEGIN IMMEDIATE")
    try:
        batch = archive_connection.execute(
            "SELECT deletion_id FROM deleted_message_batches WHERE deletion_id = ?",
            (deletion_id,),
        ).fetchone()
        if batch is None:
            raise DeletedConversationArchiveError(
                "deleted archive batch was not opened"
            )
        last_chunk = archive_connection.execute(
            """
            SELECT COALESCE(MAX(chunk_index), -1) AS chunk_index
            FROM deleted_message_batch_items
            WHERE deletion_id = ?
            """,
            (deletion_id,),
        ).fetchone()["chunk_index"]
        if chunk_index != last_chunk + 1:
            raise DeletedConversationArchiveError(
                "deleted archive chunks must arrive in order"
            )
        for item_index, message in enumerate(records):
            archive_connection.execute(
                """
                INSERT INTO deleted_message_batch_items(
                    deletion_id, chunk_index, item_index,
                    transport_session_id, working_session_id, message_id,
                    event_id, chat_id, sender_id, text, occurred_at,
                    direction, request_id, credential_like
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    deletion_id,
                    chunk_index,
                    item_index,
                    *(_archive_content_values(message)),
                ),
            )
        archive_connection.commit()
    except Exception:
        archive_connection.rollback()
        raise


def _commit_archive_batch(
    archive_connection: sqlite3.Connection,
    *,
    deletion_id: str,
) -> None:
    archive_connection.execute("BEGIN IMMEDIATE")
    try:
        batch = archive_connection.execute(
            """
            SELECT expected_count, expected_digest, deleted_at
            FROM deleted_message_batches
            WHERE deletion_id = ?
            """,
            (deletion_id,),
        ).fetchone()
        if batch is None:
            raise DeletedConversationArchiveError(
                "deleted archive batch was not opened"
            )
        rows = archive_connection.execute(
            """
            SELECT transport_session_id, working_session_id, message_id,
                   event_id, chat_id, sender_id, text, occurred_at,
                   direction, request_id, credential_like
            FROM deleted_message_batch_items
            WHERE deletion_id = ?
            ORDER BY occurred_at, transport_session_id, message_id
            """,
            (deletion_id,),
        ).fetchall()
        records = tuple(_archive_message_from_row(row) for row in rows)
        if len(records) != batch["expected_count"]:
            raise DeletedConversationArchiveError(
                "deleted archive batch count does not match its metadata"
            )
        if _conversation_message_digest(records) != batch["expected_digest"]:
            raise DeletedConversationArchiveError(
                "deleted archive batch digest does not match its metadata"
            )
        _insert_archive_records(
            archive_connection,
            records,
            deletion_id=deletion_id,
            deleted_at=datetime.fromisoformat(batch["deleted_at"]),
        )
        archive_connection.execute(
            "DELETE FROM deleted_message_batch_items WHERE deletion_id = ?",
            (deletion_id,),
        )
        archive_connection.execute(
            "DELETE FROM deleted_message_batches WHERE deletion_id = ?",
            (deletion_id,),
        )
        archive_connection.commit()
    except Exception:
        archive_connection.rollback()
        raise


def _abort_archive_batch(
    archive_connection: sqlite3.Connection,
    *,
    deletion_id: str,
) -> None:
    archive_connection.execute("BEGIN IMMEDIATE")
    try:
        archive_connection.execute(
            "DELETE FROM deleted_message_batch_items WHERE deletion_id = ?",
            (deletion_id,),
        )
        archive_connection.execute(
            "DELETE FROM deleted_message_batches WHERE deletion_id = ?",
            (deletion_id,),
        )
        archive_connection.commit()
    except Exception:
        archive_connection.rollback()
        raise


def _serve_archive_connection(connection: Any, database: str | Path) -> None:
    archive_connection: sqlite3.Connection | None = None
    try:
        archive_connection = sqlite3.connect(
            str(_archive_database_path(database)), timeout=30
        )
        archive_connection.row_factory = sqlite3.Row
        archive_connection.executescript(_ARCHIVE_SCHEMA)
        archive_connection.commit()
        _send_archive_response(connection, ok=True)
        while True:
            try:
                request = _decode_archive_request(
                    connection.recv_bytes(_MAX_ARCHIVE_FRAME_BYTES)
                )
            except (EOFError, OSError):
                return
            except (TypeError, ValueError) as exc:
                try:
                    _send_archive_response(connection, ok=False, message=str(exc))
                except (BrokenPipeError, EOFError, OSError, ValueError):
                    return
                continue
            operation = request.get("operation")
            if operation == "close":
                _send_archive_response(connection, ok=True)
                break
            try:
                if operation == "archive":
                    records = request["messages"]
                    deletion_id = request["deletion_id"]
                    normalized_deleted_at = request["deleted_at"]
                    if (
                        not isinstance(records, tuple)
                        or not isinstance(deletion_id, str)
                        or not isinstance(normalized_deleted_at, datetime)
                    ):
                        raise DeletedConversationArchiveError(
                            "deleted archive service received an invalid archive request"
                        )
                    _archive_batch(
                        archive_connection,
                        records,
                        deletion_id=deletion_id,
                        deleted_at=normalized_deleted_at,
                    )
                elif operation == "begin":
                    _begin_archive_batch(
                        archive_connection,
                        deletion_id=request["deletion_id"],
                        deleted_at=request["deleted_at"],
                        expected_count=request["expected_count"],
                        expected_digest=request["expected_digest"],
                    )
                elif operation == "chunk":
                    _append_archive_batch_chunk(
                        archive_connection,
                        request["messages"],
                        deletion_id=request["deletion_id"],
                        chunk_index=request["chunk_index"],
                    )
                elif operation == "commit":
                    _commit_archive_batch(
                        archive_connection,
                        deletion_id=request["deletion_id"],
                    )
                elif operation == "abort":
                    _abort_archive_batch(
                        archive_connection,
                        deletion_id=request["deletion_id"],
                    )
                else:
                    raise DeletedConversationArchiveError(
                        "deleted archive service received an unsupported operation"
                    )
                _send_archive_response(connection, ok=True)
            except Exception as exc:  # noqa: BLE001 - IPC reports typed failures
                try:
                    _send_archive_response(connection, ok=False, message=str(exc))
                except (BrokenPipeError, EOFError, OSError, ValueError):
                    return
    finally:
        if archive_connection is not None:
            archive_connection.close()
        try:
            connection.close()
        except OSError:
            pass


def serve_sqlite_deleted_conversation_archive(
    database: str | Path,
    endpoint: str | Path,
    *,
    authkey: bytes,
) -> None:
    """Serve the archive from a separately supervised administrative process.

    A deployment must invoke this entry point as the administrative storage
    identity, with filesystem permissions that let that identity traverse and
    read ``database`` while the Jarvis identity cannot.  The Jarvis process
    connects only to ``endpoint`` and receives no database path.
    """

    listener = _create_archive_listener(endpoint, authkey)
    try:
        connection = listener.accept()
        try:
            _serve_archive_connection(connection, database)
        finally:
            connection.close()
    finally:
        listener.close()
        _remove_archive_endpoint(endpoint)


def _archive_service_process_main(
    startup_connection: Any,
    database: str,
    endpoint: str,
    authkey: bytes,
) -> None:
    listener: Listener | None = None
    try:
        listener = _create_archive_listener(endpoint, authkey)
        _send_archive_response(startup_connection, ok=True)
        startup_connection.close()
        connection = listener.accept()
        try:
            _serve_archive_connection(connection, database)
        finally:
            connection.close()
    except Exception as exc:  # noqa: BLE001 - startup boundary reports typed errors
        try:
            _send_archive_response(startup_connection, ok=False, message=str(exc))
        except (BrokenPipeError, EOFError, OSError, ValueError):
            pass
    finally:
        if listener is not None:
            listener.close()
            _remove_archive_endpoint(endpoint)


class SQLiteDeletedConversationArchiveService:
    """Test/development launcher for the separately addressed archive service.

    Production should launch :func:`serve_sqlite_deleted_conversation_archive`
    from an administrative service manager so the service has a distinct OS
    identity.  This helper exists only to give local tests a real IPC client
    and a separate process without pretending that same-user process
    separation is a filesystem permission boundary.
    """

    def __init__(
        self,
        database: str | Path,
        *,
        endpoint: str | Path | None = None,
        authkey: bytes | None = None,
    ) -> None:
        _archive_database_path(database)
        self._owns_endpoint_directory = endpoint is None and os.name != "nt"
        if endpoint is None:
            if os.name == "nt":
                endpoint = rf"\\.\pipe\jarvis-deleted-{uuid.uuid4().hex}"
            else:
                directory = Path(tempfile.mkdtemp(prefix="jarvis-deleted-"))
                endpoint = directory / "writer.sock"
        self.endpoint = str(endpoint)
        self._endpoint_directory = (
            Path(self.endpoint).parent if self._owns_endpoint_directory else None
        )
        self._authkey = _validate_archive_authkey(authkey or os.urandom(32))
        startup_parent, startup_child = Pipe(duplex=True)
        self._process = Process(
            target=_archive_service_process_main,
            args=(startup_child, str(database), self.endpoint, self._authkey),
            daemon=True,
        )
        try:
            self._process.start()
        except Exception:
            startup_parent.close()
            startup_child.close()
            self._cleanup()
            raise
        finally:
            startup_child.close()
        try:
            if not startup_parent.poll(_ARCHIVE_RESPONSE_TIMEOUT_SECONDS):
                raise DeletedConversationArchiveError(
                    "deleted archive service did not start"
                )
            response = _decode_archive_response(
                startup_parent.recv_bytes(_MAX_ARCHIVE_FRAME_BYTES)
            )
        except DeletedConversationArchiveError:
            self.close()
            raise
        except (EOFError, OSError, TypeError, ValueError) as exc:
            self.close()
            raise DeletedConversationArchiveError(
                "deleted archive service did not start"
            ) from exc
        finally:
            startup_parent.close()
        if not response["ok"]:
            message = str(response.get("message", "deleted archive service failed"))
            self.close()
            raise DeletedConversationArchiveError(message[:200])
        try:
            self.writer = SQLiteDeletedConversationArchiveWriter(
                self.endpoint,
                authkey=self._authkey,
            )
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        writer = getattr(self, "writer", None)
        if writer is not None:
            writer.close()
            self.writer = None
        process = getattr(self, "_process", None)
        if process is not None:
            process.join(_ARCHIVE_CLOSE_TIMEOUT_SECONDS)
            if process.is_alive():
                process.terminate()
                process.join(_ARCHIVE_CLOSE_TIMEOUT_SECONDS)
        self._cleanup()

    def _cleanup(self) -> None:
        if self._endpoint_directory is not None:
            _remove_archive_endpoint(self.endpoint)
            try:
                self._endpoint_directory.rmdir()
            except OSError:
                pass


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

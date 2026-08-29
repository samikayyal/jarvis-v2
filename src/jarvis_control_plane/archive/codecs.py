"""Archive message, request, response, and bounded-chunk wire codecs."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..models import ConversationMessage
from .framing import (
    DEFAULT_MAX_ARCHIVE_FRAME_BYTES,
    decode_archive_frame,
    encode_archive_frame,
    require_exact_mapping,
)
from .records import validate_archive_request, validate_batch_metadata

ARCHIVE_MESSAGE_FIELDS = frozenset(
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
ARCHIVE_REQUEST_FIELDS = frozenset(
    {"operation", "messages", "deletion_id", "deleted_at"}
)
ARCHIVE_BEGIN_REQUEST_FIELDS = frozenset(
    {"operation", "deletion_id", "deleted_at", "expected_count", "expected_digest"}
)
ARCHIVE_CHUNK_REQUEST_FIELDS = frozenset(
    {"operation", "deletion_id", "chunk_index", "messages"}
)
ARCHIVE_COMMIT_REQUEST_FIELDS = frozenset({"operation", "deletion_id"})
ARCHIVE_ABORT_REQUEST_FIELDS = frozenset({"operation", "deletion_id"})
ARCHIVE_SUCCESS_RESPONSE_FIELDS = frozenset({"ok"})
ARCHIVE_FAILURE_RESPONSE_FIELDS = frozenset({"ok", "message"})


def archive_message_to_wire(message: ConversationMessage) -> dict[str, object]:
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


def archive_message_from_wire(value: object) -> ConversationMessage:
    raw = require_exact_mapping(value, ARCHIVE_MESSAGE_FIELDS, "archive message")
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


def encode_archive_request(
    request: dict[str, Any],
    *,
    max_frame_bytes: int = DEFAULT_MAX_ARCHIVE_FRAME_BYTES,
) -> bytes:
    operation = request.get("operation")
    if operation == "begin":
        require_exact_mapping(
            request, ARCHIVE_BEGIN_REQUEST_FIELDS, "archive begin request"
        )
        _, normalized_deleted_at, expected_count, expected_digest = (
            validate_batch_metadata(
                deletion_id=request["deletion_id"],
                deleted_at=request["deleted_at"],
                expected_count=request["expected_count"],
                expected_digest=request["expected_digest"],
            )
        )
        return encode_archive_frame(
            {
                "operation": "begin",
                "deletion_id": request["deletion_id"],
                "deleted_at": normalized_deleted_at.isoformat(),
                "expected_count": expected_count,
                "expected_digest": expected_digest,
            },
            max_frame_bytes=max_frame_bytes,
        )
    if operation == "chunk":
        require_exact_mapping(
            request, ARCHIVE_CHUNK_REQUEST_FIELDS, "archive chunk request"
        )
        chunk_index = request["chunk_index"]
        if isinstance(chunk_index, bool) or not isinstance(chunk_index, int):
            raise ValueError("archive chunk index must be a non-negative integer")
        if chunk_index < 0:
            raise ValueError("archive chunk index must be a non-negative integer")
        records, _ = validate_archive_request(
            request["messages"],
            deletion_id=request["deletion_id"],
            deleted_at=datetime.now(UTC),
        )
        return encode_archive_frame(
            {
                "operation": "chunk",
                "deletion_id": request["deletion_id"],
                "chunk_index": chunk_index,
                "messages": [archive_message_to_wire(message) for message in records],
            },
            max_frame_bytes=max_frame_bytes,
        )
    if operation in {"commit", "abort"}:
        fields = (
            ARCHIVE_COMMIT_REQUEST_FIELDS
            if operation == "commit"
            else ARCHIVE_ABORT_REQUEST_FIELDS
        )
        require_exact_mapping(request, fields, f"archive {operation} request")
        if (
            not isinstance(request["deletion_id"], str)
            or not request["deletion_id"].strip()
        ):
            raise ValueError("archive deletion_id must be non-blank")
        return encode_archive_frame(
            {"operation": operation, "deletion_id": request["deletion_id"]},
            max_frame_bytes=max_frame_bytes,
        )
    if operation != "archive":
        raise ValueError("deleted archive request has an unsupported operation")
    require_exact_mapping(request, ARCHIVE_REQUEST_FIELDS, "archive request")
    records, normalized_deleted_at = validate_archive_request(
        request["messages"],
        deletion_id=request["deletion_id"],
        deleted_at=request["deleted_at"],
    )
    return encode_archive_frame(
        {
            "operation": "archive",
            "messages": [archive_message_to_wire(message) for message in records],
            "deletion_id": request["deletion_id"],
            "deleted_at": normalized_deleted_at.isoformat(),
        },
        max_frame_bytes=max_frame_bytes,
    )


def decode_archive_request(
    frame: bytes,
    *,
    max_frame_bytes: int = DEFAULT_MAX_ARCHIVE_FRAME_BYTES,
) -> dict[str, object]:
    payload = decode_archive_frame(frame, max_frame_bytes=max_frame_bytes)
    if not isinstance(payload, dict):
        raise TypeError("deleted archive request must be a JSON object")
    operation = payload.get("operation")
    if operation == "begin":
        raw = require_exact_mapping(
            payload, ARCHIVE_BEGIN_REQUEST_FIELDS, "archive begin request"
        )
        deletion_id, deleted_at, expected_count, expected_digest = (
            validate_batch_metadata(
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
        raw = require_exact_mapping(
            payload, ARCHIVE_CHUNK_REQUEST_FIELDS, "archive chunk request"
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
        messages = tuple(archive_message_from_wire(value) for value in raw_messages)
        validate_archive_request(
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
            ARCHIVE_COMMIT_REQUEST_FIELDS
            if operation == "commit"
            else ARCHIVE_ABORT_REQUEST_FIELDS
        )
        raw = require_exact_mapping(payload, fields, f"archive {operation} request")
        deletion_id = raw["deletion_id"]
        if not isinstance(deletion_id, str) or not deletion_id.strip():
            raise ValueError("archive deletion_id must be non-blank")
        return {"operation": operation, "deletion_id": deletion_id}
    if operation != "archive":
        raise ValueError("deleted archive request has an unsupported operation")
    raw = require_exact_mapping(payload, ARCHIVE_REQUEST_FIELDS, "archive request")
    raw_messages = raw["messages"]
    if not isinstance(raw_messages, list):
        raise TypeError("archive request messages must be a JSON list")
    messages = tuple(archive_message_from_wire(value) for value in raw_messages)
    deletion_id = raw["deletion_id"]
    deleted_at = raw["deleted_at"]
    if not isinstance(deletion_id, str) or not deletion_id.strip():
        raise ValueError("archive request deletion_id must be non-blank")
    if not isinstance(deleted_at, str):
        raise TypeError("archive request deleted_at must be an ISO timestamp")
    _, normalized_deleted_at = validate_archive_request(
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


def archive_message_chunks(
    records: Sequence[ConversationMessage],
    *,
    deletion_id: str,
    max_frame_bytes: int = DEFAULT_MAX_ARCHIVE_FRAME_BYTES,
) -> Iterator[tuple[ConversationMessage, ...]]:
    """Yield complete-message chunks without encoding growing candidates."""

    current: list[ConversationMessage] = []
    chunk_index = 0
    empty_chunk_size = (
        len(
            encode_archive_frame(
                {
                    "operation": "chunk",
                    "deletion_id": deletion_id,
                    "chunk_index": chunk_index,
                    "messages": [],
                },
                max_frame_bytes=max_frame_bytes,
            )
        )
        - 2
    )
    current_size = empty_chunk_size
    for message in records:
        try:
            encoded_message_size = len(
                encode_archive_frame(
                    archive_message_to_wire(message),
                    max_frame_bytes=max_frame_bytes,
                )
            )
        except ValueError as exc:
            raise ValueError(
                "one deleted conversation message exceeds the archive frame limit"
            ) from exc
        candidate_size = current_size + encoded_message_size + (1 if current else 0)
        if candidate_size > max_frame_bytes:
            if not current:
                raise ValueError(
                    "one deleted conversation message exceeds the archive frame limit"
                )
            yield tuple(current)
            chunk_index += 1
            current = [message]
            current_size = (
                len(
                    encode_archive_frame(
                        {
                            "operation": "chunk",
                            "deletion_id": deletion_id,
                            "chunk_index": chunk_index,
                            "messages": [],
                        },
                        max_frame_bytes=max_frame_bytes,
                    )
                )
                - 2
            )
            if current_size + encoded_message_size > max_frame_bytes:
                raise ValueError(
                    "one deleted conversation message exceeds the archive frame limit"
                )
            current_size += encoded_message_size
            continue
        current.append(message)
        current_size = candidate_size
    if current:
        yield tuple(current)


def encode_archive_response(
    *,
    ok: bool,
    message: str | None = None,
    max_frame_bytes: int = DEFAULT_MAX_ARCHIVE_FRAME_BYTES,
) -> bytes:
    if type(ok) is not bool:
        raise TypeError("archive response ok must be a boolean")
    if ok:
        if message is not None:
            raise ValueError("successful archive response cannot include a message")
        return encode_archive_frame({"ok": True}, max_frame_bytes=max_frame_bytes)
    if not isinstance(message, str) or not message:
        raise ValueError("failed archive response requires a message")
    return encode_archive_frame(
        {"ok": False, "message": message[:200]},
        max_frame_bytes=max_frame_bytes,
    )


def decode_archive_response(
    frame: bytes,
    *,
    max_frame_bytes: int = DEFAULT_MAX_ARCHIVE_FRAME_BYTES,
) -> dict[str, object]:
    payload = decode_archive_frame(frame, max_frame_bytes=max_frame_bytes)
    if not isinstance(payload, dict) or "ok" not in payload:
        raise ValueError("deleted archive response has an invalid schema")
    ok = payload["ok"]
    if type(ok) is not bool:
        raise ValueError("deleted archive response ok must be a boolean")
    if ok:
        require_exact_mapping(
            payload, ARCHIVE_SUCCESS_RESPONSE_FIELDS, "successful archive response"
        )
        return {"ok": True}
    raw = require_exact_mapping(
        payload, ARCHIVE_FAILURE_RESPONSE_FIELDS, "failed archive response"
    )
    message = raw["message"]
    if not isinstance(message, str) or not message:
        raise ValueError("failed archive response message must be non-blank")
    return {"ok": False, "message": message[:200]}


def send_archive_response(
    connection: Any,
    *,
    ok: bool,
    message: str | None = None,
    max_frame_bytes: int = DEFAULT_MAX_ARCHIVE_FRAME_BYTES,
) -> None:
    connection.send_bytes(
        encode_archive_response(
            ok=ok,
            message=message,
            max_frame_bytes=max_frame_bytes,
        )
    )


@dataclass(frozen=True, slots=True)
class ArchiveWireCodec:
    """Bound codec used by a service connection or a core writer."""

    max_frame_bytes: int = DEFAULT_MAX_ARCHIVE_FRAME_BYTES

    def encode_request(self, request: dict[str, Any]) -> bytes:
        return encode_archive_request(
            request,
            max_frame_bytes=self.max_frame_bytes,
        )

    def decode_request(self, frame: bytes) -> dict[str, object]:
        return decode_archive_request(
            frame,
            max_frame_bytes=self.max_frame_bytes,
        )

    def encode_response(self, *, ok: bool, message: str | None = None) -> bytes:
        return encode_archive_response(
            ok=ok,
            message=message,
            max_frame_bytes=self.max_frame_bytes,
        )

    def decode_response(self, frame: bytes) -> dict[str, object]:
        return decode_archive_response(
            frame,
            max_frame_bytes=self.max_frame_bytes,
        )

    def message_chunks(
        self,
        records: Sequence[ConversationMessage],
        *,
        deletion_id: str,
    ) -> Iterator[tuple[ConversationMessage, ...]]:
        return archive_message_chunks(
            records,
            deletion_id=deletion_id,
            max_frame_bytes=self.max_frame_bytes,
        )


__all__ = [
    "ARCHIVE_ABORT_REQUEST_FIELDS",
    "ARCHIVE_BEGIN_REQUEST_FIELDS",
    "ARCHIVE_CHUNK_REQUEST_FIELDS",
    "ARCHIVE_COMMIT_REQUEST_FIELDS",
    "ARCHIVE_FAILURE_RESPONSE_FIELDS",
    "ARCHIVE_MESSAGE_FIELDS",
    "ARCHIVE_REQUEST_FIELDS",
    "ARCHIVE_SUCCESS_RESPONSE_FIELDS",
    "ArchiveWireCodec",
    "archive_message_chunks",
    "archive_message_from_wire",
    "archive_message_to_wire",
    "decode_archive_request",
    "decode_archive_response",
    "encode_archive_request",
    "encode_archive_response",
    "send_archive_response",
]

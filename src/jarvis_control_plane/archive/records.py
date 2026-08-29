"""Archive records, validation, and SQLite row/value conversions."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from ..models import (
    ConversationMessage,
    _conversation_message_digest,
    ensure_utc,
)


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


@dataclass(frozen=True, slots=True)
class StagedArchiveBatch:
    """Content accepted by the writer but not yet published to the archive."""

    messages: tuple[ConversationMessage, ...]
    deleted_at: datetime
    expected_count: int
    expected_digest: str


_StagedArchiveBatch = StagedArchiveBatch


def validate_archive_request(
    messages: Sequence[ConversationMessage],
    *,
    deletion_id: str,
    deleted_at: datetime,
    expected_count: int | None = None,
    expected_digest: str | None = None,
) -> tuple[tuple[ConversationMessage, ...], datetime]:
    """Validate and normalize one complete archive transfer request."""

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
        validate_expected_count(expected_count)
        if expected_count != len(records):
            raise ValueError("deleted archive message count does not match metadata")
    if expected_digest is not None:
        validate_expected_digest(expected_digest)
        if expected_digest != _conversation_message_digest(records):
            raise ValueError("deleted archive message digest does not match metadata")
    return records, ensure_utc(deleted_at)


_validate_archive_request = validate_archive_request


def validate_expected_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(
            "deleted archive expected count must be a non-negative integer"
        )
    return value


_validate_expected_count = validate_expected_count


def validate_expected_digest(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("deleted archive expected digest must be a SHA-256 hex digest")
    return value


_validate_expected_digest = validate_expected_digest


def validate_batch_metadata(
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
    count = validate_expected_count(expected_count)
    digest = validate_expected_digest(expected_digest)
    return deletion_id, ensure_utc(deleted_at), count, digest


_validate_batch_metadata = validate_batch_metadata


def archive_values(
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


_archive_values = archive_values


def archive_content_values(message: ConversationMessage) -> tuple[object, ...]:
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


_archive_content_values = archive_content_values


def archive_message_from_row(row: sqlite3.Row) -> ConversationMessage:
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


_archive_message_from_row = archive_message_from_row


def archive_record_from_row(row: sqlite3.Row) -> DeletedConversationArchiveRecord:
    return DeletedConversationArchiveRecord(
        message=archive_message_from_row(row),
        deletion_id=row["deletion_id"],
        deleted_at=datetime.fromisoformat(row["deleted_at"]),
    )


_archive_record_from_row = archive_record_from_row


__all__ = [
    "DeletedConversationArchiveRecord",
    "StagedArchiveBatch",
    "archive_content_values",
    "archive_message_from_row",
    "archive_record_from_row",
    "archive_values",
    "validate_archive_request",
    "validate_batch_metadata",
    "validate_expected_count",
    "validate_expected_digest",
]

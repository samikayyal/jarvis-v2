"""In-memory archive used by tests and local deterministic compositions."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from threading import RLock

from ..models import ConversationMessage, _conversation_message_digest
from ..ports import DeletedConversationArchiveError
from .records import (
    DeletedConversationArchiveRecord,
    StagedArchiveBatch,
    validate_archive_request,
)


class InMemoryDeletedConversationArchive:
    """Test-only write boundary with a separate administration read surface."""

    def __init__(
        self,
        *,
        request_validator: Callable[
            ..., tuple[tuple[ConversationMessage, ...], datetime]
        ]
        | None = None,
        message_digest: Callable[[tuple[ConversationMessage, ...]], str] | None = None,
    ) -> None:
        self._request_validator = request_validator or validate_archive_request
        self._message_digest = message_digest or _conversation_message_digest
        self._records: dict[tuple[str, str], DeletedConversationArchiveRecord] = {}
        self._staged_batches: dict[str, StagedArchiveBatch] = {}
        self._lock = RLock()

    def stage(
        self,
        messages: Sequence[ConversationMessage],
        *,
        deletion_id: str,
        deleted_at: datetime,
        expected_count: int | None = None,
        expected_digest: str | None = None,
    ) -> None:
        records, normalized_deleted_at = self._request_validator(
            messages,
            deletion_id=deletion_id,
            deleted_at=deleted_at,
            expected_count=expected_count,
            expected_digest=expected_digest,
        )
        normalized_expected_count = len(records)
        normalized_expected_digest = expected_digest or self._message_digest(records)
        with self._lock:
            self._staged_batches[deletion_id] = StagedArchiveBatch(
                messages=records,
                deleted_at=normalized_deleted_at,
                expected_count=normalized_expected_count,
                expected_digest=normalized_expected_digest,
            )

    def finalize(self, *, deletion_id: str) -> None:
        if not isinstance(deletion_id, str) or not deletion_id.strip():
            raise ValueError("deletion_id must be non-blank")
        with self._lock:
            batch = self._staged_batches.get(deletion_id)
            if batch is None:
                raise DeletedConversationArchiveError(
                    "deleted archive batch was not staged"
                )
            records: dict[tuple[str, str], DeletedConversationArchiveRecord] = {}
            for message in batch.messages:
                key = (message.transport_session_id, message.message_id)
                record = DeletedConversationArchiveRecord(
                    message=message,
                    deletion_id=deletion_id,
                    deleted_at=batch.deleted_at,
                )
                existing = self._records.get(key)
                if existing is not None and existing.message != message:
                    raise DeletedConversationArchiveError(
                        "deleted archive record does not match a prior transfer"
                    )
                records[key] = record
            self._records.update(records)
            del self._staged_batches[deletion_id]

    def abort(self, *, deletion_id: str) -> None:
        if not isinstance(deletion_id, str) or not deletion_id.strip():
            raise ValueError("deletion_id must be non-blank")
        with self._lock:
            self._staged_batches.pop(deletion_id, None)

    def archive(
        self,
        messages: Sequence[ConversationMessage],
        *,
        deletion_id: str,
        deleted_at: datetime,
        expected_count: int | None = None,
        expected_digest: str | None = None,
    ) -> None:
        self.stage(
            messages,
            deletion_id=deletion_id,
            deleted_at=deleted_at,
            expected_count=expected_count,
            expected_digest=expected_digest,
        )
        try:
            self.finalize(deletion_id=deletion_id)
        except DeletedConversationArchiveError:
            with self._lock:
                self._staged_batches.pop(deletion_id, None)
            raise

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
        with self._lock:
            self._staged_batches.clear()


__all__ = ["InMemoryDeletedConversationArchive"]

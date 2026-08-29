from __future__ import annotations

import os
import sqlite3
from datetime import (
    UTC,
    datetime,
)

from jarvis_control_plane import (
    AuditEvidence,
    AuditWriteError,
    ConversationMessage,
    DeletedConversationArchiveError,
    InMemoryAuditBoundary,
    InMemoryDeletedConversationArchive,
    InMemoryDurableStateStore,
    StateStoreError,
)

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

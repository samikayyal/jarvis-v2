"""Write-only SQLite archive client for the authenticated IPC endpoint."""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from datetime import datetime
from multiprocessing.connection import Client
from threading import RLock
from time import monotonic
from typing import Any

from ..models import ConversationMessage, _conversation_message_digest
from ..ports import DeletedConversationArchiveError, DeletedConversationArchiveWriter
from .records import validate_archive_request
from .wire import DEFAULT_MAX_ARCHIVE_FRAME_BYTES, ArchiveWireCodec

DEFAULT_ARCHIVE_RESPONSE_TIMEOUT_SECONDS = 30.0
DEFAULT_ARCHIVE_CLOSE_TIMEOUT_SECONDS = 2.0


def _default_ipc_family() -> str:
    return "AF_PIPE" if os.name == "nt" else "AF_UNIX"


class SQLiteDeletedConversationArchiveWriter(DeletedConversationArchiveWriter):
    """Write-only client for an independently supervised archive service."""

    def __init__(
        self,
        endpoint: str,
        *,
        authkey: bytes,
        codec: ArchiveWireCodec | None = None,
        response_timeout_seconds: float = DEFAULT_ARCHIVE_RESPONSE_TIMEOUT_SECONDS,
        close_timeout_seconds: float = DEFAULT_ARCHIVE_CLOSE_TIMEOUT_SECONDS,
        ipc_family: str | None = None,
    ) -> None:
        self._endpoint = str(endpoint)
        self._authkey = self._validate_authkey(authkey)
        self._codec = codec or ArchiveWireCodec(DEFAULT_MAX_ARCHIVE_FRAME_BYTES)
        self._response_timeout_seconds = response_timeout_seconds
        self._close_timeout_seconds = close_timeout_seconds
        self._ipc_family_name = ipc_family
        self._connection: Any | None = None
        self._active_deletion_id: str | None = None
        self._closed = False
        self._lock = RLock()
        self._connect()

    def _validate_authkey(self, authkey: bytes) -> bytes:
        if not isinstance(authkey, bytes) or not authkey:
            raise ValueError("deleted archive IPC authkey must be non-empty bytes")
        return authkey

    def _frame_limit(self) -> int:
        return self._codec.max_frame_bytes

    def _ipc_family(self) -> str:
        return self._ipc_family_name or _default_ipc_family()

    def _encode_request(self, request: dict[str, Any]) -> bytes:
        return self._codec.encode_request(request)

    def _decode_response(self, frame: bytes) -> dict[str, object]:
        return self._codec.decode_response(frame)

    def _message_chunks(
        self,
        records: Sequence[ConversationMessage],
        *,
        deletion_id: str,
    ) -> Iterator[tuple[ConversationMessage, ...]]:
        return self._codec.message_chunks(records, deletion_id=deletion_id)

    def _request_timeout(self) -> float:
        return self._response_timeout_seconds

    def _close_timeout(self) -> float:
        return self._close_timeout_seconds

    def _connect(self) -> None:
        if self._closed:
            raise DeletedConversationArchiveError(
                "deleted archive service is unavailable"
            )
        if self._connection is not None:
            return
        try:
            self._connection = Client(
                self._endpoint,
                family=self._ipc_family(),
                authkey=self._authkey,
            )
        except (OSError, EOFError) as exc:
            raise DeletedConversationArchiveError(
                "deleted archive service is unavailable"
            ) from exc
        try:
            response = self._receive_response(timeout_seconds=self._request_timeout())
        except DeletedConversationArchiveError as exc:
            self._close_connection()
            raise DeletedConversationArchiveError(
                "deleted archive service did not become ready"
            ) from exc
        if not response["ok"]:
            message = str(response.get("message", "deleted archive service failed"))
            self._close_connection()
            raise DeletedConversationArchiveError(message[:200])

    @staticmethod
    def _remaining_deadline(deadline: float) -> float:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise DeletedConversationArchiveError(
                "deleted archive transfer deadline exceeded"
            )
        return remaining

    def _request_until(
        self,
        request: dict[str, Any],
        *,
        deadline: float,
    ) -> None:
        self._request(
            request,
            timeout_seconds=self._remaining_deadline(deadline),
        )

    def _abort_locked(self, *, deletion_id: str) -> None:
        if self._active_deletion_id != deletion_id:
            return
        try:
            if self._connection is not None:
                self._request(
                    {"operation": "abort", "deletion_id": deletion_id},
                    timeout_seconds=self._close_timeout(),
                    reconnect=False,
                )
        finally:
            self._active_deletion_id = None

    def _stage_locked(
        self,
        records: tuple[ConversationMessage, ...],
        *,
        deletion_id: str,
        normalized_deleted_at: datetime,
        expected_count: int,
        expected_digest: str,
        deadline: float,
    ) -> None:
        if self._active_deletion_id is not None:
            raise DeletedConversationArchiveError(
                "deleted archive writer already has a staged batch"
            )
        try:
            self._request_until(
                {
                    "operation": "begin",
                    "deletion_id": deletion_id,
                    "deleted_at": normalized_deleted_at,
                    "expected_count": expected_count,
                    "expected_digest": expected_digest,
                },
                deadline=deadline,
            )
            self._active_deletion_id = deletion_id
            for chunk_index, chunk in enumerate(
                self._message_chunks(records, deletion_id=deletion_id)
            ):
                self._request_until(
                    {
                        "operation": "chunk",
                        "deletion_id": deletion_id,
                        "chunk_index": chunk_index,
                        "messages": chunk,
                    },
                    deadline=deadline,
                )
        except DeletedConversationArchiveError:
            try:
                self._abort_locked(deletion_id=deletion_id)
            except DeletedConversationArchiveError:
                pass
            raise

    def _validate_request(
        self,
        messages: Sequence[ConversationMessage],
        *,
        deletion_id: str,
        deleted_at: datetime,
        expected_count: int | None = None,
        expected_digest: str | None = None,
    ) -> tuple[tuple[ConversationMessage, ...], datetime]:
        return validate_archive_request(
            messages,
            deletion_id=deletion_id,
            deleted_at=deleted_at,
            expected_count=expected_count,
            expected_digest=expected_digest,
        )

    def _digest(self, records: tuple[ConversationMessage, ...]) -> str:
        return _conversation_message_digest(records)

    def stage(
        self,
        messages: Sequence[ConversationMessage],
        *,
        deletion_id: str,
        deleted_at: datetime,
        expected_count: int | None = None,
        expected_digest: str | None = None,
    ) -> None:
        records, normalized_deleted_at = self._validate_request(
            messages,
            deletion_id=deletion_id,
            deleted_at=deleted_at,
            expected_count=expected_count,
            expected_digest=expected_digest,
        )
        normalized_expected_count = len(records)
        normalized_expected_digest = expected_digest or self._digest(records)
        with self._lock:
            self._stage_locked(
                records,
                deletion_id=deletion_id,
                normalized_deleted_at=normalized_deleted_at,
                expected_count=normalized_expected_count,
                expected_digest=normalized_expected_digest,
                deadline=monotonic() + self._request_timeout(),
            )

    def finalize(self, *, deletion_id: str) -> None:
        if not isinstance(deletion_id, str) or not deletion_id.strip():
            raise ValueError("deletion_id must be non-blank")
        with self._lock:
            if self._active_deletion_id != deletion_id:
                raise DeletedConversationArchiveError(
                    "deleted archive batch was not staged"
                )
            self._request_until(
                {"operation": "commit", "deletion_id": deletion_id},
                deadline=monotonic() + self._request_timeout(),
            )
            self._active_deletion_id = None

    def abort(self, *, deletion_id: str) -> None:
        if not isinstance(deletion_id, str) or not deletion_id.strip():
            raise ValueError("deletion_id must be non-blank")
        with self._lock:
            self._abort_locked(deletion_id=deletion_id)

    def archive(
        self,
        messages: Sequence[ConversationMessage],
        *,
        deletion_id: str,
        deleted_at: datetime,
        expected_count: int | None = None,
        expected_digest: str | None = None,
    ) -> None:
        records, normalized_deleted_at = self._validate_request(
            messages,
            deletion_id=deletion_id,
            deleted_at=deleted_at,
            expected_count=expected_count,
            expected_digest=expected_digest,
        )
        normalized_expected_count = len(records)
        normalized_expected_digest = expected_digest or self._digest(records)
        with self._lock:
            deadline = monotonic() + self._request_timeout()
            self._stage_locked(
                records,
                deletion_id=deletion_id,
                normalized_deleted_at=normalized_deleted_at,
                expected_count=normalized_expected_count,
                expected_digest=normalized_expected_digest,
                deadline=deadline,
            )
            try:
                self._request_until(
                    {"operation": "commit", "deletion_id": deletion_id},
                    deadline=deadline,
                )
                self._active_deletion_id = None
            except DeletedConversationArchiveError:
                try:
                    self._abort_locked(deletion_id=deletion_id)
                except DeletedConversationArchiveError:
                    pass
                raise

    def _receive_response(self, *, timeout_seconds: float) -> dict[str, object]:
        deadline = monotonic() + timeout_seconds
        connection = self._connection
        if connection is None:
            raise DeletedConversationArchiveError(
                "deleted archive service is unavailable"
            )
        try:
            if not connection.poll(max(0.0, deadline - monotonic())):
                raise DeletedConversationArchiveError(
                    "deleted archive service response timed out"
                )
            return self._decode_response(connection.recv_bytes(self._frame_limit()))
        except DeletedConversationArchiveError:
            self._close_connection()
            raise
        except (EOFError, OSError, TypeError, ValueError) as exc:
            self._close_connection()
            raise DeletedConversationArchiveError(
                "deleted archive service returned an invalid response"
            ) from exc

    def _request(
        self,
        request: dict[str, Any],
        *,
        timeout_seconds: float,
        reconnect: bool = True,
    ) -> None:
        if self._closed:
            raise DeletedConversationArchiveError(
                "deleted archive service is unavailable"
            )
        if self._connection is None:
            if not reconnect:
                raise DeletedConversationArchiveError(
                    "deleted archive service is unavailable"
                )
            self._connect()
        connection = self._connection
        if connection is None:
            raise DeletedConversationArchiveError(
                "deleted archive service is unavailable"
            )
        try:
            connection.send_bytes(self._encode_request(request))
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
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        try:
            connection.close()
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._active_deletion_id = None
            self._close_connection()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001 - finalizer must never raise
            return


__all__ = [
    "DEFAULT_ARCHIVE_CLOSE_TIMEOUT_SECONDS",
    "DEFAULT_ARCHIVE_RESPONSE_TIMEOUT_SECONDS",
    "SQLiteDeletedConversationArchiveWriter",
]

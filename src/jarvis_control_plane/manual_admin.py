"""Manual-administration-only diagnostic trace inspection.

The normal control-plane package exposes only the append/reservation writer
contract.  This module is intentionally outside that public surface.  A
manual-administration composition root supplies a separate read source; the
writer/store object does not mint a read capability.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from .conversation_archive import (
    DeletedConversationArchiveRecord,
    _archive_record_from_row,
)
from .traces import DiagnosticTrace, _canonical_json, _DiagnosticTraceStoreBase


class _DiagnosticTraceReadSource(Protocol):
    def read_traces(
        self,
        *,
        trace_id: str | None = None,
        request_id: str | None = None,
        operation_type: str | None = None,
    ) -> tuple[DiagnosticTrace, ...]: ...

    def close(self) -> None: ...


class ManualDiagnosticTraceBoundary:
    """Read/export boundary reserved for a manual administrator."""

    def __init__(
        self,
        source: _DiagnosticTraceReadSource,
    ) -> None:
        self._source = source

    def list_traces(
        self,
        *,
        trace_id: str | None = None,
        request_id: str | None = None,
        operation_type: str | None = None,
    ) -> tuple[DiagnosticTrace, ...]:
        return self._source.read_traces(
            trace_id=trace_id,
            request_id=request_id,
            operation_type=operation_type,
        )

    def inspect(
        self,
        **filters: str | None,
    ) -> tuple[DiagnosticTrace, ...]:
        return self.list_traces(**filters)

    def export_json(self, **filters: str | None) -> bytes:
        return _canonical_json(
            [trace.to_mapping() for trace in self.list_traces(**filters)]
        )

    def export(self, **filters: str | None) -> bytes:
        return self.export_json(**filters)

    def close(self) -> None:
        self._source.close()


class ManualDeletedConversationArchiveBoundary:
    """Read/export boundary reserved for a manual administrator."""

    def __init__(self, source: _DeletedArchiveReadSource) -> None:
        self._source = source

    def list_records(self) -> tuple[DeletedConversationArchiveRecord, ...]:
        return self._source.read_records()

    def inspect(self) -> tuple[DeletedConversationArchiveRecord, ...]:
        return self.list_records()

    def close(self) -> None:
        self._source.close()


def _open_manual_trace_boundary(
    store: _DiagnosticTraceStoreBase,
) -> ManualDiagnosticTraceBoundary:
    """Open an in-process test boundary from the admin composition root."""

    return ManualDiagnosticTraceBoundary(_StoreReadSource(store))


def open_sqlite_manual_trace_boundary(
    database: str | Path,
) -> ManualDiagnosticTraceBoundary:
    """Open the durable read side independently for manual administration."""

    return ManualDiagnosticTraceBoundary(_SQLiteReadSource(database))


def open_sqlite_deleted_conversation_archive(
    database: str | Path,
) -> ManualDeletedConversationArchiveBoundary:
    """Open the deleted-conversation read side independently for administration."""

    return ManualDeletedConversationArchiveBoundary(
        _SQLiteDeletedArchiveReadSource(database)
    )


class _StoreReadSource:
    """Private adapter used only by the manual-admin composition root."""

    def __init__(self, store: _DiagnosticTraceStoreBase) -> None:
        self._store = store

    def read_traces(
        self,
        *,
        trace_id: str | None = None,
        request_id: str | None = None,
        operation_type: str | None = None,
    ) -> tuple[DiagnosticTrace, ...]:
        if self._store._service_admin_connection is not None:
            response = self._store._service_request(
                {
                    "operation": "read",
                    "trace_id": trace_id,
                    "request_id": request_id,
                    "operation_type": operation_type,
                }
            )
            return tuple(
                DiagnosticTrace.from_mapping(item) for item in response["traces"]
            )
        with self._store._lock:
            traces = self._store._read_persisted_traces()
        return tuple(
            trace
            for trace in traces
            if (trace_id is None or trace.trace_id == trace_id)
            and (request_id is None or trace.request_id == request_id)
            and (operation_type is None or trace.operation_type == operation_type)
        )

    def close(self) -> None:
        return None


class _SQLiteReadSource:
    """Read-only SQLite connection opened only by the admin composition root."""

    def __init__(self, database: str | Path) -> None:
        path = Path(database).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        uri = f"file:{quote(path.as_posix(), safe='/:')}?mode=ro"
        self._connection = sqlite3.connect(uri, uri=True)
        self._connection.row_factory = sqlite3.Row

    def read_traces(
        self,
        *,
        trace_id: str | None = None,
        request_id: str | None = None,
        operation_type: str | None = None,
    ) -> tuple[DiagnosticTrace, ...]:
        clauses: list[str] = []
        parameters: list[str] = []
        if trace_id is not None:
            clauses.append("trace_id = ?")
            parameters.append(trace_id)
        if request_id is not None:
            clauses.append("request_id = ?")
            parameters.append(request_id)
        if operation_type is not None:
            clauses.append("operation_type = ?")
            parameters.append(operation_type)
        query = "SELECT payload_json FROM diagnostic_traces"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY rowid"
        try:
            rows = self._connection.execute(query, parameters).fetchall()
            return tuple(
                DiagnosticTrace.from_mapping(json.loads(row["payload_json"]))
                for row in rows
            )
        except (
            sqlite3.Error,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise RuntimeError("manual diagnostic trace read failed") from exc

    def close(self) -> None:
        self._connection.close()


class _DeletedArchiveReadSource(Protocol):
    def read_records(self) -> tuple[DeletedConversationArchiveRecord, ...]: ...

    def close(self) -> None: ...


class _SQLiteDeletedArchiveReadSource:
    """Read-only SQLite connection opened only by the admin composition root."""

    def __init__(self, database: str | Path) -> None:
        path = Path(database).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        uri = f"file:{quote(path.as_posix(), safe='/:')}?mode=ro"
        self._connection = sqlite3.connect(uri, uri=True)
        self._connection.row_factory = sqlite3.Row

    def read_records(self) -> tuple[DeletedConversationArchiveRecord, ...]:
        try:
            rows = self._connection.execute(
                """
                SELECT transport_session_id, working_session_id, message_id,
                       event_id, chat_id, sender_id, text, occurred_at,
                       direction, request_id, credential_like, deletion_id,
                       deleted_at
                FROM deleted_messages
                ORDER BY deleted_at, transport_session_id, message_id
                """
            ).fetchall()
            return tuple(_archive_record_from_row(row) for row in rows)
        except (sqlite3.Error, KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("manual deleted-conversation read failed") from exc

    def close(self) -> None:
        self._connection.close()


__all__ = [
    "ManualDeletedConversationArchiveBoundary",
    "ManualDiagnosticTraceBoundary",
    "open_sqlite_deleted_conversation_archive",
    "open_sqlite_manual_trace_boundary",
]

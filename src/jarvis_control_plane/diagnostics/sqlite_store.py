"""SQLite-backed durable diagnostic trace store."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..ports import TraceCapacityError, TraceWriteError
from .capacity import _FileSystemTraceCapacityProvider, _TraceCapacityProvider
from .records import (
    DEFAULT_TRACE_RESERVATION_BYTES,
    MAX_TRACE_RESERVATION_BYTES,
    DiagnosticTrace,
    DiagnosticTraceLimits,
    _validate_non_negative_int,
)
from .store import _DiagnosticTraceStoreBase
from .values import _canonical_json


class SQLiteDiagnosticTraceStore(_DiagnosticTraceStoreBase):
    """SQLite-backed trace store with no expiry or automatic deletion."""

    def __init__(
        self,
        database: str | Path | sqlite3.Connection = ":memory:",
        *,
        capacity_bytes: int | None = None,
        reservation_bytes: int = DEFAULT_TRACE_RESERVATION_BYTES,
        hard_max_bytes: int = MAX_TRACE_RESERVATION_BYTES,
        capacity_provider: _TraceCapacityProvider | None = None,
        minimum_free_bytes: int = 0,
    ) -> None:
        self._database_location = str(database)
        self._owns_connection = not isinstance(database, sqlite3.Connection)
        self.connection = (
            database
            if isinstance(database, sqlite3.Connection)
            else sqlite3.connect(str(database), check_same_thread=False)
        )
        self.connection.row_factory = sqlite3.Row
        if (
            capacity_provider is None
            and not isinstance(database, sqlite3.Connection)
            and str(database) != ":memory:"
        ):
            capacity_provider = _FileSystemTraceCapacityProvider(
                database, minimum_free_bytes=minimum_free_bytes
            )
        self._minimum_free_bytes = _validate_non_negative_int(
            minimum_free_bytes, "minimum_free_bytes"
        )
        super().__init__(
            DiagnosticTraceLimits(
                reservation_bytes=reservation_bytes,
                hard_max_bytes=hard_max_bytes,
                capacity_bytes=capacity_bytes,
            ),
            capacity_provider=capacity_provider,
        )
        try:
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS diagnostic_traces (
                    trace_id TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    operation_type TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_bytes INTEGER NOT NULL
                )
                """
            )
            row = self.connection.execute(
                "SELECT COALESCE(SUM(payload_bytes), 0) AS retained FROM diagnostic_traces"
            ).fetchone()
            retained = int(row["retained"])
            if retained > self.limits.capacity_bytes:
                raise TraceCapacityError(
                    "retained diagnostic traces exceed configured capacity",
                    requested_bytes=0,
                    available_bytes=0,
                )
            self._retained_bytes = retained
            request_rows = self.connection.execute(
                """
                SELECT request_id, COALESCE(SUM(payload_bytes), 0) AS retained
                FROM diagnostic_traces
                GROUP BY request_id
                """
            ).fetchall()
            self._request_retained = {
                row["request_id"]: int(row["retained"]) for row in request_rows
            }
            if any(
                retained_bytes > self.limits.reservation_bytes
                for retained_bytes in self._request_retained.values()
            ):
                raise TraceCapacityError(
                    "retained traces exceed the configured per-request capacity",
                    requested_bytes=0,
                    available_bytes=0,
                )
            self._available_bytes -= retained
            self.connection.commit()
        except TraceCapacityError:
            raise
        except sqlite3.Error as exc:
            raise TraceWriteError(
                "could not initialize SQLite diagnostic traces"
            ) from exc

    def _writer_service_configuration(self) -> dict[str, Any]:
        if not self._owns_connection:
            raise ValueError(
                "a SQLite trace writer requires a database path, not a live connection"
            )
        physical_capacity = None
        if self._capacity_provider is not None and not isinstance(
            self._capacity_provider, _FileSystemTraceCapacityProvider
        ):
            physical_capacity = self._physical_available_locked()
        return {
            "kind": "sqlite",
            "database": self._database_location,
            "capacity_bytes": self.limits.capacity_bytes,
            "reservation_bytes": self.limits.reservation_bytes,
            "hard_max_bytes": self.limits.hard_max_bytes,
            "physical_capacity_bytes": physical_capacity,
            "minimum_free_bytes": self._minimum_free_bytes,
        }

    def _persist_trace(self, trace: DiagnosticTrace) -> None:
        try:
            payload_json = _canonical_json(trace.to_mapping()).decode("utf-8")
            self.connection.execute(
                """
                INSERT INTO diagnostic_traces(
                    trace_id, operation_id, request_id, operation_type,
                    started_at, completed_at, outcome, payload_json, payload_bytes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace.trace_id,
                    trace.operation_id,
                    trace.request_id,
                    trace.operation_type,
                    trace.started_at.isoformat(),
                    trace.completed_at.isoformat(),
                    trace.outcome,
                    payload_json,
                    trace.serialized_size_bytes,
                ),
            )
            self.connection.commit()
        except sqlite3.Error as exc:
            try:
                self.connection.rollback()
            except sqlite3.Error:
                pass
            raise TraceWriteError("could not append SQLite diagnostic trace") from exc

    def _read_persisted_traces(self) -> tuple[DiagnosticTrace, ...]:
        try:
            rows = self.connection.execute(
                "SELECT payload_json FROM diagnostic_traces ORDER BY rowid"
            ).fetchall()
        except sqlite3.Error as exc:
            raise TraceWriteError("could not read SQLite diagnostic traces") from exc
        try:
            return tuple(
                DiagnosticTrace.from_mapping(json.loads(row["payload_json"]))
                for row in rows
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TraceWriteError("stored diagnostic trace is malformed") from exc

    def close(self) -> None:
        self._close_writer_service()
        if self._owns_connection:
            self.connection.close()

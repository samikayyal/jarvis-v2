"""Write-side diagnostic trace stores and reservation accounting."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Mapping
from multiprocessing import Process
from typing import Any

from ..ports import DiagnosticTraceStore, TraceCapacityError, TraceWriteError
from ..writer_capability import close_writer_capability
from .capacity import _TraceCapacityProvider
from .records import (
    DEFAULT_TRACE_RESERVATION_BYTES,
    MAX_TRACE_RESERVATION_BYTES,
    DiagnosticTrace,
    DiagnosticTraceLimits,
    _validate_positive_int,
)
from .values import TraceReservation
from .writer import (
    _raise_trace_writer_error,
    _start_trace_writer_service,
)

_DEFAULT_START_TRACE_WRITER_SERVICE = _start_trace_writer_service


def _start_writer_service(
    configuration: Mapping[str, Any],
) -> tuple[DiagnosticTraceStore, str, Any, threading.RLock, Process]:
    """Resolve the legacy facade seam before starting a child writer."""

    # The original public composition root exposed this private factory from
    # ``traces`` and tests occasionally replace it.  Keep that seam live while
    # allowing the implementation to live in diagnostics.writer.
    from .. import traces

    local_factory = globals()["_start_trace_writer_service"]
    if local_factory is not _DEFAULT_START_TRACE_WRITER_SERVICE:
        return local_factory(configuration)
    return traces._start_trace_writer_service(configuration)


class _DiagnosticTraceStoreBase(DiagnosticTraceStore):
    """Shared reservation and manual-read mechanics for local stores."""

    def __init__(
        self,
        limits: DiagnosticTraceLimits,
        *,
        capacity_provider: _TraceCapacityProvider | None = None,
    ) -> None:
        self.limits = limits
        self._capacity_provider = capacity_provider
        self._owner = object()
        self._reservations: dict[str, TraceReservation] = {}
        self._request_reserved: dict[str, int] = {}
        self._request_retained: dict[str, int] = {}
        self._available_bytes = limits.capacity_bytes
        self._retained_bytes = 0
        self._lock = threading.RLock()
        self._service_writer_token: str | None = None
        self._service_admin_connection: Any | None = None
        self._service_lock = threading.RLock()
        self._service_process: Process | None = None

    def writer(self) -> DiagnosticTraceStore:
        """Return the write-only capability given to ordinary Jarvis code."""

        if self._service_writer_token is not None:
            raise RuntimeError("trace writer service is already open")
        client, writer_token, admin_connection, lock, process = _start_writer_service(
            self._writer_service_configuration()
        )
        self._service_writer_token = writer_token
        self._service_admin_connection = admin_connection
        self._service_lock = lock
        self._service_process = process
        return client

    def _writer_service_configuration(self) -> dict[str, Any]:
        raise NotImplementedError

    def _service_request(self, request: dict[str, Any]) -> dict[str, Any]:
        connection = self._service_admin_connection
        if connection is None:
            raise TraceWriteError("trace writer service is not open")
        with self._service_lock:
            try:
                connection.send(request)
                response = connection.recv()
            except (EOFError, OSError) as exc:
                raise TraceWriteError("trace writer service is unavailable") from exc
        if not response.get("ok", False):
            _raise_trace_writer_error(response["error"])
        return response

    def reserve(
        self,
        *,
        request_id: str,
        reservation_bytes: int | None = None,
    ) -> TraceReservation:
        if (
            not isinstance(request_id, str)
            or not request_id
            or request_id.strip() != request_id
        ):
            raise ValueError("request_id must be a non-empty canonical string")
        requested = (
            None
            if reservation_bytes is None
            else _validate_positive_int(reservation_bytes, "reservation_bytes")
        )
        requested_limit = (
            self.limits.reservation_bytes if requested is None else requested
        )
        if requested_limit > self.limits.hard_max_bytes:
            raise TraceCapacityError(
                "trace reservation exceeds the configured hard maximum",
                requested_bytes=requested_limit,
                available_bytes=self.available_bytes,
            )
        with self._lock:
            request_used = self._request_retained.get(request_id, 0)
            request_reserved = self._request_reserved.get(request_id, 0)
            request_remaining = (
                self.limits.reservation_bytes - request_used - request_reserved
            )
            if request_remaining <= 0:
                raise TraceCapacityError(
                    "per-request trace capacity is exhausted",
                    requested_bytes=requested_limit,
                    available_bytes=0,
                )
            if requested is None:
                requested = request_remaining
            elif requested > request_remaining:
                raise TraceCapacityError(
                    "per-request trace capacity is insufficient",
                    requested_bytes=requested,
                    available_bytes=request_remaining,
                )
            available = min(self._available_bytes, self._physical_available_locked())
            if requested > available:
                raise TraceCapacityError(
                    "trace capacity is insufficient for the operation",
                    requested_bytes=requested,
                    available_bytes=available,
                )
            reservation = TraceReservation(
                reservation_id=f"trace-reservation-{uuid.uuid4().hex}",
                request_id=request_id,
                reserved_bytes=requested,
                _owner=self._owner,
            )
            self._reserve_physical_locked(requested)
            self._reservations[reservation.reservation_id] = reservation
            self._request_reserved[request_id] = request_reserved + requested
            self._available_bytes -= requested
            return reservation

    def append(self, trace: DiagnosticTrace, reservation: TraceReservation) -> None:
        if not isinstance(trace, DiagnosticTrace):
            raise TypeError("trace must be a DiagnosticTrace")
        with self._lock:
            active = self._reservations.get(reservation.reservation_id)
            if active is not reservation or reservation._owner is not self._owner:
                raise TraceWriteError("trace reservation is not active in this store")
            try:
                size = trace.serialized_size_bytes
            except (TypeError, ValueError) as exc:
                self._release_locked(reservation)
                raise TraceWriteError("trace payload cannot be serialized") from exc
            if size > reservation.reserved_bytes:
                self._release_locked(reservation)
                raise TraceWriteError(
                    "complete trace exceeds its reserved capacity",
                    operation_started=True,
                )
            try:
                self._persist_trace(trace)
            except TraceWriteError:
                self._release_locked(reservation)
                raise
            except Exception as exc:  # pragma: no cover - adapter-specific guard
                self._release_locked(reservation)
                raise TraceWriteError("trace could not be persisted") from exc
            self._reservations.pop(reservation.reservation_id, None)
            request_reserved = self._request_reserved[reservation.request_id]
            if request_reserved == reservation.reserved_bytes:
                self._request_reserved.pop(reservation.request_id, None)
            else:
                self._request_reserved[reservation.request_id] = (
                    request_reserved - reservation.reserved_bytes
                )
            self._request_retained[reservation.request_id] = (
                self._request_retained.get(reservation.request_id, 0) + size
            )
            self._available_bytes += reservation.reserved_bytes - size
            self._retained_bytes += size
            self._release_physical_locked(reservation.reserved_bytes - size)

    def release(self, reservation: TraceReservation) -> None:
        with self._lock:
            active = self._reservations.get(reservation.reservation_id)
            if active is reservation and reservation._owner is self._owner:
                self._release_locked(reservation)

    def _release_locked(self, reservation: TraceReservation) -> None:
        self._reservations.pop(reservation.reservation_id, None)
        request_reserved = self._request_reserved[reservation.request_id]
        if request_reserved == reservation.reserved_bytes:
            self._request_reserved.pop(reservation.request_id, None)
        else:
            self._request_reserved[reservation.request_id] = (
                request_reserved - reservation.reserved_bytes
            )
        self._available_bytes += reservation.reserved_bytes
        self._release_physical_locked(reservation.reserved_bytes)

    def _physical_available_locked(self) -> int:
        if self._capacity_provider is None:
            return self._available_bytes
        try:
            available = self._capacity_provider.available_bytes()
        except Exception as exc:  # pragma: no cover - provider-specific guard
            raise TraceCapacityError(
                "trace capacity provider is unavailable",
                requested_bytes=0,
                available_bytes=0,
            ) from exc
        if isinstance(available, bool) or not isinstance(available, int):
            raise TraceCapacityError(
                "trace capacity provider returned invalid capacity",
                requested_bytes=0,
                available_bytes=0,
            )
        return max(0, available)

    def _reserve_physical_locked(self, amount: int) -> None:
        if self._capacity_provider is None:
            return
        try:
            self._capacity_provider.reserve(amount)
        except TraceCapacityError:
            raise
        except Exception as exc:  # pragma: no cover - provider-specific guard
            raise TraceCapacityError(
                "trace capacity provider could not reserve storage",
                requested_bytes=amount,
                available_bytes=0,
            ) from exc

    def _release_physical_locked(self, amount: int) -> None:
        if self._capacity_provider is None or amount <= 0:
            return
        self._capacity_provider.release(amount)

    @property
    def available_bytes(self) -> int:
        if self._service_admin_connection is not None:
            return int(self._service_request({"operation": "stats"})["available_bytes"])
        with self._lock:
            return min(self._available_bytes, self._physical_available_locked())

    @property
    def reserved_bytes(self) -> int:
        if self._service_admin_connection is not None:
            return int(self._service_request({"operation": "stats"})["reserved_bytes"])
        with self._lock:
            return sum(item.reserved_bytes for item in self._reservations.values())

    @property
    def retained_bytes(self) -> int:
        if self._service_admin_connection is not None:
            return int(self._service_request({"operation": "stats"})["retained_bytes"])
        with self._lock:
            return self._retained_bytes

    def request_retained_bytes(self, request_id: str) -> int:
        """Return safe size metadata without exposing trace payloads."""

        if self._service_admin_connection is not None:
            response = self._service_request(
                {"operation": "stats", "request_ids": [request_id]}
            )
            return int(response["request_retained_bytes"].get(request_id, 0))
        with self._lock:
            return self._request_retained.get(request_id, 0)

    def _close_writer_service(self) -> None:
        admin_connection = self._service_admin_connection
        writer_token = self._service_writer_token
        if admin_connection is None and writer_token is None:
            return
        process = self._service_process
        if process is not None and not process.is_alive():
            if admin_connection is not None:
                try:
                    admin_connection.close()
                except OSError:
                    pass
            self._service_writer_token = None
            self._service_admin_connection = None
            self._service_process = None
            return
        if writer_token is not None:
            close_writer_capability(writer_token)
        with self._service_lock:
            try:
                if admin_connection is not None:
                    admin_connection.send({"operation": "admin_close"})
            except OSError:
                pass
            if admin_connection is not None:
                try:
                    admin_connection.close()
                except OSError:
                    pass
        if process is not None:
            process.join(timeout=1)
            if process.is_alive():
                process.terminate()
                process.join(timeout=1)
        self._service_writer_token = None
        self._service_admin_connection = None
        self._service_process = None

    def _persist_trace(self, trace: DiagnosticTrace) -> None:
        raise NotImplementedError

    def _read_persisted_traces(self) -> tuple[DiagnosticTrace, ...]:
        raise NotImplementedError


class InMemoryDiagnosticTraceStore(_DiagnosticTraceStoreBase):
    """Controlled full-payload trace store used by unit tests."""

    def __init__(
        self,
        *,
        capacity_bytes: int | None = None,
        reservation_bytes: int = DEFAULT_TRACE_RESERVATION_BYTES,
        hard_max_bytes: int = MAX_TRACE_RESERVATION_BYTES,
    ) -> None:
        super().__init__(
            DiagnosticTraceLimits(
                reservation_bytes=reservation_bytes,
                hard_max_bytes=hard_max_bytes,
                capacity_bytes=capacity_bytes,
            )
        )
        self._traces: list[DiagnosticTrace] = []

    def _writer_service_configuration(self) -> dict[str, Any]:
        return {
            "kind": "memory",
            "capacity_bytes": self.limits.capacity_bytes,
            "reservation_bytes": self.limits.reservation_bytes,
            "hard_max_bytes": self.limits.hard_max_bytes,
        }

    def _persist_trace(self, trace: DiagnosticTrace) -> None:
        self._traces.append(trace)

    def _read_persisted_traces(self) -> tuple[DiagnosticTrace, ...]:
        return tuple(self._traces)


def __getattr__(name: str) -> Any:
    """Lazily retain the old diagnostics.store SQLite import."""

    if name == "SQLiteDiagnosticTraceStore":
        from .sqlite_store import SQLiteDiagnosticTraceStore

        return SQLiteDiagnosticTraceStore
    raise AttributeError(name)

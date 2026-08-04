"""Full-payload diagnostic traces and their manual-administration boundary.

Diagnostic traces are deliberately separate from :mod:`jarvis_control_plane`
audit evidence.  Audit evidence is bounded and redacted; a diagnostic trace is
the complete payload of one model, Codex, connector, or worker operation.  The
store exposes only reservation and append operations to the control plane.  A
separate ``ManualDiagnosticTraceBoundary`` is the only supported read/export
surface.
"""

from __future__ import annotations

import base64
import json
import shutil
import sqlite3
import tempfile
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, is_dataclass
from dataclasses import fields as dataclass_fields
from datetime import datetime
from enum import Enum
from multiprocessing import Pipe, Process
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, TypeVar

from .models import ensure_utc
from .ports import (
    Clock,
    DiagnosticTraceError,
    DiagnosticTraceStore,
    IdGenerator,
    TraceCapacityError,
    TraceWriteError,
)
from .trace_types import TraceReservation
from .writer_capability import TraceWriterCapability, close_writer_capability

DEFAULT_TRACE_RESERVATION_BYTES = 16 * 1024 * 1024
MAX_TRACE_RESERVATION_BYTES = 64 * 1024 * 1024

_T = TypeVar("_T")


def _validate_positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _validate_non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _trace_value(value: Any) -> Any:
    """Convert typed operation values to an immutable JSON-compatible shape.

    This conversion intentionally performs no redaction.  In particular, a
    string containing a password, token, or private key is copied verbatim.
    Bytes are represented losslessly as tagged base64 values so connector and
    worker payloads do not need to be coerced through an unsafe ``repr``.
    """

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return {
            "__type__": "datetime",
            "value": ensure_utc(value).isoformat(),
        }
    if isinstance(value, bytes):
        return {
            "__type__": "bytes",
            "base64": base64.b64encode(value).decode("ascii"),
        }
    if isinstance(value, Enum):
        return {
            "__type__": "enum",
            "class": f"{type(value).__module__}.{type(value).__qualname__}",
            "name": value.name,
            "value": _trace_value(value.value),
        }
    if isinstance(value, Mapping):
        return {
            "__type__": "mapping",
            "items": [
                {"key": _trace_value(key), "value": _trace_value(item)}
                for key, item in value.items()
            ],
        }
    if is_dataclass(value) and not isinstance(value, type):
        value_type = type(value)
        return {
            "__type__": "dataclass",
            "class": f"{value_type.__module__}.{value_type.__qualname__}",
            "fields": _trace_value(
                {
                    item.name: getattr(value, item.name)
                    for item in dataclass_fields(value)
                }
            ),
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        values = [_trace_value(item) for item in value]
        if isinstance(value, list):
            return {"__type__": "list", "items": values}
        if isinstance(value, tuple):
            return {"__type__": "tuple", "items": values}
        if isinstance(value, (set, frozenset)):
            try:
                values = sorted(values, key=_canonical_json)
            except (TypeError, ValueError, RecursionError) as exc:
                raise TypeError("trace set contains an unsupported value") from exc
            return {
                "__type__": "frozenset" if isinstance(value, frozenset) else "set",
                "items": values,
            }
        return values
    if isinstance(value, BaseException):
        return {
            "__type__": "exception",
            "class": f"{type(value).__module__}.{type(value).__qualname__}",
            "message": str(value),
            "args": _trace_value(value.args),
            "repr": repr(value),
        }
    if isinstance(value, Path):
        return {"__type__": "path", "value": str(value)}
    raise TypeError(
        "trace payload contains unsupported value type "
        f"{type(value).__module__}.{type(value).__qualname__}"
    )


def _freeze_trace_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_trace_value(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_trace_value(item) for item in value)
    return value


def _thaw_trace_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_trace_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_trace_value(item) for item in value]
    return value


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class DiagnosticTraceLimits:
    """Configured full-trace reservation and store capacity.

    ``reservation_bytes`` is the conservative per-operation reservation.  It
    may be lowered for a controlled environment but may not exceed the V1 hard
    maximum.  ``capacity_bytes`` defaults to four reservations so a normal
    request can retain several sequential operation spans.  It is intentionally
    not relieved by deleting old traces.
    """

    reservation_bytes: int = DEFAULT_TRACE_RESERVATION_BYTES
    hard_max_bytes: int = MAX_TRACE_RESERVATION_BYTES
    capacity_bytes: int | None = None

    def __post_init__(self) -> None:
        reservation = _validate_positive_int(
            self.reservation_bytes, "reservation_bytes"
        )
        hard_max = _validate_positive_int(self.hard_max_bytes, "hard_max_bytes")
        if reservation > hard_max:
            raise ValueError("reservation_bytes cannot exceed hard_max_bytes")
        if hard_max > MAX_TRACE_RESERVATION_BYTES:
            raise ValueError("hard_max_bytes cannot exceed the V1 hard maximum")
        capacity = self.capacity_bytes
        if capacity is None:
            capacity = reservation * 4
        _validate_positive_int(capacity, "capacity_bytes")
        object.__setattr__(self, "capacity_bytes", capacity)


@dataclass(frozen=True, slots=True)
class DiagnosticTrace:
    """One complete, permanent operation trace.

    The payload always contains ``input``, ``output``, ``arguments``,
    ``result``, ``error``, and ``telemetry`` keys.  No field is redacted or
    dropped based on its name or apparent sensitivity.
    """

    trace_id: str
    operation_id: str
    request_id: str
    operation_type: str
    started_at: datetime
    completed_at: datetime
    outcome: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        for name in (
            "trace_id",
            "operation_id",
            "request_id",
            "operation_type",
            "outcome",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value.strip() != value:
                raise ValueError(f"{name} must be a non-empty canonical string")
        if not isinstance(self.payload, Mapping):
            raise TypeError("trace payload must be a mapping")
        required_payload = {
            "input",
            "output",
            "arguments",
            "result",
            "error",
            "telemetry",
        }
        if not required_payload.issubset(self.payload):
            raise ValueError("trace payload is missing a complete operation field")
        object.__setattr__(self, "started_at", ensure_utc(self.started_at))
        object.__setattr__(self, "completed_at", ensure_utc(self.completed_at))
        object.__setattr__(self, "payload", _freeze_trace_value(self.payload))

    @property
    def span_type(self) -> str:
        """Alias matching the domain glossary's span terminology."""

        return self.operation_type

    @property
    def input_payload(self) -> Any:
        return self.payload.get("input")

    @property
    def output_payload(self) -> Any:
        return self.payload.get("output")

    @property
    def arguments(self) -> Any:
        return self.payload.get("arguments")

    @property
    def result(self) -> Any:
        return self.payload.get("result")

    @property
    def error(self) -> Any:
        return self.payload.get("error")

    @property
    def telemetry(self) -> Any:
        return self.payload.get("telemetry")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "operation_id": self.operation_id,
            "request_id": self.request_id,
            "operation_type": self.operation_type,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "outcome": self.outcome,
            "payload": _thaw_trace_value(self.payload),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> DiagnosticTrace:
        if not isinstance(value, Mapping):
            raise TypeError("trace record must be a mapping")
        return cls(
            trace_id=value["trace_id"],
            operation_id=value["operation_id"],
            request_id=value["request_id"],
            operation_type=value["operation_type"],
            started_at=datetime.fromisoformat(value["started_at"]),
            completed_at=datetime.fromisoformat(value["completed_at"]),
            outcome=value["outcome"],
            payload=value["payload"],
        )

    @property
    def serialized_size_bytes(self) -> int:
        return len(_canonical_json(self.to_mapping()))


def _trace_writer_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, TraceCapacityError):
        return {
            "kind": "capacity",
            "message": str(exc),
            "requested_bytes": exc.requested_bytes,
            "available_bytes": exc.available_bytes,
        }
    if isinstance(exc, TraceWriteError):
        return {
            "kind": "write",
            "message": str(exc),
            "operation_started": exc.operation_started,
        }
    if isinstance(exc, ValueError):
        return {"kind": "value", "message": str(exc)}
    return {"kind": "write", "message": str(exc) or "trace writer failed"}


def _raise_trace_writer_error(error: Mapping[str, Any]) -> None:
    kind = error.get("kind")
    message = str(error.get("message") or "trace writer failed")
    if kind == "capacity":
        raise TraceCapacityError(
            message,
            requested_bytes=int(error.get("requested_bytes", 0)),
            available_bytes=int(error.get("available_bytes", 0)),
        )
    if kind == "value":
        raise ValueError(message)
    raise TraceWriteError(
        message,
        operation_started=bool(error.get("operation_started", False)),
    )


def _trace_writer_mailbox(capability_id: str) -> Path:
    return Path(tempfile.gettempdir()) / f"jarvis-trace-{capability_id}"


def _trace_writer_process_main(
    admin_connection: Any,
    startup_connection: Any,
    configuration: Mapping[str, Any],
    capability_id: str,
) -> None:
    """Serve trace writes in a separate process with an isolated store."""

    if configuration["kind"] == "memory":
        store: DiagnosticTraceStore = InMemoryDiagnosticTraceStore(
            capacity_bytes=configuration["capacity_bytes"],
            reservation_bytes=configuration["reservation_bytes"],
            hard_max_bytes=configuration["hard_max_bytes"],
        )
    elif configuration["kind"] == "sqlite":
        physical_capacity = configuration.get("physical_capacity_bytes")
        store = SQLiteDiagnosticTraceStore(
            configuration["database"],
            capacity_bytes=configuration["capacity_bytes"],
            reservation_bytes=configuration["reservation_bytes"],
            hard_max_bytes=configuration["hard_max_bytes"],
            capacity_provider=(
                _StaticTraceCapacityProvider(physical_capacity)
                if physical_capacity is not None
                else None
            ),
        )
    else:
        raise RuntimeError("unknown trace writer store kind")

    writer_mailbox = _trace_writer_mailbox(capability_id)
    writer_mailbox.mkdir(parents=True, exist_ok=True)
    startup_connection.send({"ok": True})
    startup_connection.close()

    reservations: dict[str, TraceReservation] = {}
    state_lock = threading.RLock()
    stop_event = threading.Event()

    def handle_writer_request(request: Mapping[str, Any]) -> dict[str, Any]:
        operation = request.get("operation")
        with state_lock:
            if operation == "close":
                for reservation in reservations.values():
                    store.release(reservation)
                reservations.clear()
                return {"ok": True}
            try:
                if operation == "reserve":
                    reservation = store.reserve(
                        request_id=request["request_id"],
                        reservation_bytes=request.get("reservation_bytes"),
                    )
                    reservations[reservation.reservation_id] = reservation
                    return {
                        "ok": True,
                        "reservation_id": reservation.reservation_id,
                        "request_id": reservation.request_id,
                        "reserved_bytes": reservation.reserved_bytes,
                    }
                if operation == "append":
                    reservation_id = request["reservation_id"]
                    reservation = reservations.get(reservation_id)
                    if reservation is None:
                        raise TraceWriteError("trace reservation is not active")
                    store.append(
                        DiagnosticTrace.from_mapping(request["trace"]),
                        reservation,
                    )
                    reservations.pop(reservation_id, None)
                    return {"ok": True}
                if operation == "release":
                    reservation = reservations.pop(request["reservation_id"], None)
                    if reservation is not None:
                        store.release(reservation)
                    return {"ok": True}
                raise TraceWriteError(
                    "trace content is available only on the admin channel"
                )
            except Exception as exc:  # noqa: BLE001 - IPC boundary must report adapter failures
                if operation == "append":
                    reservation = reservations.pop(request.get("reservation_id"), None)
                    if reservation is not None:
                        store.release(reservation)
                return {"ok": False, "error": _trace_writer_error(exc)}

    def process_writer_requests() -> bool:
        processed = False
        for request_path in sorted(writer_mailbox.glob("*.request")):
            if request_path.name.startswith("."):
                continue
            processed = True
            try:
                request = json.loads(request_path.read_text(encoding="utf-8"))
                operation = request_path.name.split("-", 1)[0]
                if operation not in {"append", "close", "release", "reserve"}:
                    request_path.unlink(missing_ok=True)
                    continue
                request["operation"] = operation
                ipc_id = request["ipc_id"]
            except (KeyError, OSError, TypeError, ValueError):
                request_path.unlink(missing_ok=True)
                continue
            request_path.unlink(missing_ok=True)
            response = handle_writer_request(request)
            response_path = writer_mailbox / f"{ipc_id}.response"
            temporary_path = writer_mailbox / f".{ipc_id}.response"
            temporary_path.write_text(
                json.dumps(response, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary_path.replace(response_path)
            if request.get("operation") == "close":
                stop_event.set()
        return processed

    admin_open = True
    try:
        while not stop_event.is_set():
            processed = process_writer_requests()
            if stop_event.is_set():
                continue
            if not admin_open:
                stop_event.wait(0.1)
                continue
            try:
                if not admin_connection.poll(0.1):
                    if not processed:
                        stop_event.wait(0.01)
                    continue
                request = admin_connection.recv()
            except (EOFError, OSError):
                admin_open = False
                continue
            operation = request.get("operation")
            if operation == "admin_close":
                return
            with state_lock:
                try:
                    if operation == "read":
                        traces = tuple(
                            trace
                            for trace in store._read_persisted_traces()  # type: ignore[attr-defined]
                            if (
                                request.get("trace_id") is None
                                or trace.trace_id == request["trace_id"]
                            )
                            and (
                                request.get("request_id") is None
                                or trace.request_id == request["request_id"]
                            )
                            and (
                                request.get("operation_type") is None
                                or trace.operation_type == request["operation_type"]
                            )
                        )
                        response = {
                            "ok": True,
                            "traces": [trace.to_mapping() for trace in traces],
                        }
                    elif operation == "stats":
                        response = {
                            "ok": True,
                            "available_bytes": store.available_bytes,
                            "retained_bytes": store.retained_bytes,
                            "reserved_bytes": store.reserved_bytes,
                            "request_retained_bytes": {
                                request_id: store.request_retained_bytes(request_id)
                                for request_id in request.get("request_ids", [])
                            },
                        }
                    else:
                        raise TraceWriteError("unknown trace administration operation")
                except Exception as exc:  # noqa: BLE001 - IPC boundary must report adapter failures
                    response = {"ok": False, "error": _trace_writer_error(exc)}
            try:
                admin_connection.send(response)
            except (BrokenPipeError, EOFError, OSError):
                return
    finally:
        stop_event.set()
        with state_lock:
            for reservation in reservations.values():
                store.release(reservation)
            reservations.clear()
        try:
            admin_connection.close()
        except OSError:
            pass


def _start_trace_writer_service(
    configuration: Mapping[str, Any],
) -> tuple[DiagnosticTraceStore, str, Any, threading.RLock, Process]:
    capability_id = f"trace-writer-{uuid.uuid4().hex}"
    startup_server_connection, startup_client_connection = Pipe(duplex=True)
    admin_server_connection, admin_client_connection = Pipe(duplex=True)
    process = Process(
        target=_trace_writer_process_main,
        args=(
            admin_server_connection,
            startup_server_connection,
            dict(configuration),
            capability_id,
        ),
        daemon=True,
    )
    try:
        process.start()
    except Exception:
        startup_server_connection.close()
        startup_client_connection.close()
        admin_server_connection.close()
        admin_client_connection.close()
        raise
    startup_server_connection.close()
    admin_server_connection.close()
    try:
        startup_response = startup_client_connection.recv()
    except (EOFError, OSError) as exc:
        startup_client_connection.close()
        admin_client_connection.close()
        process.join(timeout=1)
        raise TraceWriteError("diagnostic trace writer failed to start") from exc
    finally:
        startup_client_connection.close()
    if not startup_response.get("ok", False):
        admin_client_connection.close()
        process.join(timeout=1)
        raise TraceWriteError("diagnostic trace writer failed to start")
    return (
        TraceWriterCapability(capability_id),
        capability_id,
        admin_client_connection,
        threading.RLock(),
        process,
    )


class _TraceCapacityProvider(Protocol):
    """Storage-side capacity reservation used by durable trace stores."""

    def available_bytes(self) -> int: ...

    def reserve(self, amount: int) -> None: ...

    def release(self, amount: int) -> None: ...


class _FileSystemTraceCapacityProvider:
    """Conservatively account for free space and concurrent local reservations."""

    def __init__(self, location: str | Path) -> None:
        location_path = Path(location)
        self._directory = (
            Path.cwd()
            if str(location_path) == ":memory:"
            else location_path.expanduser().resolve().parent
        )
        self._reserved = 0
        self._lock = threading.RLock()

    def available_bytes(self) -> int:
        with self._lock:
            try:
                free = shutil.disk_usage(self._directory).free
            except OSError:
                return 0
            return max(0, free - self._reserved)

    def reserve(self, amount: int) -> None:
        with self._lock:
            available = self.available_bytes()
            if amount > available:
                raise TraceCapacityError(
                    "filesystem trace capacity is insufficient",
                    requested_bytes=amount,
                    available_bytes=available,
                )
            self._reserved += amount

    def release(self, amount: int) -> None:
        with self._lock:
            self._reserved = max(0, self._reserved - amount)


class _StaticTraceCapacityProvider:
    """Serializable capacity snapshot used by a child trace-writer process."""

    def __init__(self, available: int) -> None:
        self._available = available

    def available_bytes(self) -> int:
        return self._available

    def reserve(self, amount: int) -> None:
        if amount > self._available:
            raise TraceCapacityError(
                "trace capacity provider is insufficient",
                requested_bytes=amount,
                available_bytes=self._available,
            )
        self._available -= amount

    def release(self, amount: int) -> None:
        self._available += amount


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
        client, writer_token, admin_connection, lock, process = (
            _start_trace_writer_service(self._writer_service_configuration())
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
            capacity_provider = _FileSystemTraceCapacityProvider(database)
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


class DiagnosticTraceRecorder:
    """Reserve capacity, run one operation, and append its complete trace."""

    _READ_OPERATIONS = (
        "read_traces",
        "list_traces",
        "inspect",
        "export",
        "export_json",
        "_read_persisted_traces",
    )

    def __init__(
        self,
        *,
        writer: DiagnosticTraceStore,
        clock: Clock,
        ids: IdGenerator,
        reservation_bytes: int | None = None,
    ) -> None:
        if any(callable(getattr(writer, name, None)) for name in self._READ_OPERATIONS):
            raise TypeError(
                "DiagnosticTraceRecorder requires a write-only trace capability"
            )
        self._writer = writer
        self.clock = clock
        self.ids = ids
        self.reservation_bytes = reservation_bytes

    def execute(
        self,
        *,
        request_id: str,
        operation_type: str,
        operation: Callable[[], _T],
        input_payload: Any = None,
        arguments: Any = None,
        telemetry: Any = None,
        operation_id: str | None = None,
        outcome: str = "completed",
        result_limit_bytes: int | None = None,
        error_limit_bytes: int | None = None,
    ) -> _T:
        """Run one operation under declared, serializable result bounds."""

        if (
            not isinstance(operation_type, str)
            or not operation_type
            or operation_type.strip() != operation_type
        ):
            raise ValueError("operation_type must be a non-empty canonical string")
        if not isinstance(outcome, str) or not outcome or outcome.strip() != outcome:
            raise ValueError("outcome must be a non-empty canonical string")
        if result_limit_bytes is None or error_limit_bytes is None:
            raise ValueError(
                "result_limit_bytes and error_limit_bytes are required trace bounds"
            )
        _validate_non_negative_int(result_limit_bytes, "result_limit_bytes")
        _validate_non_negative_int(error_limit_bytes, "error_limit_bytes")
        reservation = self._writer.reserve(
            request_id=request_id,
            reservation_bytes=self.reservation_bytes,
        )
        try:
            trace_id = self.ids.new_id("trace")
            started_at = ensure_utc(self.clock.now())
            # Normalize all input fields before the operation starts.  This
            # prevents an unserializable input from launching untraceable work.
            try:
                input_payload = _trace_value(input_payload)
                arguments = _trace_value(arguments)
                telemetry = _trace_value(telemetry)
            except Exception as exc:
                raise TraceWriteError(
                    "trace input payload cannot be represented",
                    operation_started=False,
                ) from exc
            try:
                known_trace_size = self._trace(
                    trace_id=trace_id,
                    operation_id=operation_id or trace_id,
                    request_id=request_id,
                    operation_type=operation_type,
                    started_at=started_at,
                    completed_at=started_at,
                    outcome=outcome,
                    input_payload=input_payload,
                    output_payload=None,
                    arguments=arguments,
                    result=None,
                    error=None,
                    telemetry=telemetry,
                ).serialized_size_bytes
            except Exception as exc:
                raise TraceWriteError(
                    "known trace payload cannot be represented",
                    operation_started=False,
                ) from exc
            if known_trace_size > reservation.reserved_bytes:
                raise TraceCapacityError(
                    "known trace payload exceeds its reserved capacity",
                    requested_bytes=known_trace_size,
                    available_bytes=reservation.reserved_bytes,
                )
            if result_limit_bytes is not None and error_limit_bytes is not None:
                required_result_size = known_trace_size + 2 * (result_limit_bytes + 128)
                required_error_size = known_trace_size + error_limit_bytes + 128
                required_size = max(required_result_size, required_error_size)
                if required_size > reservation.reserved_bytes:
                    raise TraceCapacityError(
                        "declared complete trace bounds exceed reserved capacity",
                        requested_bytes=required_size,
                        available_bytes=reservation.reserved_bytes,
                    )
            try:
                result = operation()
            except Exception as exc:
                completed_at = ensure_utc(self.clock.now())
                try:
                    encoded_error = _trace_value(exc)
                    if (
                        error_limit_bytes is not None
                        and len(_canonical_json(encoded_error)) > error_limit_bytes
                    ):
                        raise TraceWriteError(
                            "operation error exceeds its declared trace bound",
                            operation_started=True,
                        )
                    trace = self._trace(
                        trace_id=trace_id,
                        operation_id=operation_id or trace_id,
                        request_id=request_id,
                        operation_type=operation_type,
                        started_at=started_at,
                        completed_at=completed_at,
                        outcome="failed",
                        input_payload=input_payload,
                        output_payload=None,
                        arguments=arguments,
                        result=None,
                        error=exc,
                        telemetry=telemetry,
                    )
                except Exception as trace_error:
                    write_error = (
                        trace_error
                        if isinstance(trace_error, TraceWriteError)
                        else TraceWriteError(
                            "failed operation trace payload cannot be represented",
                            operation_started=True,
                        )
                    )
                    self._retain_trace_failure(
                        trace_id=trace_id,
                        operation_id=operation_id or trace_id,
                        request_id=request_id,
                        operation_type=operation_type,
                        started_at=started_at,
                        completed_at=completed_at,
                        input_payload=input_payload,
                        arguments=arguments,
                        telemetry=telemetry,
                        error=write_error,
                        reservation=reservation,
                    )
                    raise write_error from trace_error
                try:
                    self._append(trace, reservation, operation_started=True)
                except TraceWriteError as write_error:
                    self._retain_trace_failure(
                        trace_id=trace_id,
                        operation_id=operation_id or trace_id,
                        request_id=request_id,
                        operation_type=operation_type,
                        started_at=started_at,
                        completed_at=completed_at,
                        input_payload=input_payload,
                        arguments=arguments,
                        telemetry=telemetry,
                        error=write_error,
                        reservation=None,
                    )
                    raise
                raise
            completed_at = ensure_utc(self.clock.now())
            try:
                encoded_result = _trace_value(result)
                if (
                    result_limit_bytes is not None
                    and len(_canonical_json(encoded_result)) > result_limit_bytes
                ):
                    raise TraceWriteError(
                        "operation result exceeds its declared trace bound",
                        operation_started=True,
                    )
                trace = self._trace(
                    trace_id=trace_id,
                    operation_id=operation_id or trace_id,
                    request_id=request_id,
                    operation_type=operation_type,
                    started_at=started_at,
                    completed_at=completed_at,
                    outcome=outcome,
                    input_payload=input_payload,
                    output_payload=result,
                    arguments=arguments,
                    result=result,
                    error=None,
                    telemetry=telemetry,
                )
            except Exception as trace_error:
                write_error = (
                    trace_error
                    if isinstance(trace_error, TraceWriteError)
                    else TraceWriteError(
                        "operation result trace payload cannot be represented",
                        operation_started=True,
                    )
                )
                self._retain_trace_failure(
                    trace_id=trace_id,
                    operation_id=operation_id or trace_id,
                    request_id=request_id,
                    operation_type=operation_type,
                    started_at=started_at,
                    completed_at=completed_at,
                    input_payload=input_payload,
                    arguments=arguments,
                    telemetry=telemetry,
                    error=write_error,
                    reservation=reservation,
                )
                raise write_error from trace_error
            try:
                self._append(trace, reservation, operation_started=True)
            except TraceWriteError as write_error:
                self._retain_trace_failure(
                    trace_id=trace_id,
                    operation_id=operation_id or trace_id,
                    request_id=request_id,
                    operation_type=operation_type,
                    started_at=started_at,
                    completed_at=completed_at,
                    input_payload=input_payload,
                    arguments=arguments,
                    telemetry=telemetry,
                    error=write_error,
                    reservation=None,
                )
                raise
            return result
        finally:
            # append() consumes the reservation.  release() is intentionally
            # idempotent so unexpected failures before a complete trace is
            # built cannot strand capacity.
            self._writer.release(reservation)

    @staticmethod
    def _trace(
        *,
        trace_id: str,
        operation_id: str,
        request_id: str,
        operation_type: str,
        started_at: datetime,
        completed_at: datetime,
        outcome: str,
        input_payload: Any,
        output_payload: Any,
        arguments: Any,
        result: Any,
        error: Any,
        telemetry: Any,
    ) -> DiagnosticTrace:
        return DiagnosticTrace(
            trace_id=trace_id,
            operation_id=operation_id,
            request_id=request_id,
            operation_type=operation_type,
            started_at=started_at,
            completed_at=completed_at,
            outcome=outcome,
            payload={
                # These three values were normalized before the operation
                # started.  Re-encoding them here would wrap an explicit
                # mapping/list envelope in another mapping envelope.
                "input": input_payload,
                "output": _trace_value(output_payload),
                "arguments": arguments,
                "result": _trace_value(result),
                "error": _trace_value(error),
                "telemetry": telemetry,
            },
        )

    def _append(
        self,
        trace: DiagnosticTrace,
        reservation: TraceReservation,
        *,
        operation_started: bool,
    ) -> None:
        try:
            self._writer.append(trace, reservation)
        except TraceWriteError as exc:
            raise TraceWriteError(
                str(exc) or "diagnostic trace could not be retained",
                operation_started=operation_started or exc.operation_started,
            ) from exc

    def _retain_trace_failure(
        self,
        *,
        trace_id: str,
        operation_id: str,
        request_id: str,
        operation_type: str,
        started_at: datetime,
        completed_at: datetime,
        input_payload: Any,
        arguments: Any,
        telemetry: Any,
        error: TraceWriteError,
        reservation: TraceReservation | None,
    ) -> None:
        """Retain a bounded failure envelope when the full result cannot encode."""

        try:
            failure_trace = self._trace(
                trace_id=trace_id,
                operation_id=operation_id,
                request_id=request_id,
                operation_type=operation_type,
                started_at=started_at,
                completed_at=completed_at,
                outcome="trace_failed",
                input_payload=input_payload,
                output_payload=None,
                arguments=arguments,
                result=None,
                error=error,
                telemetry=telemetry,
            )
        except Exception:  # noqa: BLE001 - failure fallback must never mask the original error
            return

        if reservation is not None:
            try:
                self._append(failure_trace, reservation, operation_started=True)
                return
            except TraceWriteError:
                pass

        try:
            fallback_reservation = self._writer.reserve(
                request_id=request_id,
                reservation_bytes=failure_trace.serialized_size_bytes,
            )
        except (DiagnosticTraceError, ValueError):
            return
        try:
            self._writer.append(failure_trace, fallback_reservation)
        except TraceWriteError:
            return
        finally:
            self._writer.release(fallback_reservation)

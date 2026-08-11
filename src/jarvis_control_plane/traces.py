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
import math
import shutil
import sqlite3
import tempfile
import threading
import traceback
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, is_dataclass
from dataclasses import fields as dataclass_fields
from datetime import datetime
from enum import Enum
from multiprocessing import Pipe, Process
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar, Protocol, TypeVar

from .models import ensure_utc
from .ports import (
    Clock,
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


def _safe_text(value: Any, *, fallback: str) -> str:
    try:
        return str(value)
    except BaseException:  # noqa: BLE001 - trace capture must survive hostile values
        return fallback


def _safe_repr(value: Any) -> str:
    try:
        return repr(value)
    except BaseException as exc:  # noqa: BLE001 - trace capture must never call user code twice
        error_type = f"{type(exc).__module__}.{type(exc).__qualname__}"
        return (
            f"<repr failed: {error_type}: {_safe_text(exc, fallback='unknown error')}>"
        )


class _TraceValueEncoder:
    """Encode one object graph without dropping cycles or unexpected values.

    The old recursive converter treated an object graph as a tree and raised
    on the first cycle or unsupported adapter value.  A diagnostic trace is a
    record of what happened, so the encoder assigns identities to graph nodes
    and uses explicit references for cycles.  Values that do not have a
    built-in lossless JSON representation are retained as a structural object
    snapshot, including attributes and a safe representation.
    """

    __slots__ = ("_active", "_next_reference", "_references")

    def __init__(self) -> None:
        self._active: set[int] = set()
        self._next_reference = 1
        self._references: dict[int, int] = {}

    def encode(self, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, bool)):
            return value
        if isinstance(value, float):
            if math.isfinite(value):
                return value
            return {"__type__": "float", "value": _safe_repr(value)}
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
        if isinstance(value, bytearray):
            return {
                "__type__": "bytearray",
                "base64": base64.b64encode(bytes(value)).decode("ascii"),
            }
        if isinstance(value, memoryview):
            return {
                "__type__": "memoryview",
                "base64": base64.b64encode(value.tobytes()).decode("ascii"),
            }
        if isinstance(value, Path):
            return {"__type__": "path", "value": str(value)}
        if isinstance(value, Enum):
            return {
                "__type__": "enum",
                "class": self._class_name(value),
                "name": value.name,
                "value": self.encode(value.value),
            }

        reference = self._begin_node(value)
        if isinstance(reference, dict):
            return reference
        try:
            if isinstance(value, BaseException):
                return self._exception(value, reference)
            if isinstance(value, Mapping):
                return self._mapping(value, reference)
            if is_dataclass(value) and not isinstance(value, type):
                fields: dict[str, Any] = {}
                for item in dataclass_fields(value):
                    try:
                        fields[item.name] = getattr(value, item.name)
                    except BaseException as exc:  # noqa: BLE001 - preserve field access failure
                        fields[item.name] = self._attribute_error(exc)
                return self._identified(
                    {
                        "__type__": "dataclass",
                        "class": self._class_name(value),
                        "fields": self.encode(fields),
                    },
                    reference,
                )
            if isinstance(value, (list, tuple, set, frozenset)):
                return self._sequence(value, reference)
            return self._object(value, reference)
        finally:
            self._active.discard(id(value))

    def _begin_node(self, value: Any) -> int | dict[str, int]:
        identity = id(value)
        if identity in self._active:
            existing = self._references[identity]
            return {"__type__": "reference", "id": existing}
        reference = self._next_reference
        self._next_reference += 1
        self._references[identity] = reference
        self._active.add(identity)
        return reference

    @staticmethod
    def _class_name(value: Any) -> str:
        value_type = type(value)
        return f"{value_type.__module__}.{value_type.__qualname__}"

    @staticmethod
    def _identified(value: dict[str, Any], reference: int) -> dict[str, Any]:
        value["id"] = reference
        return value

    def _mapping(self, value: Mapping[Any, Any], reference: int) -> dict[str, Any]:
        return self._identified(
            {
                "__type__": "mapping",
                "items": [
                    {"key": self.encode(key), "value": self.encode(item)}
                    for key, item in value.items()
                ],
            },
            reference,
        )

    def _sequence(self, value: Any, reference: int) -> dict[str, Any]:
        values = [self.encode(item) for item in value]
        if isinstance(value, set | frozenset):
            values.sort(key=_canonical_json)
            sequence_type = "frozenset" if isinstance(value, frozenset) else "set"
        elif isinstance(value, list):
            sequence_type = "list"
        else:
            sequence_type = "tuple"
        return self._identified(
            {"__type__": sequence_type, "items": values},
            reference,
        )

    def _exception(self, value: BaseException, reference: int) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "__type__": "exception",
            "class": self._class_name(value),
            "message": _safe_text(value, fallback="<str failed>"),
            "args": self.encode(value.args),
            "repr": _safe_repr(value),
            "attributes": self.encode(self._attributes(value)),
            "traceback": self._traceback(value),
            "suppress_context": bool(value.__suppress_context__),
        }
        if value.__cause__ is not None:
            payload["cause"] = self.encode(value.__cause__)
        if value.__context__ is not None:
            payload["context"] = self.encode(value.__context__)
        notes = getattr(value, "__notes__", None)
        if notes is not None:
            payload["notes"] = self.encode(notes)
        return self._identified(payload, reference)

    @staticmethod
    def _traceback(value: BaseException) -> list[str]:
        try:
            return traceback.format_exception(type(value), value, value.__traceback__)
        except BaseException:  # noqa: BLE001 - formatting is diagnostic best effort
            return ["<traceback unavailable>"]

    def _object(self, value: Any, reference: int) -> dict[str, Any]:
        return self._identified(
            {
                "__type__": "object",
                "class": self._class_name(value),
                "attributes": self.encode(self._attributes(value)),
                "repr": _safe_repr(value),
            },
            reference,
        )

    @staticmethod
    def _attribute_error(exc: BaseException) -> dict[str, str]:
        return {
            "__type__": "attribute_error",
            "class": f"{type(exc).__module__}.{type(exc).__qualname__}",
            "message": _safe_text(exc, fallback="<str failed>"),
        }

    @staticmethod
    def _attributes(value: Any) -> dict[str, Any]:
        attributes: dict[str, Any] = {}
        try:
            attributes.update(vars(value))
        except (TypeError, AttributeError):
            pass
        for value_type in type(value).__mro__:
            slots = value_type.__dict__.get("__slots__", ())
            if isinstance(slots, str):
                slots = (slots,)
            for name in slots:
                if name in {"__dict__", "__weakref__"} or name in attributes:
                    continue
                try:
                    attributes[name] = getattr(value, name)
                except AttributeError:
                    continue
                except BaseException as exc:  # noqa: BLE001 - preserve hostile slot access
                    attributes[name] = _TraceValueEncoder._attribute_error(exc)
        return attributes


def _trace_value(value: Any) -> Any:
    """Convert one complete operation value into a JSON-safe graph snapshot."""

    return _TraceValueEncoder().encode(value)


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


class _TraceWriterLifecycle(Enum):
    STARTING = "starting"
    SERVING = "serving"
    STOPPING = "stopping"
    CLOSED = "closed"


def _build_trace_writer_store(configuration: Mapping[str, Any]) -> DiagnosticTraceStore:
    if configuration["kind"] == "memory":
        return InMemoryDiagnosticTraceStore(
            capacity_bytes=configuration["capacity_bytes"],
            reservation_bytes=configuration["reservation_bytes"],
            hard_max_bytes=configuration["hard_max_bytes"],
        )
    if configuration["kind"] == "sqlite":
        physical_capacity = configuration.get("physical_capacity_bytes")
        return SQLiteDiagnosticTraceStore(
            configuration["database"],
            capacity_bytes=configuration["capacity_bytes"],
            reservation_bytes=configuration["reservation_bytes"],
            hard_max_bytes=configuration["hard_max_bytes"],
            capacity_provider=(
                _StaticTraceCapacityProvider(physical_capacity)
                if physical_capacity is not None
                else None
            ),
            minimum_free_bytes=int(configuration.get("minimum_free_bytes", 0)),
        )
    raise RuntimeError("unknown trace writer store kind")


class _TraceWriterRuntime:
    """Own the child-process lifecycle, mailbox requests, and admin reads."""

    _MAILBOX_OPERATIONS: ClassVar[set[str]] = {
        "append",
        "close",
        "release",
        "reserve",
    }

    def __init__(
        self,
        *,
        store: DiagnosticTraceStore,
        admin_connection: Any,
        capability_id: str,
    ) -> None:
        self.store = store
        self.admin_connection = admin_connection
        self.mailbox = _trace_writer_mailbox(capability_id)
        self.reservations: dict[str, TraceReservation] = {}
        self.state_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.state = _TraceWriterLifecycle.STARTING

    def serve(self, startup_connection: Any) -> None:
        self.mailbox.mkdir(parents=True, exist_ok=True)
        startup_connection.send({"ok": True})
        startup_connection.close()
        self.state = _TraceWriterLifecycle.SERVING
        admin_open = True
        try:
            while self.state is _TraceWriterLifecycle.SERVING:
                processed = self.process_mailbox_requests()
                if self.state is not _TraceWriterLifecycle.SERVING:
                    continue
                if not admin_open:
                    self.stop_event.wait(0.1)
                    continue
                try:
                    if not self.admin_connection.poll(0.1):
                        if not processed:
                            self.stop_event.wait(0.01)
                        continue
                    request = self.admin_connection.recv()
                except (EOFError, OSError):
                    admin_open = False
                    continue
                response, should_stop = self.handle_admin_request(request)
                if should_stop:
                    self.state = _TraceWriterLifecycle.STOPPING
                    continue
                try:
                    self.admin_connection.send(response)
                except (BrokenPipeError, EOFError, OSError):
                    return
        finally:
            self.close()

    def handle_writer_request(self, request: Mapping[str, Any]) -> dict[str, Any]:
        operation = request.get("operation")
        with self.state_lock:
            if operation == "close":
                self._release_all()
                self.state = _TraceWriterLifecycle.STOPPING
                return {"ok": True}
            try:
                if operation == "reserve":
                    reservation = self.store.reserve(
                        request_id=request["request_id"],
                        reservation_bytes=request.get("reservation_bytes"),
                    )
                    self.reservations[reservation.reservation_id] = reservation
                    return {
                        "ok": True,
                        "reservation_id": reservation.reservation_id,
                        "request_id": reservation.request_id,
                        "reserved_bytes": reservation.reserved_bytes,
                    }
                if operation == "append":
                    reservation_id = request["reservation_id"]
                    reservation = self.reservations.get(reservation_id)
                    if reservation is None:
                        raise TraceWriteError("trace reservation is not active")
                    self.store.append(
                        DiagnosticTrace.from_mapping(request["trace"]),
                        reservation,
                    )
                    self.reservations.pop(reservation_id, None)
                    return {"ok": True}
                if operation == "release":
                    reservation = self.reservations.pop(request["reservation_id"], None)
                    if reservation is not None:
                        self.store.release(reservation)
                    return {"ok": True}
                raise TraceWriteError(
                    "trace content is available only on the admin channel"
                )
            except Exception as exc:  # noqa: BLE001 - IPC boundary reports adapter failures
                if operation == "append":
                    reservation = self.reservations.pop(
                        request.get("reservation_id"), None
                    )
                    if reservation is not None:
                        self.store.release(reservation)
                return {"ok": False, "error": _trace_writer_error(exc)}

    def process_mailbox_requests(self) -> bool:
        processed = False
        for request_path in sorted(self.mailbox.glob("*.request")):
            if request_path.name.startswith("."):
                continue
            processed = True
            try:
                request = json.loads(request_path.read_text(encoding="utf-8"))
                operation = request_path.name.split("-", 1)[0]
                if operation not in self._MAILBOX_OPERATIONS:
                    request_path.unlink(missing_ok=True)
                    continue
                request["operation"] = operation
                ipc_id = request["ipc_id"]
            except (KeyError, OSError, TypeError, ValueError):
                request_path.unlink(missing_ok=True)
                continue
            request_path.unlink(missing_ok=True)
            response = self.handle_writer_request(request)
            self._publish_response(ipc_id, response)
        return processed

    def handle_admin_request(
        self, request: Mapping[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        operation = request.get("operation")
        if operation == "admin_close":
            return {"ok": True}, True
        with self.state_lock:
            try:
                if operation == "read":
                    traces = tuple(
                        trace
                        for trace in self.store._read_persisted_traces()  # type: ignore[attr-defined]
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
                    return {
                        "ok": True,
                        "traces": [trace.to_mapping() for trace in traces],
                    }, False
                if operation == "stats":
                    return {
                        "ok": True,
                        "available_bytes": self.store.available_bytes,
                        "retained_bytes": self.store.retained_bytes,
                        "reserved_bytes": self.store.reserved_bytes,
                        "request_retained_bytes": {
                            request_id: self.store.request_retained_bytes(request_id)
                            for request_id in request.get("request_ids", [])
                        },
                    }, False
                raise TraceWriteError("unknown trace administration operation")
            except Exception as exc:  # noqa: BLE001 - admin IPC reports adapter failures
                return {"ok": False, "error": _trace_writer_error(exc)}, False

    def _publish_response(self, ipc_id: str, response: Mapping[str, Any]) -> None:
        response_path = self.mailbox / f"{ipc_id}.response"
        temporary_path = self.mailbox / f".{ipc_id}.response"
        temporary_path.write_text(
            json.dumps(response, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary_path.replace(response_path)

    def _release_all(self) -> None:
        for reservation in self.reservations.values():
            self.store.release(reservation)
        self.reservations.clear()

    def close(self) -> None:
        if self.state is _TraceWriterLifecycle.CLOSED:
            return
        self.state = _TraceWriterLifecycle.STOPPING
        self.stop_event.set()
        with self.state_lock:
            self._release_all()
        try:
            self.admin_connection.close()
        except OSError:
            pass
        close = getattr(self.store, "close", None)
        if callable(close):
            close()
        self.state = _TraceWriterLifecycle.CLOSED


def _trace_writer_process_main(
    admin_connection: Any,
    startup_connection: Any,
    configuration: Mapping[str, Any],
    capability_id: str,
) -> None:
    """Start the isolated writer runtime and hand it the two IPC channels."""

    runtime = _TraceWriterRuntime(
        store=_build_trace_writer_store(configuration),
        admin_connection=admin_connection,
        capability_id=capability_id,
    )
    runtime.serve(startup_connection)


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

    def __init__(self, location: str | Path, *, minimum_free_bytes: int = 0) -> None:
        location_path = Path(location)
        self._directory = (
            Path.cwd()
            if str(location_path) == ":memory:"
            else location_path.expanduser().resolve().parent
        )
        self._reserved = 0
        self._minimum_free_bytes = _validate_non_negative_int(
            minimum_free_bytes, "minimum_free_bytes"
        )
        self._lock = threading.RLock()

    def available_bytes(self) -> int:
        with self._lock:
            try:
                free = shutil.disk_usage(self._directory).free
            except OSError:
                return 0
            return max(0, free - self._minimum_free_bytes - self._reserved)

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


class _TraceExecutionState(Enum):
    """Named lifecycle states for one admitted trace-producing operation."""

    ADMITTED = "admitted"
    STARTED = "started"
    CAPTURED = "captured"
    RETAINED = "retained"


@dataclass(slots=True)
class _TraceExecution:
    """Operation metadata plus the one reservation it is allowed to consume."""

    trace_id: str
    operation_id: str
    request_id: str
    operation_type: str
    started_at: datetime
    input_payload: Any
    arguments: Any
    telemetry: Any
    outcome: str
    reservation: TraceReservation
    state: _TraceExecutionState = _TraceExecutionState.ADMITTED

    def start(self) -> None:
        self._require(_TraceExecutionState.ADMITTED)
        self.state = _TraceExecutionState.STARTED

    def preview_size_bytes(self) -> int:
        return self._build_trace(
            completed_at=self.started_at,
            outcome=self.outcome,
            output_payload=None,
            result=None,
            error=None,
        ).serialized_size_bytes

    def capture_result(self, result: Any, completed_at: datetime) -> DiagnosticTrace:
        self._require(_TraceExecutionState.STARTED)
        trace = self._build_trace(
            completed_at=completed_at,
            outcome=self.outcome,
            output_payload=result,
            result=result,
            error=None,
        )
        self.state = _TraceExecutionState.CAPTURED
        return trace

    def capture_error(
        self, error: BaseException, completed_at: datetime
    ) -> DiagnosticTrace:
        self._require(_TraceExecutionState.STARTED)
        trace = self._build_trace(
            completed_at=completed_at,
            outcome="failed",
            output_payload=None,
            result=None,
            error=error,
        )
        self.state = _TraceExecutionState.CAPTURED
        return trace

    def mark_retained(self) -> None:
        self._require(_TraceExecutionState.CAPTURED)
        self.state = _TraceExecutionState.RETAINED

    def _build_trace(
        self,
        *,
        completed_at: datetime,
        outcome: str,
        output_payload: Any,
        result: Any,
        error: Any,
    ) -> DiagnosticTrace:
        encoded_output = _trace_value(output_payload)
        encoded_result = (
            encoded_output if output_payload is result else _trace_value(result)
        )
        return DiagnosticTrace(
            trace_id=self.trace_id,
            operation_id=self.operation_id,
            request_id=self.request_id,
            operation_type=self.operation_type,
            started_at=self.started_at,
            completed_at=ensure_utc(completed_at),
            outcome=outcome,
            payload={
                # Inputs, arguments, and telemetry were normalized before the
                # operation began.  Output, result, and error are captured only
                # after the operation boundary returns or raises.
                "input": self.input_payload,
                "output": encoded_output,
                "arguments": self.arguments,
                "result": encoded_result,
                "error": _trace_value(error),
                "telemetry": self.telemetry,
            },
        )

    def _require(self, expected: _TraceExecutionState) -> None:
        if self.state is not expected:
            raise RuntimeError(
                f"trace execution is {self.state.value}, expected {expected.value}"
            )


class _TraceAdmission:
    """Validate operation metadata and reserve capacity before work starts."""

    def __init__(
        self,
        *,
        writer: DiagnosticTraceStore,
        clock: Clock,
        ids: IdGenerator,
        reservation_bytes: int | None,
    ) -> None:
        self._writer = writer
        self._clock = clock
        self._ids = ids
        self._reservation_bytes = reservation_bytes

    def admit(
        self,
        *,
        request_id: str,
        operation_type: str,
        input_payload: Any,
        arguments: Any,
        telemetry: Any,
        operation_id: str | None,
        outcome: str,
        result_limit_bytes: int | None,
        error_limit_bytes: int | None,
    ) -> _TraceExecution:
        self._validate(
            operation_type=operation_type,
            outcome=outcome,
            result_limit_bytes=result_limit_bytes,
            error_limit_bytes=error_limit_bytes,
        )
        reservation = self._writer.reserve(
            request_id=request_id,
            reservation_bytes=self._reservation_bytes,
        )
        try:
            try:
                normalized_input = _trace_value(input_payload)
                normalized_arguments = _trace_value(arguments)
                normalized_telemetry = _trace_value(telemetry)
            except Exception as exc:
                raise TraceWriteError(
                    "trace input payload cannot be represented",
                    operation_started=False,
                ) from exc
            trace_id = self._ids.new_id("trace")
            execution = _TraceExecution(
                trace_id=trace_id,
                operation_id=operation_id or trace_id,
                request_id=request_id,
                operation_type=operation_type,
                started_at=ensure_utc(self._clock.now()),
                input_payload=normalized_input,
                arguments=normalized_arguments,
                telemetry=normalized_telemetry,
                outcome=outcome,
                reservation=reservation,
            )
            known_trace_size = execution.preview_size_bytes()
            if known_trace_size > reservation.reserved_bytes:
                raise TraceCapacityError(
                    "known trace payload exceeds its reserved capacity",
                    requested_bytes=known_trace_size,
                    available_bytes=reservation.reserved_bytes,
                )
            required_size = self._required_size(
                known_trace_size=known_trace_size,
                result_limit_bytes=result_limit_bytes,
                error_limit_bytes=error_limit_bytes,
            )
            if required_size > reservation.reserved_bytes:
                raise TraceCapacityError(
                    "declared complete trace bounds exceed reserved capacity",
                    requested_bytes=required_size,
                    available_bytes=reservation.reserved_bytes,
                )
            return execution
        except Exception:
            self._writer.release(reservation)
            raise

    @staticmethod
    def _validate(
        *,
        operation_type: str,
        outcome: str,
        result_limit_bytes: int | None,
        error_limit_bytes: int | None,
    ) -> None:
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

    @staticmethod
    def _required_size(
        *,
        known_trace_size: int,
        result_limit_bytes: int | None,
        error_limit_bytes: int | None,
    ) -> int:
        if result_limit_bytes is None or error_limit_bytes is None:
            raise AssertionError("trace bounds must be validated before sizing")
        # This budget is reserved before invoking the operation.  Once the
        # boundary has started, the actual encoded payload is authoritative:
        # it is never truncated or replaced with a trace_failed envelope.
        required_result_size = known_trace_size + 2 * (result_limit_bytes + 128)
        required_error_size = known_trace_size + error_limit_bytes + 128
        return max(required_result_size, required_error_size)


class _TracePersistence:
    """Append a captured trace and translate writer failures at one seam."""

    def __init__(self, writer: DiagnosticTraceStore) -> None:
        self._writer = writer

    def append(self, execution: _TraceExecution, trace: DiagnosticTrace) -> None:
        try:
            self._writer.append(trace, execution.reservation)
        except TraceWriteError as exc:
            raise TraceWriteError(
                str(exc) or "diagnostic trace could not be retained",
                operation_started=execution.state is not _TraceExecutionState.ADMITTED,
            ) from exc
        execution.mark_retained()


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
        self._admission = _TraceAdmission(
            writer=writer,
            clock=clock,
            ids=ids,
            reservation_bytes=reservation_bytes,
        )
        self._persistence = _TracePersistence(writer)

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
        """Run one admitted operation and retain its complete outcome."""

        execution = self._admission.admit(
            request_id=request_id,
            operation_type=operation_type,
            input_payload=input_payload,
            arguments=arguments,
            telemetry=telemetry,
            operation_id=operation_id,
            outcome=outcome,
            result_limit_bytes=result_limit_bytes,
            error_limit_bytes=error_limit_bytes,
        )
        try:
            execution.start()
            try:
                result = operation()
            except Exception as exc:
                trace = execution.capture_error(exc, self.clock.now())
                try:
                    self._persistence.append(execution, trace)
                except TraceWriteError as write_error:
                    # The domain exception remains visible.  Persistence is
                    # its cause, never a replacement trace payload.
                    raise exc from write_error
                raise
            trace = execution.capture_result(result, self.clock.now())
            self._persistence.append(execution, trace)
            return result
        finally:
            # append() consumes the reservation.  release() is idempotent so
            # every pre-retention path gives capacity back.
            self._writer.release(execution.reservation)

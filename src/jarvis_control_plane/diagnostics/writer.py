"""Isolated diagnostic-trace writer lifecycle and administrative IPC."""

from __future__ import annotations

import json
import tempfile
import threading
import uuid
from collections.abc import Mapping
from enum import Enum
from multiprocessing import Pipe, Process
from pathlib import Path
from typing import Any, ClassVar

from ..ports import DiagnosticTraceStore, TraceCapacityError, TraceWriteError
from ..writer_capability import (
    TraceWriterCapability,
    close_writer_capability,  # noqa: F401 - compatibility re-export
)
from .records import DiagnosticTrace
from .values import TraceReservation


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
    """Named lifecycle values for the isolated writer runtime."""

    STARTING = "starting"
    SERVING = "serving"
    STOPPING = "stopping"
    CLOSED = "closed"


def _build_trace_writer_store(configuration: Mapping[str, Any]) -> DiagnosticTraceStore:
    # Keep the store import lazy: store.py delegates service startup here.
    from .capacity import _StaticTraceCapacityProvider
    from .sqlite_store import SQLiteDiagnosticTraceStore
    from .store import InMemoryDiagnosticTraceStore

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

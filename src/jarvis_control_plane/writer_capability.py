"""Write-only mailbox capability for the isolated diagnostic-trace process."""

from __future__ import annotations

import json
import tempfile
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .ports import TraceCapacityError, TraceWriteError
from .trace_types import TraceReservation

if TYPE_CHECKING:
    from .traces import DiagnosticTrace

_RESPONSE_TIMEOUT_SECONDS = 30.0
_CLOSE_RESPONSE_TIMEOUT_SECONDS = 2.0


def _mailbox(capability_id: str) -> Path:
    return Path(tempfile.gettempdir()) / f"jarvis-trace-{capability_id}"


def _read_response_until_ready(
    response_path: Path,
    *,
    deadline: float,
    operation_started: bool,
) -> dict[str, Any]:
    while True:
        try:
            response = json.loads(response_path.read_text(encoding="utf-8"))
            if not isinstance(response, dict):
                raise TypeError("trace writer response is not an object")
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ):
            # The writer publishes with an atomic rename, but Windows can
            # still expose a short interval in which the new name exists while
            # the file is locked or its contents are not yet readable.  Treat
            # every read/parse/acknowledgement failure as transient until the
            # same deadline rather than treating ``exists()`` as readiness.
            if time.monotonic() >= deadline:
                raise TraceWriteError(
                    "diagnostic trace writer is unavailable",
                    operation_started=operation_started,
                )
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
            continue
        try:
            response_path.unlink(missing_ok=True)
        except FileNotFoundError:
            # Another acknowledgement cleanup may have removed the response
            # after we parsed it.  The response itself is still valid.
            return response
        except OSError:
            if time.monotonic() >= deadline:
                raise TraceWriteError(
                    "diagnostic trace writer is unavailable",
                    operation_started=operation_started,
                )
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
            continue
        return response


def _raise_writer_error(error: dict[str, Any]) -> None:
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


def close_writer_capability(capability_id: str) -> None:
    """Ask the isolated writer process to release reservations and stop."""

    mailbox = _mailbox(capability_id)
    ipc_id = uuid.uuid4().hex
    request_path = mailbox / f"close-{ipc_id}.request"
    temporary_path = mailbox / f".{ipc_id}.request"
    response_path = mailbox / f"{ipc_id}.response"
    try:
        temporary_path.write_text(
            json.dumps({"ipc_id": ipc_id}, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary_path.replace(request_path)
        deadline = time.monotonic() + _CLOSE_RESPONSE_TIMEOUT_SECONDS
        _read_response_until_ready(
            response_path,
            deadline=deadline,
            operation_started=False,
        )
    except (OSError, ValueError, TypeError, TraceWriteError):
        return
    try:
        mailbox.rmdir()
    except OSError:
        pass


class TraceWriterCapability:
    """Opaque write-only capability with exactly three public operations."""

    __slots__ = ("_capability_id",)

    def __init__(self, capability_id: str) -> None:
        self._capability_id = capability_id

    def __del__(self) -> None:
        try:
            close_writer_capability(self._capability_id)
        except Exception:  # noqa: BLE001, S110 - finalizer must never raise
            pass

    def reserve(
        self,
        *,
        request_id: str,
        reservation_bytes: int | None = None,
    ) -> TraceReservation:
        mailbox = _mailbox(self._capability_id)
        ipc_id = uuid.uuid4().hex
        request_path = mailbox / f"reserve-{ipc_id}.request"
        temporary_path = mailbox / f".{ipc_id}.request"
        response_path = mailbox / f"{ipc_id}.response"
        try:
            temporary_path.write_text(
                json.dumps(
                    {
                        "ipc_id": ipc_id,
                        "request_id": request_id,
                        "reservation_bytes": reservation_bytes,
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            temporary_path.replace(request_path)
            deadline = time.monotonic() + _RESPONSE_TIMEOUT_SECONDS
            response = _read_response_until_ready(
                response_path,
                deadline=deadline,
                operation_started=False,
            )
        except TraceWriteError:
            raise
        except (AttributeError, KeyError, OSError, TypeError, ValueError) as exc:
            raise TraceWriteError("diagnostic trace writer is unavailable") from exc
        if not response.get("ok", False):
            _raise_writer_error(response["error"])
        return TraceReservation(
            reservation_id=response["reservation_id"],
            request_id=response["request_id"],
            reserved_bytes=response["reserved_bytes"],
            _owner=self,
        )

    def append(self, trace: DiagnosticTrace, reservation: TraceReservation) -> None:
        if reservation._owner is not self:
            raise TraceWriteError("trace reservation belongs to another writer")
        mailbox = _mailbox(self._capability_id)
        ipc_id = uuid.uuid4().hex
        request_path = mailbox / f"append-{ipc_id}.request"
        temporary_path = mailbox / f".{ipc_id}.request"
        response_path = mailbox / f"{ipc_id}.response"
        try:
            temporary_path.write_text(
                json.dumps(
                    {
                        "ipc_id": ipc_id,
                        "reservation_id": reservation.reservation_id,
                        "trace": trace.to_mapping(),
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            temporary_path.replace(request_path)
            deadline = time.monotonic() + _RESPONSE_TIMEOUT_SECONDS
            response = _read_response_until_ready(
                response_path,
                deadline=deadline,
                operation_started=True,
            )
        except TraceWriteError:
            raise
        except (AttributeError, KeyError, OSError, TypeError, ValueError) as exc:
            raise TraceWriteError(
                "diagnostic trace writer is unavailable",
                operation_started=True,
            ) from exc
        if not response.get("ok", False):
            _raise_writer_error(response["error"])

    def release(self, reservation: TraceReservation) -> None:
        if reservation._owner is not self:
            return
        mailbox = _mailbox(self._capability_id)
        ipc_id = uuid.uuid4().hex
        request_path = mailbox / f"release-{ipc_id}.request"
        temporary_path = mailbox / f".{ipc_id}.request"
        response_path = mailbox / f"{ipc_id}.response"
        try:
            temporary_path.write_text(
                json.dumps(
                    {
                        "ipc_id": ipc_id,
                        "reservation_id": reservation.reservation_id,
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            temporary_path.replace(request_path)
            deadline = time.monotonic() + _RESPONSE_TIMEOUT_SECONDS
            response = _read_response_until_ready(
                response_path,
                deadline=deadline,
                operation_started=False,
            )
            if not response.get("ok", False):
                _raise_writer_error(response["error"])
        except (
            AttributeError,
            KeyError,
            OSError,
            TypeError,
            ValueError,
            TraceWriteError,
        ):
            return

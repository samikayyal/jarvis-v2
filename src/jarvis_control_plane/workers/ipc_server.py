"""Server adapter for one already-accepted Ubuntu worker connection."""

from __future__ import annotations

import socket
from collections.abc import Mapping
from threading import Event, RLock, Thread
from time import monotonic

from ..ports import (
    ActionCancellationStatus,
    ActionDispatcherError,
)
from .contracts import WorkerInvocation
from .ipc_protocol import (
    _error_to_wire,
    _identity_to_wire,
    _invocation_from_wire,
    _lifecycle_payload,
    _PollingFrameReceiver,
    _progress_to_wire,
    _require_keys,
    _required_int,
    _required_object,
    _required_text,
    _result_to_wire,
    _send_frame,
)
from .ubuntu_service import UbuntuWorkerService


def serve_ubuntu_worker_connection(
    connection: socket.socket,
    worker: UbuntuWorkerService,
    *,
    stop: Event | None = None,
) -> None:
    """Serve one already-accepted local connection; no listener is created."""

    if not worker.binds(connection):
        raise ValueError("Ubuntu worker service must authenticate its exact channel")
    connection.setblocking(False)
    try:
        worker.authenticate(selected_host="ubuntu", timeout_seconds=10)
    except BaseException:
        connection.close()
        raise
    send_lock = RLock()
    active_lock = RLock()
    active_action_id: str | None = None
    retention_by_action: dict[str, int] = {}
    receiver = _PollingFrameReceiver()

    def send(request_id: str, message: Mapping[str, object]) -> None:
        _send_frame(
            connection,
            {"request_id": request_id, **message},
            deadline=monotonic() + 30,
            lock=send_lock,
        )

    def execute(request_id: str, invocation: WorkerInvocation, action_id: str) -> None:
        nonlocal active_action_id
        try:
            result = worker.execute(
                invocation,
                lambda event: send(
                    request_id,
                    {"type": "progress", "payload": _progress_to_wire(event)},
                ),
            )
            send(request_id, {"type": "result", "payload": _result_to_wire(result)})
        except ActionDispatcherError as exc:
            send(request_id, _error_to_wire(exc))
        except BaseException:  # noqa: BLE001 - close an untrusted process boundary
            send(
                request_id,
                {
                    "type": "error",
                    "message": "Ubuntu worker execution failed",
                    "may_have_dispatched": True,
                },
            )
        finally:
            with active_lock:
                if active_action_id == action_id:
                    active_action_id = None

    try:
        while stop is None or not stop.is_set():
            try:
                request = receiver.receive(
                    connection,
                    deadline=(monotonic() + 0.25 if stop is not None else None),
                )
            except TimeoutError:
                continue
            _require_keys(request, {"request_id", "operation", "payload"})
            request_id = _required_text(request, "request_id")
            operation = _required_text(request, "operation")
            payload = _required_object(request, "payload")
            try:
                if operation == "register":
                    action_id, timeout, retention = _lifecycle_payload(payload)
                    worker.register_execution(
                        action_id=action_id,
                        timeout_seconds=timeout,
                        retention_seconds=retention,
                    )
                    retention_by_action[action_id] = retention
                    send(request_id, {"type": "result", "payload": {}})
                elif operation == "authenticate":
                    _require_keys(payload, {"selected_host", "timeout_seconds"})
                    identity = worker.authenticate(
                        selected_host=_required_text(payload, "selected_host"),
                        timeout_seconds=_required_int(payload, "timeout_seconds"),
                    )
                    send(
                        request_id,
                        {"type": "result", "payload": _identity_to_wire(identity)},
                    )
                elif operation == "execute":
                    _require_keys(payload, {"invocation"})
                    invocation = _invocation_from_wire(
                        _required_object(payload, "invocation")
                    )
                    with active_lock:
                        if active_action_id is not None:
                            raise ActionDispatcherError("native Ubuntu worker is busy")
                        active_action_id = invocation.action_id
                    Thread(
                        target=execute,
                        args=(request_id, invocation, invocation.action_id),
                        daemon=True,
                    ).start()
                elif operation == "cancel":
                    action_id, timeout, retention = _lifecycle_payload(payload)
                    result = worker.cancel(
                        action_id=action_id,
                        timeout_seconds=timeout,
                        retention_seconds=retention,
                    )
                    send(
                        request_id,
                        {
                            "type": "result",
                            "payload": {
                                "status": ActionCancellationStatus(result.status).value
                            },
                        },
                    )
                elif operation == "finalize":
                    action_id, timeout, retention = _lifecycle_payload(payload)
                    worker.finalize_execution(
                        action_id=action_id,
                        timeout_seconds=timeout,
                        retention_seconds=retention,
                    )
                    retention_by_action.pop(action_id, None)
                    send(request_id, {"type": "result", "payload": {}})
                else:
                    raise ActionDispatcherError("Ubuntu worker operation is unknown")
            except ActionDispatcherError as exc:
                send(request_id, _error_to_wire(exc))
            except (TypeError, ValueError, KeyError):
                send(
                    request_id,
                    {
                        "type": "error",
                        "message": "Ubuntu worker request was malformed",
                        "may_have_dispatched": False,
                    },
                )
    except (ActionDispatcherError, OSError, EOFError):
        pass
    finally:
        try:
            for action_id, retention in tuple(retention_by_action.items()):
                try:
                    worker.cancel(
                        action_id=action_id,
                        timeout_seconds=10,
                        retention_seconds=retention,
                    )
                except (ActionDispatcherError, TypeError, ValueError):
                    pass
        finally:
            connection.close()


__all__ = ["serve_ubuntu_worker_connection"]

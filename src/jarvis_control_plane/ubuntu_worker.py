"""Unactivated native Ubuntu worker contract.

The worker is deliberately a transport adapter, not a policy authority.  It
accepts only gateway-approved :class:`WorkerInvocation` values over an
authenticated, permission-restricted local Unix-socket channel.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import socket
import stat
import struct
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from threading import Event, RLock, Thread
from time import monotonic, sleep
from typing import BinaryIO, Protocol, cast

from .ports import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ActionDispatcherError,
)
from .terminal_policy import TerminalComponent
from .ubuntu_worker_runner import COMPOUND_RESULT_MARKER
from .worker_gateway import (
    WorkerExecutionLimits,
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerIdentity,
    WorkerInvocation,
    WorkerOutputStream,
    WorkerProgressEvent,
    WorkerProgressKind,
    WorkerProgressSink,
)

_COMPOUND_METADATA_LIMIT_BYTES = 4096


class UbuntuWorkerReadiness(str, Enum):
    """The native worker states relevant at the dispatch barrier."""

    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class UbuntuLocalPeerExpectation:
    """Exact OS identity and socket boundary trusted for one local peer."""

    peer_uid: int
    socket_owner_uid: int
    socket_path: str
    socket_mode: int = 0o600

    def __post_init__(self) -> None:
        for name in ("peer_uid", "socket_owner_uid"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"expected Ubuntu {name} is invalid")
        if not PurePosixPath(self.socket_path).is_absolute():
            raise ValueError("expected Ubuntu worker socket path must be absolute")
        if str(PurePosixPath(self.socket_path)) != self.socket_path:
            raise ValueError("expected Ubuntu worker socket path must be canonical")
        if self.socket_mode != 0o600:
            raise ValueError("expected Ubuntu worker socket mode must be 0600")

    def matches(self, peer: UbuntuLocalPeerIdentity) -> bool:
        return (
            peer.peer_uid == self.peer_uid
            and peer.socket_owner_uid == self.socket_owner_uid
            and peer.socket_path == self.socket_path
            and peer.socket_mode == self.socket_mode
        )


@dataclass(frozen=True, slots=True)
class UbuntuLocalPeerIdentity:
    """Identity evidence obtained from one connected local Unix socket."""

    peer_pid: int
    peer_uid: int
    peer_gid: int
    socket_path: str
    socket_owner_uid: int
    socket_mode: int
    connection_id: str

    def __post_init__(self) -> None:
        for name in ("peer_pid", "peer_uid", "peer_gid", "socket_owner_uid"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"Ubuntu local {name} must be a non-negative integer")
        if self.peer_pid < 1:
            raise ValueError("Ubuntu local peer PID must be positive")
        if self.socket_mode != 0o600:
            raise ValueError("Ubuntu worker socket mode must be exactly 0600")
        if not self.socket_path.startswith("/"):
            raise ValueError("Ubuntu worker socket path must be absolute")
        if not self.connection_id.strip():
            raise ValueError("Ubuntu local connection identifier must be non-blank")


class UbuntuLocalAuthenticator(Protocol):
    """OS-backed authentication seam for the already-connected local channel."""

    def authenticate(self, *, timeout_seconds: int) -> UbuntuLocalPeerIdentity: ...

    def binds(self, connection: socket.socket) -> bool: ...


class ControlledUbuntuLocalAuthenticator:
    """Deterministic local-channel identity provider for contract tests."""

    def __init__(
        self,
        identity: UbuntuLocalPeerIdentity,
        *,
        connection: socket.socket | None = None,
    ) -> None:
        self.identity = identity
        self._connection = connection

    def authenticate(self, *, timeout_seconds: int) -> UbuntuLocalPeerIdentity:
        if isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= 30:
            raise ValueError("Ubuntu local authentication timeout is invalid")
        return self.identity

    def binds(self, connection: socket.socket) -> bool:
        return self._connection is connection


class UnixSocketUbuntuLocalAuthenticator:
    """Read Linux ``SO_PEERCRED`` evidence from one accepted Unix connection.

    This class neither creates nor exposes a listener.  The native service's
    manually activated socket owner passes its already-accepted connection in;
    authentication then binds that connection to OS credentials and the exact
    restricted socket inode.
    """

    def __init__(
        self,
        *,
        connection: socket.socket,
        socket_path: str,
        connection_id: str,
    ) -> None:
        unix_family = getattr(socket, "AF_UNIX", None)
        if unix_family is None or connection.family != unix_family:
            raise ValueError("Ubuntu local channel must be a Unix socket")
        if not os.path.isabs(socket_path):
            raise ValueError("Ubuntu worker socket path must be absolute")
        if os.path.realpath(socket_path) != socket_path:
            raise ValueError("Ubuntu worker socket path must be canonical")
        if not connection_id.strip():
            raise ValueError("Ubuntu local connection identifier must be non-blank")
        self._connection = connection
        self._socket_path = socket_path
        self._connection_id = connection_id.strip()

    def binds(self, connection: socket.socket) -> bool:
        return self._connection is connection

    def authenticate(self, *, timeout_seconds: int) -> UbuntuLocalPeerIdentity:
        if isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= 30:
            raise ValueError("Ubuntu local authentication timeout is invalid")
        peer_credentials_option = getattr(socket, "SO_PEERCRED", None)
        if not isinstance(peer_credentials_option, int):
            raise ActionDispatcherError("Linux peer credentials are unavailable")
        try:
            socket_info = os.lstat(self._socket_path)
            if not stat.S_ISSOCK(socket_info.st_mode):
                raise ActionDispatcherError("Ubuntu local channel is not a socket")
            mode = stat.S_IMODE(socket_info.st_mode)
            if mode != 0o600:
                raise ActionDispatcherError(
                    "Ubuntu worker socket permissions are not restricted"
                )
            channel_paths = (
                self._connection.getsockname(),
                self._connection.getpeername(),
            )
            if not any(
                isinstance(channel_path, str)
                and channel_path
                and os.path.samefile(channel_path, self._socket_path)
                for channel_path in channel_paths
            ):
                raise ActionDispatcherError("Ubuntu worker socket identity changed")
            raw = self._connection.getsockopt(
                socket.SOL_SOCKET, peer_credentials_option, struct.calcsize("3i")
            )
            peer_pid, peer_uid, peer_gid = struct.unpack("3i", raw)
        except ActionDispatcherError:
            raise
        except (OSError, ValueError, struct.error) as exc:
            raise ActionDispatcherError(
                "Ubuntu local peer authentication failed"
            ) from exc
        return UbuntuLocalPeerIdentity(
            peer_pid=peer_pid,
            peer_uid=peer_uid,
            peer_gid=peer_gid,
            socket_path=self._socket_path,
            socket_owner_uid=socket_info.st_uid,
            socket_mode=mode,
            connection_id=self._connection_id,
        )


class UbuntuProcessScope(Protocol):
    """Least-privileged process-scope boundary owned by the native worker."""

    def reserve(self, *, action_id: str) -> None: ...

    def retire(self, *, action_id: str) -> None: ...

    def execute(
        self, invocation: WorkerInvocation, progress: WorkerProgressSink
    ) -> WorkerExecutionResult: ...

    def cancel(
        self, *, action_id: str, timeout_seconds: int
    ) -> ActionCancellationResult: ...


class ControlledUbuntuProcessScope:
    """Controlled process scope with the same typed boundary as Ubuntu."""

    def __init__(
        self,
        *,
        result: WorkerExecutionResult | None = None,
        progress_events: tuple[WorkerProgressEvent, ...] = (),
        execution_hook: Callable[[WorkerInvocation], WorkerExecutionResult]
        | None = None,
        cancellation_hook: Callable[[str, int], ActionCancellationResult] | None = None,
    ) -> None:
        self.result = result or WorkerExecutionResult.completed()
        self.progress_events = tuple(progress_events)
        self.execution_hook = execution_hook
        self.cancellation_hook = cancellation_hook
        self.invocations: list[WorkerInvocation] = []
        self.cancellations: list[tuple[str, int]] = []
        self._lock = RLock()
        self._reserved: set[str] = set()
        self._running: set[str] = set()
        self._cancelled: set[str] = set()

    def reserve(self, *, action_id: str) -> None:
        with self._lock:
            if action_id in self._reserved | self._running | self._cancelled:
                raise ActionDispatcherError(
                    f"Ubuntu process scope {action_id} is already reserved"
                )
            self._reserved.add(action_id)

    def retire(self, *, action_id: str) -> None:
        with self._lock:
            self._reserved.discard(action_id)
            self._cancelled.discard(action_id)

    def execute(
        self, invocation: WorkerInvocation, progress: WorkerProgressSink
    ) -> WorkerExecutionResult:
        with self._lock:
            if invocation.action_id in self._cancelled:
                return WorkerExecutionResult(
                    status=WorkerExecutionStatus.CANCELLED,
                    process_tree_stopped=True,
                )
            if invocation.action_id not in self._reserved:
                raise ActionDispatcherError("Ubuntu process scope was not reserved")
            self._reserved.remove(invocation.action_id)
            self._running.add(invocation.action_id)
            self.invocations.append(invocation)
        try:
            for event in self.progress_events:
                progress(event)
            if self.execution_hook is not None:
                return self.execution_hook(invocation)
            return self.result
        finally:
            with self._lock:
                self._running.discard(invocation.action_id)

    def cancel(
        self, *, action_id: str, timeout_seconds: int
    ) -> ActionCancellationResult:
        self.cancellations.append((action_id, timeout_seconds))
        with self._lock:
            if action_id in self._reserved:
                self._reserved.remove(action_id)
                self._cancelled.add(action_id)
                return ActionCancellationResult(ActionCancellationStatus.NOT_STARTED)
            running = action_id in self._running
        if not running:
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
        if self.cancellation_hook is not None:
            return self.cancellation_hook(action_id, timeout_seconds)
        return ActionCancellationResult(ActionCancellationStatus.STOPPED)


@dataclass(slots=True)
class _StartingSystemdScope:
    cancel_requested: Event
    resolved: Event


@dataclass(slots=True)
class _RunningSystemdScope:
    unit_name: str
    process: subprocess.Popen[bytes]
    cancel_requested: Event
    termination_lock: RLock
    unit_observed: Event


class SystemdUbuntuProcessScope:
    """Run one exact process in a least-privileged transient user service.

    ``systemd-run`` creates the cgroup-backed process scope and enforces
    ``TasksMax`` for descendants.  Cancellation targets the whole unit, never
    only the wrapper PID.  This source adapter does not install or activate a
    service and never invokes a shell or Docker.
    """

    def __init__(
        self,
        *,
        systemd_run_path: str = "/usr/bin/systemd-run",
        systemctl_path: str = "/usr/bin/systemctl",
        process_limit: int = 32,
    ) -> None:
        if isinstance(process_limit, bool) or not isinstance(process_limit, int):
            raise TypeError("Ubuntu process limit must be an integer")
        if not 1 <= process_limit <= 64:
            raise ValueError("Ubuntu process limit must be between one and 64")
        for name, value in (
            ("systemd-run", systemd_run_path),
            ("systemctl", systemctl_path),
        ):
            if not isinstance(value, str) or not PurePosixPath(value).is_absolute():
                raise ValueError(f"{name} path must be absolute")
        self._systemd_run_path = systemd_run_path
        self._systemctl_path = systemctl_path
        self._process_limit = process_limit
        runtime_uid = os.getuid() if hasattr(os, "getuid") else 0
        self._user_runtime_directory = f"/run/user/{runtime_uid}"
        self._lock = RLock()
        self._running: dict[str, _RunningSystemdScope] = {}
        self._starting: dict[str, _StartingSystemdScope] = {}
        self._active_action_ids: set[str] = set()
        self._reserved_action_ids: set[str] = set()
        self._cancelled_action_ids: set[str] = set()
        self._stopped_action_ids: set[str] = set()

    def reserve(self, *, action_id: str) -> None:
        with self._lock:
            if self._active_action_ids:
                raise ActionDispatcherError("native Ubuntu process scope is busy")
            known = (
                self._reserved_action_ids
                | self._active_action_ids
                | self._cancelled_action_ids
                | self._stopped_action_ids
            )
            if action_id in known:
                raise ActionDispatcherError(
                    f"Ubuntu process scope {action_id} is already reserved"
                )
            self._reserved_action_ids.add(action_id)

    def retire(self, *, action_id: str) -> None:
        with self._lock:
            self._reserved_action_ids.discard(action_id)
            self._cancelled_action_ids.discard(action_id)
            self._stopped_action_ids.discard(action_id)

    def command_for(self, invocation: WorkerInvocation) -> tuple[str, ...]:
        """Return the exact argv used to create the bounded native scope."""

        if invocation.interactive:
            raise ActionDispatcherError("Ubuntu process scopes are non-interactive")
        if invocation.action.host != "ubuntu":
            raise ActionDispatcherError("Ubuntu process scope rejected another host")
        components = tuple(
            cast(TerminalComponent, component)
            for component in invocation.action.components
        )
        if any(component.redirections for component in components):
            raise ActionDispatcherError(
                "Ubuntu process scope cannot execute directionless redirections"
            )
        unit_name = self._unit_name(invocation.action_id)
        action_command = self._action_command(components)
        return (
            self._systemd_run_path,
            "--user",
            "--quiet",
            "--wait",
            "--collect",
            "--pipe",
            "--service-type=exec",
            f"--unit={unit_name}",
            f"--property=TasksMax={self._process_limit}",
            "--property=NoNewPrivileges=yes",
            "--property=RestrictNamespaces=yes",
            (
                "--property=InaccessiblePaths=/run/systemd/private "
                "/run/dbus/system_bus_socket "
                f"{self._user_runtime_directory}/systemd/private "
                f"{self._user_runtime_directory}/bus"
            ),
            f"--property=RuntimeMaxSec={invocation.deadline_seconds}s",
            f"--property=TimeoutStopSec={invocation.cancellation_grace_seconds}s",
            f"--working-directory={invocation.action.cwd}",
            "--",
            *action_command,
        )

    @staticmethod
    def _action_command(
        components: tuple[TerminalComponent, ...],
    ) -> tuple[str, ...]:
        if len(components) == 1:
            component = components[0]
            return (component.executable, *component.arguments)
        plan = [
            {
                "executable": component.executable,
                "arguments": list(component.arguments),
                "operator_before": component.operator_before,
            }
            for component in components
        ]
        encoded = base64.urlsafe_b64encode(
            json.dumps(plan, separators=(",", ":")).encode()
        ).decode()
        return (
            sys.executable,
            str(Path(__file__).with_name("ubuntu_worker_runner.py").resolve()),
            encoded,
        )

    def execute(
        self, invocation: WorkerInvocation, progress: WorkerProgressSink
    ) -> WorkerExecutionResult:
        try:
            if sys.platform != "linux":
                raise ActionDispatcherError(
                    "native Ubuntu process scope requires Linux"
                )
            command = self.command_for(invocation)
        except ActionDispatcherError:
            with self._lock:
                self._reserved_action_ids.discard(invocation.action_id)
            raise
        with self._lock:
            if invocation.action_id in self._cancelled_action_ids:
                return WorkerExecutionResult(
                    status=WorkerExecutionStatus.CANCELLED,
                    process_tree_stopped=True,
                )
            if invocation.action_id not in self._reserved_action_ids:
                raise ActionDispatcherError("Ubuntu process scope was not reserved")
            self._reserved_action_ids.remove(invocation.action_id)
            self._active_action_ids.add(invocation.action_id)
            starting = _StartingSystemdScope(cancel_requested=Event(), resolved=Event())
            self._starting[invocation.action_id] = starting
        if starting.cancel_requested.is_set():
            with self._lock:
                self._starting.pop(invocation.action_id, None)
                self._active_action_ids.discard(invocation.action_id)
                self._cancelled_action_ids.add(invocation.action_id)
                starting.resolved.set()
            return WorkerExecutionResult(
                status=WorkerExecutionStatus.CANCELLED,
                process_tree_stopped=True,
            )
        unit_name = self._unit_name(invocation.action_id)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            with self._lock:
                self._active_action_ids.discard(invocation.action_id)
                self._starting.pop(invocation.action_id, None)
                if starting.cancel_requested.is_set():
                    self._cancelled_action_ids.add(invocation.action_id)
                starting.resolved.set()
            raise ActionDispatcherError(
                "native Ubuntu process scope could not start"
            ) from exc
        running = _RunningSystemdScope(
            unit_name=unit_name,
            process=process,
            cancel_requested=starting.cancel_requested,
            termination_lock=RLock(),
            unit_observed=Event(),
        )
        with self._lock:
            self._running[invocation.action_id] = running
            self._starting.pop(invocation.action_id, None)
            starting.resolved.set()
        if running.cancel_requested.is_set():
            stopped = self._stop_scope(running, invocation.cancellation_grace_seconds)
            process.stdout.close()
            process.stderr.close()
            if stopped:
                with self._lock:
                    if self._running.get(invocation.action_id) is running:
                        del self._running[invocation.action_id]
                    self._active_action_ids.discard(invocation.action_id)
                    self._stopped_action_ids.add(invocation.action_id)
            return WorkerExecutionResult(
                status=(
                    WorkerExecutionStatus.CANCELLED
                    if stopped
                    else WorkerExecutionStatus.UNKNOWN
                ),
                process_tree_stopped=stopped,
            )
        release_scope = False
        try:
            try:
                result = self._observe(running, invocation, progress)
            except BaseException:
                release_scope = self._stop_scope(
                    running, invocation.cancellation_grace_seconds
                )
                process.stdout.close()
                process.stderr.close()
                raise
            release_scope = result.process_tree_stopped
            return result
        finally:
            with self._lock:
                if self._running.get(invocation.action_id) is running and release_scope:
                    del self._running[invocation.action_id]
                    self._active_action_ids.discard(invocation.action_id)

    def cancel(
        self, *, action_id: str, timeout_seconds: int
    ) -> ActionCancellationResult:
        deadline = monotonic() + timeout_seconds
        with self._lock:
            if action_id in self._reserved_action_ids:
                self._reserved_action_ids.remove(action_id)
                self._cancelled_action_ids.add(action_id)
                return ActionCancellationResult(ActionCancellationStatus.NOT_STARTED)
            starting = self._starting.get(action_id)
            running = self._running.get(action_id)
        if starting is not None:
            starting.cancel_requested.set()
            if not starting.resolved.wait(timeout=max(deadline - monotonic(), 0)):
                return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
            with self._lock:
                if action_id in self._stopped_action_ids:
                    return ActionCancellationResult(ActionCancellationStatus.STOPPED)
                if action_id in self._cancelled_action_ids:
                    return ActionCancellationResult(
                        ActionCancellationStatus.NOT_STARTED
                    )
                running = self._running.get(action_id)
        if running is None:
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
        running.cancel_requested.set()
        remaining = deadline - monotonic()
        if remaining <= 0:
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
        stopped = self._stop_scope(running, remaining)
        if stopped:
            with self._lock:
                if self._running.get(action_id) is running:
                    del self._running[action_id]
                self._active_action_ids.discard(action_id)
                self._stopped_action_ids.add(action_id)
        return ActionCancellationResult(
            ActionCancellationStatus.STOPPED
            if stopped
            else ActionCancellationStatus.UNKNOWN
        )

    def _observe(
        self,
        running: _RunningSystemdScope,
        invocation: WorkerInvocation,
        progress: WorkerProgressSink,
    ) -> WorkerExecutionResult:
        # A small bounded queue applies backpressure to the pipe readers.  Each
        # reader also discards bytes beyond its own stream cap before enqueueing,
        # so fast child output can never accumulate unbounded Python memory.
        output: queue.Queue[tuple[WorkerOutputStream, bytes | None]] = queue.Queue(
            maxsize=8
        )

        def read_stream(
            stream: BinaryIO,
            tag: WorkerOutputStream,
            byte_limit: int,
            tail_limit: int = 0,
        ) -> None:
            read = getattr(stream, "read1", stream.read)
            remaining = byte_limit - tail_limit
            tail = bytearray()
            truncation_reported = False
            try:
                while chunk := read(16 * 1024):
                    accepted = chunk[:remaining]
                    if accepted:
                        output.put((tag, accepted))
                        remaining -= len(accepted)
                    excess = chunk[len(accepted) :]
                    if excess:
                        if tail_limit:
                            tail.extend(excess)
                            del tail[:-tail_limit]
                        if not truncation_reported:
                            output.put((tag, b""))
                            truncation_reported = True
            except (OSError, ValueError):
                # Deadline cleanup closes the pipes to release readers even if
                # a descendant inherited the wrapper's file descriptors.
                pass
            finally:
                try:
                    if tail:
                        output.put((tag, bytes(tail)), timeout=1)
                    output.put((tag, None), timeout=1)
                except queue.Full:
                    pass

        assert running.process.stdout is not None
        assert running.process.stderr is not None
        compound = len(invocation.action.components) > 1
        stderr_capture_limit = invocation.stderr_limit_bytes + (
            _COMPOUND_METADATA_LIMIT_BYTES if compound else 0
        )
        readers = (
            Thread(
                target=read_stream,
                args=(
                    running.process.stdout,
                    WorkerOutputStream.STDOUT,
                    invocation.stdout_limit_bytes,
                ),
                daemon=True,
            ),
            Thread(
                target=read_stream,
                args=(
                    running.process.stderr,
                    WorkerOutputStream.STDERR,
                    stderr_capture_limit,
                    _COMPOUND_METADATA_LIMIT_BYTES if compound else 0,
                ),
                daemon=True,
            ),
        )
        for reader in readers:
            reader.start()
        sequence = 2
        emitted = 0
        if invocation.progress_event_limit > 1:
            progress(
                WorkerProgressEvent(
                    sequence=sequence,
                    kind=WorkerProgressKind.MILESTONE,
                    text="native-scope-started",
                )
            )
            sequence += 1
            emitted += 1
        buffers = {
            WorkerOutputStream.STDOUT: bytearray(),
            WorkerOutputStream.STDERR: bytearray(),
        }
        limits = {
            WorkerOutputStream.STDOUT: invocation.stdout_limit_bytes,
            WorkerOutputStream.STDERR: stderr_capture_limit,
        }
        truncated = {stream: False for stream in buffers}
        ended: set[WorkerOutputStream] = set()
        deadline = monotonic() + invocation.deadline_seconds
        timed_out = False
        cleanup_failed = False
        deadline_cleanup_stopped: bool | None = None
        next_unit_probe = monotonic()

        def record_chunk(stream: WorkerOutputStream, chunk: bytes | None) -> None:
            nonlocal emitted, sequence
            if chunk is None:
                ended.add(stream)
                return
            available = max(limits[stream] - len(buffers[stream]), 0)
            accepted = chunk[:available]
            buffers[stream].extend(accepted)
            chunk_truncated = not chunk or len(accepted) != len(chunk)
            truncated[stream] = truncated[stream] or chunk_truncated
            if (
                accepted
                and not (compound and stream is WorkerOutputStream.STDERR)
                and emitted < invocation.progress_event_limit - 1
            ):
                progress(
                    WorkerProgressEvent(
                        sequence=sequence,
                        kind=WorkerProgressKind.OUTPUT,
                        stream=stream,
                        text=accepted.decode(errors="replace"),
                        truncated=chunk_truncated,
                    )
                )
                sequence += 1
                emitted += 1

        while len(ended) < 2 or running.process.poll() is None:
            now = monotonic()
            if now >= deadline:
                timed_out = True
                deadline_cleanup_stopped = self._stop_scope(
                    running, invocation.cancellation_grace_seconds
                )
                cleanup_failed = not deadline_cleanup_stopped
                break
            if not running.unit_observed.is_set() and now >= next_unit_probe:
                self._unit_is_stopped(
                    running,
                    timeout_seconds=min(0.25, max(deadline - now, 0.001)),
                    wrapper_completed=False,
                )
                next_unit_probe = monotonic() + 0.25
            try:
                stream, chunk = output.get(timeout=0.05)
            except queue.Empty:
                continue
            record_chunk(stream, chunk)
        if timed_out:
            running.process.stdout.close()
            running.process.stderr.close()
        while True:
            try:
                stream, chunk = output.get_nowait()
            except queue.Empty:
                break
            record_chunk(stream, chunk)
        for reader in readers:
            reader.join(timeout=1)
        while True:
            try:
                stream, chunk = output.get_nowait()
            except queue.Empty:
                break
            record_chunk(stream, chunk)
        return_code = running.process.poll()
        process_tree_stopped = (
            deadline_cleanup_stopped
            if deadline_cleanup_stopped is not None
            else (
                not cleanup_failed
                and return_code is not None
                and self._unit_is_stopped(
                    running, timeout_seconds=2, wrapper_completed=True
                )
            )
        )
        status = (
            WorkerExecutionStatus.UNKNOWN
            if cleanup_failed
            else (
                WorkerExecutionStatus.TIMED_OUT
                if timed_out
                else (
                    WorkerExecutionStatus.CANCELLED
                    if running.cancel_requested.is_set()
                    else (
                        WorkerExecutionStatus.COMPLETED
                        if return_code == 0
                        else WorkerExecutionStatus.FAILED
                    )
                )
            )
        )
        started_components = (0,)
        completed_components = (0,) if status is WorkerExecutionStatus.COMPLETED else ()
        raw_stderr = bytes(buffers[WorkerOutputStream.STDERR])
        if compound:
            compound_result = _extract_compound_result(raw_stderr)
            if compound_result is None:
                if status in {
                    WorkerExecutionStatus.COMPLETED,
                    WorkerExecutionStatus.FAILED,
                }:
                    status = WorkerExecutionStatus.UNKNOWN
                raw_stderr = raw_stderr[: invocation.stderr_limit_bytes]
                started_components = ()
                completed_components = ()
            else:
                raw_stderr, started_components, completed_components = compound_result
            if raw_stderr and emitted < invocation.progress_event_limit - 1:
                progress(
                    WorkerProgressEvent(
                        sequence=sequence,
                        kind=WorkerProgressKind.OUTPUT,
                        stream=WorkerOutputStream.STDERR,
                        text=_render_captured(
                            raw_stderr,
                            truncated[WorkerOutputStream.STDERR],
                            invocation.stderr_limit_bytes,
                        ),
                        truncated=truncated[WorkerOutputStream.STDERR],
                    )
                )
        stdout = _render_captured(
            buffers[WorkerOutputStream.STDOUT],
            truncated[WorkerOutputStream.STDOUT],
            invocation.stdout_limit_bytes,
        )
        stderr = _render_captured(
            raw_stderr,
            truncated[WorkerOutputStream.STDERR],
            invocation.stderr_limit_bytes,
        )
        return WorkerExecutionResult(
            status=status,
            started_components=started_components,
            completed_components=completed_components,
            process_tree_stopped=process_tree_stopped,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=truncated[WorkerOutputStream.STDOUT],
            stderr_truncated=truncated[WorkerOutputStream.STDERR],
        )

    def _stop_scope(
        self, running: _RunningSystemdScope, timeout_seconds: float
    ) -> bool:
        deadline = monotonic() + timeout_seconds
        with running.termination_lock:
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            if self._unit_is_stopped(
                running,
                timeout_seconds=min(remaining, 1),
                wrapper_completed=running.process.poll() is not None,
            ):
                return True
            self._signal_unit(running.unit_name, "TERM", deadline)
            if running.process.poll() is None:
                remaining = max(deadline - monotonic(), 0.001)
                try:
                    running.process.wait(timeout=max(remaining / 2, 0.001))
                except subprocess.TimeoutExpired:
                    pass
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            if self._unit_is_stopped(
                running,
                timeout_seconds=min(remaining, 1),
                wrapper_completed=running.process.poll() is not None,
            ):
                return True
            self._signal_unit(running.unit_name, "KILL", deadline)
            if running.process.poll() is None:
                remaining = max(deadline - monotonic(), 0.001)
                try:
                    running.process.wait(timeout=max(remaining / 2, 0.001))
                except subprocess.TimeoutExpired:
                    return False
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            return self._unit_is_stopped(
                running,
                timeout_seconds=remaining,
                wrapper_completed=running.process.poll() is not None,
            )

    def _signal_unit(self, unit_name: str, signal: str, deadline: float) -> None:
        remaining = max(deadline - monotonic(), 0.001)
        try:
            subprocess.run(
                (
                    self._systemctl_path,
                    "--user",
                    "kill",
                    "--kill-whom=all",
                    f"--signal={signal}",
                    unit_name,
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=remaining,
            )
        except (OSError, subprocess.TimeoutExpired):
            return

    def _unit_is_stopped(
        self,
        running: _RunningSystemdScope,
        *,
        timeout_seconds: float,
        wrapper_completed: bool = False,
    ) -> bool:
        if timeout_seconds <= 0:
            return False
        deadline = monotonic() + timeout_seconds
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            try:
                check = subprocess.run(
                    (
                        self._systemctl_path,
                        "--user",
                        "is-active",
                        running.unit_name,
                    ),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=min(remaining, 0.25),
                    text=True,
                )
            except subprocess.TimeoutExpired:
                continue
            except OSError:
                return False
            state = check.stdout.strip()
            if state in {"active", "activating", "deactivating", "inactive", "failed"}:
                running.unit_observed.set()
            if check.returncode == 3 and state in {"inactive", "failed"}:
                return True
            if (
                wrapper_completed
                and check.returncode == 4
                and state in {"inactive", "unknown"}
            ):
                return True
            if state not in {"active", "activating", "deactivating"}:
                return False
            sleep(min(0.05, max(deadline - monotonic(), 0)))

    @staticmethod
    def _unit_name(action_id: str) -> str:
        digest = hashlib.sha256(action_id.encode()).hexdigest()[:24]
        return f"jarvis-action-{digest}.service"


class _ActionState(str, Enum):
    RESERVED = "reserved"
    RUNNING = "running"
    CANCELLED = "cancelled"
    TERMINAL = "terminal"
    FINALIZED = "finalized"


def _extract_compound_result(
    captured: bytes,
) -> tuple[bytes, tuple[int, ...], tuple[int, ...]] | None:
    user_output, marker, suffix = captured.rpartition(COMPOUND_RESULT_MARKER)
    if not marker:
        return None
    metadata, separator, trailing = suffix.partition(b"\n")
    if not separator or trailing:
        return None
    try:
        value = json.loads(metadata)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or set(value) != {"started", "completed"}:
        return None
    started = value["started"]
    completed = value["completed"]
    if not isinstance(started, list) or not isinstance(completed, list):
        return None
    if any(
        isinstance(index, bool) or not isinstance(index, int) or index < 0
        for index in (*started, *completed)
    ):
        return None
    started_tuple = tuple(started)
    completed_tuple = tuple(completed)
    if (
        tuple(sorted(set(started_tuple))) != started_tuple
        or tuple(sorted(set(completed_tuple))) != completed_tuple
        or not set(completed_tuple) <= set(started_tuple)
    ):
        return None
    return user_output, started_tuple, completed_tuple


@dataclass(slots=True)
class _ActionRecord:
    state: _ActionState
    retention_seconds: int
    expires_at: float | None = None
    finalize_requested: bool = False


class UbuntuWorkerService:
    """Worker-side service core reached only through the local wire adapter."""

    def __init__(
        self,
        *,
        worker_id: str,
        expected_peer: UbuntuLocalPeerExpectation,
        authenticator: UbuntuLocalAuthenticator,
        readiness: Callable[[], UbuntuWorkerReadiness],
        process_scope: UbuntuProcessScope,
        limits: WorkerExecutionLimits | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("Ubuntu worker identifier must be non-blank")
        self._worker_id = worker_id.strip()
        self._expected_peer = expected_peer
        self._authenticator = authenticator
        self._readiness = readiness
        self._process_scope = process_scope
        self._limits = limits or WorkerExecutionLimits()
        self._clock = clock
        self._lock = RLock()
        self._actions: dict[str, _ActionRecord] = {}
        self._active_action_id: str | None = None
        self._authenticated_identity: WorkerIdentity | None = None

    def binds(self, connection: socket.socket) -> bool:
        """Return whether OS identity checks use this exact accepted channel."""

        return self._authenticator.binds(connection)

    def register_execution(
        self,
        *,
        action_id: str,
        timeout_seconds: int,
        retention_seconds: int,
    ) -> None:
        del timeout_seconds  # There is no remote registration hop.
        self._validate_retention(retention_seconds)
        with self._lock:
            self._prune_expired_locked()
            if action_id in self._actions:
                raise ActionDispatcherError(
                    f"Ubuntu worker action {action_id} is already registered",
                    may_have_dispatched=True,
                )
            self._process_scope.reserve(action_id=action_id)
            try:
                self._actions[action_id] = _ActionRecord(
                    state=_ActionState.RESERVED,
                    retention_seconds=retention_seconds,
                    expires_at=self._clock() + retention_seconds,
                )
            except BaseException:
                self._process_scope.retire(action_id=action_id)
                raise

    def authenticate(
        self, *, selected_host: str, timeout_seconds: int
    ) -> WorkerIdentity:
        if selected_host != "ubuntu":
            raise ActionDispatcherError("native Ubuntu worker is bound only to ubuntu")
        readiness = UbuntuWorkerReadiness(self._readiness())
        if readiness is not UbuntuWorkerReadiness.READY:
            raise ActionDispatcherError(f"native Ubuntu worker is {readiness.value}")
        identity = self._authenticate_local_identity(timeout_seconds=timeout_seconds)
        with self._lock:
            self._authenticated_identity = identity
        return identity

    def _authenticate_local_identity(self, *, timeout_seconds: int) -> WorkerIdentity:
        peer = self._authenticator.authenticate(timeout_seconds=timeout_seconds)
        if not self._expected_peer.matches(peer):
            raise ActionDispatcherError(
                "native Ubuntu worker local peer identity failed"
            )
        return WorkerIdentity(
            host="ubuntu",
            worker_id=self._worker_id,
            connection_id=peer.connection_id,
        )

    def execute(
        self, invocation: WorkerInvocation, progress: WorkerProgressSink
    ) -> WorkerExecutionResult:
        with self._lock:
            self._prune_expired_locked()
            record = self._actions.get(invocation.action_id)
            state = record.state if record is not None else None
            if state is _ActionState.CANCELLED:
                return WorkerExecutionResult(
                    status=WorkerExecutionStatus.CANCELLED,
                    process_tree_stopped=True,
                )
            if state is not _ActionState.RESERVED:
                raise ActionDispatcherError(
                    f"Ubuntu worker action {invocation.action_id} is not executable",
                    may_have_dispatched=state
                    in {_ActionState.RUNNING, _ActionState.TERMINAL},
                )
            if self._active_action_id is not None:
                raise ActionDispatcherError("native Ubuntu worker is busy")
            self._validate_invocation_locked(invocation)
            actual_identity = self._authenticate_local_identity(
                timeout_seconds=min(invocation.deadline_seconds, 10)
            )
            if actual_identity != invocation.worker_identity:
                raise ActionDispatcherError(
                    "native Ubuntu worker connection identity changed"
                )
            assert record is not None
            record.state = _ActionState.RUNNING
            record.expires_at = None
            self._active_action_id = invocation.action_id
        try:
            result = self._process_scope.execute(invocation, progress)
            return _bound_result(result, invocation)
        finally:
            with self._lock:
                record = self._actions.get(invocation.action_id)
                if record is not None:
                    record.state = (
                        _ActionState.FINALIZED
                        if record.finalize_requested
                        else _ActionState.TERMINAL
                    )
                    record.expires_at = self._clock() + record.retention_seconds
                if self._active_action_id == invocation.action_id:
                    self._active_action_id = None

    def cancel(
        self,
        *,
        action_id: str,
        timeout_seconds: int,
        retention_seconds: int,
    ) -> ActionCancellationResult:
        self._validate_retention(retention_seconds)
        with self._lock:
            self._prune_expired_locked()
            record = self._actions.get(action_id)
            state = record.state if record is not None else None
            if state in {None, _ActionState.RESERVED, _ActionState.CANCELLED}:
                if record is None:
                    record = _ActionRecord(
                        state=_ActionState.CANCELLED,
                        retention_seconds=retention_seconds,
                    )
                    self._actions[action_id] = record
                else:
                    record.state = _ActionState.CANCELLED
                record.expires_at = self._clock() + record.retention_seconds
                scope_result = self._process_scope.cancel(
                    action_id=action_id, timeout_seconds=timeout_seconds
                )
                if scope_result.status not in {
                    ActionCancellationStatus.NOT_STARTED,
                    ActionCancellationStatus.UNKNOWN,
                }:
                    raise ActionDispatcherError(
                        "Ubuntu process scope returned invalid pre-start cancellation"
                    )
                return ActionCancellationResult(ActionCancellationStatus.NOT_STARTED)
            if state in {_ActionState.TERMINAL, _ActionState.FINALIZED}:
                return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
        result = self._process_scope.cancel(
            action_id=action_id, timeout_seconds=timeout_seconds
        )
        if not isinstance(result, ActionCancellationResult):
            raise TypeError("Ubuntu process scope returned an invalid cancellation")
        return result

    def finalize_execution(
        self,
        *,
        action_id: str,
        timeout_seconds: int,
        retention_seconds: int,
    ) -> None:
        del timeout_seconds
        self._validate_retention(retention_seconds)
        with self._lock:
            self._prune_expired_locked()
            record = self._actions.get(action_id)
            if record is None:
                record = _ActionRecord(
                    state=_ActionState.FINALIZED,
                    retention_seconds=retention_seconds,
                )
                self._actions[action_id] = record
            elif record.state is _ActionState.RUNNING:
                record.finalize_requested = True
            else:
                record.state = _ActionState.FINALIZED
                record.expires_at = self._clock() + record.retention_seconds
            self._process_scope.retire(action_id=action_id)

    def _validate_invocation_locked(self, invocation: WorkerInvocation) -> None:
        if invocation.action.host != "ubuntu":
            raise ActionDispatcherError("native Ubuntu worker rejected another host")
        if invocation.interactive:
            raise ActionDispatcherError("native Ubuntu worker is non-interactive")
        bounded_values = (
            (invocation.deadline_seconds, self._limits.deadline_seconds),
            (invocation.stdout_limit_bytes, self._limits.stdout_limit_bytes),
            (invocation.stderr_limit_bytes, self._limits.stderr_limit_bytes),
            (
                invocation.cancellation_grace_seconds,
                self._limits.cancellation_grace_seconds,
            ),
            (invocation.progress_event_limit, self._limits.progress_event_limit),
            (invocation.milestone_limit_bytes, self._limits.milestone_limit_bytes),
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 1
            or value > maximum
            for value, maximum in bounded_values
        ):
            raise ActionDispatcherError("native Ubuntu worker limits are invalid")
        if self._readiness() is not UbuntuWorkerReadiness.READY:
            raise ActionDispatcherError("native Ubuntu worker is not ready")
        if invocation.worker_identity != self._authenticated_identity:
            raise ActionDispatcherError(
                "native Ubuntu worker connection identity changed"
            )

    def _prune_expired_locked(self) -> None:
        now = self._clock()
        for action_id, record in tuple(self._actions.items()):
            if record.expires_at is not None and record.expires_at <= now:
                del self._actions[action_id]
                self._process_scope.retire(action_id=action_id)

    def _validate_retention(self, retention_seconds: int) -> None:
        if (
            isinstance(retention_seconds, bool)
            or not isinstance(retention_seconds, int)
            or retention_seconds < 1
            or retention_seconds > self._limits.action_state_retention_seconds
        ):
            raise ValueError(
                "Ubuntu worker retention must be between one second and the "
                "configured maximum"
            )


def _bound_result(
    result: WorkerExecutionResult, invocation: WorkerInvocation
) -> WorkerExecutionResult:
    if not isinstance(result, WorkerExecutionResult):
        raise TypeError("Ubuntu process scope returned an invalid result")
    stdout_overflow = len(result.stdout.encode()) > invocation.stdout_limit_bytes
    stderr_overflow = len(result.stderr.encode()) > invocation.stderr_limit_bytes
    stdout = _truncate_utf8(result.stdout, invocation.stdout_limit_bytes)
    stderr = _truncate_utf8(result.stderr, invocation.stderr_limit_bytes)
    return WorkerExecutionResult(
        status=result.status,
        started_components=result.started_components,
        completed_components=result.completed_components,
        process_tree_stopped=result.process_tree_stopped,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=result.stdout_truncated or stdout_overflow,
        stderr_truncated=result.stderr_truncated or stderr_overflow,
    )


def _truncate_utf8(value: str, limit: int) -> str:
    encoded = value.encode()
    if len(encoded) <= limit:
        return value
    marker = "\n[output truncated]"
    room = max(limit - len(marker.encode()), 0)
    prefix = encoded[:room]
    while prefix:
        try:
            return prefix.decode() + marker
        except UnicodeDecodeError:
            prefix = prefix[:-1]
    return marker.encode()[:limit].decode(errors="ignore")


def _render_captured(value: bytes | bytearray, truncated: bool, limit: int) -> str:
    text = bytes(value).decode(errors="replace")
    return _truncate_utf8(f"{text}\n[output truncated]", limit) if truncated else text

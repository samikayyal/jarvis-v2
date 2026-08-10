"""Unactivated native Ubuntu worker contract.

The worker is deliberately a transport adapter, not a policy authority.  It
accepts only gateway-approved :class:`WorkerInvocation` values over an
authenticated, permission-restricted local Unix-socket channel.
"""

from __future__ import annotations

import hashlib
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
from pathlib import PurePosixPath
from threading import Event, RLock, Thread
from time import monotonic
from typing import Protocol

from .ports import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ActionDispatcherError,
)
from .worker_gateway import (
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerIdentity,
    WorkerInvocation,
    WorkerOutputStream,
    WorkerProgressEvent,
    WorkerProgressKind,
    WorkerProgressSink,
)


class UbuntuWorkerReadiness(str, Enum):
    """The native worker states relevant at the dispatch barrier."""

    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


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


class ControlledUbuntuLocalAuthenticator:
    """Deterministic local-channel identity provider for contract tests."""

    def __init__(self, identity: UbuntuLocalPeerIdentity) -> None:
        self.identity = identity

    def authenticate(self, *, timeout_seconds: int) -> UbuntuLocalPeerIdentity:
        if isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= 30:
            raise ValueError("Ubuntu local authentication timeout is invalid")
        return self.identity


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
        if connection.family != socket.AF_UNIX:
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

    def authenticate(self, *, timeout_seconds: int) -> UbuntuLocalPeerIdentity:
        if isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= 30:
            raise ValueError("Ubuntu local authentication timeout is invalid")
        if not hasattr(socket, "SO_PEERCRED"):
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
            local_path = self._connection.getsockname()
            if not isinstance(local_path, str) or not os.path.samefile(
                local_path, self._socket_path
            ):
                raise ActionDispatcherError("Ubuntu worker socket identity changed")
            raw = self._connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
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

    def execute(
        self, invocation: WorkerInvocation, progress: WorkerProgressSink
    ) -> WorkerExecutionResult:
        self.invocations.append(invocation)
        for event in self.progress_events:
            progress(event)
        if self.execution_hook is not None:
            return self.execution_hook(invocation)
        return self.result

    def cancel(
        self, *, action_id: str, timeout_seconds: int
    ) -> ActionCancellationResult:
        self.cancellations.append((action_id, timeout_seconds))
        if self.cancellation_hook is not None:
            return self.cancellation_hook(action_id, timeout_seconds)
        return ActionCancellationResult(ActionCancellationStatus.STOPPED)


@dataclass(slots=True)
class _RunningSystemdScope:
    unit_name: str
    process: subprocess.Popen[bytes]
    cancel_requested: Event
    termination_lock: RLock


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
        self._lock = RLock()
        self._running: dict[str, _RunningSystemdScope] = {}
        self._active_action_ids: set[str] = set()

    def command_for(self, invocation: WorkerInvocation) -> tuple[str, ...]:
        """Return the exact argv used to create the bounded native scope."""

        if invocation.interactive:
            raise ActionDispatcherError("Ubuntu process scopes are non-interactive")
        if invocation.action.host != "ubuntu":
            raise ActionDispatcherError("Ubuntu process scope rejected another host")
        if len(invocation.action.components) != 1:
            raise ActionDispatcherError(
                "Ubuntu process scope accepts one process component"
            )
        component = invocation.action.components[0]
        if component.redirections:
            raise ActionDispatcherError(
                "Ubuntu process scope does not interpret shell redirections"
            )
        unit_name = self._unit_name(invocation.action_id)
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
            f"--property=TimeoutStopSec={invocation.cancellation_grace_seconds}s",
            f"--working-directory={invocation.action.cwd}",
            "--",
            component.executable,
            *component.arguments,
        )

    def execute(
        self, invocation: WorkerInvocation, progress: WorkerProgressSink
    ) -> WorkerExecutionResult:
        if sys.platform != "linux":
            raise ActionDispatcherError("native Ubuntu process scope requires Linux")
        command = self.command_for(invocation)
        unit_name = self._unit_name(invocation.action_id)
        with self._lock:
            if invocation.action_id in self._active_action_ids:
                raise ActionDispatcherError(
                    f"Ubuntu process scope {invocation.action_id} is already active",
                    may_have_dispatched=True,
                )
            self._active_action_ids.add(invocation.action_id)
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
            raise ActionDispatcherError(
                "native Ubuntu process scope could not start"
            ) from exc
        running = _RunningSystemdScope(
            unit_name=unit_name,
            process=process,
            cancel_requested=Event(),
            termination_lock=RLock(),
        )
        with self._lock:
            self._running[invocation.action_id] = running
        try:
            return self._observe(running, invocation, progress)
        finally:
            with self._lock:
                if (
                    self._running.get(invocation.action_id) is running
                    and running.process.poll() is not None
                ):
                    del self._running[invocation.action_id]
                    self._active_action_ids.discard(invocation.action_id)

    def cancel(
        self, *, action_id: str, timeout_seconds: int
    ) -> ActionCancellationResult:
        with self._lock:
            running = self._running.get(action_id)
        if running is None:
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
        running.cancel_requested.set()
        stopped = self._stop_scope(running, timeout_seconds)
        if stopped:
            with self._lock:
                if self._running.get(action_id) is running:
                    del self._running[action_id]
                self._active_action_ids.discard(action_id)
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
        output: queue.Queue[tuple[WorkerOutputStream, bytes | None]] = queue.Queue()

        def read_stream(stream: object, tag: WorkerOutputStream) -> None:
            assert hasattr(stream, "read")
            read = getattr(stream, "read1", stream.read)
            try:
                while chunk := read(16 * 1024):
                    output.put((tag, chunk))
            finally:
                output.put((tag, None))

        assert running.process.stdout is not None
        assert running.process.stderr is not None
        readers = (
            Thread(
                target=read_stream,
                args=(running.process.stdout, WorkerOutputStream.STDOUT),
                daemon=True,
            ),
            Thread(
                target=read_stream,
                args=(running.process.stderr, WorkerOutputStream.STDERR),
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
            WorkerOutputStream.STDERR: invocation.stderr_limit_bytes,
        }
        truncated = {stream: False for stream in buffers}
        ended: set[WorkerOutputStream] = set()
        deadline = monotonic() + invocation.deadline_seconds
        timed_out = False
        cleanup_failed = False
        while len(ended) < 2 or running.process.poll() is None:
            if monotonic() >= deadline and running.process.poll() is None:
                timed_out = True
                if not self._stop_scope(running, invocation.cancellation_grace_seconds):
                    cleanup_failed = True
                    break
            try:
                stream, chunk = output.get(timeout=0.05)
            except queue.Empty:
                continue
            if chunk is None:
                ended.add(stream)
                continue
            available = max(limits[stream] - len(buffers[stream]), 0)
            accepted = chunk[:available]
            buffers[stream].extend(accepted)
            chunk_truncated = len(accepted) != len(chunk)
            truncated[stream] = truncated[stream] or chunk_truncated
            if accepted and emitted < invocation.progress_event_limit - 1:
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
        for reader in readers:
            reader.join(timeout=1)
        return_code = running.process.poll()
        process_tree_stopped = (
            not cleanup_failed
            and return_code is not None
            and self._unit_is_stopped(running.unit_name, timeout_seconds=2)
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
        stdout = _render_captured(
            buffers[WorkerOutputStream.STDOUT],
            truncated[WorkerOutputStream.STDOUT],
            invocation.stdout_limit_bytes,
        )
        stderr = _render_captured(
            buffers[WorkerOutputStream.STDERR],
            truncated[WorkerOutputStream.STDERR],
            invocation.stderr_limit_bytes,
        )
        return WorkerExecutionResult(
            status=status,
            started_components=(0,),
            completed_components=(0,)
            if status is WorkerExecutionStatus.COMPLETED
            else (),
            process_tree_stopped=process_tree_stopped,
            stdout=stdout,
            stderr=stderr,
        )

    def _stop_scope(self, running: _RunningSystemdScope, timeout_seconds: int) -> bool:
        deadline = monotonic() + timeout_seconds
        with running.termination_lock:
            if running.process.poll() is None:
                self._signal_unit(running.unit_name, "TERM", deadline)
                remaining = max(deadline - monotonic(), 0.001)
                try:
                    running.process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    self._signal_unit(running.unit_name, "KILL", deadline)
                    remaining = max(deadline - monotonic(), 0.001)
                    try:
                        running.process.wait(timeout=remaining)
                    except subprocess.TimeoutExpired:
                        return False
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            return self._unit_is_stopped(running.unit_name, timeout_seconds=remaining)

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

    def _unit_is_stopped(self, unit_name: str, *, timeout_seconds: float) -> bool:
        if timeout_seconds <= 0:
            return False
        try:
            check = subprocess.run(
                (
                    self._systemctl_path,
                    "--user",
                    "is-active",
                    "--quiet",
                    unit_name,
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return check.returncode != 0

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


@dataclass(slots=True)
class _ActionRecord:
    state: _ActionState
    retention_seconds: int
    expires_at: float | None = None
    finalize_requested: bool = False


class UbuntuWorkerTransport:
    """Host-bound native Ubuntu worker implementing the shared transport seam."""

    def __init__(
        self,
        *,
        worker_id: str,
        expected_peer_uid: int,
        expected_socket_owner_uid: int,
        expected_socket_path: str,
        authenticator: UbuntuLocalAuthenticator,
        readiness: Callable[[], UbuntuWorkerReadiness],
        process_scope: UbuntuProcessScope,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("Ubuntu worker identifier must be non-blank")
        for name, value in (
            ("peer", expected_peer_uid),
            ("socket owner", expected_socket_owner_uid),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"expected Ubuntu {name} UID is invalid")
        self._worker_id = worker_id.strip()
        self._expected_peer_uid = expected_peer_uid
        self._expected_socket_owner_uid = expected_socket_owner_uid
        if not PurePosixPath(expected_socket_path).is_absolute():
            raise ValueError("expected Ubuntu worker socket path must be absolute")
        if str(PurePosixPath(expected_socket_path)) != expected_socket_path:
            raise ValueError("expected Ubuntu worker socket path must be canonical")
        self._expected_socket_path = expected_socket_path
        self._authenticator = authenticator
        self._readiness = readiness
        self._process_scope = process_scope
        self._clock = clock
        self._lock = RLock()
        self._actions: dict[str, _ActionRecord] = {}
        self._active_action_id: str | None = None
        self._authenticated_identity: WorkerIdentity | None = None

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
            self._actions[action_id] = _ActionRecord(
                state=_ActionState.RESERVED,
                retention_seconds=retention_seconds,
            )

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
        if (
            peer.peer_uid != self._expected_peer_uid
            or peer.socket_owner_uid != self._expected_socket_owner_uid
            or peer.socket_mode != 0o600
            or peer.socket_path != self._expected_socket_path
        ):
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

    def _validate_invocation_locked(self, invocation: WorkerInvocation) -> None:
        if invocation.action.host != "ubuntu":
            raise ActionDispatcherError("native Ubuntu worker rejected another host")
        if invocation.interactive:
            raise ActionDispatcherError("native Ubuntu worker is non-interactive")
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

    @staticmethod
    def _validate_retention(retention_seconds: int) -> None:
        if (
            isinstance(retention_seconds, bool)
            or not isinstance(retention_seconds, int)
            or retention_seconds < 1
        ):
            raise ValueError("Ubuntu worker retention must be positive")


def _bound_result(
    result: WorkerExecutionResult, invocation: WorkerInvocation
) -> WorkerExecutionResult:
    if not isinstance(result, WorkerExecutionResult):
        raise TypeError("Ubuntu process scope returned an invalid result")
    stdout = _truncate_utf8(result.stdout, invocation.stdout_limit_bytes)
    stderr = _truncate_utf8(result.stderr, invocation.stderr_limit_bytes)
    return WorkerExecutionResult(
        status=result.status,
        started_components=result.started_components,
        completed_components=result.completed_components,
        process_tree_stopped=result.process_tree_stopped,
        stdout=stdout,
        stderr=stderr,
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


def _render_captured(value: bytearray, truncated: bool, limit: int) -> str:
    text = bytes(value).decode(errors="replace")
    return _truncate_utf8(f"{text}\n[output truncated]", limit) if truncated else text

"""Unactivated outbound Windows-worker and Job Object contract.

The capability broker still owns policy and dispatch admission.  This adapter
only makes one registered Windows worker available after an outbound session
has proven both its mTLS certificate identity and its closed application
identity.  Session epochs fence disconnect/reconnect races so work is never
queued for, failed over to, or inherited by a replacement connection.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from threading import RLock, Thread
from time import monotonic, sleep
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

_CREATE_SUSPENDED = 0x00000004
_AUTHENTICATED_EVIDENCE_TOKEN = object()
_APPLICATION_HELLO_LIMIT_BYTES = 4096


@dataclass(frozen=True, slots=True)
class WindowsMtlsClientConfig:
    """Files and private-overlay endpoint for the outbound worker connection."""

    overlay_host: str
    overlay_port: int
    server_name: str
    ca_file: Path
    certificate_file: Path
    private_key_file: Path

    def __post_init__(self) -> None:
        for name in ("overlay_host", "server_name"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Windows mTLS {name.replace('_', ' ')} is required")
            if value != value.strip():
                raise ValueError(
                    f"Windows mTLS {name.replace('_', ' ')} must be canonical"
                )
        if (
            isinstance(self.overlay_port, bool)
            or not isinstance(self.overlay_port, int)
            or not 1 <= self.overlay_port <= 65535
        ):
            raise ValueError("Windows mTLS overlay port is invalid")
        for name in ("ca_file", "certificate_file", "private_key_file"):
            value = Path(getattr(self, name))
            if not value.is_absolute():
                raise ValueError(
                    f"Windows mTLS {name.replace('_', ' ')} must be absolute"
                )
            object.__setattr__(self, name, value)


def open_windows_worker_mtls_session(
    config: WindowsMtlsClientConfig, *, timeout_seconds: int = 10
) -> ssl.SSLSocket:
    """Initiate the worker's outbound TLS 1.3 connection.

    This helper is deliberately not called during import or application start.
    Service activation, credential provisioning, overlay routing, and retry
    supervision remain manual deployment concerns.
    """

    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 1 <= timeout_seconds <= 30
    ):
        raise ValueError("Windows mTLS timeout must be between one and 30 seconds")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    context.load_verify_locations(cafile=str(config.ca_file))
    context.load_cert_chain(
        certfile=str(config.certificate_file), keyfile=str(config.private_key_file)
    )
    raw = socket.create_connection(
        (config.overlay_host, config.overlay_port), timeout=timeout_seconds
    )
    try:
        return context.wrap_socket(raw, server_hostname=config.server_name)
    except BaseException:
        raw.close()
        raise


@dataclass(frozen=True, slots=True)
class WindowsWorkerRegistration:
    """Administrative identity registered for the one Windows worker."""

    identity: WorkerIdentity
    certificate_identity: str
    application_identity: str

    def __post_init__(self) -> None:
        if self.identity.host != "windows":
            raise ValueError("Windows worker registration must use the windows host")
        for name in ("certificate_identity", "application_identity"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Windows worker {name.replace('_', ' ')} is required")
            if value != value.strip():
                raise ValueError(
                    f"Windows worker {name.replace('_', ' ')} must be canonical"
                )


@dataclass(frozen=True, slots=True, init=False)
class WindowsWorkerSessionEvidence:
    """Identity evidence from one authenticated outbound mTLS session.

    ``certificate_identity`` is populated only from the authenticated peer
    certificate. ``application_identity`` and the remaining fields come from
    the worker's closed application handshake.  Keeping the sources explicit
    prevents either identity from substituting for the other.
    """

    host: str
    worker_id: str
    connection_id: str
    certificate_identity: str
    application_identity: str
    _authenticated: bool

    def __init__(
        self,
        *,
        host: str,
        worker_id: str,
        connection_id: str,
        certificate_identity: str,
        application_identity: str,
        _token: object | None = None,
    ) -> None:
        object.__setattr__(self, "host", host)
        object.__setattr__(self, "worker_id", worker_id)
        object.__setattr__(self, "connection_id", connection_id)
        object.__setattr__(self, "certificate_identity", certificate_identity)
        object.__setattr__(self, "application_identity", application_identity)
        object.__setattr__(
            self, "_authenticated", _token is _AUTHENTICATED_EVIDENCE_TOKEN
        )
        self.__post_init__()

    def __post_init__(self) -> None:
        for name in (
            "host",
            "worker_id",
            "connection_id",
            "certificate_identity",
            "application_identity",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"Windows session {name.replace('_', ' ')} is required"
                )
            if value != value.strip():
                raise ValueError(
                    f"Windows session {name.replace('_', ' ')} must be canonical"
                )

    @property
    def worker_identity(self) -> WorkerIdentity:
        return WorkerIdentity(
            host=self.host,
            worker_id=self.worker_id,
            connection_id=self.connection_id,
        )

    @property
    def authenticated(self) -> bool:
        """Whether evidence came from the mTLS and closed-handshake factory."""

        return self._authenticated


def authenticate_windows_worker_session(
    *,
    registration: WindowsWorkerRegistration,
    tls_socket: ssl.SSLSocket,
    application_hello: bytes,
) -> WindowsWorkerSessionEvidence:
    """Bind TLS peer-certificate evidence to one closed application hello."""

    if tls_socket.version() != "TLSv1.3":
        raise ActionDispatcherError("Windows worker session did not negotiate TLS 1.3")
    peer = tls_socket.getpeercert()
    if not isinstance(peer, dict):
        raise ActionDispatcherError("Windows worker peer certificate is unavailable")
    subject_alt_names = peer.get("subjectAltName", ())
    certificate_identities = {
        value
        for kind, value in subject_alt_names
        if kind == "URI" and isinstance(value, str)
    }
    if certificate_identities != {registration.certificate_identity}:
        raise ActionDispatcherError("Windows worker certificate identity mismatch")
    if not isinstance(application_hello, bytes) or not application_hello:
        raise ActionDispatcherError("Windows worker application hello is invalid")
    if len(application_hello) > _APPLICATION_HELLO_LIMIT_BYTES:
        raise ActionDispatcherError("Windows worker application hello is oversized")
    try:
        hello = json.loads(application_hello.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ActionDispatcherError(
            "Windows worker application hello is malformed"
        ) from exc
    required = {"host", "worker_id", "connection_id", "application_identity"}
    if not isinstance(hello, dict) or set(hello) != required:
        raise ActionDispatcherError("Windows worker application hello schema mismatch")
    if any(not isinstance(hello[key], str) for key in required):
        raise ActionDispatcherError("Windows worker application hello schema mismatch")
    evidence = WindowsWorkerSessionEvidence(
        host=hello["host"],
        worker_id=hello["worker_id"],
        connection_id=hello["connection_id"],
        certificate_identity=registration.certificate_identity,
        application_identity=hello["application_identity"],
        _token=_AUTHENTICATED_EVIDENCE_TOKEN,
    )
    if (
        evidence.worker_identity != registration.identity
        or evidence.application_identity != registration.application_identity
    ):
        raise ActionDispatcherError("Windows worker application identity mismatch")
    return evidence


class WindowsWorkerSession(Protocol):
    """Accepted outbound session backed by a Windows Job Object worker."""

    @property
    def evidence(self) -> WindowsWorkerSessionEvidence: ...

    def execute(
        self, invocation: WorkerInvocation, progress: WorkerProgressSink
    ) -> WorkerExecutionResult: ...

    def terminate_job_object(self, *, action_id: str, timeout_seconds: int) -> bool: ...

    def finalize(self, *, action_id: str, timeout_seconds: int) -> None: ...


class WindowsJobObjectExecutor(Protocol):
    """Native worker seam that owns one complete Windows process tree.

    A production implementation must create and assign the process to a Job
    Object before allowing it to execute, apply the invocation's deadline and
    output bounds internally, keep standard input closed, and return ``True``
    from ``terminate`` only after the entire Job Object is stopped.
    """

    def execute(
        self, invocation: WorkerInvocation, progress: WorkerProgressSink
    ) -> WorkerExecutionResult: ...

    def terminate(self, *, action_id: str, timeout_seconds: int) -> bool: ...

    def finalize(self, *, action_id: str, timeout_seconds: int) -> None: ...


class WindowsJobObjectWorkerSession:
    """Production-shaped session that delegates only to a Job Object executor."""

    def __init__(
        self,
        *,
        evidence: WindowsWorkerSessionEvidence,
        executor: WindowsJobObjectExecutor,
    ) -> None:
        self._evidence = evidence
        self._executor = executor

    @property
    def evidence(self) -> WindowsWorkerSessionEvidence:
        return self._evidence

    def execute(
        self, invocation: WorkerInvocation, progress: WorkerProgressSink
    ) -> WorkerExecutionResult:
        if invocation.interactive:
            raise ActionDispatcherError(
                "Windows worker execution must be non-interactive"
            )
        return self._executor.execute(invocation, progress)

    def terminate_job_object(self, *, action_id: str, timeout_seconds: int) -> bool:
        return self._executor.terminate(
            action_id=action_id, timeout_seconds=timeout_seconds
        )

    def finalize(self, *, action_id: str, timeout_seconds: int) -> None:
        self._executor.finalize(action_id=action_id, timeout_seconds=timeout_seconds)


@dataclass(slots=True)
class _RunningWindowsJob:
    process: subprocess.Popen[bytes] | None
    job_handle: int
    cancel_requested: bool = False


class SubprocessWindowsJobObjectExecutor:
    """Native, non-interactive single-process Windows Job Object executor.

    The child starts suspended, is assigned to a kill-on-close Job Object with
    the V1 process-count bound, and is resumed only after assignment succeeds.
    Output readers retain at most the invocation limits.  A terminal result is
    definite only after the Job Object reports that its complete process tree
    has stopped.

    Compound terminal actions are rejected before process creation.  They need
    a separate structured pipeline/redirection executor; invoking ``cmd.exe``
    here would silently expand the already-authorized command identity.
    """

    def __init__(self, *, process_limit: int = 32) -> None:
        if (
            isinstance(process_limit, bool)
            or not isinstance(process_limit, int)
            or not 1 <= process_limit <= 64
        ):
            raise ValueError("Windows Job Object process limit must be from one to 64")
        self.process_limit = process_limit
        self._lock = RLock()
        self._running: dict[str, _RunningWindowsJob] = {}

    def execute(
        self, invocation: WorkerInvocation, progress: WorkerProgressSink
    ) -> WorkerExecutionResult:
        if os.name != "nt":
            raise ActionDispatcherError("Windows Job Object execution requires Windows")
        if invocation.interactive:
            raise ActionDispatcherError(
                "Windows Job Object execution is non-interactive"
            )
        if any(
            component.operator_before == "|" or component.redirections
            for component in invocation.action.components
        ):
            raise ActionDispatcherError(
                "Windows Job Object executor cannot preserve pipeline or redirection semantics"
            )
        with self._lock:
            if self._running:
                raise ActionDispatcherError(
                    "Windows Job Object executor already has an action in progress"
                )
            if invocation.action_id in self._running:
                raise ActionDispatcherError(
                    f"Windows Job Object action {invocation.action_id} already started",
                    may_have_dispatched=True,
                )

        job_handle = self._create_job_object()
        record = _RunningWindowsJob(process=None, job_handle=job_handle)
        with self._lock:
            self._running[invocation.action_id] = record
        process: subprocess.Popen[bytes] | None = None
        assigned_to_job = False
        stdout_parts: list[bytes] = []
        stderr_parts: list[bytes] = []
        stdout_truncated = [False]
        stderr_truncated = [False]
        started_components: list[int] = []
        completed_components: list[int] = []
        timed_out = False
        return_code: int | None = None
        deadline = monotonic() + invocation.deadline_seconds
        try:
            flags = (
                subprocess.CREATE_NO_WINDOW
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | _CREATE_SUSPENDED
            )
            for index, component in enumerate(invocation.action.components):
                if index and component.operator_before == "&&" and return_code != 0:
                    continue
                if index and component.operator_before == "||" and return_code == 0:
                    continue
                with self._lock:
                    if record.cancel_requested:
                        break
                remaining = deadline - monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                assigned_to_job = False
                process = subprocess.Popen(
                    [component.executable, *component.arguments],
                    cwd=invocation.action.cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=flags,
                )
                record.process = process
                self._assign_process(job_handle, process)
                assigned_to_job = True
                self._resume_process(process)
                started_components.append(index)

                assert process.stdout is not None
                assert process.stderr is not None
                stdout_reader = Thread(
                    target=self._read_bounded,
                    args=(
                        process.stdout,
                        max(
                            invocation.stdout_limit_bytes
                            - sum(len(part) for part in stdout_parts),
                            0,
                        ),
                        stdout_parts,
                        stdout_truncated,
                    ),
                    daemon=True,
                )
                stderr_reader = Thread(
                    target=self._read_bounded,
                    args=(
                        process.stderr,
                        max(
                            invocation.stderr_limit_bytes
                            - sum(len(part) for part in stderr_parts),
                            0,
                        ),
                        stderr_parts,
                        stderr_truncated,
                    ),
                    daemon=True,
                )
                stdout_reader.start()
                stderr_reader.start()
                try:
                    return_code = process.wait(timeout=max(deadline - monotonic(), 0))
                except subprocess.TimeoutExpired:
                    timed_out = True
                    self._terminate_and_wait(
                        job_handle, invocation.cancellation_grace_seconds
                    )
                    return_code = process.poll()
                stdout_reader.join(timeout=invocation.cancellation_grace_seconds)
                stderr_reader.join(timeout=invocation.cancellation_grace_seconds)
                if stdout_reader.is_alive() or stderr_reader.is_alive():
                    timed_out = True
                if return_code == 0:
                    completed_components.append(index)
                if timed_out:
                    break

            # A successful root process may have left descendants running.
            # Terminating the Job Object closes that ambiguity before result.
            stopped = self._terminate_and_wait(
                job_handle, invocation.cancellation_grace_seconds
            )

            stdout = b"".join(stdout_parts).decode(errors="replace")
            stderr = b"".join(stderr_parts).decode(errors="replace")
            sequence = 2
            if stdout or stdout_truncated[0]:
                progress(
                    WorkerProgressEvent(
                        sequence=sequence,
                        kind=WorkerProgressKind.OUTPUT,
                        text=stdout,
                        stream=WorkerOutputStream.STDOUT,
                        truncated=stdout_truncated[0],
                    )
                )
                sequence += 1
            if stderr or stderr_truncated[0]:
                progress(
                    WorkerProgressEvent(
                        sequence=sequence,
                        kind=WorkerProgressKind.OUTPUT,
                        text=stderr,
                        stream=WorkerOutputStream.STDERR,
                        truncated=stderr_truncated[0],
                    )
                )

            with self._lock:
                cancelled = record.cancel_requested
            if not stopped:
                status = WorkerExecutionStatus.UNKNOWN
            elif cancelled:
                status = WorkerExecutionStatus.CANCELLED
            elif timed_out:
                status = WorkerExecutionStatus.TIMED_OUT
            elif return_code == 0:
                status = WorkerExecutionStatus.COMPLETED
            else:
                status = WorkerExecutionStatus.FAILED
            return WorkerExecutionResult(
                status=status,
                started_components=tuple(started_components),
                completed_components=tuple(completed_components),
                process_tree_stopped=stopped,
                stdout=stdout,
                stderr=stderr,
            )
        except BaseException:
            if process is not None:
                if assigned_to_job:
                    self._terminate_and_wait(
                        job_handle, invocation.cancellation_grace_seconds
                    )
                else:
                    self._terminate_unassigned_process(
                        process, invocation.cancellation_grace_seconds
                    )
                    self._terminate_and_wait(
                        job_handle, invocation.cancellation_grace_seconds
                    )
            raise
        finally:
            with self._lock:
                self._running.pop(invocation.action_id, None)
            self._close_handle(job_handle)

    def terminate(self, *, action_id: str, timeout_seconds: int) -> bool:
        with self._lock:
            record = self._running.get(action_id)
            if record is None:
                return False
            record.cancel_requested = True
            job_handle = record.job_handle
        return self._terminate_and_wait(job_handle, timeout_seconds)

    def finalize(self, *, action_id: str, timeout_seconds: int) -> None:
        del timeout_seconds
        with self._lock:
            if action_id in self._running:
                raise ActionDispatcherError(
                    "Windows Job Object action cannot be finalized while running",
                    may_have_dispatched=True,
                )

    @staticmethod
    def _read_bounded(
        stream: object,
        limit: int,
        output: list[bytes],
        truncated: list[bool],
    ) -> None:
        retained = 0
        while True:
            chunk = stream.read(64 * 1024)  # type: ignore[attr-defined]
            if not chunk:
                return
            available = max(limit - retained, 0)
            if available:
                bounded = chunk[:available]
                output.append(bounded)
                retained += len(bounded)
            if len(chunk) > available:
                truncated[0] = True

    def _create_job_object(self) -> int:
        import ctypes
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = 0x00002000 | 0x00000008
        limits.BasicLimitInformation.ActiveProcessLimit = self.process_limit
        success = kernel32.SetInformationJobObject(
            handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        )
        if not success:
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise ctypes.WinError(error)
        return int(handle)

    @staticmethod
    def _assign_process(job_handle: int, process: subprocess.Popen[bytes]) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        success = kernel32.AssignProcessToJobObject(
            wintypes.HANDLE(job_handle), wintypes.HANDLE(process._handle)
        )
        if not success:
            raise ctypes.WinError(ctypes.get_last_error())

    @staticmethod
    def _resume_process(process: subprocess.Popen[bytes]) -> None:
        import ctypes
        from ctypes import wintypes

        ntdll = ctypes.WinDLL("ntdll")
        status = ntdll.NtResumeProcess(wintypes.HANDLE(process._handle))
        if status != 0:
            raise OSError(f"NtResumeProcess failed with status {status:#x}")

    @staticmethod
    def _terminate_unassigned_process(
        process: subprocess.Popen[bytes], timeout_seconds: int
    ) -> None:
        """Kill and reap a suspended child that never entered the Job Object."""

        try:
            process.kill()
            process.wait(timeout=timeout_seconds)
        except BaseException as exc:
            raise ActionDispatcherError(
                "unassigned suspended Windows child could not be stopped",
                may_have_dispatched=True,
            ) from exc
        finally:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

    @staticmethod
    def _terminate_and_wait(job_handle: int, timeout_seconds: int) -> bool:
        import ctypes
        from ctypes import wintypes

        class BasicAccountingInformation(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong),
                ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if not kernel32.TerminateJobObject(wintypes.HANDLE(job_handle), 1):
            return False
        deadline = monotonic() + timeout_seconds
        while True:
            info = BasicAccountingInformation()
            returned = wintypes.DWORD()
            success = kernel32.QueryInformationJobObject(
                wintypes.HANDLE(job_handle),
                1,
                ctypes.byref(info),
                ctypes.sizeof(info),
                ctypes.byref(returned),
            )
            if not success:
                return False
            if info.ActiveProcesses == 0:
                return True
            if monotonic() >= deadline:
                return False
            sleep(0.01)

    @staticmethod
    def _close_handle(job_handle: int) -> None:
        import ctypes
        from ctypes import wintypes

        if job_handle:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(
                wintypes.HANDLE(job_handle)
            )


class _ActionState(str, Enum):
    RESERVED = "reserved"
    RUNNING = "running"
    CANCELLATION_TOMBSTONE = "cancellation_tombstone"
    TERMINAL = "terminal"
    FINALIZED = "finalized"


@dataclass(slots=True)
class _ActionRecord:
    state: _ActionState
    session_epoch: int
    retention_seconds: int
    expires_at: float | None = None


class OutboundWindowsWorkerTransport:
    """WorkerTransport backed by one outbound, identity-bound Windows session."""

    def __init__(
        self,
        *,
        registration: WindowsWorkerRegistration,
        readiness_expiry_seconds: int = 30,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if (
            isinstance(readiness_expiry_seconds, bool)
            or not isinstance(readiness_expiry_seconds, int)
            or not 1 <= readiness_expiry_seconds <= 45
        ):
            raise ValueError(
                "Windows worker readiness expiry must be between one and 45 seconds"
            )
        self.registration = registration
        self.readiness_expiry_seconds = readiness_expiry_seconds
        self._clock = clock or monotonic
        self._lock = RLock()
        self._session: WindowsWorkerSession | None = None
        self._session_epoch = 0
        self._last_heartbeat: float | None = None
        self._actions: dict[str, _ActionRecord] = {}

    def attach(self, session: WindowsWorkerSession) -> None:
        """Accept one already-mTLS-authenticated outbound worker session."""

        evidence = getattr(session, "evidence", None)
        if not isinstance(evidence, WindowsWorkerSessionEvidence):
            raise ActionDispatcherError("Windows worker session evidence is invalid")
        if not self._evidence_is_authenticated(evidence):
            raise ActionDispatcherError(
                "Windows worker session evidence is not mTLS authenticated"
            )
        expected = self.registration
        if (
            evidence.worker_identity != expected.identity
            or evidence.certificate_identity != expected.certificate_identity
            or evidence.application_identity != expected.application_identity
        ):
            raise ActionDispatcherError("Windows worker session identity mismatch")
        with self._lock:
            if self._session is not None:
                raise ActionDispatcherError(
                    "registered Windows worker already has an outbound session"
                )
            self._session_epoch += 1
            self._session = session
            self._last_heartbeat = self._clock()

    @staticmethod
    def _evidence_is_authenticated(evidence: WindowsWorkerSessionEvidence) -> bool:
        return evidence.authenticated

    def heartbeat(self, session: WindowsWorkerSession) -> None:
        """Refresh readiness only for the exact attached session object."""

        with self._lock:
            if session is not self._session:
                raise ActionDispatcherError("Windows worker heartbeat session mismatch")
            self._last_heartbeat = self._clock()

    def disconnect(self, session: WindowsWorkerSession) -> None:
        """Make the worker unavailable without transferring any reserved work."""

        with self._lock:
            if session is not self._session:
                raise ActionDispatcherError(
                    "Windows worker disconnect session mismatch"
                )
            self._session = None
            self._last_heartbeat = None
            self._session_epoch += 1

    def register_execution(
        self,
        *,
        action_id: str,
        timeout_seconds: int,
        retention_seconds: int,
    ) -> None:
        del timeout_seconds  # Session readiness is an in-memory accepted-session fact.
        self._validate_action_arguments(action_id, retention_seconds)
        with self._lock:
            self._prune_expired_locked()
            self._require_ready_locked()
            if action_id in self._actions:
                raise ActionDispatcherError(
                    f"Windows worker action {action_id} is already registered",
                    may_have_dispatched=True,
                )
            if any(
                record.state in {_ActionState.RESERVED, _ActionState.RUNNING}
                for record in self._actions.values()
            ):
                raise ActionDispatcherError(
                    "registered Windows worker already has an action in progress"
                )
            self._actions[action_id] = _ActionRecord(
                state=_ActionState.RESERVED,
                session_epoch=self._session_epoch,
                retention_seconds=retention_seconds,
            )

    def authenticate(
        self, *, selected_host: str, timeout_seconds: int
    ) -> WorkerIdentity:
        del timeout_seconds
        if selected_host != self.registration.identity.host:
            raise ActionDispatcherError(
                f"selected execution host {selected_host} is unavailable"
            )
        with self._lock:
            self._require_ready_locked()
            return self.registration.identity

    def execute(
        self, invocation: WorkerInvocation, progress: WorkerProgressSink
    ) -> WorkerExecutionResult:
        if not isinstance(invocation, WorkerInvocation):
            raise TypeError("Windows worker invocation is invalid")
        if invocation.interactive:
            raise ActionDispatcherError(
                "Windows worker execution must be non-interactive"
            )
        if invocation.worker_identity != self.registration.identity:
            raise ActionDispatcherError("Windows worker invocation identity mismatch")
        with self._lock:
            self._prune_expired_locked()
            record = self._actions.get(invocation.action_id)
            if record is None or record.state is not _ActionState.RESERVED:
                if (
                    record is not None
                    and record.state is _ActionState.CANCELLATION_TOMBSTONE
                ):
                    return WorkerExecutionResult(
                        status="cancelled", process_tree_stopped=True
                    )
                raise ActionDispatcherError(
                    f"Windows worker action {invocation.action_id} was not executable",
                    may_have_dispatched=record is not None
                    and record.state in {_ActionState.RUNNING, _ActionState.TERMINAL},
                )
            session = self._require_ready_locked()
            if record.session_epoch != self._session_epoch:
                record.state = _ActionState.FINALIZED
                record.expires_at = self._clock() + record.retention_seconds
                raise ActionDispatcherError(
                    "Windows worker action reserved session disconnected"
                )
            epoch = self._session_epoch
            record.state = _ActionState.RUNNING
        try:
            result = session.execute(invocation, progress)
            if not isinstance(result, WorkerExecutionResult):
                raise TypeError("Windows worker returned an invalid terminal result")
        except BaseException as exc:
            with self._lock:
                self._mark_terminal_locked(invocation.action_id)
            if isinstance(exc, ActionDispatcherError) and exc.may_have_dispatched:
                raise
            raise ActionDispatcherError(
                "Windows worker execution outcome is unknown",
                may_have_dispatched=True,
            ) from exc
        with self._lock:
            connected_session = self._session
            connected_epoch = self._session_epoch
            self._mark_terminal_locked(invocation.action_id)
        if connected_session is not session or connected_epoch != epoch:
            raise ActionDispatcherError(
                "Windows worker disconnected after execution started",
                may_have_dispatched=True,
            )
        return result

    def cancel(
        self,
        *,
        action_id: str,
        timeout_seconds: int,
        retention_seconds: int,
    ) -> ActionCancellationResult:
        self._validate_action_arguments(action_id, retention_seconds)
        with self._lock:
            self._prune_expired_locked()
            record = self._actions.get(action_id)
            if record is None:
                self._actions[action_id] = _ActionRecord(
                    state=_ActionState.CANCELLATION_TOMBSTONE,
                    session_epoch=self._session_epoch,
                    retention_seconds=retention_seconds,
                    expires_at=self._clock() + retention_seconds,
                )
                return ActionCancellationResult(ActionCancellationStatus.NOT_STARTED)
            if record.state in {
                _ActionState.RESERVED,
                _ActionState.CANCELLATION_TOMBSTONE,
            }:
                record.state = _ActionState.CANCELLATION_TOMBSTONE
                record.expires_at = self._clock() + record.retention_seconds
                return ActionCancellationResult(ActionCancellationStatus.NOT_STARTED)
            if record.state is not _ActionState.RUNNING:
                return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
            session = self._session
            epoch_matches = record.session_epoch == self._session_epoch
        if session is None or not epoch_matches:
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
        try:
            stopped = session.terminate_job_object(
                action_id=action_id, timeout_seconds=timeout_seconds
            )
        except BaseException:  # noqa: BLE001 - no process-tree proof means unknown
            stopped = False
        return ActionCancellationResult(
            ActionCancellationStatus.STOPPED
            if stopped is True
            else ActionCancellationStatus.UNKNOWN
        )

    def finalize_execution(
        self,
        *,
        action_id: str,
        timeout_seconds: int,
        retention_seconds: int,
    ) -> None:
        self._validate_action_arguments(action_id, retention_seconds)
        with self._lock:
            self._prune_expired_locked()
            record = self._actions.get(action_id)
            if record is None:
                record = _ActionRecord(
                    state=_ActionState.FINALIZED,
                    session_epoch=self._session_epoch,
                    retention_seconds=retention_seconds,
                )
                self._actions[action_id] = record
            else:
                record.state = _ActionState.FINALIZED
            record.expires_at = self._clock() + record.retention_seconds
            session = (
                self._session if record.session_epoch == self._session_epoch else None
            )
        if session is not None:
            try:
                session.finalize(action_id=action_id, timeout_seconds=timeout_seconds)
            except BaseException:  # noqa: BLE001 - bounded retention is the fallback
                return

    def _require_ready_locked(self) -> WindowsWorkerSession:
        session = self._session
        heartbeat = self._last_heartbeat
        if session is None or heartbeat is None:
            raise ActionDispatcherError("registered Windows worker is unavailable")
        if self._clock() - heartbeat > self.readiness_expiry_seconds:
            raise ActionDispatcherError("registered Windows worker heartbeat expired")
        return session

    def _mark_terminal_locked(self, action_id: str) -> None:
        record = self._actions.get(action_id)
        if record is None:
            return
        record.state = _ActionState.TERMINAL
        record.expires_at = self._clock() + record.retention_seconds

    def _prune_expired_locked(self) -> None:
        now = self._clock()
        for action_id in tuple(self._actions):
            expires_at = self._actions[action_id].expires_at
            if expires_at is not None and expires_at <= now:
                self._actions.pop(action_id, None)

    @staticmethod
    def _validate_action_arguments(action_id: str, retention_seconds: int) -> None:
        if not isinstance(action_id, str) or not action_id.strip():
            raise ValueError("Windows worker action identifier must be non-blank")
        if (
            isinstance(retention_seconds, bool)
            or not isinstance(retention_seconds, int)
            or retention_seconds < 1
        ):
            raise ValueError("Windows worker action-state retention must be positive")


class ControlledOutboundWindowsWorkerTransport(OutboundWindowsWorkerTransport):
    """Test-only transport that accepts explicitly controlled identity evidence."""

    @staticmethod
    def _evidence_is_authenticated(evidence: WindowsWorkerSessionEvidence) -> bool:
        del evidence
        return True


class ControlledWindowsWorkerSession:
    """Deterministic Job Object session used only at the public contract seam."""

    def __init__(
        self,
        *,
        evidence: WindowsWorkerSessionEvidence,
        result: WorkerExecutionResult | None = None,
        execution_hook: Callable[[WorkerInvocation], WorkerExecutionResult]
        | None = None,
        process_tree_stopped: bool = True,
    ) -> None:
        self._evidence = evidence
        self.result = result or WorkerExecutionResult.completed()
        self.execution_hook = execution_hook
        self.process_tree_stopped = process_tree_stopped
        self.invocations: list[WorkerInvocation] = []
        self.job_object_terminations: list[str] = []
        self.finalizations: list[str] = []

    @property
    def evidence(self) -> WindowsWorkerSessionEvidence:
        return self._evidence

    def execute(
        self, invocation: WorkerInvocation, progress: WorkerProgressSink
    ) -> WorkerExecutionResult:
        del progress
        self.invocations.append(invocation)
        if self.execution_hook is not None:
            return self.execution_hook(invocation)
        return self.result

    def terminate_job_object(self, *, action_id: str, timeout_seconds: int) -> bool:
        del timeout_seconds
        self.job_object_terminations.append(action_id)
        return self.process_tree_stopped

    def finalize(self, *, action_id: str, timeout_seconds: int) -> None:
        del timeout_seconds
        self.finalizations.append(action_id)

"""Windows Job Object execution contracts and bounded process runner."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from threading import RLock, Thread
from time import monotonic
from typing import BinaryIO, Protocol

from ..ports import ActionDispatcherError
from .contracts import (
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerInvocation,
    WorkerOutputStream,
    WorkerProgressEvent,
    WorkerProgressKind,
    WorkerProgressSink,
)
from .windows_job_native import _WindowsJobObjectNativeMixin
from .windows_mtls import WindowsWorkerSessionEvidence

_CREATE_SUSPENDED = 0x00000004


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


class SubprocessWindowsJobObjectExecutor(_WindowsJobObjectNativeMixin):
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
        self._cancelled_actions: set[str] = set()

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
            len(component.redirections) > 1
            for component in invocation.action.components
        ):
            raise ActionDispatcherError(
                "Windows Job Object executor accepts one redirection target per component"
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
            if invocation.action_id in self._cancelled_actions:
                return WorkerExecutionResult(
                    status=WorkerExecutionStatus.CANCELLED,
                    process_tree_stopped=True,
                )
            record = _RunningWindowsJob(process=None, job_handle=0)
            self._running[invocation.action_id] = record

        job_handle = 0
        unassigned_process: subprocess.Popen[bytes] | None = None
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
            job_handle = self._create_job_object()
            with self._lock:
                record.job_handle = job_handle
                if record.cancel_requested:
                    return WorkerExecutionResult(
                        status=WorkerExecutionStatus.CANCELLED,
                        process_tree_stopped=True,
                    )
            flags = (
                subprocess.CREATE_NO_WINDOW
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | _CREATE_SUSPENDED
            )
            components = invocation.action.components
            group_start = 0
            while group_start < len(components):
                group_end = group_start + 1
                while (
                    group_end < len(components)
                    and components[group_end].operator_before == "|"
                ):
                    group_end += 1
                group_operator = components[group_start].operator_before
                if group_start and group_operator == "&&" and return_code != 0:
                    group_start = group_end
                    continue
                if group_start and group_operator == "||" and return_code == 0:
                    group_start = group_end
                    continue
                with self._lock:
                    if record.cancel_requested:
                        break
                if deadline - monotonic() <= 0:
                    timed_out = True
                    break
                group_processes: list[subprocess.Popen[bytes]] = []
                group_readers: list[Thread] = []
                redirection_streams: list[BinaryIO] = []
                previous_pipe: object | None = None
                cancelled_during_launch = False
                try:
                    for index in range(group_start, group_end):
                        component = components[index]
                        redirects_stdout = bool(component.redirections)
                        pipes_to_next = index + 1 < group_end and not redirects_stdout
                        stdout_target: int | BinaryIO = subprocess.PIPE
                        # Serialize cancellation with every file open and suspended
                        # process publication. Once terminate() acknowledges, this
                        # block can no longer create a side effect or a new child.
                        with self._lock:
                            if record.cancel_requested:
                                cancelled_during_launch = True
                                break
                            if redirects_stdout:
                                stdout_target = self._open_frozen_redirection_target(
                                    component.redirections[0]
                                )
                                redirection_streams.append(stdout_target)
                            unassigned_process = subprocess.Popen(
                                [component.executable, *component.arguments],
                                cwd=invocation.action.cwd,
                                stdin=(previous_pipe or subprocess.DEVNULL),
                                stdout=stdout_target,
                                stderr=subprocess.PIPE,
                                creationflags=flags,
                            )
                            process = unassigned_process
                            self._assign_process(job_handle, process)
                            unassigned_process = None
                            record.process = process
                        group_processes.append(process)
                        started_components.append(index)
                        if previous_pipe is not None:
                            previous_pipe.close()  # type: ignore[attr-defined]
                        previous_pipe = process.stdout if pipes_to_next else None
                    for stream in redirection_streams:
                        stream.close()
                    redirection_streams.clear()
                    if cancelled_during_launch:
                        for process in group_processes:
                            process.wait(timeout=invocation.cancellation_grace_seconds)
                        break

                    endpoint = group_processes[-1]
                    endpoint_stdout: list[bytes] = []
                    endpoint_stdout_truncated = [False]
                    if endpoint.stdout is not None:
                        stdout_reader = Thread(
                            target=self._read_bounded,
                            args=(
                                endpoint.stdout,
                                max(
                                    invocation.stdout_limit_bytes
                                    - sum(len(part) for part in stdout_parts),
                                    0,
                                ),
                                endpoint_stdout,
                                endpoint_stdout_truncated,
                            ),
                            daemon=True,
                        )
                        stdout_reader.start()
                        group_readers.append(stdout_reader)
                    for process in group_processes:
                        assert process.stderr is not None
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
                        stderr_reader.start()
                        group_readers.append(stderr_reader)

                    # Consumers must be able to drain pipes before producers run.
                    # Cancellation and resume are mutually exclusive: after a
                    # successful cancellation acknowledgement no child is resumed.
                    with self._lock:
                        if not record.cancel_requested:
                            for process in reversed(group_processes):
                                self._resume_process(process)
                    for process in group_processes:
                        try:
                            process.wait(timeout=max(deadline - monotonic(), 0))
                        except subprocess.TimeoutExpired:
                            timed_out = True
                            self._terminate_and_wait(
                                job_handle, invocation.cancellation_grace_seconds
                            )
                            break
                    for reader in group_readers:
                        reader.join(timeout=invocation.cancellation_grace_seconds)
                    if any(reader.is_alive() for reader in group_readers):
                        timed_out = True
                    return_code = endpoint.poll()
                    stdout_parts.extend(endpoint_stdout)
                    if endpoint_stdout_truncated[0]:
                        stdout_truncated[0] = True
                    for offset, process in enumerate(group_processes):
                        if process.poll() == 0:
                            completed_components.append(group_start + offset)
                finally:
                    if previous_pipe is not None:
                        previous_pipe.close()  # type: ignore[attr-defined]
                    for stream in redirection_streams:
                        stream.close()
                group_start = group_end
                if timed_out:
                    break

            # A successful root process may have left descendants running.
            # Terminating the Job Object closes that ambiguity before result.
            stopped = self._terminate_and_wait(
                job_handle, invocation.cancellation_grace_seconds
            )

            stdout = self._decode_bounded_output(
                stdout_parts,
                truncated=stdout_truncated[0],
                limit=invocation.stdout_limit_bytes,
            )
            stderr = self._decode_bounded_output(
                stderr_parts,
                truncated=stderr_truncated[0],
                limit=invocation.stderr_limit_bytes,
            )
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
                stdout_truncated=stdout_truncated[0],
                stderr_truncated=stderr_truncated[0],
            )
        except BaseException:
            if unassigned_process is not None:
                self._terminate_unassigned_process(
                    unassigned_process, invocation.cancellation_grace_seconds
                )
            if job_handle:
                self._terminate_and_wait(
                    job_handle, invocation.cancellation_grace_seconds
                )
            raise
        finally:
            with self._lock:
                self._running.pop(invocation.action_id, None)
            if job_handle:
                self._close_handle(job_handle)

    def terminate(self, *, action_id: str, timeout_seconds: int) -> bool:
        with self._lock:
            record = self._running.get(action_id)
            if record is None:
                self._cancelled_actions.add(action_id)
                return True
            record.cancel_requested = True
            job_handle = record.job_handle
            if not job_handle:
                return True
        return self._terminate_and_wait(job_handle, timeout_seconds)

    def finalize(self, *, action_id: str, timeout_seconds: int) -> None:
        del timeout_seconds
        with self._lock:
            if action_id in self._running:
                raise ActionDispatcherError(
                    "Windows Job Object action cannot be finalized while running",
                    may_have_dispatched=True,
                )
            self._cancelled_actions.discard(action_id)

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

    @staticmethod
    def _decode_bounded_output(
        parts: list[bytes], *, truncated: bool, limit: int
    ) -> str:
        encoded = b"".join(parts)
        if truncated:
            marker = b"\n[truncated]"
            if limit >= len(marker):
                encoded = encoded[: limit - len(marker)] + marker
        return encoded[:limit].decode(errors="ignore")


__all__ = [
    "SubprocessWindowsJobObjectExecutor",
    "WindowsJobObjectExecutor",
    "WindowsJobObjectWorkerSession",
    "WindowsWorkerSession",
]

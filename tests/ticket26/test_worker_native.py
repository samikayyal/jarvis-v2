# ruff: noqa: F401, I001, RUF100 -- split modules retain ticket context.
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep

import pytest

from jarvis_control_plane import (
    ActionCancellationStatus,
    ActionDispatcherError,
    ControlledOutboundWindowsWorkerTransport,
    ControlledWindowsWorkerSession,
    OutboundWindowsWorkerTransport,
    SubprocessWindowsJobObjectExecutor,
    WindowsMtlsClientConfig,
    WindowsWorkerRegistration,
    WindowsWorkerSessionEvidence,
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerIdentity,
    WorkerInvocation,
    authenticate_windows_worker_session,
    open_windows_worker_mtls_session,
)
from jarvis_control_plane import windows_worker as windows_worker_module
from jarvis_control_plane.terminal_policy import TerminalAction
from jarvis_control_plane.windows_worker_session import SocketWindowsWorkerSession

WINDOWS_IDENTITY = WorkerIdentity(
    host="windows", worker_id="windows-01", connection_id="boot-01"
)
REGISTRATION = WindowsWorkerRegistration(
    identity=WINDOWS_IDENTITY,
    certificate_identity="spiffe://jarvis/workers/windows-01",
    application_identity="jarvis-windows-worker/windows-01",
)


def _evidence(**changes: str) -> WindowsWorkerSessionEvidence:
    values = {
        "host": "windows",
        "worker_id": "windows-01",
        "connection_id": "boot-01",
        "certificate_identity": "spiffe://jarvis/workers/windows-01",
        "application_identity": "jarvis-windows-worker/windows-01",
    }
    values.update(changes)
    return WindowsWorkerSessionEvidence(**values)


def _invocation(
    action_id: str = "action-001", *, action: TerminalAction | None = None
) -> WorkerInvocation:
    return WorkerInvocation(
        action_id=action_id,
        action=action
        or TerminalAction(
            host="windows",
            executable="C:\\Windows\\System32\\whoami.exe",
            arguments=(),
            cwd="C:\\Windows\\System32",
        ),
        interactive=False,
        deadline_seconds=30,
        stdout_limit_bytes=1024 * 1024,
        stderr_limit_bytes=1024 * 1024,
        cancellation_grace_seconds=5,
        progress_event_limit=32,
        milestone_limit_bytes=4096,
        worker_identity=WINDOWS_IDENTITY,
    )


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows Job Objects")
def test_failed_job_assignment_terminates_and_reaps_the_suspended_child() -> None:
    class FailingAssignmentExecutor(SubprocessWindowsJobObjectExecutor):
        spawned: subprocess.Popen[bytes] | None = None

        def _assign_process(
            self, job_handle: int, process: subprocess.Popen[bytes]
        ) -> None:
            del job_handle
            self.spawned = process
            raise OSError("controlled assignment failure")

    executor = FailingAssignmentExecutor()

    with pytest.raises(OSError, match="controlled assignment failure"):
        executor.execute(_invocation("assignment-failure"), lambda _event: None)

    assert executor.spawned is not None
    assert executor.spawned.poll() is not None


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows Job Objects")
def test_native_job_executor_preserves_structured_compound_component_progress() -> None:
    action = TerminalAction(
        host="windows",
        executable=sys.executable,
        arguments=("-c", "print('first')"),
        cwd=str(Path.cwd()),
        components=(
            {
                "executable": sys.executable,
                "arguments": ["-c", "print('first')"],
            },
            {
                "executable": sys.executable,
                "arguments": ["-c", "print('second')"],
                "operator_before": "&&",
            },
        ),
    )

    result = SubprocessWindowsJobObjectExecutor().execute(
        _invocation("compound", action=action), lambda _event: None
    )

    assert result.status is WorkerExecutionStatus.COMPLETED
    assert result.started_components == (0, 1)
    assert result.completed_components == (0, 1)
    assert result.stdout.splitlines() == ["first", "second"]


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows Job Objects")
def test_native_job_executor_preserves_pipeline_and_redirection_structure(
    tmp_path: Path,
) -> None:
    redirected = tmp_path / "redirected.txt"
    action = TerminalAction(
        host="windows",
        executable=sys.executable,
        arguments=("-c", "print('pipe me')"),
        cwd=str(tmp_path),
        components=(
            {
                "executable": sys.executable,
                "arguments": ["-c", "print('pipe me')"],
            },
            {
                "executable": sys.executable,
                "arguments": [
                    "-c",
                    "import sys; print(sys.stdin.read().upper(), end='')",
                ],
                "operator_before": "|",
                "redirections": [str(redirected)],
            },
        ),
    )

    result = SubprocessWindowsJobObjectExecutor().execute(
        _invocation("pipeline-redirection", action=action), lambda _event: None
    )

    assert result.status is WorkerExecutionStatus.COMPLETED
    assert result.started_components == (0, 1)
    assert result.completed_components == (0, 1)
    assert result.stdout == ""
    assert redirected.read_text() == "PIPE ME\n"


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows Job Objects")
def test_native_pipeline_streams_while_the_producer_is_running(tmp_path: Path) -> None:
    action = TerminalAction(
        host="windows",
        executable=sys.executable,
        arguments=(
            "-c",
            "while True: print('record', flush=True)",
        ),
        cwd=str(tmp_path),
        components=(
            {
                "executable": sys.executable,
                "arguments": ["-c", "while True: print('record', flush=True)"],
            },
            {
                "executable": sys.executable,
                "arguments": [
                    "-c",
                    "import sys; print(sys.stdin.readline(), end='')",
                ],
                "operator_before": "|",
            },
        ),
    )
    started = monotonic()

    result = SubprocessWindowsJobObjectExecutor().execute(
        replace(_invocation("streaming-pipeline", action=action), deadline_seconds=5),
        lambda _event: None,
    )

    assert monotonic() - started < 5
    assert result.status is WorkerExecutionStatus.COMPLETED
    assert result.stdout.splitlines() == ["record"]


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows Job Objects")
def test_false_conditional_skips_the_complete_pipeline(tmp_path: Path) -> None:
    side_effect = tmp_path / "consumer-ran.txt"
    action = TerminalAction(
        host="windows",
        executable=sys.executable,
        arguments=("-c", "raise SystemExit(1)"),
        cwd=str(tmp_path),
        components=(
            {
                "executable": sys.executable,
                "arguments": ["-c", "raise SystemExit(1)"],
            },
            {
                "executable": sys.executable,
                "arguments": ["-c", "print('guarded')"],
                "operator_before": "&&",
            },
            {
                "executable": sys.executable,
                "arguments": [
                    "-c",
                    f"from pathlib import Path; Path({str(side_effect)!r}).write_text('ran')",
                ],
                "operator_before": "|",
            },
        ),
    )

    result = SubprocessWindowsJobObjectExecutor().execute(
        _invocation("guarded-pipeline", action=action), lambda _event: None
    )

    assert result.status is WorkerExecutionStatus.FAILED
    assert result.started_components == (0,)
    assert not side_effect.exists()


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows Job Objects")
def test_redirection_takes_precedence_over_a_following_pipe(tmp_path: Path) -> None:
    redirected = tmp_path / "redirected.txt"
    action = TerminalAction(
        host="windows",
        executable=sys.executable,
        arguments=("-c", "print('redirected')"),
        cwd=str(tmp_path),
        components=(
            {
                "executable": sys.executable,
                "arguments": ["-c", "print('redirected')"],
                "redirections": [str(redirected)],
            },
            {
                "executable": sys.executable,
                "arguments": [
                    "-c",
                    "import sys; print(repr(sys.stdin.read()))",
                ],
                "operator_before": "|",
            },
        ),
    )

    result = SubprocessWindowsJobObjectExecutor().execute(
        _invocation("redirect-before-pipe", action=action), lambda _event: None
    )

    assert result.status is WorkerExecutionStatus.COMPLETED
    assert redirected.read_text() == "redirected\n"
    assert result.stdout.splitlines() == ["''"]

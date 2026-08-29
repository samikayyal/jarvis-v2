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
def test_redirection_stops_mutating_when_the_action_deadline_expires(
    tmp_path: Path,
) -> None:
    redirected = tmp_path / "bounded.txt"
    action = TerminalAction(
        host="windows",
        executable=sys.executable,
        arguments=(
            "-c",
            "import sys; chunk='x'*65536;\nwhile True: sys.stdout.write(chunk); sys.stdout.flush()",
        ),
        cwd=str(tmp_path),
        components=(
            {
                "executable": sys.executable,
                "arguments": [
                    "-c",
                    "import sys; chunk='x'*65536;\nwhile True: sys.stdout.write(chunk); sys.stdout.flush()",
                ],
                "redirections": [str(redirected)],
            },
        ),
    )

    result = SubprocessWindowsJobObjectExecutor().execute(
        replace(_invocation("deadline-redirection", action=action), deadline_seconds=1),
        lambda _event: None,
    )
    size_after_result = redirected.stat().st_size
    sleep(0.2)

    assert result.status is WorkerExecutionStatus.TIMED_OUT
    assert redirected.stat().st_size == size_after_result


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows Job Objects")
def test_executor_retains_cancellation_before_the_action_record_is_published(
    tmp_path: Path,
) -> None:
    side_effect = tmp_path / "must-not-run.txt"
    action = TerminalAction(
        host="windows",
        executable=sys.executable,
        arguments=(
            "-c",
            f"from pathlib import Path; Path({str(side_effect)!r}).write_text('ran')",
        ),
        cwd=str(tmp_path),
    )
    executor = SubprocessWindowsJobObjectExecutor()

    assert executor.terminate(action_id="handoff-cancel", timeout_seconds=1) is True
    result = executor.execute(
        _invocation("handoff-cancel", action=action), lambda _event: None
    )

    assert result.status is WorkerExecutionStatus.CANCELLED
    assert result.process_tree_stopped is True
    assert not side_effect.exists()


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows reparse paths")
def test_native_redirection_rechecks_the_frozen_reparse_target(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(actual, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    redirected = linked / "out.txt"
    action = TerminalAction(
        host="windows",
        executable=sys.executable,
        arguments=("-c", "print('blocked')"),
        cwd=str(tmp_path),
        components=(
            {
                "executable": sys.executable,
                "arguments": ["-c", "print('blocked')"],
                "redirections": [str(redirected)],
            },
        ),
    )

    with pytest.raises(ActionDispatcherError, match="changed through a reparse path"):
        SubprocessWindowsJobObjectExecutor().execute(
            _invocation("reparse-redirection", action=action), lambda _event: None
        )

    assert not (actual / "out.txt").exists()


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows Job Objects")
def test_native_output_overflow_is_visibly_marked_and_traceable() -> None:
    action = TerminalAction(
        host="windows",
        executable=sys.executable,
        arguments=("-c", "print('x' * 100)"),
        cwd=str(Path.cwd()),
    )
    invocation = replace(_invocation("truncated", action=action), stdout_limit_bytes=32)
    events = []

    result = SubprocessWindowsJobObjectExecutor().execute(invocation, events.append)

    assert len(result.stdout.encode()) <= 32
    assert result.stdout.endswith("[truncated]")
    assert result.stdout_truncated is True
    assert events[0].truncated is True


def test_disconnect_during_started_action_returns_unknown_after_reconnect() -> None:
    started = Event()
    release = Event()

    def execute(_invocation: WorkerInvocation) -> WorkerExecutionResult:
        started.set()
        assert release.wait(timeout=5)
        return WorkerExecutionResult.completed(stdout="may have completed")

    first = ControlledWindowsWorkerSession(evidence=_evidence(), execution_hook=execute)
    transport = ControlledOutboundWindowsWorkerTransport(registration=REGISTRATION)
    transport.attach(first)
    transport.register_execution(
        action_id="ambiguous", timeout_seconds=1, retention_seconds=60
    )

    errors: list[BaseException] = []

    def run() -> None:
        try:
            transport.execute(_invocation("ambiguous"), lambda _event: None)
        except BaseException as exc:  # noqa: BLE001 - assertion captures boundary
            errors.append(exc)

    worker = Thread(target=run)
    worker.start()
    try:
        assert started.wait(timeout=5)
        transport.disconnect(first)
        transport.attach(ControlledWindowsWorkerSession(evidence=_evidence()))
    finally:
        release.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], ActionDispatcherError)
    assert errors[0].may_have_dispatched is True

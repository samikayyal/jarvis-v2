from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from threading import Event, Thread
from time import monotonic
from typing import cast
from unittest.mock import Mock

import pytest

import jarvis_control_plane.ubuntu_worker as ubuntu_worker_module
from jarvis_control_plane import (
    ActionCancellationStatus,
    SystemdUbuntuProcessScope,
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerIdentity,
    ubuntu_worker_runner,
)
from jarvis_control_plane.terminal_policy import TerminalAction, TerminalComponent

from .helpers import (
    _ControlledUbuntuProcessScopeAdapter,
    _execute_systemd_scope,
    _ExitedProcess,
    _invocation,
    _unit_check,
)


def test_systemd_scope_is_noninteractive_bounded_and_never_uses_a_shell() -> None:
    scope = SystemdUbuntuProcessScope(
        systemd_run_path="/usr/bin/systemd-run",
        systemctl_path="/usr/bin/systemctl",
        process_limit=32,
    )
    invocation = _invocation(
        "action-ubuntu-command",
        WorkerIdentity(
            host="ubuntu", worker_id="ubuntu-01", connection_id="local-boot-01"
        ),
    )
    command = scope.command_for(invocation)

    assert command[0] == "/usr/bin/systemd-run"
    assert "--property=TasksMax=32" in command
    assert "--property=NoNewPrivileges=yes" in command
    assert "--property=RestrictNamespaces=yes" in command
    assert "--property=RuntimeMaxSec=120s" in command
    inaccessible = next(
        argument
        for argument in command
        if argument.startswith("--property=InaccessiblePaths=")
    )
    runtime_uid = os.getuid() if hasattr(os, "getuid") else 0
    assert f"/run/user/{runtime_uid}/systemd/private" in inaccessible
    assert f"/run/user/{runtime_uid}/bus" in inaccessible
    assert "%t" not in inaccessible
    assert "--pipe" in command
    assert "--wait" in command
    assert command[-3:] == ("--", "/usr/bin/printf", "hello")
    assert all("docker" not in argument for argument in command)


def test_production_systemd_adapter_checks_one_unit_with_exact_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = subprocess.CompletedProcess(
        ["/usr/bin/systemctl"], 3, stdout="inactive\n"
    )
    run = Mock(return_value=completed)
    monkeypatch.setattr(ubuntu_worker_module.subprocess, "run", run)
    adapter = ubuntu_worker_module._SystemdUbuntuProcessScopeAdapter(
        systemctl_path="/usr/bin/systemctl"
    )

    observed = adapter.check_unit("jarvis-action-test.service", timeout_seconds=1.25)

    assert observed is completed
    run.assert_called_once_with(
        (
            "/usr/bin/systemctl",
            "--user",
            "is-active",
            "jarvis-action-test.service",
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=1.25,
        text=True,
    )


@pytest.mark.parametrize("signal", ["TERM", "KILL"])
def test_production_systemd_adapter_signals_the_whole_unit_with_exact_arguments(
    monkeypatch: pytest.MonkeyPatch,
    signal: str,
) -> None:
    run = Mock()
    monkeypatch.setattr(ubuntu_worker_module.subprocess, "run", run)
    adapter = ubuntu_worker_module._SystemdUbuntuProcessScopeAdapter(
        systemctl_path="/usr/bin/systemctl"
    )

    adapter.signal_unit("jarvis-action-test.service", signal, timeout_seconds=2.5)

    run.assert_called_once_with(
        (
            "/usr/bin/systemctl",
            "--user",
            "kill",
            "--kill-whom=all",
            f"--signal={signal}",
            "jarvis-action-test.service",
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=2.5,
    )


@pytest.mark.parametrize("process_limit", [0, 65, True])
def test_systemd_scope_rejects_an_invalid_process_tree_bound(
    process_limit: object,
) -> None:
    with pytest.raises((TypeError, ValueError), match="process limit"):
        SystemdUbuntuProcessScope(process_limit=process_limit)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("return_code", "state", "expected"),
    [
        (1, "", False),
        (3, "inactive\n", True),
        (3, "failed\n", True),
        (4, "inactive\n", True),
        (4, "unknown\n", True),
    ],
)
def test_systemd_scope_reports_unit_state_through_public_execution(
    monkeypatch: pytest.MonkeyPatch,
    return_code: int,
    state: str,
    expected: bool,
) -> None:
    adapter = _ControlledUbuntuProcessScopeAdapter(_unit_check(return_code, state))
    scope = SystemdUbuntuProcessScope(systemd_adapter=adapter)
    invocation = _invocation(
        "action-ubuntu-unit-state",
        WorkerIdentity(
            host="ubuntu", worker_id="ubuntu-01", connection_id="local-boot-01"
        ),
    )
    result = _execute_systemd_scope(
        monkeypatch,
        scope,
        invocation,
        _ExitedProcess(return_code=return_code),
    )

    assert result.process_tree_stopped is expected
    assert adapter.unit_checks


def test_systemd_scope_waits_for_a_deactivating_unit_to_be_collected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _ControlledUbuntuProcessScopeAdapter(
        _unit_check(3, "deactivating\n"),
        _unit_check(4, "inactive\n"),
    )
    scope = SystemdUbuntuProcessScope(systemd_adapter=adapter)
    invocation = _invocation(
        "action-ubuntu-deactivating",
        WorkerIdentity(
            host="ubuntu", worker_id="ubuntu-01", connection_id="local-boot-01"
        ),
    )
    result = _execute_systemd_scope(
        monkeypatch,
        scope,
        invocation,
        _ExitedProcess(),
    )

    assert result.process_tree_stopped is True
    assert len(adapter.unit_checks) >= 2


def test_systemd_scope_accepts_a_collected_unit_after_wait_wrapper_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _ControlledUbuntuProcessScopeAdapter(_unit_check(4, "unknown\n"))
    scope = SystemdUbuntuProcessScope(systemd_adapter=adapter)
    invocation = _invocation(
        "action-ubuntu-collected",
        WorkerIdentity(
            host="ubuntu", worker_id="ubuntu-01", connection_id="local-boot-01"
        ),
    )
    result = _execute_systemd_scope(
        monkeypatch,
        scope,
        invocation,
        _ExitedProcess(),
    )

    assert result.status is WorkerExecutionStatus.COMPLETED
    assert result.process_tree_stopped is True
    assert len(adapter.unit_checks) >= 2


def test_systemd_scope_reports_real_stream_truncation_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _ControlledUbuntuProcessScopeAdapter(_unit_check(3, "inactive\n"))
    scope = SystemdUbuntuProcessScope(systemd_adapter=adapter)
    invocation = replace(
        _invocation(
            "action-ubuntu-truncation",
            WorkerIdentity(
                host="ubuntu", worker_id="ubuntu-01", connection_id="local-boot-01"
            ),
        ),
        stdout_limit_bytes=32,
        stderr_limit_bytes=32,
    )
    result = _execute_systemd_scope(
        monkeypatch,
        scope,
        invocation,
        _ExitedProcess(stdout=b"A" * 33, stderr=b"B" * 33),
    )

    assert len(result.stdout.encode()) == 32
    assert len(result.stderr.encode()) == 32
    assert result.stdout.endswith("[output truncated]")
    assert result.stderr.endswith("[output truncated]")
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True


def test_systemd_scope_reports_ambiguous_cancellation_until_unit_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RunningProcess:
        def __init__(self) -> None:
            self.started = Event()
            self.finished = Event()
            self.stdout = BytesIO()
            self.stderr = BytesIO()

        def poll(self) -> int | None:
            self.started.set()
            return 0 if self.finished.is_set() else None

        def wait(self, timeout: float | None = None) -> int:
            if self.finished.wait(timeout=timeout):
                return 0
            raise subprocess.TimeoutExpired("controlled-process", timeout)

    adapter = _ControlledUbuntuProcessScopeAdapter(
        _unit_check(1, "unknown\n"),
        _unit_check(1, "unknown\n"),
        _unit_check(1, "unknown\n"),
        _unit_check(1, "unknown\n"),
        _unit_check(3, "inactive\n"),
    )
    scope = SystemdUbuntuProcessScope(systemd_adapter=adapter)
    invocation = replace(
        _invocation(
            "action-ubuntu-ambiguous-cancel",
            WorkerIdentity(
                host="ubuntu", worker_id="ubuntu-01", connection_id="local-boot-01"
            ),
        ),
        deadline_seconds=5,
        cancellation_grace_seconds=1,
    )
    process = RunningProcess()
    monkeypatch.setattr(ubuntu_worker_module.sys, "platform", "linux")
    monkeypatch.setattr(
        ubuntu_worker_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )
    scope.reserve(action_id=invocation.action_id)
    results: list[WorkerExecutionResult] = []
    execution = Thread(
        target=lambda: results.append(scope.execute(invocation, lambda _event: None))
    )
    execution.start()
    assert process.started.wait(timeout=2)

    cancellation = scope.cancel(action_id=invocation.action_id, timeout_seconds=1)

    assert cancellation.status is ActionCancellationStatus.UNKNOWN
    assert [signal for _, signal, _ in adapter.signals] == ["TERM", "KILL"]

    process.finished.set()
    execution.join(timeout=2)
    assert not execution.is_alive()
    assert results[0].status is WorkerExecutionStatus.CANCELLED
    assert results[0].process_tree_stopped is True


def test_systemd_scope_runs_structured_compounds_inside_the_same_unit() -> None:
    scope = SystemdUbuntuProcessScope()
    invocation = replace(
        _invocation(
            "action-ubuntu-compound",
            WorkerIdentity(
                host="ubuntu", worker_id="ubuntu-01", connection_id="local-boot-01"
            ),
        ),
        action=TerminalAction(
            host="ubuntu",
            executable="/usr/bin/printf",
            arguments=("hello",),
            cwd="/workspace",
            components=(
                TerminalComponent("/usr/bin/printf", ("hello",)),
                TerminalComponent("/usr/bin/tr", ("a-z", "A-Z"), "|"),
                TerminalComponent("/usr/bin/printf", ("done",), "&&"),
            ),
        ),
    )

    command = scope.command_for(invocation)
    separator = command.index("--")
    action_command = command[separator + 1 :]

    assert action_command[0] == sys.executable
    runner = Path(action_command[1])
    assert runner.is_absolute()
    assert runner.name == "ubuntu_worker_runner.py"
    assert all(
        cast(TerminalComponent, component).executable not in command
        for component in invocation.action.components
    )


def test_compound_runner_preserves_control_flow_without_a_shell(
    capfd: pytest.CaptureFixture[str],
) -> None:
    plan = (
        (sys.executable, ("-c", "raise SystemExit(1)"), ""),
        (sys.executable, ("-c", "print('wrong')"), "&&"),
        (sys.executable, ("-c", "print('recovered')"), "||"),
    )

    status = ubuntu_worker_runner._run_plan(plan)
    captured = capfd.readouterr()

    assert status == 0
    assert captured.out.splitlines() == ["recovered"]
    assert '"started":[0,2]' in captured.err
    assert '"completed":[0,2]' in captured.err


def test_compound_runner_connects_a_structured_pipeline(
    capfd: pytest.CaptureFixture[str],
) -> None:
    plan = (
        (sys.executable, ("-c", "import sys; sys.stdout.write('hello')"), ""),
        (
            sys.executable,
            (
                "-c",
                "import sys; sys.stdout.write(sys.stdin.read().upper())",
            ),
            "|",
        ),
    )

    status = ubuntu_worker_runner._run_plan(plan)
    captured = capfd.readouterr()

    assert status == 0
    assert captured.out == "HELLO"
    assert '"started":[0,1]' in captured.err
    assert '"completed":[0,1]' in captured.err


def test_systemd_scope_deadline_applies_after_wrapper_exit_with_open_pipes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InheritedPipe:
        def __init__(self) -> None:
            self.closed = Event()

        def read(self, _size: int) -> bytes:
            assert self.closed.wait(timeout=5)
            return b""

        read1 = read

        def close(self) -> None:
            self.closed.set()

    class ExitedWrapper:
        def __init__(self) -> None:
            self.stdout = InheritedPipe()
            self.stderr = InheritedPipe()

        @staticmethod
        def poll() -> int:
            return 0

    scope = SystemdUbuntuProcessScope(
        systemd_adapter=_ControlledUbuntuProcessScopeAdapter(
            _unit_check(3, "inactive\n")
        )
    )
    wrapper = ExitedWrapper()
    invocation = replace(
        _invocation(
            "action-ubuntu-inherited-pipe",
            WorkerIdentity(
                host="ubuntu", worker_id="ubuntu-01", connection_id="local-boot-01"
            ),
        ),
        deadline_seconds=1,
        cancellation_grace_seconds=1,
    )

    started = monotonic()
    result = _execute_systemd_scope(monkeypatch, scope, invocation, wrapper)

    assert monotonic() - started < 3
    assert result.status is WorkerExecutionStatus.TIMED_OUT
    assert result.process_tree_stopped is True

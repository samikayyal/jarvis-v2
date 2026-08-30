from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path

import pytest

from jarvis_personal_runtime.permissions import TomlPermissionStore
from jarvis_personal_runtime.responses import DirectResponsesRunner, ResponsesResult
from jarvis_personal_runtime.runtime import (
    ApprovalRequired,
    InboundText,
    PersonalRuntime,
)
from jarvis_personal_runtime.terminal import (
    CommandResult,
    NativeUbuntuExecutor,
    RunTerminalTool,
)


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


class FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Path, float, int]] = []

    async def run(
        self, command: str, *, cwd: Path, timeout: float, max_output_chars: int
    ) -> CommandResult:
        self.calls.append((command, cwd, timeout, max_output_chars))
        return CommandResult(exit_code=0, stdout="ok\n", stderr="")


class FailingExecutor(FakeExecutor):
    async def run(
        self, command: str, *, cwd: Path, timeout: float, max_output_chars: int
    ) -> CommandResult:
        self.calls.append((command, cwd, timeout, max_output_chars))
        raise OSError("process start failed")


class BlockingExecutor(FakeExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def run(
        self, command: str, *, cwd: Path, timeout: float, max_output_chars: int
    ) -> CommandResult:
        self.calls.append((command, cwd, timeout, max_output_chars))
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class OversizedExecutor(FakeExecutor):
    async def run(
        self, command: str, *, cwd: Path, timeout: float, max_output_chars: int
    ) -> CommandResult:
        self.calls.append((command, cwd, timeout, max_output_chars))
        return CommandResult(
            exit_code=0,
            stdout='"\\n' * 1000,
            stderr="error" * 1000,
            output_truncated=True,
        )


class MemoryTrace:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def record(self, event: str, payload: dict[str, object]) -> None:
        self.events.append((event, payload))


class FakeResponses:
    def __init__(self, *results: ResponsesResult) -> None:
        self.results = list(results)
        self.calls: list[dict[str, object]] = []

    async def create(
        self, request: dict[str, object], *, timeout: float
    ) -> ResponsesResult:
        self.calls.append(request)
        return self.results.pop(0)


NOW = datetime(2026, 8, 30, 12, tzinfo=UTC)


def inbound(message_id: str, text: str, *, hours: int = 0) -> InboundText:
    return InboundText(message_id, text, NOW + timedelta(hours=hours))


@pytest.mark.parametrize(
    "command",
    [
        "git status | head",
        "git status; id",
        "git status && id",
        "git status || id",
        "git status > result.txt",
        "git status $(id)",
        "git status $HOME",
        'git status "${HOME}"',
        'git status "`id`"',
        "git status\nid",
        "bash status.sh",
    ],
)
@async_test
async def test_compound_shell_syntax_never_inherits_read_only_authority(
    tmp_path: Path, command: str
) -> None:
    executor = FakeExecutor()
    tool = RunTerminalTool(
        working_directory=tmp_path,
        read_only_prefixes=("git status",),
        permission_store=TomlPermissionStore(tmp_path / "jarvis.toml"),
        executor=executor,
        timeout_seconds=17,
        max_output_chars=999,
    )

    result = await tool.execute("run_terminal", {"host": "ubuntu", "command": command})

    assert isinstance(result, ApprovalRequired)
    assert result.action.host == "ubuntu"
    assert result.action.prefix == command
    assert f"Command: {json.dumps(command)}" in result.action.display
    assert executor.calls == []


@async_test
async def test_unmatched_command_waits_indefinitely_and_option_two_saves_displayed_rule(
    tmp_path: Path,
) -> None:
    call = {
        "type": "function_call",
        "call_id": "call_terminal",
        "name": "run_terminal",
        "arguments": '{"host":"ubuntu","command":"touch marker"}',
    }
    responses = FakeResponses(
        ResponsesResult(output=(call,), output_text=""),
        ResponsesResult(output=(), output_text="Created it."),
    )
    executor = FakeExecutor()
    store = TomlPermissionStore(tmp_path / "jarvis.toml")
    tool = RunTerminalTool(
        working_directory=tmp_path,
        read_only_prefixes=("git status",),
        permission_store=store,
        executor=executor,
        timeout_seconds=17,
        max_output_chars=999,
    )
    runner = DirectResponsesRunner(
        responses, tools=tool, request_timeout_seconds=30, max_output_chars=999
    )
    runtime = PersonalRuntime(request_runner=runner, permission_store=store)

    pending = await runtime.receive(inbound("m1", "create a marker"))
    ignored = await runtime.receive(inbound("m2", "yes", hours=48))
    approved = await runtime.receive(inbound("m3", " 2 ", hours=72))

    assert pending.disposition == "approval_required"
    assert 'Permission literal prefix: "touch marker"' in pending.replies[0]
    assert ignored.disposition == "ignored"
    assert approved.replies == ("Created it.",)
    assert [(rule.host, rule.prefix) for rule in store.list_rules()] == [
        ("ubuntu", "touch marker")
    ]
    assert executor.calls == [("touch marker", tmp_path.resolve(), 17, 999)]


@async_test
async def test_rejection_returns_a_tool_result_without_running_the_command(
    tmp_path: Path,
) -> None:
    call = {
        "type": "function_call",
        "call_id": "call_terminal",
        "name": "run_terminal",
        "arguments": '{"host":"ubuntu","command":"touch marker"}',
    }
    responses = FakeResponses(
        ResponsesResult(output=(call,), output_text=""),
        ResponsesResult(output=(), output_text="I did not run it."),
    )
    executor = FakeExecutor()
    store = TomlPermissionStore(tmp_path / "jarvis.toml")
    runner = DirectResponsesRunner(
        responses,
        tools=RunTerminalTool(
            working_directory=tmp_path,
            read_only_prefixes=(),
            permission_store=store,
            executor=executor,
            timeout_seconds=17,
            max_output_chars=999,
        ),
        request_timeout_seconds=30,
    )
    runtime = PersonalRuntime(request_runner=runner, permission_store=store)

    await runtime.receive(inbound("r1", "create a marker"))
    rejected = await runtime.receive(inbound("r2", "9"))

    assert rejected.disposition == "rejected"
    assert rejected.replies == ("I did not run it.",)
    assert responses.calls[1]["input"][-1] == {
        "type": "function_call_output",
        "call_id": "call_terminal",
        "output": '{"rejected":true}',
    }
    assert executor.calls == []


@async_test
async def test_terminal_failure_is_returned_once_and_never_retried(
    tmp_path: Path,
) -> None:
    call = {
        "type": "function_call",
        "call_id": "call_terminal",
        "name": "run_terminal",
        "arguments": '{"host":"ubuntu","command":"git status"}',
    }
    responses = FakeResponses(
        ResponsesResult(output=(call,), output_text=""),
        ResponsesResult(output=(), output_text="The command could not start."),
    )
    executor = FailingExecutor()
    store = TomlPermissionStore(tmp_path / "jarvis.toml")
    runner = DirectResponsesRunner(
        responses,
        tools=RunTerminalTool(
            working_directory=tmp_path,
            read_only_prefixes=("git status",),
            permission_store=store,
            executor=executor,
            timeout_seconds=17,
            max_output_chars=999,
        ),
        request_timeout_seconds=30,
    )

    result = await runner.run(
        "check it",
        model="gpt-5.6-luna",
        reasoning="medium",
        system_prompt="Help.",
    )

    assert result.replies == ("The command could not start.",)
    assert len(executor.calls) == 1
    assert "OSError: process start failed" in responses.calls[1]["input"][-1]["output"]


@async_test
async def test_native_executor_uses_cwd_and_bounds_combined_output(
    tmp_path: Path,
) -> None:
    argv = [
        sys.executable,
        "-c",
        "import os,sys; print(os.getcwd()); print('x' * 1000); print('err', file=sys.stderr)",
    ]
    command = shlex.join(argv) if os.name == "posix" else subprocess.list2cmdline(argv)

    result = await NativeUbuntuExecutor().run(
        command, cwd=tmp_path, timeout=10, max_output_chars=256
    )

    assert result.exit_code == 0
    assert len(result.stdout) + len(result.stderr) == 256
    assert str(tmp_path) in result.stdout
    assert result.output_truncated


@async_test
async def test_terminal_cancellation_is_local_best_effort_and_not_retried(
    tmp_path: Path,
) -> None:
    executor = BlockingExecutor()
    trace = MemoryTrace()
    store = TomlPermissionStore(tmp_path / "jarvis.toml")
    tool = RunTerminalTool(
        working_directory=tmp_path,
        read_only_prefixes=("pwd",),
        permission_store=store,
        executor=executor,
        timeout_seconds=17,
        max_output_chars=999,
        trace=trace,
    )
    task = asyncio.create_task(
        tool.execute("run_terminal", {"host": "ubuntu", "command": "pwd"})
    )
    await executor.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(executor.calls) == 1
    assert trace.events[-1][0] == "terminal_execution_cancelled"
    assert trace.events[-1][1]["scope"] == "local_best_effort"


@async_test
async def test_serialized_terminal_tool_result_stays_within_output_limit(
    tmp_path: Path,
) -> None:
    store = TomlPermissionStore(tmp_path / "jarvis.toml")
    tool = RunTerminalTool(
        working_directory=tmp_path,
        read_only_prefixes=("pwd",),
        permission_store=store,
        executor=OversizedExecutor(),
        timeout_seconds=17,
        max_output_chars=120,
    )

    output = await tool.execute("run_terminal", {"host": "ubuntu", "command": "pwd"})

    assert isinstance(output, str)
    assert len(output) <= 120
    assert json.loads(output)["output_truncated"] is True


@pytest.mark.skipif(os.name != "posix", reason="native Ubuntu process-group test")
@async_test
async def test_native_timeout_contains_background_child_holding_output_pipes(
    tmp_path: Path,
) -> None:
    loop = asyncio.get_running_loop()
    started = loop.time()

    result = await NativeUbuntuExecutor().run(
        "sleep 5 &", cwd=tmp_path, timeout=0.1, max_output_chars=100
    )

    assert result.timed_out
    assert loop.time() - started < 2

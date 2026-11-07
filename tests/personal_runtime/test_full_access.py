"""Full access through the runtime, tool collection, and real terminal tool."""

import asyncio
from datetime import timedelta

import pytest

from jarvis_personal_runtime.permissions import TomlPermissionStore
from jarvis_personal_runtime.responses import (
    DirectResponsesRunner,
    PreparedToolCollection,
    ResponsesResult,
)
from jarvis_personal_runtime.runtime import ContextLimitReached, PersonalRuntime
from jarvis_personal_runtime.skill_tools import SkillTools
from jarvis_personal_runtime.terminal import RunTerminalTool

from .test_assistant_redesign import Provider, call
from .test_runtime import (
    FakeClock,
    FakeRunner,
    async_test,
    inbound,
)
from .test_terminal import FakeExecutor

DONE = ResponsesResult(output=(), output_text="Done.")


def setup_runtime(tmp_path, provider, executor=None, **limits):
    executor = executor or FakeExecutor()
    permissions = TomlPermissionStore(tmp_path / "jarvis.toml")
    tools = SkillTools(
        PreparedToolCollection(
            RunTerminalTool(
                working_directory=tmp_path,
                read_only_prefixes=(),
                executor=executor,
                permission_store=permissions,
                timeout_seconds=10,
                max_output_chars=65536,
            )
        )
    )
    runner = DirectResponsesRunner(
        provider,
        tools=tools,
        request_timeout_seconds=10,
        full_access=lambda: runtime.full_access,
        **limits,
    )
    runtime = PersonalRuntime(request_runner=runner, permission_store=permissions)
    return runtime, executor


@async_test
async def test_commands_and_session_lifetime():
    clock = FakeClock()
    runner = FakeRunner()
    runtime = PersonalRuntime(request_runner=runner, clock=clock)
    assert not runtime.full_access
    for i, command in enumerate(("/full-access", "/full-access", "/full-access nope")):
        result = await runtime.receive(inbound(str(i), command))
        assert result.status.full_access
    assert result.disposition == "malformed_command"
    assert (
        "Full access: on"
        in (await runtime.receive(inbound("status", "/status"))).replies[0]
    )
    for i, command in enumerate(("/full-access off", "/cancel", "/new")):
        await runtime.receive(inbound(f"enable-{i}", "/full-access"))
        await runtime.receive(inbound(f"reset-{i}", command))
        assert not runtime.full_access
    await runtime.receive(inbound("enable-expiry", "/full-access"))
    clock.current += timedelta(minutes=60)
    expired = await runtime.receive(inbound("expired", "/status"))
    assert not expired.status.full_access and expired.status.session_id is None
    await runtime.receive(inbound("enable-limit", "/full-access"))
    runner.step = ContextLimitReached()
    assert (
        await runtime.receive(inbound("limit", "Work"))
    ).disposition == "context_limit"
    assert not runtime.full_access
    await runtime.receive(inbound("enable-close", "/full-access"))
    await runtime.close()
    assert not runtime.full_access
    assert not PersonalRuntime(request_runner=FakeRunner()).full_access


@async_test
async def test_enable_resumes_pending_once_and_later_actions_run_automatically(
    tmp_path,
):
    provider = Provider(call(1), call(2), DONE, call(3), DONE)
    runtime, executor = setup_runtime(tmp_path, provider)
    saved = runtime.permission_store.add("ubuntu", "echo saved")
    pending = await runtime.receive(inbound("work", "Work"))
    assert pending.disposition == "approval_required" and not executor.calls
    status = await runtime.receive(inbound("status", "/status"))
    assert status.status.pending_action and "Full access: off" in status.replies[0]
    await runtime.receive(inbound("off", "/full-access off"))
    assert runtime.status().pending_action and not executor.calls
    malformed = await runtime.receive(inbound("bad", "/full-access yes"))
    assert malformed.disposition == "malformed_command" and not runtime.full_access
    result = await runtime.receive(inbound("on", "/full-access"))
    assert result.replies == ("Full access: on.", "Done.")
    assert len(executor.calls) == 2
    assert all("Full access: on" in x["instructions"] for x in provider.requests[1:])
    assert runtime.permission_store.list_rules() == (saved,)
    assert (
        await runtime.receive(inbound("on", "/full-access"))
    ).disposition == "duplicate"
    await runtime.receive(inbound("off-again", "/full-access off"))
    assert (
        await runtime.receive(inbound("more", "More"))
    ).disposition == "approval_required"
    assert len(executor.calls) == 2
    await runtime.receive(inbound("cancel", "/cancel"))
    assert not runtime.full_access and runtime.status().pending_action is None


@pytest.mark.parametrize("control", ["/full-access off", "/cancel", "/new"])
@pytest.mark.parametrize("from_pending", [False, True])
@async_test
async def test_control_during_execution(tmp_path, control, from_pending):
    class Executor(FakeExecutor):
        started = asyncio.Event()
        release = asyncio.Event()

        async def run(self, *args, **kwargs):
            result = await super().run(*args, **kwargs)
            self.started.set()
            await self.release.wait()
            return result

    provider = Provider(call(1), call(2), DONE)
    runtime, executor = setup_runtime(tmp_path, provider, Executor())
    if from_pending:
        assert (
            await runtime.receive(inbound("work", "Work"))
        ).disposition == "approval_required"
        work = asyncio.create_task(runtime.receive(inbound("on", "/full-access")))
    else:
        await runtime.receive(inbound("on", "/full-access"))
        work = asyncio.create_task(runtime.receive(inbound("work", "Work")))
    await asyncio.wait_for(executor.started.wait(), 2)
    await runtime.receive(inbound("control", control))
    assert not runtime.full_access
    executor.release.set()
    result = await asyncio.wait_for(work, 2)
    assert len(executor.calls) == 1
    assert "Full access: on." not in result.replies
    if control == "/full-access off":
        assert result.disposition == "approval_required"
        assert "Full access: off" in provider.requests[-1]["instructions"]
    else:
        assert result.disposition == "ignored"
        assert runtime.status().pending_action is None


@async_test
async def test_enable_while_provider_is_working_applies_to_next_tool(tmp_path):
    class WaitingProvider(Provider):
        started = asyncio.Event()
        release = asyncio.Event()

        async def create(self, request, *, timeout):
            self.started.set()
            await self.release.wait()
            return await super().create(request, timeout=timeout)

    provider = WaitingProvider(call(1), DONE)
    runtime, executor = setup_runtime(tmp_path, provider)
    work = asyncio.create_task(runtime.receive(inbound("work", "Work")))
    await asyncio.wait_for(provider.started.wait(), 2)
    await runtime.receive(inbound("on", "/full-access"))
    provider.release.set()
    result = await asyncio.wait_for(work, 2)
    assert result.disposition == "completed" and len(executor.calls) == 1
    assert "Full access: on" in provider.requests[-1]["instructions"]


@async_test
async def test_full_access_keeps_tool_limit_and_reports_execution_error(tmp_path):
    class Executor(FakeExecutor):
        async def run(self, *args, **kwargs):
            await super().run(*args, **kwargs)
            raise OSError("execution failed")

    provider = Provider(call(1), call(2), DONE)
    runtime, executor = setup_runtime(tmp_path, provider, Executor(), max_tool_rounds=1)
    await runtime.receive(inbound("on", "/full-access"))
    result = await runtime.receive(inbound("work", "Work"))
    assert result.disposition == "incomplete" and len(executor.calls) == 1
    assert "execution failed" in str(provider.requests[1]["input"])
    assert provider.requests[-1]["tools"] == []

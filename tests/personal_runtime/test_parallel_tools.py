"""Parallel prepared calls through the real runner and runtime boundaries."""

import asyncio
import json

import pytest

from jarvis_personal_runtime.permissions import TomlPermissionStore
from jarvis_personal_runtime.responses import ResponsesResult

from .test_assistant_redesign import Provider
from .test_full_access import DONE, setup_runtime
from .test_runtime import async_test, inbound
from .test_terminal import FakeExecutor


def batch(*commands):
    return ResponsesResult(
        output=tuple(
            {
                "type": "function_call",
                "name": "run_terminal",
                "call_id": f"call_{index}",
                "arguments": json.dumps({"host": "ubuntu", "command": command}),
            }
            for index, command in enumerate(commands)
        ),
        output_text="",
    )


@async_test
async def test_authorized_terminal_writes_overlap_and_return_in_call_order(tmp_path):
    class Executor(FakeExecutor):
        def __init__(self):
            super().__init__()
            self.both_started = asyncio.Event()

        async def run(self, *args, **kwargs):
            result = await super().run(*args, **kwargs)
            if len(self.calls) == 2:
                self.both_started.set()
            await asyncio.wait_for(self.both_started.wait(), 1)
            return result

    provider = Provider(batch("touch first", "touch second"), DONE)
    runtime, executor = setup_runtime(tmp_path, provider, Executor())
    await runtime.receive(inbound("access", "/full-access"))
    result = await runtime.receive(inbound("work", "Create both files"))
    assert result.disposition == "completed"
    assert executor.both_started.is_set()
    outputs = [
        x
        for x in provider.requests[-1]["input"]
        if x.get("type") == "function_call_output"
    ]
    assert [x["call_id"] for x in outputs] == ["call_0", "call_1"]
    assert all(json.loads(x["output"])["exit_code"] == 0 for x in outputs)
    assert provider.requests[0]["parallel_tool_calls"] is True


class BlockingExecutor(FakeExecutor):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.stopped = asyncio.Event()

    async def run(self, *args, **kwargs):
        result = await super().run(*args, **kwargs)
        self.started.set()
        try:
            await self.release.wait()
            return result
        finally:
            self.stopped.set()


@async_test
async def test_cancel_during_timeout_cleanup_still_drains_executor(tmp_path):
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleaned = asyncio.Event()

    class Executor(FakeExecutor):
        async def run(self, *args, **kwargs):
            await super().run(*args, **kwargs)
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await release_cleanup.wait()
                cleaned.set()

    provider = Provider(batch("touch slow"))
    runtime, _executor = setup_runtime(
        tmp_path,
        provider,
        Executor(),
        request_timeout_seconds=0.2,
    )
    await runtime.receive(inbound("access", "/full-access"))
    work = asyncio.create_task(runtime.receive(inbound("work", "Work")))
    await asyncio.wait_for(cleanup_started.wait(), 2)
    cancel = asyncio.create_task(runtime.receive(inbound("cancel", "/cancel")))
    await asyncio.sleep(0)
    release_cleanup.set()
    await asyncio.wait_for(cancel, 2)
    await asyncio.wait_for(work, 2)
    assert cleaned.is_set()


@pytest.mark.parametrize("control", ["/cancel", "/new", "shutdown"])
@async_test
async def test_approval_is_visible_while_sibling_runs_and_controls_drain_it(
    tmp_path, control
):
    provider = Provider(batch("touch protected", "echo allowed"), DONE)
    runtime, executor = setup_runtime(tmp_path, provider, BlockingExecutor())
    runtime.permission_store.add("ubuntu", "echo allowed")
    result = await asyncio.wait_for(runtime.receive(inbound("work", "Work")), 2)
    assert result.disposition == "approval_required"
    assert "touch protected" in result.replies[0]
    assert executor.started.is_set() and not executor.stopped.is_set()
    assert (await runtime.receive(inbound("ignored", "hello"))).disposition == "ignored"
    if control == "shutdown":
        await runtime.close()
    else:
        await runtime.receive(inbound("control", control))
    assert executor.stopped.is_set()
    assert runtime.status().pending_action is None
    assert len(provider.requests) == 1


@async_test
async def test_next_approval_does_not_wait_for_approved_action_and_rejects_only_it(
    tmp_path,
):
    provider = Provider(batch("touch first", "touch second"), DONE)
    runtime, executor = setup_runtime(tmp_path, provider, BlockingExecutor())
    first = await runtime.receive(inbound("work", "Work"))
    assert "touch first" in first.replies[0]
    second = await asyncio.wait_for(runtime.receive(inbound("yes", "1")), 2)
    assert "touch second" in second.replies[0]
    assert executor.started.is_set() and not executor.stopped.is_set()
    rejected = asyncio.create_task(runtime.receive(inbound("no", "9")))
    await asyncio.sleep(0)
    assert len(provider.requests) == 1
    executor.release.set()
    result = await asyncio.wait_for(rejected, 2)
    assert result.disposition == "rejected"
    assert len(executor.calls) == 1
    outputs = [
        x
        for x in provider.requests[-1]["input"]
        if x.get("type") == "function_call_output"
    ]
    assert json.loads(outputs[0]["output"])["exit_code"] == 0
    assert json.loads(outputs[1]["output"])["rejected"] is True


@pytest.mark.parametrize("full_access", [False, True])
@pytest.mark.parametrize("limit,count", [(5, 6), (2, 3)])
@async_test
async def test_oversized_batch_has_no_effects_and_corrections_consume_allowance(
    tmp_path, full_access, limit, count
):
    oversized = batch(*(f"touch file_{i}" for i in range(count)))
    provider = Provider(oversized, oversized, DONE)
    runtime, executor = setup_runtime(
        tmp_path, provider, max_tool_rounds=count * 2 - 1, max_parallel_tool_calls=limit
    )
    if full_access:
        await runtime.receive(inbound("access", "/full-access"))
    result = await runtime.receive(inbound("work", "Work"))
    assert result.disposition == "incomplete"
    assert executor.calls == []
    assert len(provider.requests) == 3
    outputs = [
        x
        for x in provider.requests[1]["input"]
        if x.get("type") == "function_call_output"
    ]
    assert [x["call_id"] for x in outputs] == [f"call_{i}" for i in range(count)]
    assert all(
        f"Maximum {limit}" in x["output"] and "smaller batch" in x["output"]
        for x in outputs
    )
    assert provider.requests[-1]["tools"] == []
    assert f"at most {limit}" in provider.requests[0]["instructions"]


@async_test
async def test_remaining_allowance_counts_each_call_across_approvals(tmp_path):
    provider = Provider(batch("touch first", "touch second", "touch excess"), DONE)
    runtime, executor = setup_runtime(tmp_path, provider, max_tool_rounds=2)
    first = await runtime.receive(inbound("work", "Work"))
    assert first.disposition == "approval_required"
    second = await runtime.receive(inbound("yes", "1"))
    assert "touch second" in second.replies[0]
    result = await runtime.receive(inbound("no", "9"))
    assert result.disposition == "incomplete"
    assert len(executor.calls) == 1
    assert len(provider.requests) == 2
    assert provider.requests[-1]["tools"] == []
    outputs = [
        x
        for x in provider.requests[-1]["input"]
        if x.get("type") == "function_call_output"
    ]
    assert json.loads(outputs[-1]["output"])["outcome"] == "not_attempted"


@async_test
async def test_human_wait_outlives_request_timeout_and_retains_timed_out_sibling(
    tmp_path,
):
    provider = Provider(batch("echo allowed", "touch protected"), DONE)
    runtime, executor = setup_runtime(
        tmp_path,
        provider,
        BlockingExecutor(),
        request_timeout_seconds=0.2,
    )
    runtime.permission_store.add("ubuntu", "echo allowed")
    pending = await runtime.receive(inbound("work", "Work"))
    assert pending.disposition == "approval_required"
    await asyncio.wait_for(executor.stopped.wait(), 2)
    # The active call expired; the human proposal and model allowance did not.
    assert runtime.status().pending_action is not None
    result = await runtime.receive(inbound("no", "9"))
    assert result.disposition == "rejected"
    outputs = [
        x
        for x in provider.requests[-1]["input"]
        if x.get("type") == "function_call_output"
    ]
    assert "TimeoutError" in outputs[0]["output"]
    assert json.loads(outputs[1]["output"])["rejected"]
    assert len(executor.calls) == 1


@async_test
async def test_cancel_preserves_success_and_distinguishes_unstarted_from_uncertain(
    tmp_path,
):
    class Executor(BlockingExecutor):
        async def run(self, command, **kwargs):
            if command == "echo complete":
                return await FakeExecutor.run(self, command, **kwargs)
            return await super().run(command, **kwargs)

    provider = Provider(batch("echo complete", "echo slow", "touch protected"), DONE)
    runtime, executor = setup_runtime(tmp_path, provider, Executor())
    runtime.permission_store.add("ubuntu", "echo")
    await runtime.receive(inbound("work", "Work"))
    await runtime.receive(inbound("cancel", "/cancel"))
    await runtime.receive(inbound("followup", "What happened?"))
    outputs = [
        json.loads(x["output"])
        for x in provider.requests[-1]["input"]
        if x.get("type") == "function_call_output"
    ]
    assert outputs[0]["exit_code"] == 0
    assert outputs[1]["outcome"] == "unknown"
    assert outputs[2]["outcome"] == "not_attempted"
    assert len(executor.calls) == 2


@async_test
async def test_full_access_resolves_all_retained_proposals_once(tmp_path):
    provider = Provider(batch("touch first", "touch second", "touch third"), DONE)
    runtime, executor = setup_runtime(tmp_path, provider)
    await runtime.receive(inbound("work", "Work"))
    result = await runtime.receive(inbound("access", "/full-access"))
    assert result.disposition == "completed"
    assert len(executor.calls) == 3
    assert len(provider.requests) == 2


@async_test
async def test_tool_failure_keeps_successful_sibling(tmp_path):
    class Executor(FakeExecutor):
        async def run(self, command, **kwargs):
            if command == "fail":
                raise OSError("remote outcome uncertain")
            return await super().run(command, **kwargs)

    provider = Provider(batch("fail", "echo success"), DONE)
    runtime, _executor = setup_runtime(tmp_path, provider, Executor())
    await runtime.receive(inbound("access", "/full-access"))
    result = await runtime.receive(inbound("work", "Work"))
    assert result.disposition == "completed"
    outputs = [
        json.loads(x["output"])
        for x in provider.requests[-1]["input"]
        if x.get("type") == "function_call_output"
    ]
    assert "remote outcome uncertain" in outputs[0]["error"]
    assert outputs[1]["exit_code"] == 0


@async_test
async def test_approval_is_delivered_by_openwa_before_sibling_finishes(tmp_path):
    from jarvis_personal_runtime.openwa import OpenWAMessageFlow

    from .test_openwa_message_flow import (
        SECRET,
        _event,
        _headers,
        _RecordingSender,
        _settings,
    )

    provider = Provider(batch("touch protected", "echo allowed"), DONE)
    runtime, executor = setup_runtime(tmp_path, provider, BlockingExecutor())
    runtime.permission_store.add("ubuntu", "echo allowed")
    sender = _RecordingSender()
    flow = OpenWAMessageFlow(
        settings=_settings(),
        signing_secret=SECRET,
        runtime=runtime,
        sender=sender,
    )
    raw = _event(body="Work")
    assert flow.receive_webhook(raw, _headers(raw)).status_code == 202
    await asyncio.wait_for(flow.drain(), 2)
    assert len(sender.sent) == 1 and "touch protected" in sender.sent[0][1]
    assert executor.started.is_set() and not executor.stopped.is_set()
    await runtime.close()
    assert executor.stopped.is_set() and len(sender.sent) == 1


@async_test
async def test_google_write_overlaps_terminal_through_real_prepared_tools(tmp_path):
    import httpx

    from jarvis_personal_runtime.responses import (
        DirectResponsesRunner,
        PreparedToolCollection,
    )
    from jarvis_personal_runtime.runtime import PersonalRuntime
    from jarvis_personal_runtime.terminal import RunTerminalTool

    from .test_google import make_tools

    terminal_started, write_started = asyncio.Event(), asyncio.Event()

    class Executor(FakeExecutor):
        async def run(self, *args, **kwargs):
            result = await super().run(*args, **kwargs)
            terminal_started.set()
            await asyncio.wait_for(write_started.wait(), 2)
            return result

    async def handler(request):
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "person@example.com"})
        assert request.method == "POST"
        write_started.set()
        await asyncio.wait_for(terminal_started.wait(), 2)
        return httpx.Response(200, json={"id": "created-event"})

    google, _, client, _ = make_tools(handler)
    terminal = RunTerminalTool(
        working_directory=tmp_path,
        read_only_prefixes=(),
        executor=Executor(),
        timeout_seconds=10,
        max_output_chars=65536,
        permission_store=TomlPermissionStore(tmp_path / "jarvis.toml"),
    )
    write = {
        "type": "function_call",
        "call_id": "write",
        "name": "google_calendar_create",
        "arguments": json.dumps(
            {
                "calendarId": "primary",
                "summary": "Review",
                "startTime": "2026-11-11T09:00:00+03:00",
                "endTime": "2026-11-11T10:00:00+03:00",
                "timeZone": "Asia/Amman",
            }
        ),
    }
    provider = Provider(
        ResponsesResult((*batch("echo allowed").output, write), ""), DONE
    )
    runner = DirectResponsesRunner(
        provider,
        tools=PreparedToolCollection(terminal, google),
        request_timeout_seconds=10,
        full_access=lambda: runtime.full_access,
    )
    runtime = PersonalRuntime(request_runner=runner)
    try:
        await google.connect()
        await runtime.receive(inbound("access", "/full-access"))
        result = await runtime.receive(inbound("work", "Work"))
        assert result.disposition == "completed"
        assert terminal_started.is_set() and write_started.is_set()
        assert "created-event" in provider.requests[-1]["input"][-1]["output"]
    finally:
        await runtime.close()
        await client.aclose()


@pytest.mark.parametrize("control", ["/cancel", "/new", "shutdown", "9"])
def test_reader_lives_through_approval_and_is_drained_at_request_end(
    monkeypatch, tmp_path, control
):
    from jarvis_personal_runtime.config import WebReaderConfig
    from jarvis_personal_runtime.playwright_reader import PlaywrightReaderTools
    from jarvis_personal_runtime.responses import (
        DirectResponsesRunner,
        PreparedToolCollection,
    )
    from jarvis_personal_runtime.runtime import PersonalRuntime
    from jarvis_personal_runtime.terminal import RunTerminalTool

    from .test_concurrent_readers import AssignmentModel, invoke
    from .test_playwright_browser import public_fixture
    from .test_playwright_reader import call

    async def scenario(url):
        model = AssignmentModel()
        readers = PlaywrightReaderTools(WebReaderConfig(), model)
        terminal = RunTerminalTool(
            working_directory=tmp_path,
            read_only_prefixes=(),
            executor=FakeExecutor(),
            timeout_seconds=10,
            max_output_chars=65536,
            permission_store=TomlPermissionStore(tmp_path / "jarvis.toml"),
        )
        provider = Provider(
            call("start_browser_reader", url=url, prompt="research"),
            batch("touch protected"),
            DONE,
        )
        runner = DirectResponsesRunner(
            provider,
            tools=PreparedToolCollection(readers, terminal),
            request_timeout_seconds=45,
        )
        runtime = PersonalRuntime(request_runner=runner)
        try:
            pending = await runtime.receive(inbound("work", "Work"))
            assert pending.disposition == "approval_required"
            rid = json.loads(provider.requests[1]["input"][-1]["output"])["reader_id"]
            await model.arrived()
            state = await invoke(readers, "browser_reader_status", reader_id=rid)
            assert state["state"] == "running" and state["browser_state"] == "open"
            assert model.requests[0]["parallel_tool_calls"] is False
            if control == "9":
                model.gates["research"].set()
                completed = await invoke(
                    readers, "wait_browser_reader", reader_id=rid, seconds=10
                )
                assert completed["state"] == "completed"
                assert runtime.status().pending_action is not None
            if control == "shutdown":
                await runtime.close()
            else:
                await runtime.receive(inbound("control", control))
            state = await invoke(readers, "browser_reader_status", reader_id=rid)
            if control in {"/new", "shutdown"}:
                assert state["error"] == "expired_reader_id"
            else:
                assert state["browser_state"] == "closed"
                assert state["state"] == (
                    "completed" if control == "9" else "cancelled"
                )
            assert len(provider.requests) == (3 if control == "9" else 2)
        finally:
            await runtime.close()
            await readers.close_request()

    with public_fixture(monkeypatch) as url:
        asyncio.run(scenario(url))

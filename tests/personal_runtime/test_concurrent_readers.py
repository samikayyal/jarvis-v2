"""Concurrent and retained readers at the public prepared-tool boundary."""

import asyncio
import copy
import json

from jarvis_personal_runtime.config import WebReaderConfig
from jarvis_personal_runtime.playwright_reader import PlaywrightReaderTools
from jarvis_personal_runtime.responses import ResponsesResult

from .test_playwright_browser import public_fixture


async def invoke(tools, name, **arguments):
    return json.loads(await tools.execute(name, arguments))


class AssignmentModel:
    """Control only the external provider boundary; use real browsers."""

    def __init__(self):
        self.requests = []
        self.arrivals = asyncio.Queue()
        self.gates = {}

    async def create(self, request, *, timeout):
        prompt = next(
            item["content"]
            for item in reversed(request["input"])
            if item.get("role") == "user"
            and isinstance(item.get("content"), str)
            and not item["content"].startswith("{")
        )
        self.requests.append(copy.deepcopy(request))
        gate = self.gates.setdefault(prompt, asyncio.Event())
        await self.arrivals.put(prompt)
        await gate.wait()
        return ResponsesResult((), "Findings for " + prompt)

    async def arrived(self, count=1):
        return [await asyncio.wait_for(self.arrivals.get(), 20) for _ in range(count)]


def test_three_real_readers_overlap_and_deliver_out_of_order(monkeypatch):
    async def scenario(url):
        model = AssignmentModel()
        tools = PlaywrightReaderTools(WebReaderConfig(), model)
        try:
            starts = [
                await invoke(tools, "start_browser_reader", url=url, prompt=p)
                for p in ("slow", "fast", "other")
            ]
            assert all(r.get("state") == "running" for r in starts), starts
            assert set(await model.arrived(3)) == {"slow", "fast", "other"}
            rejected = await invoke(
                tools, "start_browser_reader", url=url, prompt="extra"
            )
            assert rejected["error"] == "reader_capacity"
            busy = await invoke(
                tools,
                "wait_browser_reader",
                reader_id=starts[0]["reader_id"],
                seconds=0.01,
            )
            assert busy["state"] == "running"
            model.gates["fast"].set()
            fast = await invoke(
                tools,
                "wait_browser_reader",
                reader_id=starts[1]["reader_id"],
                seconds=10,
            )
            assert fast["state"] == "completed"
            assert fast["findings"] == "Findings for fast"
            assert (
                await invoke(
                    tools, "browser_reader_status", reader_id=starts[0]["reader_id"]
                )
            )["state"] == "running"
        finally:
            await tools.close_request()
        assert all(r.browser.closed for r in tools.readers.values())

    with public_fixture(monkeypatch) as url:
        asyncio.run(scenario(url))


def test_followups_retain_conversation_reject_busy_and_reset_turn_budget(monkeypatch):
    from dataclasses import replace

    async def scenario(url):
        model = AssignmentModel()
        tools = PlaywrightReaderTools(replace(WebReaderConfig(), max_attempts=1), model)
        try:
            start = await invoke(tools, "start_browser_reader", url=url, prompt="first")
            rid = start["reader_id"]
            await model.arrived()
            busy = await invoke(
                tools, "follow_up_browser_reader", reader_id=rid, prompt="queued"
            )
            assert busy["error"] == "reader_busy"
            model.gates["first"].set()
            assert (
                await invoke(tools, "wait_browser_reader", reader_id=rid, seconds=10)
            )["state"] == "completed"
            follow = await invoke(
                tools, "follow_up_browser_reader", reader_id=rid, prompt="second"
            )
            assert follow["state"] == "running"
            assert follow["model_turns"] == 0
            await model.arrived()
            second_input = json.dumps(model.requests[-1]["input"])
            assert "first" in second_input and "Findings for first" in second_input
            assert "queued" not in second_input
            model.gates["second"].set()
            result = await invoke(
                tools, "wait_browser_reader", reader_id=rid, seconds=10
            )
            assert result["state"] == "completed" and result["model_turns"] == 1
            assert result["browser_state"] == "open"
            await tools.close_request()
            tools.request_started()
            assert (await invoke(tools, "browser_reader_status", reader_id=rid))[
                "findings"
            ] == "Findings for second"
            follow = await invoke(
                tools, "follow_up_browser_reader", reader_id=rid, prompt="third"
            )
            assert "lost" in follow["browser_state_notice"].lower()
            await model.arrived()
            assert "interactive state" in json.dumps(model.requests[-1]["input"])
            observation = json.loads(model.requests[-1]["input"][-1]["content"])
            assert observation["browser_state"] == "open"
            assert "new browser is open" in observation["browser_state_notice"].lower()
            assert "Findings for second" in json.dumps(model.requests[-1]["input"])
            model.gates["third"].set()
            assert (
                await invoke(tools, "wait_browser_reader", reader_id=rid, seconds=10)
            )["state"] == "completed"
        finally:
            await tools.close_request()

    with public_fixture(monkeypatch) as url:
        asyncio.run(scenario(url))


def test_retention_requires_release_and_reclaims_least_recent_idle_browser(monkeypatch):
    from dataclasses import replace

    async def scenario(url):
        model = AssignmentModel()
        tools = PlaywrightReaderTools(
            replace(WebReaderConfig(), max_readers=3, max_browser_contexts=2), model
        )
        ids = []

        async def complete(prompt):
            started = await invoke(
                tools, "start_browser_reader", url=url, prompt=prompt
            )
            ids.append(started["reader_id"])
            await model.arrived()
            model.gates[prompt].set()
            return await invoke(
                tools, "wait_browser_reader", reader_id=ids[-1], seconds=10
            )

        try:
            await complete("one")
            await complete("two")
            # Touch the first reader again: the second becomes least recently used.
            await invoke(
                tools, "follow_up_browser_reader", reader_id=ids[0], prompt="one-again"
            )
            await model.arrived()
            model.gates["one-again"].set()
            await invoke(tools, "wait_browser_reader", reader_id=ids[0], seconds=10)
            await complete("three")
            assert (await invoke(tools, "browser_reader_status", reader_id=ids[0]))[
                "browser_state"
            ] == "open"
            evicted = await invoke(tools, "browser_reader_status", reader_id=ids[1])
            assert evicted["browser_state"] == "closed"
            assert evicted["findings"] == "Findings for two"
            assert "reclaimed" in evicted["browser_state_notice"]
            capacity = await invoke(
                tools, "start_browser_reader", url=url, prompt="four"
            )
            assert (
                capacity["error"] == "reader_capacity"
                and "release" in capacity["reason"].lower()
            )
            released = await invoke(tools, "release_browser_reader", reader_id=ids[0])
            assert released["state"] == "released"
            assert (await invoke(tools, "browser_reader_status", reader_id=ids[0]))[
                "error"
            ] == "expired_reader_id"
            reopened = await invoke(
                tools, "follow_up_browser_reader", reader_id=ids[1], prompt="reopened"
            )
            assert "lost" in reopened["browser_state_notice"]
            await model.arrived()
            assert "Findings for two" in json.dumps(model.requests[-1]["input"])
        finally:
            await tools.close_request()

    with public_fixture(monkeypatch) as url:
        asyncio.run(scenario(url))


def test_cancel_prevents_late_provider_result_and_frees_active_capacity(monkeypatch):
    from dataclasses import replace

    class LateModel:
        def __init__(self):
            self.started = asyncio.Event()

        async def create(self, request, *, timeout):
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return ResponsesResult((), "LATE ANSWER MUST NOT SURVIVE")

    async def scenario(url):
        model = LateModel()
        tools = PlaywrightReaderTools(
            replace(WebReaderConfig(), max_active_readers=1), model
        )
        try:
            start = await invoke(
                tools, "start_browser_reader", url=url, prompt="cancel"
            )
            await asyncio.wait_for(model.started.wait(), 20)
            result = await invoke(
                tools, "cancel_browser_reader", reader_id=start["reader_id"]
            )
            assert result["state"] == "cancelled"
            assert not result["findings"]
            next_start = await invoke(
                tools, "start_browser_reader", url=url, prompt="next"
            )
            assert next_start["state"] == "running"
            tools.start_session()
            assert (
                await invoke(
                    tools, "browser_reader_status", reader_id=start["reader_id"]
                )
            )["error"] == "expired_reader_id"
        finally:
            await tools.close_request()

    with public_fixture(monkeypatch) as url:
        asyncio.run(scenario(url))


def test_browser_crash_fails_waiting_assignment_and_preserves_other_readers(
    monkeypatch,
):
    async def scenario(url):
        model = AssignmentModel()
        tools = PlaywrightReaderTools(WebReaderConfig(), model)
        try:
            starts = [
                await invoke(tools, "start_browser_reader", url=url, prompt=p)
                for p in ("crash", "survivor")
            ]
            await model.arrived(2)
            # External fault injection, using the real Chromium process boundary.
            crashed_browser = tools.readers[starts[0]["reader_id"]].browser
            await crashed_browser._browser.close()
            failed = await invoke(
                tools,
                "wait_browser_reader",
                reader_id=starts[0]["reader_id"],
                seconds=2,
            )
            assert failed["state"] == "failed", failed
            assert "lost" in failed["reason"] and failed["browser_state"] == "closed"
            model.gates["survivor"].set()
            survivor = await invoke(
                tools,
                "wait_browser_reader",
                reader_id=starts[1]["reader_id"],
                seconds=10,
            )
            assert survivor["state"] == "completed"
        finally:
            await tools.close_request()

    with public_fixture(monkeypatch) as url:
        asyncio.run(scenario(url))


def test_cancellation_during_browser_navigation_and_reset_close_resources(monkeypatch):
    import threading

    entered, release = threading.Event(), threading.Event()

    async def scenario(url):
        model = AssignmentModel()
        tools = PlaywrightReaderTools(WebReaderConfig(), model)
        try:
            start = await invoke(
                tools, "start_browser_reader", url=url + "slow", prompt="navigation"
            )
            assert await asyncio.to_thread(entered.wait, 15)
            cancelled = await invoke(
                tools, "cancel_browser_reader", reader_id=start["reader_id"]
            )
            assert (
                cancelled["state"] == "cancelled"
                and cancelled["browser_state"] == "closed"
            )
            assert not model.requests
            # Reset while another real navigation is in flight, then start anew.
            entered.clear()
            start = await invoke(
                tools, "start_browser_reader", url=url + "slow", prompt="reset"
            )
            assert await asyncio.to_thread(entered.wait, 15)
            tools.start_session()
            assert (
                await invoke(
                    tools, "browser_reader_status", reader_id=start["reader_id"]
                )
            )["error"] == "expired_reader_id"
            new = await invoke(tools, "start_browser_reader", url=url, prompt="fresh")
            await model.arrived()
            model.gates["fresh"].set()
            assert (
                await invoke(
                    tools, "wait_browser_reader", reader_id=new["reader_id"], seconds=10
                )
            )["state"] == "completed"
        finally:
            release.set()
            await tools.close_request()
        assert all(r.browser.closed for r in tools.readers.values())

    with public_fixture(monkeypatch, slow_page=(entered, release)) as url:
        asyncio.run(scenario(url))


def test_followup_after_interrupted_browser_action_has_valid_retained_history(
    monkeypatch,
):
    import threading

    from .test_playwright_reader import call

    entered, release = threading.Event(), threading.Event()

    class InterruptedModel:
        async def create(self, request, *, timeout):
            if request["input"][0]["content"] == request["input"][-2].get("content"):
                observation = json.loads(request["input"][-1]["content"])
                ref = next(
                    e["ref"]
                    for e in observation["elements"]
                    if e["label"] == "Slow page"
                )
                return call("browser_click", ref=ref)
            calls = {
                i["call_id"]
                for i in request["input"]
                if i.get("type") == "function_call"
            }
            answered = {
                i["call_id"]
                for i in request["input"]
                if i.get("type") == "function_call_output"
            }
            assert calls <= answered, "Interrupted tool call missing its output"
            return ResponsesResult((), "Recovered using retained conversation")

    async def scenario(url):
        tools = PlaywrightReaderTools(WebReaderConfig(), InterruptedModel())
        try:
            start = await invoke(
                tools, "start_browser_reader", url=url, prompt="initial"
            )
            assert await asyncio.to_thread(entered.wait, 15)
            await invoke(tools, "cancel_browser_reader", reader_id=start["reader_id"])
            follow = await invoke(
                tools,
                "follow_up_browser_reader",
                reader_id=start["reader_id"],
                prompt="recover",
            )
            assert follow["state"] == "running"
            result = await invoke(
                tools, "wait_browser_reader", reader_id=start["reader_id"], seconds=15
            )
            assert result["state"] == "completed", result
        finally:
            release.set()
            await tools.close_request()

    with public_fixture(monkeypatch, slow_page=(entered, release)) as url:
        asyncio.run(scenario(url))


def test_parallel_contexts_isolate_state_and_followups_keep_surviving_tabs(monkeypatch):
    from .test_playwright_reader import call

    class StateModel:
        def __init__(self):
            self.arrivals = asyncio.Queue()
            self.release = asyncio.Event()
            self.observations = {}

        async def create(self, request, *, timeout):
            prompt = next(
                i["content"]
                for i in reversed(request["input"])
                if i.get("role") == "user"
                and isinstance(i.get("content"), str)
                and not i["content"].startswith("{")
            )
            if request["input"][-1].get("type") == "function_call_output":
                result = json.loads(request["input"][-1]["output"])
                self.observations[prompt] = result
                return ResponsesResult((), "State inspected: " + prompt)
            observation = json.loads(request["input"][-1]["content"])
            self.observations[prompt] = observation
            await self.arrivals.put(prompt)
            await self.release.wait()
            if prompt == "writer":
                return call(
                    "browser_click",
                    ref=next(
                        e["ref"]
                        for e in observation["elements"]
                        if e["label"] == "Open tab"
                    ),
                )
            return call("browser_tabs")

    async def scenario(url):
        model = StateModel()
        tools = PlaywrightReaderTools(WebReaderConfig(), model)
        try:
            writer = await invoke(
                tools, "start_browser_reader", url=url + "set-state", prompt="writer"
            )
            observer = await invoke(
                tools, "start_browser_reader", url=url + "show-state", prompt="observer"
            )
            arrivals = {
                await asyncio.wait_for(model.arrivals.get(), 20) for _ in range(2)
            }
            assert arrivals == {"writer", "observer"}
            assert "EMPTY" in model.observations["observer"]["text"]
            assert "research=one" not in model.observations["observer"]["text"]
            model.release.set()
            for start in (writer, observer):
                assert (
                    await invoke(
                        tools,
                        "wait_browser_reader",
                        reader_id=start["reader_id"],
                        seconds=10,
                    )
                )["state"] == "completed"
            assert len(model.observations["observer"]["tabs"]) == 1
            await invoke(
                tools,
                "follow_up_browser_reader",
                reader_id=writer["reader_id"],
                prompt="retained",
            )
            assert (
                await invoke(
                    tools,
                    "wait_browser_reader",
                    reader_id=writer["reader_id"],
                    seconds=10,
                )
            )["state"] == "completed"
            assert len(model.observations["retained"]["tabs"]) == 2
            assert any(
                t["url"].endswith("/set-state")
                for t in model.observations["retained"]["tabs"]
            )
        finally:
            await tools.close_request()

    with public_fixture(monkeypatch) as url:
        asyncio.run(scenario(url))


def test_main_runtime_keeps_tools_sequential_and_cancel_stops_readers(monkeypatch):
    from jarvis_personal_runtime.responses import (
        DirectResponsesRunner,
        PreparedToolCollection,
    )
    from jarvis_personal_runtime.runtime import PersonalRuntime
    from jarvis_personal_runtime.skill_tools import SkillTools

    from .test_playwright_reader import call
    from .test_runtime import inbound

    async def scenario(url):
        model = AssignmentModel()
        readers = PlaywrightReaderTools(WebReaderConfig(), model)
        main_waiting = asyncio.Event()
        ids = []

        class OtherMainTool:
            definitions = (
                {
                    "type": "function",
                    "name": "other_work",
                    "parameters": {"type": "object", "properties": {}},
                },
            )
            active = 0
            calls = 0

            async def execute(self, name, arguments):
                self.active += 1
                assert self.active == 1
                self.calls += 1
                assert set(await model.arrived(3)) == {"slow", "fast", "third"}
                model.gates["fast"].set()
                await asyncio.sleep(0)
                self.active -= 1
                return "main mutation completed"

        class MainModel:
            step = 0

            async def create(self, request, *, timeout):
                assert request["parallel_tool_calls"] is False
                self.step += 1
                if self.step == 1:
                    return call("load_skill", name="web")
                if 2 <= self.step <= 4:
                    if self.step > 2:
                        ids.append(
                            json.loads(request["input"][-1]["output"])["reader_id"]
                        )
                    return call(
                        "start_browser_reader",
                        url=url,
                        prompt=("slow", "fast", "third")[self.step - 2],
                    )
                if self.step == 5:
                    ids.append(json.loads(request["input"][-1]["output"])["reader_id"])
                    return call("other_work")
                if self.step == 6:
                    return call("wait_browser_reader", reader_id=ids[1], seconds=10)
                assert (
                    json.loads(request["input"][-1]["output"])["state"] == "completed"
                )
                main_waiting.set()
                await asyncio.Event().wait()

        other = OtherMainTool()
        runner = DirectResponsesRunner(
            MainModel(),
            tools=SkillTools(PreparedToolCollection(readers, other)),
            request_timeout_seconds=60,
        )
        runtime = PersonalRuntime(request_runner=runner)
        work = asyncio.create_task(
            runtime.receive(inbound("work", "PRIVATE_MAIN_CONTEXT research"))
        )
        try:
            await asyncio.wait_for(main_waiting.wait(), 30)
            assert other.calls == 1 and other.active == 0
            busy = await runtime.receive(inbound("busy", "another ordinary request"))
            assert busy.disposition == "busy"
            status = await runtime.receive(inbound("status", "/status"))
            assert status.status.active_request
            cancelled = await runtime.receive(inbound("cancel", "/cancel"))
            assert cancelled.disposition == "cancelled"
            stopped = await work
            assert not stopped.replies
            assert (await invoke(readers, "browser_reader_status", reader_id=ids[1]))[
                "findings"
            ] == "Findings for fast"
            assert (await invoke(readers, "browser_reader_status", reader_id=ids[0]))[
                "state"
            ] == "cancelled"
            assert all(r.browser.closed for r in readers.readers.values())
            assert "PRIVATE_MAIN_CONTEXT" not in json.dumps(model.requests)
            await runtime.receive(inbound("new", "/new"))
            assert (await invoke(readers, "browser_reader_status", reader_id=ids[1]))[
                "error"
            ] == "expired_reader_id"
        finally:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            await readers.close_request()

    with public_fixture(monkeypatch) as url:
        asyncio.run(scenario(url))


def test_runtime_inactivity_context_reset_and_restart_discard_reader_state(monkeypatch):
    from datetime import timedelta

    from jarvis_personal_runtime.responses import DirectResponsesRunner
    from jarvis_personal_runtime.runtime import PersonalRuntime

    from .test_playwright_reader import call
    from .test_runtime import FakeClock, inbound

    async def scenario(url, reset):
        model = AssignmentModel()
        readers = PlaywrightReaderTools(WebReaderConfig(), model)
        forced_limit = False

        class MainModel:
            stage = 0
            rid = None

            async def create(self, request, *, timeout):
                self.stage += 1
                if self.stage == 1:
                    return call("start_browser_reader", url=url, prompt="retained")
                if self.stage == 2:
                    self.rid = json.loads(request["input"][-1]["output"])["reader_id"]
                    await model.arrived()
                    model.gates["retained"].set()
                    return call("wait_browser_reader", reader_id=self.rid, seconds=10)
                return ResponsesResult((), "Research done")

        main = MainModel()
        clock = FakeClock()
        runner = DirectResponsesRunner(
            main,
            tools=readers,
            request_timeout_seconds=45,
            context_counter=lambda request: 1_000_000 if forced_limit else 100,
        )
        runtime = PersonalRuntime(request_runner=runner, clock=clock)
        try:
            result = await runtime.receive(inbound("work-" + reset, "research"))
            assert result.replies == ("Research done",)
            assert (await invoke(readers, "browser_reader_status", reader_id=main.rid))[
                "state"
            ] == "completed"
            if reset == "inactivity":
                clock.current += timedelta(minutes=61)
                await runtime.receive(inbound("status", "/status"))
            elif reset == "context":
                forced_limit = True
                result = await runtime.receive(inbound("context", "another request"))
                assert result.disposition == "context_limit"
            else:
                readers = PlaywrightReaderTools(WebReaderConfig(), model)
                PersonalRuntime(
                    request_runner=DirectResponsesRunner(
                        main, tools=readers, request_timeout_seconds=45
                    )
                )
            assert (await invoke(readers, "browser_reader_status", reader_id=main.rid))[
                "error"
            ] == "expired_reader_id"
        finally:
            await readers.close_request()

    with public_fixture(monkeypatch) as url:
        for reset in ("inactivity", "context", "restart"):
            asyncio.run(scenario(url, reset))


def test_release_active_and_session_reset_reap_driver_processes_and_contexts(
    monkeypatch,
):
    async def scenario(url):
        model = AssignmentModel()
        tools = PlaywrightReaderTools(WebReaderConfig(), model)
        try:
            starts = [
                await invoke(tools, "start_browser_reader", url=url, prompt=p)
                for p in ("release", "reset-a", "reset-b")
            ]
            await model.arrived(3)
            closed = []
            drivers, proxy_threads = [], []
            for reader in tools.readers.values():
                # Inspect external resource handles only to verify actual teardown.
                browser = reader.browser
                browser._context.on("close", lambda *_: closed.append(True))
                drivers.append(
                    browser._playwright._impl_obj._connection._transport._proc
                )
                proxy_threads.append(browser._proxy._thread)
            await invoke(
                tools, "release_browser_reader", reader_id=starts[0]["reader_id"]
            )
            assert drivers[0].returncode is not None
            tools.start_session()
            await tools.close_request()
            assert len(closed) == 3
            assert all(proc.returncode is not None for proc in drivers)
            assert all(not thread.is_alive() for thread in proxy_threads)
            for start in starts:
                assert (
                    await invoke(
                        tools, "browser_reader_status", reader_id=start["reader_id"]
                    )
                )["error"] == "expired_reader_id"
        finally:
            await tools.close_request()

    with public_fixture(monkeypatch) as url:
        asyncio.run(scenario(url))


def test_followup_budget_is_fresh_but_main_deadline_and_retained_context_still_apply(
    monkeypatch,
):
    from dataclasses import replace

    class BudgetModel:
        def __init__(self):
            self.timeouts = []

        async def create(self, request, *, timeout):
            self.timeouts.append(timeout)
            return ResponsesResult((), "Existing supported findings")

    async def scenario(url):
        model = BudgetModel()
        tools = PlaywrightReaderTools(
            replace(WebReaderConfig(), context_tokens=16_000), model
        )
        try:
            start = await invoke(
                tools, "start_browser_reader", url=url, prompt="initial"
            )
            rid = start["reader_id"]
            assert (
                await invoke(tools, "wait_browser_reader", reader_id=rid, seconds=15)
            )["state"] == "completed"
            await invoke(
                tools, "follow_up_browser_reader", reader_id=rid, prompt="fresh budget"
            )
            assert (
                await invoke(tools, "wait_browser_reader", reader_id=rid, seconds=15)
            )["state"] == "completed"
            assert (
                model.timeouts[1] > 115
            )  # Surviving browser; a fresh 120-second assignment.
            tools.set_request_deadline(asyncio.get_running_loop().time() + 10)
            await invoke(
                tools, "follow_up_browser_reader", reader_id=rid, prompt="main deadline"
            )
            assert (
                await invoke(tools, "wait_browser_reader", reader_id=rid, seconds=15)
            )["state"] == "completed"
            assert 0 < model.timeouts[2] <= 10
            await invoke(
                tools,
                "follow_up_browser_reader",
                reader_id=rid,
                prompt="large retained question " * 10_000,
            )
            limited = await invoke(
                tools, "wait_browser_reader", reader_id=rid, seconds=15
            )
            assert limited["state"] == "limit" and limited["reason"] == "context_limit"
            assert limited["findings"] == "Existing supported findings"
            assert len(model.timeouts) == 3
        finally:
            await tools.close_request()

    with public_fixture(monkeypatch) as url:
        asyncio.run(scenario(url))


def test_main_deadline_rejects_provider_answer_returned_after_timeout(monkeypatch):
    class LateAfterTimeout:
        calls = 0

        async def create(self, request, *, timeout):
            self.calls += 1
            if self.calls == 1:
                return ResponsesResult((), "Completed findings survive")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return ResponsesResult((), "Late after deadline")

    async def scenario(url):
        tools = PlaywrightReaderTools(WebReaderConfig(), LateAfterTimeout())
        try:
            start = await invoke(
                tools, "start_browser_reader", url=url, prompt="initial"
            )
            rid = start["reader_id"]
            await invoke(tools, "wait_browser_reader", reader_id=rid, seconds=15)
            tools.set_request_deadline(asyncio.get_running_loop().time() + 2)
            await invoke(
                tools, "follow_up_browser_reader", reader_id=rid, prompt="deadline"
            )
            result = await invoke(
                tools, "wait_browser_reader", reader_id=rid, seconds=10
            )
            assert result["state"] == "limit" and result["reason"] == "time_limit", (
                result
            )
            assert result["findings"] == "Completed findings survive"
        finally:
            await tools.close_request()

    with public_fixture(monkeypatch) as url:
        asyncio.run(scenario(url))

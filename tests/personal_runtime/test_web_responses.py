from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from jarvis_personal_runtime.config import WebSearchConfig
from jarvis_personal_runtime.responses import (
    DirectResponsesRunner,
    PreparedToolCollection,
    ResponsesResult,
)
from jarvis_personal_runtime.runtime import (
    ApprovalDecision,
    ApprovalRequired,
    InboundText,
    PendingAction,
    PersonalRuntime,
)
from jarvis_personal_runtime.skill_tools import SkillTools
from jarvis_personal_runtime.trace import JsonlRuntimeTrace
from jarvis_personal_runtime.web_tools import WebTools


class Responses:
    def __init__(self, *results: ResponsesResult) -> None:
        self.results = list(results)
        self.requests = []

    async def create(
        self, request: dict[str, object], *, timeout: float
    ) -> ResponsesResult:
        self.requests.append(request)
        return self.results.pop(0)


class LifecycleTools:
    definitions = (
        {
            "type": "function",
            "name": "read_web_pages",
            "description": "Read public pages.",
            "strict": True,
            "parameters": {"type": "object"},
        },
    )

    def __init__(self, *, approval: bool = False) -> None:
        self.approval = approval
        self.started = 0
        self.closed = 0
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.continuation = object()

    def request_started(self) -> None:
        self.started += 1

    async def close_request(self) -> None:
        self.closed += 1

    def finish_answer(self, text: str) -> str:
        return f"[web-finished] {text}"

    async def execute(self, name: str, arguments: dict[str, object]) -> str:
        self.calls.append((name, arguments))
        if self.approval:
            return ApprovalRequired(
                PendingAction(host="web", prefix="read", display="Read pages?"),
                self.continuation,
            )
        return json.dumps({"outcome": "ok", "pages": []})

    async def resume(self, continuation: object, *, approved: bool) -> str:
        assert continuation is self.continuation
        return json.dumps({"approved": approved})


def call(name: str = "read_web_pages") -> dict[str, object]:
    return {
        "type": "function_call",
        "name": name,
        "call_id": "web-call",
        "arguments": '{"urls":["https://example.com"],"depth":"quick"}',
    }


def run(coro):
    return asyncio.run(coro)


def test_request_lifecycle_resets_each_ordinary_run_and_finishes_answer() -> None:
    async def scenario() -> None:
        responses = Responses(
            ResponsesResult(output=(), output_text="First answer."),
            ResponsesResult(output=(), output_text="Second answer."),
        )
        tools = LifecycleTools()
        runner = DirectResponsesRunner(
            responses, tools=tools, request_timeout_seconds=30
        )

        first = await runner.run(
            "One", model="gpt-6-luna", reasoning="medium", system_prompt="Help."
        )
        second = await runner.run(
            "Two", model="gpt-6-luna", reasoning="medium", system_prompt="Help."
        )

        assert first.reply == "[web-finished] First answer."
        assert second.reply == "[web-finished] Second answer."
        assert tools.started == 2
        assert tools.closed == 2

    run(scenario())


def test_approval_continuation_keeps_consumed_request_state_without_reset() -> None:
    async def scenario() -> None:
        responses = Responses(
            ResponsesResult(output=(call(),), output_text=""),
            ResponsesResult(output=(), output_text="Approved."),
        )
        tools = LifecycleTools(approval=True)
        runner = DirectResponsesRunner(
            responses, tools=tools, request_timeout_seconds=30
        )

        pending = await runner.run(
            "Read this", model="gpt-6-luna", reasoning="medium", system_prompt="Help."
        )
        assert isinstance(pending, ApprovalRequired)
        assert tools.started == 1
        assert tools.closed == 1

        completed = await runner.resume(
            ApprovalDecision.APPROVE_ONCE,
            pending.continuation,
        )
        assert completed.reply == "[web-finished] Approved."
        assert tools.started == 1
        assert tools.closed == 2

    run(scenario())


def test_web_skill_exposes_tools_only_after_loading() -> None:
    tools = SkillTools(LifecycleTools())
    loader = tools.definitions[0]
    assert loader["name"] == "load_skill"
    assert loader["parameters"]["properties"]["name"]["enum"] == ["web"]
    assert "read_web_pages" not in tools.instructions

    with pytest.raises(ValueError, match="load the web skill"):
        run(tools.execute("read_web_pages", {"urls": [], "depth": "quick"}))

    run(tools.execute("load_skill", {"name": "web"}))
    assert "read_web_pages" in tools.instructions
    assert tools.definitions[1]["name"] == "read_web_pages"


@pytest.mark.parametrize("outcome", ["ok", "blocked", "unavailable"])
def test_real_web_tool_supplies_evidence_and_enforces_failure_notice(tmp_path, outcome):
    class Browser:
        closed = False

        async def read(self, url):
            return {
                "outcome": outcome,
                "title": "Example source",
                "url": url,
                "text": "The published value is 42." if outcome == "ok" else "",
            }

        async def close(self):
            self.closed = True

    async def scenario():
        browser = Browser()
        web = WebTools(
            WebSearchConfig(enabled=True),
            runtime_dir=tmp_path,
            max_output_chars=4096,
            browser_factory=lambda *args: browser,
        )
        tools = SkillTools(PreparedToolCollection(web))
        loader = dict(call(), name="load_skill", arguments='{"name":"web"}')
        provider_answer = (
            "The value is 42 [1].\n[1] Example source: https://example.com"
        )
        responses = Responses(
            ResponsesResult(output=(loader,), output_text=""),
            ResponsesResult(output=(call(),), output_text=""),
            ResponsesResult(output=(), output_text=provider_answer),
        )
        runner = DirectResponsesRunner(
            responses, tools=tools, request_timeout_seconds=30
        )
        answer = await runner.run(
            "Read https://example.com",
            model="gpt-6-luna",
            reasoning="medium",
            system_prompt="Help.",
        )
        evidence = json.loads(responses.requests[-1]["input"][-1]["output"])
        assert evidence["pages"][0]["outcome"] == outcome
        assert evidence["pages"][0]["url"] == "https://example.com"
        assert evidence["remaining"]["pages"] == 1
        assert browser.closed
        if outcome == "ok":
            assert answer.reply == provider_answer
            assert evidence["pages"][0]["text"] == "The published value is 42."
        else:
            assert answer.reply.startswith("Web access is unavailable")
            assert "not current web verification" in answer.reply
        assert "untrusted evidence" in responses.requests[-1]["instructions"]

    run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("operation", ["read_web_pages", "search_web"])
def test_web_request_timeout_and_cancel_close_worker_without_late_answer(
    tmp_path, cancel, operation
):
    class BlockingBrowser:
        def __init__(self):
            self.started = asyncio.Event()
            self.closed = False

        async def read(self, url):
            self.started.set()
            await asyncio.Future()

        async def search(self, query, limit):
            return await self.read(query)

        async def close(self):
            self.closed = True

    async def scenario():
        browser = BlockingBrowser()
        web = WebTools(
            WebSearchConfig(enabled=True),
            runtime_dir=tmp_path,
            max_output_chars=4096,
            browser_factory=lambda *args: browser,
        )
        invocation = call()
        if operation == "search_web":
            invocation = dict(
                call(), name=operation, arguments='{"query":"news","depth":"quick"}'
            )
        responses = Responses(ResponsesResult(output=(invocation,), output_text=""))
        runner = DirectResponsesRunner(
            responses, tools=web, request_timeout_seconds=30 if cancel else 0.1
        )
        task = asyncio.create_task(
            runner.run(
                "Read this",
                model="gpt-6-luna",
                reasoning="medium",
                system_prompt="Help.",
            )
        )
        await browser.started.wait()
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
            await task
        assert browser.closed
        assert len(responses.requests) == 1

    run(scenario())


def test_next_request_waits_for_cancelled_browser_cleanup(tmp_path):
    async def scenario():
        started = asyncio.Event()
        closing = asyncio.Event()
        released = asyncio.Event()

        class Browser:
            async def read(self, url):
                started.set()
                await asyncio.Future()

            async def close(self):
                closing.set()
                await released.wait()

        web = WebTools(
            WebSearchConfig(enabled=True),
            runtime_dir=tmp_path,
            max_output_chars=4096,
            browser_factory=lambda *args: Browser(),
        )
        responses = Responses(
            ResponsesResult(output=(call(),), output_text=""),
            ResponsesResult(output=(), output_text="Next request."),
        )
        runner = DirectResponsesRunner(responses, tools=web, request_timeout_seconds=30)
        first = asyncio.create_task(
            runner.run(
                "One", model="gpt-6-luna", reasoning="medium", system_prompt="Help."
            )
        )
        await started.wait()
        first.cancel()
        await closing.wait()
        second = asyncio.create_task(
            runner.run(
                "Two", model="gpt-6-luna", reasoning="medium", system_prompt="Help."
            )
        )
        await asyncio.sleep(0)
        assert len(responses.requests) == 1
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert (await second).reply == "Next request."

    run(scenario())


def test_enabled_runtime_builds_the_web_skill(tmp_path, monkeypatch):
    from jarvis_personal_runtime.config import RuntimeConfig
    from jarvis_personal_runtime.responses import (
        OpenAIRawResponsesAdapter,
        build_direct_responses_runner,
    )

    config = RuntimeConfig(
        root=tmp_path,
        ubuntu_working_directory=tmp_path,
        trace_path=tmp_path / "trace.jsonl",
        web_search=WebSearchConfig(enabled=True),
    )
    responses = Responses(ResponsesResult(output=(), output_text="Ready."))
    monkeypatch.setattr(
        OpenAIRawResponsesAdapter, "from_api_key", lambda *a, **kw: responses
    )
    runner = build_direct_responses_runner("sk-test", config)
    run(
        runner.run(
            "Hello", model="gpt-6-luna", reasoning="medium", system_prompt="Help."
        )
    )
    request = responses.requests[0]
    assert "web:" in request["instructions"]
    loader = next(tool for tool in request["tools"] if tool["name"] == "load_skill")
    assert "web" in loader["parameters"]["properties"]["name"]["enum"]


def test_real_web_allowance_survives_unrelated_tool_approval(tmp_path):
    async def scenario():
        browsers = []

        def factory(*args):
            browser = ResearchBrowser()
            browsers.append(browser)
            return browser

        web = WebTools(
            WebSearchConfig(enabled=True),
            runtime_dir=tmp_path,
            max_output_chars=4096,
            browser_factory=factory,
        )
        approval = LifecycleTools(approval=True)
        approval.definitions = (dict(approval.definitions[0], name="run_terminal"),)
        after_approval = dict(
            call(),
            arguments=json.dumps(
                {
                    "urls": [f"https://example.com/{i}" for i in range(5)],
                    "depth": "quick",
                }
            ),
        )
        responses = Responses(
            web_step("search_web", query="first", depth="quick"),
            ResponsesResult(output=(call(),), output_text=""),
            ResponsesResult(
                output=(dict(call(), name="run_terminal"),), output_text=""
            ),
            web_step("search_web", query="compare", depth="moderate"),
            ResponsesResult(output=(after_approval,), output_text=""),
            ResponsesResult(output=(), output_text="Answer [1]."),
        )
        runner = DirectResponsesRunner(
            responses,
            tools=PreparedToolCollection(web, approval),
            request_timeout_seconds=30,
        )
        pending = await runner.run(
            "Read then act",
            model="gpt-6-luna",
            reasoning="medium",
            system_prompt="Help.",
        )
        assert isinstance(pending, ApprovalRequired)
        assert browsers[0].closed
        await runner.resume(ApprovalDecision.APPROVE_ONCE, pending.continuation)
        result = json.loads(responses.requests[-1]["input"][-1]["output"])
        assert result["remaining"]["pages"] == 0
        assert result["remaining"]["queries"] == 1
        assert (
            result["remaining"]["evidence_chars"]
            < tool_outputs(responses.requests[2])[-1]["remaining"]["evidence_chars"]
        )
        assert result["pages"][0]["outcome"] == "ok"
        assert result["pages"][4]["outcome"] == "unattempted"
        assert len(browsers) == 2
        assert [len(browser.urls) for browser in browsers] == [1, 4]
        assert [len(browser.queries) for browser in browsers] == [1, 1]
        assert all(browser.closed for browser in browsers)

    run(scenario())


def web_step(tool_name, **arguments):
    return ResponsesResult(
        output=(dict(call(), name=tool_name, arguments=json.dumps(arguments)),),
        output_text="",
    )


class ResearchBrowser:
    def __init__(self, *, first_search_fails=False, value=42):
        self.first_search_fails = first_search_fails
        self.value = value
        self.queries = []
        self.urls = []
        self.closed = False

    async def search(self, query, limit):
        self.queries.append((query, limit))
        if self.first_search_fails and len(self.queries) == 1:
            return {"outcome": "extraction_failed"}
        return {
            "outcome": "ok",
            "results": [
                {
                    "title": "Primary source",
                    "url": "https://example.com/primary",
                    "snippet": f"Published value: {self.value}.",
                }
            ],
        }

    async def read(self, url):
        self.urls.append(url)
        if url.endswith("/failed"):
            return {"outcome": "timeout", "usable": True}
        return {
            "outcome": "ok",
            "title": "Primary source",
            "url": url,
            "text": f"Published value: {self.value}.",
        }

    async def close(self):
        self.closed = True


def tool_outputs(request):
    return [
        json.loads(item["output"])
        for item in request["input"]
        if item.get("type") == "function_call_output"
    ]


def research_runner(tmp_path, responses, browsers, **options):
    def factory(*args):
        browser = ResearchBrowser(value=42 + len(browsers))
        browsers.append(browser)
        return browser

    web = WebTools(
        WebSearchConfig(enabled=True),
        runtime_dir=tmp_path,
        max_output_chars=4096,
        browser_factory=factory,
    )
    return DirectResponsesRunner(
        responses,
        tools=SkillTools(PreparedToolCollection(web)),
        request_timeout_seconds=30,
        **options,
    )


def test_conversation_reuses_session_evidence_and_recheck_does_fresh_work(tmp_path):
    async def scenario():
        responses = Responses(
            web_step("load_skill", name="web"),
            web_step("search_web", query="published value", depth="moderate"),
            web_step(
                "read_web_pages", urls=["https://example.com/primary"], depth="moderate"
            ),
            ResponsesResult(
                output=(),
                output_text="The value is 42 [1]. https://example.com/primary",
            ),
            ResponsesResult(
                output=(),
                output_text="The earlier source reported 42 [1]. https://example.com/primary",
            ),
            web_step("search_web", query="published value", depth="quick"),
            web_step(
                "read_web_pages", urls=["https://example.com/primary"], depth="quick"
            ),
            ResponsesResult(
                output=(), output_text="Rechecked: 43 [1]. https://example.com/primary"
            ),
        )
        browsers = []
        runtime = PersonalRuntime(
            request_runner=research_runner(tmp_path, responses, browsers),
            system_prompt="Help.",
        )
        now = datetime(2026, 10, 7, tzinfo=UTC)
        first = await runtime.receive(
            InboundText("first", "Find the published value.", now)
        )
        assert "42" in first.replies[0]
        assert len(browsers) == 1 and browsers[0].closed
        reused = await runtime.receive(
            InboundText("followup", "What did that source report?", now)
        )
        assert "42" in reused.replies[0]
        assert len(browsers) == 1  # Evidence reuse requires no browser or attempt.
        followup = responses.requests[4]
        assert tool_outputs(followup)[-1]["pages"][0]["text"] == "Published value: 42."
        assert {t["name"] for t in followup["tools"]} == {
            "load_skill",
            "search_web",
            "read_web_pages",
        }
        assert (
            "Refresh time-sensitive claims and explicit recheck requests"
            in followup["instructions"]
        )
        checked = await runtime.receive(
            InboundText("recheck", "Recheck the current value now.", now)
        )
        assert "43" in checked.replies[0]
        assert len(browsers) == 2 and all(b.closed for b in browsers)
        output = tool_outputs(responses.requests[-1])
        assert output[-2]["remaining"]["queries"] == 0
        assert output[-2]["remaining"]["pages"] == 2
        assert output[-1]["remaining"]["pages"] == 1
        assert output[-1]["pages"][0]["text"] == "Published value: 43."

    run(scenario())


@pytest.mark.parametrize("reset", ["new", "expiry", "context", "restart"])
def test_session_reset_discards_web_evidence_and_loaded_skill(tmp_path, reset):
    async def scenario():
        responses = Responses(
            web_step("load_skill", name="web"),
            web_step(
                "read_web_pages", urls=["https://example.com/old"], depth="moderate"
            ),
            ResponsesResult(output=(), output_text="Previous evidence."),
            ResponsesResult(output=(), output_text="Fresh session."),
        )
        browsers = []
        full = False
        runner = research_runner(
            tmp_path,
            responses,
            browsers,
            context_counter=lambda request: 100_000 if full else 100,
        )
        clock = SimpleNamespace(current=datetime(2026, 10, 7, tzinfo=UTC))
        clock.now = lambda: clock.current
        runtime = PersonalRuntime(
            request_runner=runner, system_prompt="Help.", clock=clock
        )
        await runtime.receive(
            InboundText("first", "Read the old source.", clock.current)
        )
        assert (
            tool_outputs(responses.requests[-1])[-1]["pages"][0]["url"]
            == "https://example.com/old"
        )
        assert browsers[0].closed
        if reset == "new":
            await runtime.receive(InboundText("reset", "/new", clock.current))
        elif reset == "expiry":
            clock.current += timedelta(minutes=61)
        elif reset == "context":
            full = True
            limited = await runtime.receive(
                InboundText("full", "Another question.", clock.current)
            )
            assert limited.disposition == "context_limit"
            full = False
        else:
            runtime = PersonalRuntime(
                request_runner=research_runner(tmp_path, responses, browsers),
                system_prompt="Help.",
                clock=clock,
            )
        await runtime.receive(
            InboundText("after", "What evidence do you have?", clock.current)
        )
        fresh = responses.requests[-1]
        assert fresh["input"] == [
            {"role": "user", "content": "What evidence do you have?"}
        ]
        assert [t["name"] for t in fresh["tools"]] == ["load_skill"]
        assert "## Loaded skill: web" not in fresh["instructions"]
        assert len(browsers) == 1

    run(scenario())


@pytest.mark.parametrize("command", ["/cancel", "/new"])
def test_cancel_or_new_during_moderate_research_closes_browser_without_late_reply(
    tmp_path, command
):
    async def scenario():
        started = asyncio.Event()

        class BlockingBrowser(ResearchBrowser):
            async def read(self, url):
                started.set()
                await asyncio.Future()

        browser = BlockingBrowser()
        web = WebTools(
            WebSearchConfig(enabled=True),
            runtime_dir=tmp_path,
            max_output_chars=4096,
            browser_factory=lambda *args: browser,
        )
        responses = Responses(
            web_step("load_skill", name="web"),
            web_step("search_web", query="compare", depth="moderate"),
            web_step(
                "read_web_pages", urls=["https://example.com/slow"], depth="moderate"
            ),
            ResponsesResult(output=(), output_text="Next request."),
        )
        runtime = PersonalRuntime(
            request_runner=DirectResponsesRunner(
                responses,
                tools=SkillTools(PreparedToolCollection(web)),
                request_timeout_seconds=30,
            ),
            system_prompt="Help.",
        )
        now = datetime(2026, 10, 7, tzinfo=UTC)
        task = asyncio.create_task(
            runtime.receive(InboundText("research", "Compare sources.", now))
        )
        await started.wait()
        await runtime.receive(InboundText("command", command, now))
        cancelled = await task
        assert cancelled.replies == ()
        assert browser.closed
        assert len(responses.requests) == 3
        fresh = await runtime.receive(InboundText("next", "Hello.", now))
        assert fresh.replies == ("Next request.",)
        if command == "/new":
            assert tool_outputs(responses.requests[-1]) == []

    run(scenario())


@pytest.mark.parametrize("output_limit", [280, 600])
def test_research_evidence_and_serialized_trace_stay_bounded_across_upgrade(
    tmp_path, output_limit
):
    class LongBrowser(ResearchBrowser):
        async def search(self, query, limit):
            response = await super().search(query, limit)
            response["results"][0]["snippet"] = '\\"\n' * 1000
            return response

        async def read(self, url):
            response = await super().read(url)
            response["text"] = '\\"\n' * 1000
            return response

    async def scenario():
        browser = LongBrowser()
        config = replace(
            WebSearchConfig(enabled=True),
            max_chars_per_page=50,
            max_total_evidence_chars=80,
        )
        web = WebTools(
            config,
            runtime_dir=tmp_path,
            max_output_chars=output_limit,
            browser_factory=lambda *args: browser,
        )
        trace_path = tmp_path / "trace.jsonl"
        responses = Responses(
            web_step("search_web", query="value", depth="quick"),
            web_step(
                "read_web_pages", urls=["https://example.com/a"], depth="moderate"
            ),
            web_step("read_web_pages", urls=["https://example.com/b"], depth="quick"),
            web_step("search_web", query="over", depth="moderate"),
            ResponsesResult(
                output=(), output_text="Available evidence [1]. https://example.com/a"
            ),
        )
        runner = DirectResponsesRunner(
            responses,
            tools=web,
            request_timeout_seconds=30,
            max_output_chars=output_limit,
            trace=JsonlRuntimeTrace(trace_path, max_bytes=1_000_000),
        )
        result = await runner.run(
            "Research this.",
            model="gpt-6-luna",
            reasoning="medium",
            system_prompt="Help.",
        )
        assert "Available evidence" in result.reply
        outputs = tool_outputs(responses.requests[-1])
        used = 0
        for output in outputs:
            used += sum(
                len(s["title"]) + len(s["snippet"]) for s in output.get("results", [])
            )
            used += sum(len(p["text"]) for p in output.get("pages", []))
            assert output["remaining"]["evidence_chars"] == 80 - used
        assert used == 80
        assert outputs[-1]["outcome"] == "limit_reached"
        assert len(browser.queries) == 1
        assert browser.closed
        events = [
            json.loads(line)
            for line in trace_path.read_text(encoding="utf-8").splitlines()
        ]
        traced = [e["payload"]["output"] for e in events if e["event"] == "tool_result"]
        assert [json.loads(raw) for raw in traced] == outputs
        assert all(len(raw) <= output_limit for raw in traced)

    run(scenario())


@pytest.mark.parametrize("first_depth", ["quick", "moderate"])
@pytest.mark.parametrize("first_search_fails", [False, True])
def test_research_loop_shares_all_allowances_and_keeps_partial_evidence(
    tmp_path, first_depth, first_search_fails
):
    async def scenario():
        browser = ResearchBrowser(first_search_fails=first_search_fails)
        tools = SkillTools(
            PreparedToolCollection(
                WebTools(
                    WebSearchConfig(enabled=True),
                    runtime_dir=tmp_path,
                    max_output_chars=4096,
                    browser_factory=lambda *args: browser,
                )
            )
        )
        answer = (
            "The primary and comparison pages report 42 [1][2]. "
            "One source timed out, so coverage is incomplete.\n"
            "[1] Primary source: https://example.com/primary\n"
            "[2] Primary source: https://example.com/comparison"
        )
        responses = Responses(
            web_step("load_skill", name="web"),
            web_step("search_web", query="published value", depth=first_depth),
            web_step(
                "read_web_pages",
                urls=["https://example.com/primary", "https://example.com/failed"],
                depth="quick",
            ),
            web_step("load_skill", name="web"),
            web_step("search_web", query="comparison", depth="moderate"),
            web_step(
                "read_web_pages",
                urls=[
                    "https://example.com/comparison",
                    "https://example.com/comparison",
                    "https://example.com/third",
                    "https://example.com/overflow",
                ],
                depth="quick",
            ),
            web_step("search_web", query="final verification", depth="quick"),
            web_step("search_web", query="over allowance", depth="moderate"),
            ResponsesResult(output=(), output_text=answer),
        )
        runner = DirectResponsesRunner(
            responses, tools=tools, request_timeout_seconds=30
        )
        result = await runner.run(
            "Compare published values from multiple public sources.",
            model="gpt-6-luna",
            reasoning="medium",
            system_prompt="Help.",
        )
        outputs = tool_outputs(responses.requests[-1])
        assert outputs[1]["outcome"] == (
            "extraction_failed" if first_search_fails else "ok"
        )
        assert outputs[2]["outcome"] == "partial"
        assert outputs[4]["remaining"]["queries"] == 1
        assert outputs[4]["remaining"]["pages"] == 3
        assert (
            outputs[4]["remaining"]["evidence_chars"]
            < outputs[2]["remaining"]["evidence_chars"]
        )
        batch = outputs[5]
        assert batch["outcome"] == "partial"
        assert batch["remaining"]["pages"] == 0
        assert [p["outcome"] for p in batch["pages"]] == [
            "ok",
            "ok",
            "ok",
            "unattempted",
        ]
        assert batch["pages"][-1]["requested_url"] == "https://example.com/overflow"
        assert outputs[6]["outcome"] == "ok"
        assert outputs[7]["outcome"] == "limit_reached"
        assert len(browser.queries) == 3
        assert len(browser.urls) == 5
        assert browser.closed
        assert answer in result.reply
        assert "Web access is unavailable" not in result.reply
        instructions = responses.requests[-1]["instructions"]
        assert "material disagreements" in instructions
        assert "moderate" in instructions
        for definition in responses.requests[-1]["tools"]:
            if definition["name"] in {"search_web", "read_web_pages"}:
                assert definition["parameters"]["properties"]["depth"]["enum"] == [
                    "quick",
                    "moderate",
                ]

    run(scenario())


def test_failure_notice_keeps_a_fallback_answer_within_the_output_limit(tmp_path):
    class Browser:
        async def read(self, url):
            return {"outcome": "unavailable"}

        async def close(self):
            pass

    async def scenario():
        web = WebTools(
            WebSearchConfig(enabled=True),
            runtime_dir=tmp_path,
            max_output_chars=500,
            browser_factory=lambda *args: Browser(),
        )
        responses = Responses(
            ResponsesResult(output=(call(),), output_text=""),
            ResponsesResult(output=(), output_text="Background knowledge. " * 21),
        )
        runner = DirectResponsesRunner(
            responses, tools=web, request_timeout_seconds=30, max_output_chars=500
        )
        result = await runner.run(
            "Read this", model="gpt-6-luna", reasoning="medium", system_prompt="Help."
        )
        assert result.reply.startswith("Web access is unavailable")
        assert "Background knowledge." in result.reply
        assert "shortened" in result.reply
        assert len(result.reply) <= 500

    run(scenario())


@pytest.mark.parametrize(
    "outcome", ["ok", "no_results", "blocked", "extraction_failed", "unavailable"]
)
@pytest.mark.parametrize("read_source", [False, True])
def test_search_model_loop_exposes_citations_privacy_and_fallback(
    tmp_path, outcome, read_source
):
    class Browser:
        closed = False

        def __init__(self):
            self.queries = []

        async def search(self, query, limit):
            self.queries.append(query)
            return {
                "outcome": outcome,
                "results": [
                    {
                        "title": "Primary source",
                        "url": "https://example.com",
                        "snippet": "The value is 42.",
                    }
                ]
                if outcome == "ok"
                else [],
            }

        async def read(self, url):
            return {
                "outcome": "ok",
                "title": "Primary source",
                "url": url,
                "text": "The page reports 42.",
            }

        async def close(self):
            self.closed = True

    async def scenario():
        browser = Browser()
        browser.queries = []
        web = WebTools(
            WebSearchConfig(enabled=True),
            runtime_dir=tmp_path,
            max_output_chars=4096,
            browser_factory=lambda *args: browser,
        )
        tools = SkillTools(PreparedToolCollection(web))
        assert [t["name"] for t in tools.definitions] == ["load_skill"]
        assert "freshness" in tools.instructions
        loader = dict(call(), name="load_skill", arguments='{"name":"web"}')
        search = dict(
            call(),
            name="search_web",
            arguments='{"query":"current published value","depth":"quick"}',
        )
        sequence = [
            ResponsesResult(output=(loader,), output_text=""),
            ResponsesResult(output=(search,), output_text=""),
        ]
        if read_source and outcome == "ok":
            sequence.append(ResponsesResult(output=(call(),), output_text=""))
        cited = "The value is 42 [1].\n[1] Primary source: https://example.com"
        sequence.append(
            ResponsesResult(
                output=(),
                output_text=cited
                if outcome == "ok"
                else "Current value remains unverified.",
            )
        )
        responses = Responses(*sequence)
        runner = DirectResponsesRunner(
            responses, tools=tools, request_timeout_seconds=30
        )
        result = await runner.run(
            "Search for the current published value.",
            model="gpt-6-luna",
            reasoning="medium",
            system_prompt="Help.",
        )
        assert browser.closed
        assert browser.queries == ["current published value"]
        instructions = responses.requests[-1]["instructions"]
        for required in (
            "explicit operator intent",
            "complete conversation",
            "Google Workspace",
            "search_result",
            "actually read",
            "primary sources",
            "appropriate to the question",
        ):
            assert required in instructions
        evidence = json.loads(responses.requests[2]["input"][-1]["output"])
        assert evidence["outcome"] == outcome
        if outcome == "ok":
            assert result.reply == cited
            assert evidence["results"][0]["evidence_type"] == "search_result"
            if read_source:
                page = json.loads(responses.requests[-1]["input"][-1]["output"])
                assert page["pages"][0]["evidence_type"] == "page"
                assert page["remaining"]["queries"] == 0
        elif outcome == "no_results":
            assert result.reply.startswith("No useful search results")
        else:
            assert result.reply.startswith("Web access is unavailable")

    run(scenario())

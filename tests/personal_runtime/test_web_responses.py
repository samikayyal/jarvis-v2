from __future__ import annotations

import asyncio
import json

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
    PendingAction,
)
from jarvis_personal_runtime.skill_tools import SkillTools
from jarvis_personal_runtime.web_tools import WebTools


class Responses:
    def without_retries(self):
        return self

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
            "name": "search_web",
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


def call(name: str = "search_web") -> dict[str, object]:
    return {
        "type": "function_call",
        "name": name,
        "call_id": "web-call",
        "arguments": '{"query":"current fact","depth":"quick"}',
    }


def run(coro):
    return asyncio.run(coro)


def test_request_lifecycle_resets_each_ordinary_run_and_preserves_answer() -> None:
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

        assert first.reply == "First answer."
        assert second.reply == "Second answer."
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
        assert tools.closed == 0

        completed = await runner.resume(
            ApprovalDecision.APPROVE_ONCE,
            pending.continuation,
        )
        assert completed.reply == "Approved."
        assert tools.started == 1
        assert tools.closed == 1

    run(scenario())


def test_web_skill_exposes_tools_only_after_loading() -> None:
    tools = SkillTools(LifecycleTools())
    loader = tools.definitions[0]
    assert loader["name"] == "load_skill"
    assert loader["parameters"]["properties"]["name"]["enum"] == ["web"]
    assert "search_web" not in tools.instructions

    with pytest.raises(ValueError, match="load the web skill"):
        run(tools.execute("search_web", {"urls": [], "depth": "quick"}))

    run(tools.execute("load_skill", {"name": "web"}))
    assert "start_browser_reader" in tools.instructions
    assert tools.definitions[1]["name"] == "search_web"


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("operation", ["search_web"])
def test_web_request_timeout_and_cancel_close_worker_without_late_answer(
    tmp_path, cancel, operation
):
    class BlockingBrowser:
        def __init__(self):
            self.started = asyncio.Event()
            self.closed = False

        async def search(self, query, limit):
            self.started.set()
            await asyncio.Future()

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
            async def search(self, query, limit):
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


def web_step(tool_name, **arguments):
    return ResponsesResult(
        output=(dict(call(), name=tool_name, arguments=json.dumps(arguments)),),
        output_text="",
    )


def test_failed_search_preserves_the_model_answer_within_the_output_limit(tmp_path):
    class Browser:
        async def search(self, query, limit):
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
        assert result.reply == "Background knowledge. " * 21
        assert len(result.reply) <= 500

    run(scenario())


@pytest.mark.parametrize(
    "outcome", ["ok", "no_results", "blocked", "extraction_failed", "unavailable"]
)
def test_search_model_loop_exposes_citations_privacy_and_fallback(tmp_path, outcome):
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
            "operator's requested length",
        ):
            assert required in instructions
        evidence = json.loads(responses.requests[2]["input"][-1]["output"])
        assert evidence["outcome"] == outcome
        if outcome == "ok":
            assert result.reply == cited
            assert evidence["results"][0]["evidence_type"] == "search_result"
        elif outcome == "no_results":
            assert result.reply == "Current value remains unverified."
        else:
            assert result.reply == "Current value remains unverified."

    run(scenario())

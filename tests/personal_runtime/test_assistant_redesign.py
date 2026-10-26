from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from jarvis_personal_runtime.config import RuntimeConfig
from jarvis_personal_runtime.openwa import split_text
from jarvis_personal_runtime.responses import DirectResponsesRunner, ResponsesResult
from jarvis_personal_runtime.runtime import (
    ApprovalRequired,
    InboundText,
    PendingAction,
    PersonalRuntime,
    RuntimeDisposition,
)
from jarvis_personal_runtime.whatsapp_format import format_whatsapp


def message(number: int, text: str) -> InboundText:
    return InboundText(str(number), text, datetime.now(UTC))


class Provider:
    def __init__(self, *results):
        self.results = list(results)
        self.requests = []

    async def create(self, request, *, timeout):
        self.requests.append(request)
        value = self.results.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def call(number: int) -> ResponsesResult:
    return ResponsesResult(
        output=(
            {
                "type": "function_call",
                "name": "run_terminal",
                "call_id": f"call_{number}",
                "arguments": '{"host":"ubuntu","command":"uptime"}',
            },
        ),
        output_text="",
    )


class Tools:
    definitions = ({"type": "function", "name": "run_terminal"},)

    def __init__(self):
        self.executed = 0
        self.resumed = 0

    async def execute(self, name, arguments):
        self.executed += 1
        return ApprovalRequired(
            PendingAction("ubuntu", "uptime", "Run uptime?"), "pending"
        )

    async def resume(self, continuation, *, approved):
        self.resumed += 1
        return json.dumps({"exit_code": 0, "stdout": "up 82 days"})


def test_runtime_context_tracks_model_reasoning_and_excludes_private_config():
    async def scenario():
        provider = Provider(
            *(ResponsesResult(output=(), output_text="Jarvis") for _ in range(2))
        )
        runner = DirectResponsesRunner(provider, request_timeout_seconds=10)
        runtime = PersonalRuntime(
            RuntimeConfig(openwa_authorized_operator_number="PRIVATE_NUMBER"),
            request_runner=runner,
            system_prompt="You are Jarvis.",
        )
        await runtime.receive(message(1, "Who are you?"))
        await runtime.receive(message(2, "/model gpt-6-astra"))
        await runtime.receive(message(3, "/reasoning high"))
        await runtime.receive(message(4, "What is your model now?"))
        first, second = provider.requests
        assert '"active_model": "gpt-6-luna"' in first["instructions"]
        assert '"active_model": "gpt-6-astra"' in second["instructions"]
        assert '"reasoning_effort": "high"' in second["instructions"]
        assert "OpenAI Responses API; remote model inference" in second["instructions"]
        assert "PRIVATE_NUMBER" not in second["instructions"]
        assert "request_context_time" in second["instructions"]
        assert "WhatsApp" in second["instructions"]
        assert "runtime_host" in second["instructions"]

    asyncio.run(scenario())


def test_limit_after_approval_preserves_results_and_allows_safe_followup():
    async def scenario():
        tools = Tools()
        provider = Provider(
            call(1),
            call(2),
            ResponsesResult(
                output=(), output_text="Uptime verified; pricing remains unchecked."
            ),
            ResponsesResult(
                output=(), output_text="The earlier uptime check succeeded."
            ),
        )
        runner = DirectResponsesRunner(
            provider, tools=tools, request_timeout_seconds=10, max_tool_rounds=1
        )
        runtime = PersonalRuntime(request_runner=runner)
        pending = await runtime.receive(message(1, "Check uptime and pricing"))
        assert pending.disposition is RuntimeDisposition.APPROVAL_REQUIRED
        result = await runtime.receive(message(2, "1"))
        assert result.disposition is RuntimeDisposition.INCOMPLETE
        assert "limit of 1 tool calls" in result.replies[0]
        assert "pricing remains unchecked" in result.replies[0]
        assert result.status.active_request is None
        assert result.status.pending_action is None
        summary = provider.requests[2]
        assert summary["tools"] == [] and summary["tool_choice"] == "none"
        outputs = [
            x for x in summary["input"] if x.get("type") == "function_call_output"
        ]
        assert json.loads(outputs[0]["output"])["exit_code"] == 0
        assert json.loads(outputs[1]["output"])["outcome"] == "not_attempted"
        await runtime.receive(message(3, "What succeeded?"))
        assert tools.executed == tools.resumed == 1
        assert any("limit of 1" in str(x) for x in provider.requests[3]["input"])

    asyncio.run(scenario())


def test_limit_summary_failure_still_returns_specific_notice():
    async def scenario():
        provider = Provider(call(1), call(2), OSError("provider unavailable"))
        runner = DirectResponsesRunner(
            provider, tools=Tools(), request_timeout_seconds=10, max_tool_rounds=1
        )
        runtime = PersonalRuntime(request_runner=runner)
        await runtime.receive(message(1, "Check"))
        result = await runtime.receive(message(2, "1"))
        assert result.disposition is RuntimeDisposition.INCOMPLETE
        assert "limit of 1" in result.replies[0]
        assert "couldn't produce a partial summary" in result.replies[0]
        assert result.status.pending_action is None

    asyncio.run(scenario())


def test_timeout_after_approval_is_specific_and_does_not_reexecute():
    async def scenario():
        provider = Provider(
            call(1),
            TimeoutError(),
            ResponsesResult(output=(), output_text="Previously checked."),
        )
        tools = Tools()
        runner = DirectResponsesRunner(
            provider, tools=tools, request_timeout_seconds=10
        )
        runtime = PersonalRuntime(request_runner=runner)
        await runtime.receive(message(1, "Check"))
        result = await runtime.receive(message(2, "1"))
        assert result.disposition is RuntimeDisposition.FAILED
        assert "time limit" in result.replies[0]
        await runtime.receive(message(3, "What happened?"))
        assert tools.executed == tools.resumed == 1

    asyncio.run(scenario())


def test_whatsapp_prose_conversion_preserves_code_and_sources():
    text = "## Recommendation\n**Keep the laptop.**\n\n| Option | Cost |\n|---|---|\n| VM | $5 |\n\n[Price](https://example.org/pricing)\n`**literal**`\n```python\n# code\na = '**keep**'\n```"
    result = format_whatsapp(text)
    assert "*Recommendation*" in result
    assert "*Keep the laptop.*" in result
    assert "- Option: VM; Cost: $5" in result
    assert "Price (https://example.org/pricing)" in result
    assert "`**literal**`" in result
    assert "# code\na = '**keep**'" in result
    assert format_whatsapp(result) == result
    assert (
        split_text("First paragraph.\n\nSecond paragraph with words.", 30)[0]
        == "First paragraph."
    )

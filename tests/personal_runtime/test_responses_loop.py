from __future__ import annotations

import asyncio
from functools import wraps

import httpx
import pytest

import jarvis_personal_runtime.responses as responses_module
from jarvis_personal_runtime.responses import (
    DirectResponsesRunner,
    OpenAIRawResponsesAdapter,
    ResponsesResult,
    canonical_context,
    local_input_tokens,
)
from jarvis_personal_runtime.runtime import (
    ApprovalRequired,
    ContextLimitReached,
    PendingAction,
)


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


class FakeResponses:
    def __init__(self, *results: ResponsesResult) -> None:
        self.results = list(results)
        self.calls: list[tuple[dict[str, object], float]] = []

    async def create(
        self, request: dict[str, object], *, timeout: float
    ) -> ResponsesResult:
        self.calls.append((request, timeout))
        return self.results.pop(0)


class FailingResponses(FakeResponses):
    async def create(
        self, request: dict[str, object], *, timeout: float
    ) -> ResponsesResult:
        self.calls.append((request, timeout))
        if self.results:
            return self.results.pop(0)
        raise OSError("provider unavailable")


class FakeTools:
    definitions = (
        {
            "type": "function",
            "name": "read_vault",
            "description": "Read the vault.",
            "parameters": {"type": "object"},
            "strict": True,
        },
    )

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute(self, name: str, arguments: dict[str, object]) -> str:
        self.calls.append((name, arguments))
        return '{"text":"found"}'


class FailingTools(FakeTools):
    async def execute(self, name: str, arguments: dict[str, object]) -> str:
        self.calls.append((name, arguments))
        raise OSError("vault unavailable")


class ApprovalTools(FakeTools):
    async def execute(
        self, name: str, arguments: dict[str, object]
    ) -> ApprovalRequired:
        self.calls.append((name, arguments))
        return ApprovalRequired(
            PendingAction(host="ubuntu", prefix="touch", display="Run command?"),
            object(),
        )


class MemoryTrace:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def record(self, event: str, payload: dict[str, object]) -> None:
        self.events.append((event, payload))


class BlockingResponses:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def create(
        self, request: dict[str, object], *, timeout: float
    ) -> ResponsesResult:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def test_candidate_context_uses_the_stable_research_serializer() -> None:
    assert canonical_context(
        instructions="Help café.",
        tools=({"name": "z", "type": "function"},),
        input_items=({"content": "<|endoftext|>", "role": "user"},),
    ) == (
        '{"input":[{"content":"<|endoftext|>","role":"user"}],'
        '"instructions":"Help café.",'
        '"tools":[{"name":"z","type":"function"}]}'
    )


def test_local_counter_treats_special_looking_text_as_ordinary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, tuple[str, ...]]] = []

    class Encoding:
        def encode(
            self, text: str, *, disallowed_special: tuple[str, ...]
        ) -> list[int]:
            observed.append((text, disallowed_special))
            return [1, 2, 3]

    monkeypatch.setattr(responses_module, "_CONTEXT_ENCODING", Encoding())
    candidate = {
        "instructions": "Help.",
        "tools": [],
        "input": [{"role": "user", "content": "<|endoftext|>"}],
    }

    assert local_input_tokens(candidate) == 3
    assert observed == [
        (
            (
                '{"input":[{"content":"<|endoftext|>","role":"user"}],'
                '"instructions":"Help.","tools":[]}'
            ),
            (),
        )
    ]


@async_test
async def test_exact_context_limit_ends_session_before_initial_provider_call() -> None:
    responses = FakeResponses(ResponsesResult(output=(), output_text="not called"))
    observed: list[dict[str, object]] = []

    def count(candidate: dict[str, object]) -> int:
        observed.append(candidate)
        return 100_000

    runner = DirectResponsesRunner(
        responses,
        request_timeout_seconds=30,
        max_context_tokens=100_000,
        context_counter=count,
    )

    result = await runner.run(
        "Hi", model="gpt-5.6-luna", reasoning="medium", system_prompt="Help."
    )

    assert isinstance(result, ContextLimitReached)
    assert responses.calls == []
    assert observed == [
        {
            "instructions": "Help.",
            "tools": [],
            "input": [{"role": "user", "content": "Hi"}],
        }
    ]


@async_test
async def test_continuation_is_gated_with_complete_tool_result_context() -> None:
    call = {
        "type": "function_call",
        "name": "read_vault",
        "call_id": "call_1",
        "arguments": '{"query":"notes"}',
    }
    responses = FakeResponses(ResponsesResult(output=(call,), output_text=""))
    tools = FakeTools()
    candidates: list[dict[str, object]] = []

    def count(candidate: dict[str, object]) -> int:
        candidates.append(candidate)
        return 1 if len(candidates) == 1 else 2

    runner = DirectResponsesRunner(
        responses,
        tools=tools,
        request_timeout_seconds=30,
        max_context_tokens=2,
        context_counter=count,
    )

    result = await runner.run(
        "Read", model="gpt-5.6-luna", reasoning="medium", system_prompt="Help."
    )

    assert isinstance(result, ContextLimitReached)
    assert len(responses.calls) == 1
    assert candidates[-1]["input"] == [
        {"role": "user", "content": "Read"},
        call,
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": '{"text":"found"}',
        },
    ]


@async_test
async def test_oversized_final_text_is_rejected_without_entering_transcript() -> None:
    responses = FakeResponses(
        ResponsesResult(
            output=({"type": "message", "content": "123456"},),
            output_text="123456",
        ),
        ResponsesResult(output=(), output_text="small"),
    )
    runner = DirectResponsesRunner(
        responses, request_timeout_seconds=30, max_output_chars=5
    )

    with pytest.raises(RuntimeError, match="configured output character limit"):
        await runner.run(
            "First",
            model="gpt-5.6-luna",
            reasoning="medium",
            system_prompt="Help.",
        )

    await runner.run(
        "Second",
        model="gpt-5.6-luna",
        reasoning="medium",
        system_prompt="Help.",
    )
    request, _ = responses.calls[-1]
    assert request["input"] == [{"role": "user", "content": "Second"}]


@async_test
async def test_provider_usage_is_traced_next_to_the_local_estimate() -> None:
    trace = MemoryTrace()
    runner = DirectResponsesRunner(
        FakeResponses(
            ResponsesResult(
                output=(),
                output_text="done",
                usage={"input_tokens": 43, "output_tokens": 2},
            )
        ),
        trace=trace,
        request_timeout_seconds=30,
        context_counter=lambda _candidate: 41,
    )

    await runner.run(
        "Hi", model="gpt-5.6-luna", reasoning="medium", system_prompt="Help."
    )

    output = next(
        payload for event, payload in trace.events if event == "responses_output"
    )
    assert output["projected_input_tokens"] == 41
    assert output["usage"] == {"input_tokens": 43, "output_tokens": 2}


@async_test
async def test_final_text_uses_the_direct_stateless_responses_contract() -> None:
    responses = FakeResponses(
        ResponsesResult(
            output=({"type": "message", "id": "msg_1"},), output_text="Hello"
        )
    )
    runner = DirectResponsesRunner(responses, request_timeout_seconds=30)

    result = await runner.run(
        "Hi", model="gpt-5.6-luna", reasoning="medium", system_prompt="Be useful."
    )

    assert result.reply == "Hello"
    request, timeout = responses.calls[0]
    assert request == {
        "model": "gpt-5.6-luna",
        "instructions": "Be useful.",
        "input": [{"role": "user", "content": "Hi"}],
        "tools": [],
        "reasoning": {"effort": "medium"},
        "parallel_tool_calls": False,
        "store": False,
        "truncation": "disabled",
    }
    assert 0 < timeout <= 30


@async_test
async def test_one_tool_call_replays_complete_output_before_exact_result() -> None:
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_1",
        "encrypted_content": "opaque-ciphertext",
        "summary": [],
    }
    function_call = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_exact",
        "name": "read_vault",
        "arguments": '{"query":"roadmap"}',
    }
    responses = FakeResponses(
        ResponsesResult(output=(reasoning_item, function_call), output_text=""),
        ResponsesResult(
            output=({"type": "message", "id": "msg_2"},),
            output_text="I found it.",
        ),
    )
    tools = FakeTools()
    trace = MemoryTrace()
    runner = DirectResponsesRunner(
        responses,
        tools=tools,
        trace=trace,
        request_timeout_seconds=30,
        max_tool_rounds=2,
    )

    result = await runner.run(
        "Find it", model="gpt-5.6-sol", reasoning="high", system_prompt="Use tools."
    )

    assert result.reply == "I found it."
    assert tools.calls == [("read_vault", {"query": "roadmap"})]
    continuation, _ = responses.calls[1]
    assert continuation["instructions"] == "Use tools."
    assert continuation["input"] == [
        {"role": "user", "content": "Find it"},
        reasoning_item,
        function_call,
        {
            "type": "function_call_output",
            "call_id": "call_exact",
            "output": '{"text":"found"}',
        },
    ]
    events = {event: payload for event, payload in trace.events}
    assert events["responses_request"]["request"] == responses.calls[1][0]
    assert events["responses_output"]["output"] == [{"type": "message", "id": "msg_2"}]
    assert events["tool_call"] == {
        "name": "read_vault",
        "call_id": "call_exact",
        "arguments": {"query": "roadmap"},
    }
    assert events["tool_result"] == {
        "name": "read_vault",
        "call_id": "call_exact",
        "output": '{"text":"found"}',
    }


@async_test
async def test_output_replay_omits_response_only_status() -> None:
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_1",
        "encrypted_content": "opaque-ciphertext",
        "summary": [],
        "status": None,
    }
    function_call = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_exact",
        "name": "read_vault",
        "arguments": '{"query":"roadmap"}',
        "status": "completed",
    }
    responses = FakeResponses(
        ResponsesResult(output=(reasoning_item, function_call), output_text=""),
        ResponsesResult(output=(), output_text="Found it."),
    )
    runner = DirectResponsesRunner(
        responses,
        tools=FakeTools(),
        request_timeout_seconds=30,
        max_tool_rounds=2,
    )

    result = await runner.run(
        "Find it", model="gpt-5.6-sol", reasoning="high", system_prompt="Use tools."
    )

    assert result.reply == "Found it."
    continuation, _ = responses.calls[1]
    assert continuation["input"] == [
        {"role": "user", "content": "Find it"},
        {
            "type": "reasoning",
            "id": "rs_1",
            "encrypted_content": "opaque-ciphertext",
            "summary": [],
        },
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_exact",
            "name": "read_vault",
            "arguments": '{"query":"roadmap"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_exact",
            "output": '{"text":"found"}',
        },
    ]


@async_test
async def test_cancelling_pending_tool_keeps_next_request_transcript_valid() -> None:
    function_call = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_pending",
        "name": "read_vault",
        "arguments": '{"query":"roadmap"}',
        "status": "completed",
    }
    responses = FakeResponses(
        ResponsesResult(output=(function_call,), output_text=""),
        ResponsesResult(output=(), output_text="Next request completed."),
    )
    runner = DirectResponsesRunner(
        responses,
        tools=ApprovalTools(),
        request_timeout_seconds=30,
        max_tool_rounds=2,
    )

    pending = await runner.run(
        "Find it", model="gpt-5.6-sol", reasoning="high", system_prompt="Use tools."
    )
    assert isinstance(pending, ApprovalRequired)

    runner.cancel_pending(pending.continuation)
    result = await runner.run(
        "Continue", model="gpt-5.6-sol", reasoning="high", system_prompt="Use tools."
    )

    assert result.reply == "Next request completed."
    continuation, _ = responses.calls[1]
    assert continuation["input"] == [
        {"role": "user", "content": "Find it"},
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_pending",
            "name": "read_vault",
            "arguments": '{"query":"roadmap"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_pending",
            "output": '{"cancelled":true}',
        },
        {"role": "user", "content": "Continue"},
    ]


@async_test
async def test_tool_error_is_returned_to_the_model_and_continuation_proceeds() -> None:
    call = {
        "type": "function_call",
        "call_id": "call_failed",
        "name": "read_vault",
        "arguments": "{}",
    }
    responses = FakeResponses(
        ResponsesResult(output=(call,), output_text=""),
        ResponsesResult(output=(), output_text="The vault is unavailable."),
    )
    tools = FailingTools()
    runner = DirectResponsesRunner(
        responses, tools=tools, request_timeout_seconds=30, max_tool_rounds=2
    )

    result = await runner.run(
        "Read it", model="gpt-5.6-luna", reasoning="medium", system_prompt="Help."
    )

    assert result.reply == "The vault is unavailable."
    continuation, _ = responses.calls[1]
    assert continuation["input"][-1] == {
        "type": "function_call_output",
        "call_id": "call_failed",
        "output": '{"error":"OSError: vault unavailable"}',
    }


@async_test
async def test_malformed_tool_arguments_continue_as_a_tool_error() -> None:
    call = {
        "type": "function_call",
        "call_id": "call_bad_json",
        "name": "read_vault",
        "arguments": "{not-json",
    }
    responses = FakeResponses(
        ResponsesResult(output=(call,), output_text=""),
        ResponsesResult(output=(), output_text="I could not use that tool call."),
    )
    tools = FakeTools()
    runner = DirectResponsesRunner(
        responses, tools=tools, request_timeout_seconds=30, max_tool_rounds=2
    )

    result = await runner.run(
        "Read it", model="gpt-5.6-luna", reasoning="medium", system_prompt="Help."
    )

    assert result.reply == "I could not use that tool call."
    assert tools.calls == []
    continuation, _ = responses.calls[1]
    assert continuation["input"][-1]["call_id"] == "call_bad_json"
    assert "JSONDecodeError" in continuation["input"][-1]["output"]


@async_test
async def test_failed_continuation_preserves_observed_tool_turn_in_session() -> None:
    call = {
        "type": "function_call",
        "call_id": "call_done",
        "name": "read_vault",
        "arguments": "{}",
    }
    responses = FailingResponses(ResponsesResult(output=(call,), output_text=""))
    runner = DirectResponsesRunner(
        responses, tools=FakeTools(), request_timeout_seconds=30
    )

    with pytest.raises(OSError, match="provider unavailable"):
        await runner.run(
            "First",
            model="gpt-5.6-luna",
            reasoning="medium",
            system_prompt="Help.",
        )
    responses.results.append(ResponsesResult(output=(), output_text="Recovered"))
    result = await runner.run(
        "Continue",
        model="gpt-5.6-luna",
        reasoning="medium",
        system_prompt="Help.",
    )

    assert result.reply == "Recovered"
    assert responses.calls[-1][0]["input"] == [
        {"role": "user", "content": "First"},
        call,
        {
            "type": "function_call_output",
            "call_id": "call_done",
            "output": '{"text":"found"}',
        },
        {"role": "user", "content": "Continue"},
    ]


@async_test
async def test_response_without_tool_call_or_final_text_is_rejected() -> None:
    responses = FakeResponses(ResponsesResult(output=(), output_text=""))
    trace = MemoryTrace()
    runner = DirectResponsesRunner(responses, trace=trace, request_timeout_seconds=30)

    with pytest.raises(RuntimeError, match="final text"):
        await runner.run(
            "Hello",
            model="gpt-5.6-luna",
            reasoning="medium",
            system_prompt="Help.",
        )

    assert (
        "provider_protocol_error",
        {"reason": "missing_final_text"},
    ) in trace.events


@async_test
async def test_multiple_function_calls_are_traced_and_rejected_without_execution() -> (
    None
):
    calls = tuple(
        {
            "type": "function_call",
            "call_id": f"call_{index}",
            "name": "read_vault",
            "arguments": "{}",
        }
        for index in range(2)
    )
    responses = FakeResponses(ResponsesResult(output=calls, output_text=""))
    tools = FakeTools()
    trace = MemoryTrace()
    runner = DirectResponsesRunner(
        responses,
        tools=tools,
        trace=trace,
        request_timeout_seconds=30,
        max_tool_rounds=2,
    )

    with pytest.raises(RuntimeError, match="multiple function calls"):
        await runner.run(
            "Read twice",
            model="gpt-5.6-luna",
            reasoning="medium",
            system_prompt="Help.",
        )

    assert tools.calls == []
    assert ("provider_protocol_error", {"function_call_count": 2}) in trace.events


@async_test
async def test_tool_round_limit_rejects_the_next_call_before_execution() -> None:
    first = {
        "type": "function_call",
        "call_id": "call_1",
        "name": "read_vault",
        "arguments": '{"query":"one"}',
    }
    second = {
        "type": "function_call",
        "call_id": "call_2",
        "name": "read_vault",
        "arguments": '{"query":"two"}',
    }
    responses = FakeResponses(
        ResponsesResult(output=(first,), output_text=""),
        ResponsesResult(output=(second,), output_text=""),
    )
    tools = FakeTools()
    trace = MemoryTrace()
    runner = DirectResponsesRunner(
        responses,
        tools=tools,
        trace=trace,
        request_timeout_seconds=30,
        max_tool_rounds=1,
    )

    with pytest.raises(RuntimeError, match="tool-round limit"):
        await runner.run(
            "Keep reading",
            model="gpt-5.6-luna",
            reasoning="medium",
            system_prompt="Help.",
        )

    assert tools.calls == [("read_vault", {"query": "one"})]
    assert trace.events[-1] == (
        "request_error",
        {"error": "RuntimeError: configured tool-round limit reached"},
    )


@async_test
async def test_current_session_history_and_each_turns_overrides_are_replayed() -> None:
    first_message = {"type": "message", "id": "msg_1", "role": "assistant"}
    responses = FakeResponses(
        ResponsesResult(output=(first_message,), output_text="First"),
        ResponsesResult(output=(), output_text="Second"),
    )
    runner = DirectResponsesRunner(responses, request_timeout_seconds=30)

    await runner.run(
        "One", model="gpt-5.6-luna", reasoning="medium", system_prompt="Always."
    )
    await runner.run(
        "Two", model="gpt-5.6-sol", reasoning="max", system_prompt="Always."
    )

    request, _ = responses.calls[1]
    assert request["model"] == "gpt-5.6-sol"
    assert request["reasoning"] == {"effort": "max"}
    assert request["instructions"] == "Always."
    assert request["input"] == [
        {"role": "user", "content": "One"},
        first_message,
        {"role": "user", "content": "Two"},
    ]


@async_test
async def test_starting_a_new_session_discards_the_prior_transcript() -> None:
    responses = FakeResponses(
        ResponsesResult(output=({"type": "message", "id": "old"},), output_text="Old"),
        ResponsesResult(output=(), output_text="New"),
    )
    runner = DirectResponsesRunner(responses, request_timeout_seconds=30)
    await runner.run(
        "Old request",
        model="gpt-5.6-luna",
        reasoning="medium",
        system_prompt="Always.",
    )

    runner.start_session()
    await runner.run(
        "New request",
        model="gpt-5.6-luna",
        reasoning="medium",
        system_prompt="Always.",
    )

    request, _ = responses.calls[1]
    assert request["input"] == [{"role": "user", "content": "New request"}]


@async_test
async def test_foreground_cancellation_is_traced_as_local_best_effort() -> None:
    responses = BlockingResponses()
    trace = MemoryTrace()
    runner = DirectResponsesRunner(responses, trace=trace, request_timeout_seconds=30)
    task = asyncio.create_task(
        runner.run(
            "Wait",
            model="gpt-5.6-luna",
            reasoning="medium",
            system_prompt="Help.",
        )
    )
    await responses.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert trace.events[-1] == (
        "foreground_cancelled",
        {
            "scope": "local_wait",
            "provider_cancellation_confirmed": False,
        },
    )


@async_test
async def test_overall_deadline_bounds_the_whole_request() -> None:
    responses = BlockingResponses()
    trace = MemoryTrace()
    runner = DirectResponsesRunner(responses, trace=trace, request_timeout_seconds=0.01)

    with pytest.raises(TimeoutError):
        await runner.run(
            "Wait",
            model="gpt-5.6-luna",
            reasoning="medium",
            system_prompt="Help.",
        )

    assert trace.events[-1] == (
        "request_timeout",
        {"timeout_seconds": 0.01},
    )


@async_test
async def test_openai_adapter_traces_every_sdk_retry_attempt_and_raw_exchange() -> None:
    attempts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(500, json={"error": {"message": "retry"}})
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": "gpt-5.6-luna",
                "output": [
                    {
                        "id": "msg_1",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Hello",
                                "annotations": [],
                                "logprobs": [],
                            }
                        ],
                    }
                ],
                "parallel_tool_calls": False,
                "store": False,
                "tools": [],
            },
        )

    trace = MemoryTrace()
    adapter = OpenAIRawResponsesAdapter.from_api_key(
        "sk-test", trace=trace, transport=httpx.MockTransport(handle)
    )
    try:
        result = await adapter.create(
            {
                "model": "gpt-5.6-luna",
                "instructions": "Help.",
                "input": [{"role": "user", "content": "Hi"}],
                "tools": [],
                "reasoning": {"effort": "medium"},
                "parallel_tool_calls": False,
                "store": False,
                "truncation": "disabled",
            },
            timeout=5,
        )
    finally:
        await adapter.close()

    assert result.output_text == "Hello"
    assert attempts == 2
    request_attempts = [p for e, p in trace.events if e == "http_attempt_request"]
    response_attempts = [p for e, p in trace.events if e == "http_attempt_response"]
    assert [item["attempt"] for item in request_attempts] == [1, 2]
    assert [item["status_code"] for item in response_attempts] == [500, 200]
    assert all("authorization" not in item["headers"] for item in request_attempts)
    raw = [p for e, p in trace.events if e == "responses_raw_exchange"][-1]
    assert '"store":false' in raw["request_body"].replace(" ", "")
    assert '"id":"resp_1"' in raw["response_body"].replace(" ", "")
    parsed = [p for e, p in trace.events if e == "responses_parsed"][-1]
    assert parsed["response"]["id"] == "resp_1"
    assert parsed["response"]["status"] == "completed"


@async_test
async def test_sdk_retry_backoff_cannot_escape_the_runner_deadline() -> None:
    def unavailable(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "retry"}})

    trace = MemoryTrace()
    adapter = OpenAIRawResponsesAdapter.from_api_key(
        "sk-test", trace=trace, transport=httpx.MockTransport(unavailable)
    )
    runner = DirectResponsesRunner(adapter, trace=trace, request_timeout_seconds=0.5)
    try:
        with pytest.raises(TimeoutError):
            await runner.run(
                "Hi",
                model="gpt-5.6-luna",
                reasoning="medium",
                system_prompt="Help.",
            )
    finally:
        await adapter.close()

    assert any(event == "http_attempt_request" for event, _ in trace.events)
    assert trace.events[-1] == (
        "request_timeout",
        {"timeout_seconds": 0.5},
    )

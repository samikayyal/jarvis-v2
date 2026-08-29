from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from agents.exceptions import ModelBehaviorError

from jarvis_control_plane.models import (
    InboundMessage,
    OrchestrationRequest,
    RequestState,
    SignedInboundEvent,
)
from jarvis_control_plane.orchestration import (
    AgentsSdkOrchestrationAdapter,
    AgentsSdkPlan,
    AgentsSdkProposal,
    BoundedReadInput,
    BoundedReadOutput,
    BoundedReadTool,
)
from jarvis_control_plane.ports import OrchestrationAdapterError

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


class _FakeReasoning:
    def __init__(self, *, effort: str) -> None:
        self.effort = effort


class _FakeModelSettings:
    def __init__(self, **values: object) -> None:
        self.reasoning = values["reasoning"]
        self.parallel_tool_calls = values["parallel_tool_calls"]
        self.store = values["store"]


class _FakeRunConfig:
    def __init__(self, **values: object) -> None:
        self.values = values


def _request(text: str, *, reasoning: str = "high") -> OrchestrationRequest:
    return OrchestrationRequest(
        state=RequestState(
            request_id="request-001",
            event_id="event-001",
            message_id="message-001",
            operator_id="operator.test",
            session_id="working-session-001",
            chat_id="operator.test",
            created_at=NOW,
            updated_at=NOW,
            status="accepted",
            phase="orchestration",
            model="gpt-5.6-terra",
            reasoning=reasoning,
        ),
        text=text,
    )


def _event(text: str, *, event_id: str = "event-ticket14") -> SignedInboundEvent:
    return SignedInboundEvent.from_message(
        InboundMessage(
            event_type="message.received",
            session_id="session.test",
            event_id=event_id,
            message_id=f"{event_id}-message",
            sender_id="operator.test",
            chat_id="operator.test",
            chat_type="direct",
            message_type="text",
            from_me=False,
            text=text,
        ),
        b"ticket14-test-secret",
    )


def test_agents_adapter_uses_explicit_stateless_sequential_responses_settings() -> None:
    captured: dict[str, object] = {}

    def agent_factory(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    def run_sync(agent: object, text: str, **kwargs: object) -> object:
        captured["run_agent"] = agent
        captured["run_text"] = text
        captured["run_kwargs"] = kwargs
        return SimpleNamespace(
            final_output=AgentsSdkPlan(
                reply_text="I will inspect the repository.",
            )
        )

    result = AgentsSdkOrchestrationAdapter(
        agent_factory=agent_factory,
        run_sync=run_sync,
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
    ).run(_request("inspect the repository"))

    settings = captured["model_settings"]
    assert isinstance(settings, _FakeModelSettings)
    assert settings.store is False
    assert settings.parallel_tool_calls is False
    assert settings.reasoning is not None
    assert settings.reasoning.effort == "high"
    assert captured["model"] == "gpt-5.6-terra"
    output_type = captured["output_type"]
    assert output_type.output_type.__name__ == "_AgentsSdkStructuredPlan"
    assert output_type.is_strict_json_schema() is False
    schema = output_type.json_schema()
    assert "_CalendarInsertEvent" not in schema["$defs"]
    with pytest.raises(ModelBehaviorError, match="Invalid JSON"):
        output_type.validate_json(
            json.dumps(
                {
                    "reply_text": "I prepared the event.",
                    "execution_host": None,
                    "host_reason_code": None,
                    "proposal": {
                        "kind": "calendar_insert",
                        "preview": "Create the event.",
                        "payload": {
                            "calendar_id": "secondary-calendar",
                            "complete_event": {
                                "summary": "Design review",
                                "start": {"dateTime": "2026-08-10T10:00:00Z"},
                                "end": {"dateTime": "2026-08-10T11:00:00Z"},
                            },
                            "notification": "none",
                        },
                    },
                }
            )
        )
    assert captured["run_text"] == "inspect the repository"
    assert captured["run_kwargs"]["previous_response_id"] is None
    assert captured["run_text"] == "inspect the repository"
    assert captured["run_kwargs"] == {
        "max_turns": 5,
        "run_config": captured["run_kwargs"]["run_config"],
        "previous_response_id": None,
        "auto_previous_response_id": False,
        "conversation_id": None,
    }
    assert captured["run_kwargs"]["run_config"].values == {
        "tracing_disabled": True,
        "trace_include_sensitive_data": False,
    }
    assert result.reply_text == "I will inspect the repository."
    assert result.proposal is None
    assert result.execution_host is None
    assert result.host_reason_code is None


def test_default_turn_budget_allows_four_sequential_tools_then_final_reply() -> None:
    async def run_async(agent: object, _text: str, **kwargs: object) -> object:
        for _ in range(4):
            await agent.tools[0].on_invoke_tool(None, '{"max_chars":8}')
        if kwargs["max_turns"] < 5:
            raise RuntimeError("final reply turn was unavailable")
        return SimpleNamespace(
            final_output=AgentsSdkPlan(reply_text="Four bounded reads completed.")
        )

    result = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        run_async=run_async,
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
    ).run(_request("perform four sequential bounded reads"))

    assert result.reply_text == "Four bounded reads completed."


def test_agents_adapter_executes_one_closed_bounded_read_and_returns_milestone_and_final() -> (
    None
):
    captured: dict[str, object] = {}

    def agent_factory(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    def run_sync(_agent: object, _text: str, **_kwargs: object) -> object:
        tools = captured["tools"]
        assert isinstance(tools, list)
        assert [tool.name for tool in tools] == ["read_request_context"]
        read_tool = tools[0]
        captured["tool_result"] = asyncio.run(
            read_tool.on_invoke_tool(None, json.dumps({"max_chars": 8}))
        )
        return SimpleNamespace(
            final_output=AgentsSdkPlan(
                reply_text="The bounded read is complete.",
            )
        )

    result = AgentsSdkOrchestrationAdapter(
        agent_factory=agent_factory,
        run_sync=run_sync,
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
    ).run(_request("read the current request context"))

    tool_result = captured["tool_result"]
    assert tool_result["source"] == "authorized_request"
    assert len(tool_result["text"]) <= 8
    assert captured["tools"][0].needs_approval is False
    assert result.reply_text == "The bounded read is complete."
    assert result.reply_text.endswith("The bounded read is complete.")
    assert [milestone.stage for milestone in result.milestones] == [
        "orchestration_started",
        "bounded_read",
    ]


def test_agents_adapter_returns_safe_tool_result_when_remote_read_is_unavailable() -> (
    None
):
    captured: dict[str, object] = {}
    calls = 0

    def unavailable_read(
        _request: OrchestrationRequest, _input: BoundedReadInput, _deadline: float
    ) -> BoundedReadOutput:
        nonlocal calls
        calls += 1
        from jarvis_control_plane.service_protocol import RemoteServiceError

        raise RemoteServiceError("GoogleReadError", "google_read_disconnected")

    def run_sync(agent: object, _text: str, **_kwargs: object) -> object:
        captured["tool_result"] = asyncio.run(
            agent.tools[0].on_invoke_tool(None, json.dumps({"max_chars": 8}))
        )
        captured["output_schema"] = agent.tools[0].output_json_schema
        captured["second_tool_result"] = asyncio.run(
            agent.tools[0].on_invoke_tool(None, json.dumps({"max_chars": 8}))
        )
        return SimpleNamespace(
            final_output=AgentsSdkPlan(
                reply_text="Ignore the unavailable result.",
                proposal=AgentsSdkProposal(
                    kind="gmail_send",
                    preview="This proposal must be discarded.",
                    payload={},
                ),
            )
        )

    result = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        run_sync=run_sync,
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
        read_tool=BoundedReadTool(
            "read_request_context",
            "A remote read that is deliberately unavailable.",
            BoundedReadInput,
            BoundedReadOutput,
            unavailable_read,
        ),
    ).run(_request("read disconnected data"))

    expected_tool_result = {
        "unavailable": True,
        "message": (
            "The connected service is unavailable or not authorized. "
            "Explain that the requested read could not be completed, "
            "do not claim any retrieved data, and do not retry."
        ),
    }
    assert captured["tool_result"] == expected_tool_result
    assert captured["second_tool_result"] == expected_tool_result
    from agents.items import ItemHelpers
    from openai.types.responses import ResponseFunctionToolCall

    sdk_output = ItemHelpers.tool_call_output_item(
        ResponseFunctionToolCall(
            arguments='{"max_chars":8}',
            call_id="call-unavailable-read",
            name="read_request_context",
            type="function_call",
        ),
        captured["tool_result"],
        output_json_schema=captured["output_schema"],
    )
    assert json.loads(sdk_output["output"]) == expected_tool_result
    assert calls == 1
    assert result.outcome == "unavailable"
    assert result.reply_text == (
        "The requested request context read could not be completed because Google is "
        "disconnected. I did not retry the unavailable read."
    )
    assert result.proposal is None
    assert [milestone.stage for milestone in result.milestones] == [
        "orchestration_started",
        "bounded_read_unavailable",
    ]


def test_agents_adapter_does_not_mask_non_connectivity_remote_read_failures() -> None:
    def ambiguous_read(
        _request: OrchestrationRequest, _input: BoundedReadInput, _deadline: float
    ) -> BoundedReadOutput:
        from jarvis_control_plane.service_protocol import RemoteServiceError

        raise RemoteServiceError("VaultReadError", "ambiguous_title")

    def run_sync(agent: object, _text: str, **_kwargs: object) -> object:
        asyncio.run(agent.tools[0].on_invoke_tool(None, '{"max_chars": 8}'))
        raise AssertionError("unreachable")

    adapter = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        run_sync=run_sync,
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
        read_tool=BoundedReadTool(
            "read_request_context",
            "A remote read with a query-specific failure.",
            BoundedReadInput,
            BoundedReadOutput,
            ambiguous_read,
        ),
    )

    with pytest.raises(OrchestrationAdapterError, match="returned malformed data"):
        adapter.run(_request("read an ambiguous title"))


def test_v1_model_contract_states_the_exact_gmail_reply_payload_shape() -> None:
    captured: dict[str, object] = {}
    adapter = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **kwargs: captured.update(kwargs) or object(),
        run_sync=lambda *_args, **_kwargs: SimpleNamespace(
            final_output=AgentsSdkPlan(reply_text="No Gmail action was needed.")
        ),
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
    )
    adapter.run(_request("prepare a reply"))

    instructions = captured["instructions"]
    assert isinstance(instructions, str)
    assert (
        "add exactly source_message_id, source_thread_id, in_reply_to, and references"
    ) in instructions
    assert "Never emit a separate thread_id field" in instructions
    assert "bare mailbox address without a display name" in instructions

    schema = captured["output_type"].json_schema()
    reply_payload = schema["$defs"]["_GmailReplyStructuredPayload"]
    assert reply_payload["additionalProperties"] is False
    assert set(reply_payload["required"]) == {
        "to",
        "cc",
        "bcc",
        "subject",
        "body",
        "mime_type",
        "source_message_id",
        "source_thread_id",
        "in_reply_to",
        "references",
    }
    assert "thread_id" not in reply_payload["properties"]
    assert reply_payload["properties"]["references"]["minItems"] == 1
    assert reply_payload["properties"]["references"]["maxItems"] == 20

    with pytest.raises(ModelBehaviorError, match="Invalid JSON"):
        captured["output_type"].validate_json(
            json.dumps(
                {
                    "reply_text": "I prepared the reply.",
                    "execution_host": None,
                    "host_reason_code": None,
                    "proposal": {
                        "kind": "gmail_reply",
                        "preview": "Reply to the source message.",
                        "payload": {
                            "to": ["recipient@example.com"],
                            "cc": [],
                            "bcc": [],
                            "subject": "Re: Check-in",
                            "body": "Thanks.",
                            "mime_type": "text/plain",
                            "source_message_id": "source-001",
                            "source_thread_id": "thread-001",
                            "in_reply_to": "<source-001@example.com>",
                            "references": [],
                        },
                    },
                }
            )
        )


def test_provider_schema_rejects_redundant_gmail_reply_thread_id() -> None:
    captured: dict[str, object] = {}
    adapter = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **kwargs: captured.update(kwargs) or object(),
        run_sync=lambda *_args, **_kwargs: SimpleNamespace(
            final_output=AgentsSdkPlan(reply_text="No Gmail action was needed.")
        ),
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
    )
    adapter.run(_request("prepare a reply"))

    with pytest.raises(ModelBehaviorError, match="Invalid JSON"):
        captured["output_type"].validate_json(
            json.dumps(
                {
                    "reply_text": "I prepared the reply.",
                    "execution_host": None,
                    "host_reason_code": None,
                    "proposal": {
                        "kind": "gmail_reply",
                        "preview": "Reply to the source message.",
                        "payload": {
                            "to": ["recipient@example.com"],
                            "cc": [],
                            "bcc": [],
                            "subject": "Re: Check-in",
                            "body": "Thanks.",
                            "mime_type": "text/plain",
                            "thread_id": "wrong-thread",
                            "source_message_id": "source-001",
                            "source_thread_id": "thread-001",
                            "in_reply_to": "<source-001@example.com>",
                            "references": ["<source-001@example.com>"],
                        },
                    },
                }
            )
        )

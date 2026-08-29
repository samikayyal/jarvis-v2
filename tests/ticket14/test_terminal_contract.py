from __future__ import annotations

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


def test_windows_dependent_request_selects_windows_and_turns_a_typed_plan_into_proposal() -> (
    None
):
    def run_sync(_agent: object, _text: str, **_kwargs: object) -> object:
        return SimpleNamespace(
            final_output=AgentsSdkPlan(
                reply_text="I can prepare the exact command for approval.",
                execution_host="windows",
                host_reason_code="explicit_windows",
                proposal=AgentsSdkProposal(
                    kind="terminal",
                    preview="Run git status on the Windows workspace.",
                    payload={
                        "host": "windows",
                        "executable": "C:/Program Files/Git/bin/git.exe",
                        "arguments": ["status"],
                        "cwd": "C:/workspace",
                    },
                ),
            )
        )

    result = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **_kwargs: object(),
        run_sync=run_sync,
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
    ).run(_request("on my Windows laptop, show the repository status"))

    assert result.proposal is not None
    assert result.proposal.action_id == "request-001:proposal"
    assert '"host":"windows"' in result.proposal.payload


def test_malformed_or_invalid_host_decision_fails_closed() -> None:
    def run_sync(_agent: object, _text: str, **_kwargs: object) -> object:
        return SimpleNamespace(
            final_output=SimpleNamespace(
                reply_text="Use Windows.",
                execution_host="windows",
                host_reason_code="default_ubuntu",
            )
        )

    with pytest.raises(OrchestrationAdapterError):
        AgentsSdkOrchestrationAdapter(
            agent_factory=lambda **_kwargs: object(),
            run_sync=run_sync,
            model_settings_factory=_FakeModelSettings,
            reasoning_factory=_FakeReasoning,
            run_config_factory=_FakeRunConfig,
        ).run(_request("inspect the repository"))


def test_model_failure_and_malformed_output_are_adapter_errors() -> None:
    def run_sync(_agent: object, _text: str, **_kwargs: object) -> object:
        raise RuntimeError("provider unavailable")

    adapter = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **_kwargs: object(),
        run_sync=run_sync,
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
    )

    with pytest.raises(OrchestrationAdapterError, match="run was unavailable"):
        adapter.run(_request("read the repository"))

    malformed = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **_kwargs: object(),
        run_sync=lambda _agent, _text, **_kwargs: SimpleNamespace(
            final_output={"reply_text": "run as administrator"}
        ),
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
    )
    with pytest.raises(OrchestrationAdapterError, match="malformed structured output"):
        malformed.run(_request("read the repository"))


def test_v1_model_contract_excludes_calendar_tools_and_proposals() -> None:
    captured: dict[str, object] = {}

    def agent_factory(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    adapter = AgentsSdkOrchestrationAdapter(
        agent_factory=agent_factory,
        run_sync=lambda *_args, **_kwargs: SimpleNamespace(
            final_output=AgentsSdkPlan(reply_text="Calendar is unavailable in v1.")
        ),
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
    )
    adapter.run(_request("summarize my inbox"))

    assert "read_google_calendar" not in {tool.name for tool in captured["tools"]}
    schema = captured["output_type"].json_schema()
    assert "calendar_insert" not in json.dumps(schema)
    assert "Calendar" not in captured["instructions"]


def test_v1_model_contract_states_the_exact_terminal_payload_shape() -> None:
    captured: dict[str, object] = {}

    def agent_factory(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    adapter = AgentsSdkOrchestrationAdapter(
        agent_factory=agent_factory,
        run_sync=lambda *_args, **_kwargs: SimpleNamespace(
            final_output=AgentsSdkPlan(reply_text="No terminal action was needed.")
        ),
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
    )
    adapter.run(_request("inspect the operating system"))

    instructions = captured["instructions"]
    assert isinstance(instructions, str)
    assert (
        "payload must contain exactly the required fields host, executable, "
        "arguments, and cwd"
    ) in instructions
    assert "may contain only the optional field components" in instructions
    assert "host must equal execution_host" in instructions
    assert r"C:\Windows\System32\hostname.exe" in instructions
    assert r"cwd C:\Windows\System32" in instructions
    for forbidden_metadata in (
        "stdin",
        "timeout",
        "environment",
        "approval",
        "permission",
        "sandbox",
    ):
        assert forbidden_metadata in instructions

    output_type = captured["output_type"]
    schema = output_type.json_schema()
    terminal_payload = schema["$defs"]["_TerminalStructuredPayload"]
    assert terminal_payload["additionalProperties"] is False
    assert set(terminal_payload["required"]) == {
        "host",
        "executable",
        "arguments",
        "cwd",
    }
    assert set(terminal_payload["properties"]) == {
        "host",
        "executable",
        "arguments",
        "cwd",
        "components",
    }


@pytest.mark.parametrize(
    "payload",
    (
        {
            "host": "ubuntu",
            "executable": "/usr/bin/uname",
            "arguments": ["-s"],
        },
        {
            "host": "ubuntu",
            "executable": "/usr/bin/uname",
            "arguments": ["-s"],
            "cwd": "/workspace",
            "approval": "persistent",
        },
        {
            "host": "ubuntu",
            "executable": "/usr/bin/uname",
            "arguments": "-s",
            "cwd": "/workspace",
        },
    ),
)
def test_provider_schema_rejects_malformed_terminal_payloads(
    payload: dict[str, object],
) -> None:
    captured: dict[str, object] = {}

    adapter = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **kwargs: captured.update(kwargs) or object(),
        run_sync=lambda *_args, **_kwargs: SimpleNamespace(
            final_output=AgentsSdkPlan(reply_text="No action was needed.")
        ),
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
    )
    adapter.run(_request("inspect the operating system"))

    with pytest.raises(ModelBehaviorError, match="Invalid JSON"):
        captured["output_type"].validate_json(
            json.dumps(
                {
                    "reply_text": "I prepared the safe read.",
                    "execution_host": "ubuntu",
                    "host_reason_code": "default_ubuntu",
                    "proposal": {
                        "kind": "terminal",
                        "preview": "Read the operating-system name.",
                        "payload": payload,
                    },
                }
            )
        )


def test_semantically_invalid_terminal_proposal_has_a_stable_diagnostic_code() -> None:
    adapter = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **_kwargs: object(),
        run_sync=lambda *_args, **_kwargs: SimpleNamespace(
            final_output=AgentsSdkPlan(
                reply_text="I prepared the safe read.",
                execution_host="ubuntu",
                host_reason_code="default_ubuntu",
                proposal=AgentsSdkProposal(
                    kind="terminal",
                    preview="Read the operating-system name.",
                    payload={
                        "host": "ubuntu",
                        "executable": "uname",
                        "arguments": ["-s"],
                        "cwd": "/workspace",
                    },
                ),
            )
        ),
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
    )

    with pytest.raises(OrchestrationAdapterError) as caught:
        adapter.run(_request("inspect the operating system"))

    assert caught.value.code == "terminal_executable_not_absolute"

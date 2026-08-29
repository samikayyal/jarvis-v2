from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

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


def test_operating_system_name_safe_read_uses_broker_owned_cwd() -> None:
    adapter = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **_kwargs: object(),
        run_sync=lambda *_args, **_kwargs: SimpleNamespace(
            final_output=AgentsSdkPlan(
                reply_text="I read the operating-system name.",
                execution_host="ubuntu",
                host_reason_code="default_ubuntu",
                proposal=AgentsSdkProposal(
                    kind="terminal",
                    preview="Read the operating-system name.",
                    payload={
                        "host": "ubuntu",
                        "executable": "/usr/bin/uname",
                        "arguments": ["-s"],
                        "cwd": ".",
                        "components": [],
                    },
                ),
            )
        ),
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
    )

    result = adapter.run(_request("inspect the operating system"))

    assert result.proposal is not None
    assert json.loads(result.proposal.payload) == {
        "host": "ubuntu",
        "executable": "/usr/bin/uname",
        "arguments": ["-s"],
        "cwd": "/tmp",
        "components": [],
    }


def test_windows_hostname_safe_read_uses_broker_owned_cwd() -> None:
    adapter = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **_kwargs: object(),
        run_sync=lambda *_args, **_kwargs: SimpleNamespace(
            final_output=AgentsSdkPlan(
                reply_text="I read the Windows host name.",
                execution_host="windows",
                host_reason_code="windows_dependency",
                proposal=AgentsSdkProposal(
                    kind="terminal",
                    preview="Read the authorized Windows laptop host name.",
                    payload={
                        "host": "windows",
                        "executable": r"C:\Windows\System32\hostname.exe",
                        "arguments": [],
                        "cwd": ".",
                        "components": [],
                    },
                ),
            )
        ),
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
    )

    result = adapter.run(_request("read the authorized Windows laptop host name"))

    assert result.proposal is not None
    assert json.loads(result.proposal.payload) == {
        "host": "windows",
        "executable": r"C:\Windows\System32\hostname.exe",
        "arguments": [],
        "cwd": r"C:\Windows\System32",
        "components": [],
    }


def test_relative_cwd_still_fails_for_an_ordinary_terminal_action() -> None:
    adapter = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **_kwargs: object(),
        run_sync=lambda *_args, **_kwargs: SimpleNamespace(
            final_output=AgentsSdkPlan(
                reply_text="I prepared the action.",
                execution_host="ubuntu",
                host_reason_code="default_ubuntu",
                proposal=AgentsSdkProposal(
                    kind="terminal",
                    preview="Create the acceptance marker.",
                    payload={
                        "host": "ubuntu",
                        "executable": "/usr/bin/touch",
                        "arguments": ["/tmp/ticket32-marker"],
                        "cwd": ".",
                        "components": [],
                    },
                ),
            )
        ),
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
    )

    with pytest.raises(OrchestrationAdapterError) as caught:
        adapter.run(_request("create an acceptance marker"))

    assert caught.value.code == "terminal_cwd_not_absolute"


@pytest.mark.parametrize(
    "components",
    (
        None,
        [
            {
                "executable": "/usr/bin/printf",
                "arguments": ["Linux\\n"],
            },
            {
                "executable": "/usr/bin/head",
                "arguments": ["-n", "1"],
                "operator_before": "|",
                "redirections": [],
            },
        ],
    ),
)
def test_provider_schema_accepts_valid_single_and_compound_terminal_payloads(
    components: list[dict[str, object]] | None,
) -> None:
    captured: dict[str, object] = {}
    payload: dict[str, object] = {
        "host": "ubuntu",
        "executable": ("/usr/bin/uname" if components is None else "/usr/bin/printf"),
        "arguments": ["-s"] if components is None else ["Linux\\n"],
        "cwd": "/workspace",
    }
    if components is not None:
        payload["components"] = components

    def run_sync(_agent: object, _text: str, **_kwargs: object) -> object:
        return SimpleNamespace(
            final_output=captured["output_type"].validate_json(
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
        )

    adapter = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **kwargs: captured.update(kwargs) or object(),
        run_sync=run_sync,
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
    )

    result = adapter.run(_request("inspect the operating system"))

    assert result.proposal is not None
    frozen_payload = json.loads(result.proposal.payload)
    assert {
        field: frozen_payload[field]
        for field in ("host", "executable", "arguments", "cwd")
    } == {field: payload[field] for field in ("host", "executable", "arguments", "cwd")}
    if components is None:
        assert frozen_payload["components"] == []
    else:
        assert [
            component["executable"] for component in frozen_payload["components"]
        ] == [
            "/usr/bin/printf",
            "/usr/bin/head",
        ]
        assert frozen_payload["components"][1]["operator_before"] == "|"

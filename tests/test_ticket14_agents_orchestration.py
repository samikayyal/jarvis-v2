from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from jarvis_control_plane.models import OrchestrationRequest, RequestState
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
                execution_host="ubuntu",
                host_reason_code="default_ubuntu",
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
    assert captured["output_type"] is AgentsSdkPlan
    assert captured["run_text"] == "inspect the repository"
    assert captured["run_kwargs"] == {
        "max_turns": 4,
        "run_config": captured["run_kwargs"]["run_config"],
    }
    assert result.reply_text.startswith("[ubuntu: The request is host-neutral")
    assert result.proposal is None


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


@pytest.mark.parametrize(
    ("request_text", "host", "reason_code"),
    (
        (
            "Inspect an .exe stored in the Ubuntu repository.",
            "ubuntu",
            "default_ubuntu",
        ),
        (
            "Use my laptop's Windows-only accounting app.",
            "windows",
            "windows_dependency",
        ),
        ("Run this on my Windows laptop.", "windows", "explicit_windows"),
    ),
)
def test_typed_host_selection_does_not_depend_on_substring_routing(
    request_text: str, host: str, reason_code: str
) -> None:
    def run_sync(_agent: object, _text: str, **_kwargs: object) -> object:
        return SimpleNamespace(
            final_output=AgentsSdkPlan(
                reply_text="The bounded plan is ready.",
                execution_host=host,
                host_reason_code=reason_code,
            )
        )

    result = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **_kwargs: object(),
        run_sync=run_sync,
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
    ).run(_request(request_text))

    assert result.reply_text.startswith(f"[{host}:")

from __future__ import annotations

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


@pytest.mark.parametrize(
    ("request_text", "expected_reply"),
    [
        (
            "Read my Google Calendar and list today's events.",
            "Calendar is not available in Jarvis v1.",
        ),
        (
            (
                "Ticket 31 excluded Gmail gate. Prepare, but do not execute, "
                "deletion of the Gmail message with the labeled subject."
            ),
            "Destructive Gmail operations are not available in Jarvis v1.",
        ),
        (
            "Delete every email in my inbox.",
            "Destructive Gmail operations are not available in Jarvis v1.",
        ),
        (
            "In Gmail, trash the selected thread.",
            "Destructive Gmail operations are not available in Jarvis v1.",
        ),
    ],
)
def test_v1_exclusions_are_refused_before_model_or_tool_access(
    request_text: str,
    expected_reply: str,
) -> None:
    model_called = False

    def fail_if_model_called(*_args: object, **_kwargs: object) -> object:
        nonlocal model_called
        model_called = True
        raise AssertionError("excluded request reached the model")

    adapter = AgentsSdkOrchestrationAdapter(
        agent_factory=fail_if_model_called,
        run_sync=fail_if_model_called,
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
    )

    result = adapter.run(_request(request_text))

    assert model_called is False
    assert result.reply_text.startswith(expected_reply)
    assert result.proposal is None
    assert result.proposal_intent is None
    assert result.milestones[-1].stage == "excluded_capability_refused"


def test_gmail_read_about_deletion_is_not_misclassified_as_a_destructive_action() -> (
    None
):
    model_called = False

    def run_sync(_agent: object, _text: str, **_kwargs: object) -> object:
        nonlocal model_called
        model_called = True
        return SimpleNamespace(
            final_output=AgentsSdkPlan(reply_text="I found the deletion notices.")
        )

    adapter = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **_kwargs: object(),
        run_sync=run_sync,
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
    )

    result = adapter.run(
        _request("Find Gmail messages about account deletion notices.")
    )

    assert model_called is True
    assert result.reply_text == "I found the deletion notices."


def test_vault_write_single_change_wrapper_normalizes_to_a_path_mapping() -> None:
    current_content = "# Synthetic note\n\nExisting content.\n"
    adapter = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **_kwargs: object(),
        run_sync=lambda _agent, _text, **_kwargs: SimpleNamespace(
            final_output=AgentsSdkPlan(
                reply_text="I prepared the note change.",
                proposal=AgentsSdkProposal(
                    kind="knowledge_vault_write",
                    preview="Append the Ticket 31 marker.",
                    payload={
                        "changes": {
                            "path": "Projects/Synthetic.md",
                            "content": current_content + "- marker\n",
                        }
                    },
                ),
            )
        ),
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
        vault_write_enabled=True,
    )

    result = adapter.run(_request("append one marker to Projects/Synthetic.md"))

    assert result.proposal_intent is not None
    assert result.proposal_intent.payload == {
        "changes": {
            "Projects/Synthetic.md": current_content + "- marker\n",
        }
    }


@pytest.mark.parametrize(
    "payload",
    (
        {
            "changes": {
                "path": "Projects/Synthetic.md",
                "content": "# Note\n",
                "authority": "persistent",
            }
        },
        {"changes": {"authority": "persistent"}},
    ),
)
def test_vault_write_unknown_fields_fail_closed(payload: dict[str, object]) -> None:
    adapter = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **_kwargs: object(),
        run_sync=lambda _agent, _text, **_kwargs: SimpleNamespace(
            final_output=AgentsSdkPlan(
                reply_text="I prepared the note change.",
                proposal=AgentsSdkProposal(
                    kind="knowledge_vault_write",
                    preview="Change the note.",
                    payload=payload,
                ),
            )
        ),
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
        vault_write_enabled=True,
    )

    with pytest.raises(OrchestrationAdapterError, match="unexpected shape"):
        adapter.run(_request("change a note"))


def test_model_proposed_authority_fields_fail_closed_before_freezing() -> None:
    def run_sync(_agent: object, _text: str, **_kwargs: object) -> object:
        return SimpleNamespace(
            final_output=AgentsSdkPlan(
                reply_text="I will change the permission policy.",
                execution_host="ubuntu",
                host_reason_code="default_ubuntu",
                proposal=AgentsSdkProposal(
                    kind="terminal",
                    preview="Grant persistent authority.",
                    payload={
                        "host": "ubuntu",
                        "executable": "/usr/bin/git",
                        "arguments": ["status"],
                        "cwd": "/workspace",
                        "approval": "persistent",
                    },
                ),
            )
        )

    adapter = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **_kwargs: object(),
        run_sync=run_sync,
        model_settings_factory=_FakeModelSettings,
        reasoning_factory=_FakeReasoning,
        run_config_factory=_FakeRunConfig,
    )

    with pytest.raises(OrchestrationAdapterError, match="outside terminal authority"):
        adapter.run(_request("grant yourself persistent access"))

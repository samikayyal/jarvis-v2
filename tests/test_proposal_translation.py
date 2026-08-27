from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from jarvis_control_plane.models import OrchestrationRequest, RequestState
from jarvis_control_plane.ports import OrchestrationAdapterError
from jarvis_control_plane.proposal_translation import (
    AgentsSdkPlan,
    AgentsSdkProposal,
    PlanTranslation,
    build_instructions,
    translate_plan,
)

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def _request() -> OrchestrationRequest:
    return OrchestrationRequest(
        state=RequestState(
            request_id="request-translation-001",
            event_id="event-translation-001",
            message_id="message-translation-001",
            operator_id="operator.test",
            session_id="working-session-translation-001",
            chat_id="operator.test",
            created_at=NOW,
            updated_at=NOW,
            status="accepted",
            phase="orchestration",
            model="gpt-5.6-terra",
            reasoning="high",
        ),
        text="prepare the requested action",
    )


def test_translation_interface_returns_reply_without_an_action() -> None:
    result = translate_plan(
        _request(), AgentsSdkPlan(reply_text="Nothing needs approval.")
    )

    assert result == PlanTranslation(reply_text="Nothing needs approval.")


def test_translation_interface_freezes_terminal_action_and_host_reason() -> None:
    result = translate_plan(
        _request(),
        AgentsSdkPlan(
            reply_text="I prepared the safe operating-system read.",
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
        ),
    )

    assert result.execution_host == "ubuntu"
    assert result.host_reason_code == "default_ubuntu"
    assert result.reply_text.startswith(
        "[ubuntu: The request is host-neutral, so Ubuntu is the default execution host.]"
    )
    assert result.proposal is not None
    assert json.loads(result.proposal.payload)["cwd"] == "/tmp"
    assert result.proposal_intent is None


def test_translation_interface_normalizes_gmail_send_metadata() -> None:
    result = translate_plan(
        _request(),
        AgentsSdkPlan(
            reply_text="I prepared the email.",
            proposal=AgentsSdkProposal(
                kind="gmail_send",
                preview="Model preview is replaced by the canonical preview.",
                payload={
                    "to": ["recipient@example.com"],
                    "cc": [],
                    "bcc": [],
                    "subject": "Hello",
                    "body": "Body",
                    "mime_type": "text/plain",
                    "attachments": [],
                    "threading": "new_message",
                },
            ),
        ),
    )

    assert result.proposal is not None
    payload = json.loads(result.proposal.payload)
    assert payload == {
        "bcc": [],
        "body": "Body",
        "cc": [],
        "mime_type": "text/plain",
        "subject": "Hello",
        "threading": "new_message",
        "to": ["recipient@example.com"],
    }
    assert result.proposal.preview.startswith("Gmail new send\n")


def test_translation_interface_normalizes_gmail_reply_thread_binding() -> None:
    result = translate_plan(
        _request(),
        AgentsSdkPlan(
            reply_text="I prepared the reply.",
            proposal=AgentsSdkProposal(
                kind="gmail_reply",
                preview="Model preview is replaced by the canonical preview.",
                payload={
                    "to": ["recipient@example.com"],
                    "cc": [],
                    "bcc": [],
                    "subject": "Re: Hello",
                    "body": "Thanks.",
                    "mime_type": "text/plain",
                    "source_message_id": "source-001",
                    "source_thread_id": "thread-001",
                    "thread_id": "thread-001",
                    "in_reply_to": "<source-001@example.com>",
                    "references": ["<source-001@example.com>"],
                    "threading": "gmail_threaded_reply",
                    "attachments": [],
                },
            ),
        ),
    )

    assert result.proposal is not None
    payload = json.loads(result.proposal.payload)
    assert payload["thread_id"] == "thread-001"
    assert payload["source_message_id"] == "source-001"
    assert payload["references"] == ["<source-001@example.com>"]
    assert result.proposal.preview.startswith("Gmail typed reply\n")


def test_translation_interface_normalizes_vault_single_change_wrapper() -> None:
    result = translate_plan(
        _request(),
        AgentsSdkPlan(
            reply_text="I prepared the note change.",
            proposal=AgentsSdkProposal(
                kind="knowledge_vault_write",
                preview="Append the marker.",
                payload={
                    "changes": {
                        "path": "Projects/Synthetic.md",
                        "content": "# Synthetic\n\n- marker\n",
                    }
                },
            ),
        ),
    )

    assert result.proposal is None
    assert result.proposal_intent is not None
    assert result.proposal_intent.payload == {
        "changes": {"Projects/Synthetic.md": "# Synthetic\n\n- marker\n"}
    }


def test_translation_interface_maps_terminal_validation_to_stable_code() -> None:
    with pytest.raises(OrchestrationAdapterError) as caught:
        translate_plan(
            _request(),
            AgentsSdkPlan(
                reply_text="I prepared the action.",
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
            ),
        )

    assert caught.value.code == "terminal_executable_not_absolute"


def test_translation_interface_rejects_host_fields_for_connected_actions() -> None:
    with pytest.raises(
        OrchestrationAdapterError,
        match="must not select an execution host",
    ):
        translate_plan(
            _request(),
            AgentsSdkPlan(
                reply_text="I prepared the email.",
                execution_host="ubuntu",
                host_reason_code="default_ubuntu",
            ),
        )


def test_instructions_live_with_the_translation_interface() -> None:
    instructions = build_instructions(has_vault_read=True, has_vault_write=True)

    assert "payload must contain exactly the required fields host, executable" in (
        instructions
    )
    assert "read_knowledge_vault for each exact target path" in instructions
    assert "No tool, proposal" not in instructions

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from jarvis_control_plane.codex_specialist import (
    CodexAdapterResult,
    CodexInvocation,
    CodexMcpAdapter,
    CodexPolicyError,
    CodexSpecialist,
    CodexSpecialistConfig,
    CodexTimeoutError,
    CodexVerificationError,
    CodexWorkspace,
    CodexWorkspaceApproval,
    CodexWorkspaceProposal,
    CodexWorkspaceSnapshot,
)
from jarvis_control_plane.models import OrchestrationRequest, RequestState
from jarvis_control_plane.orchestration import (
    AgentsSdkOrchestrationAdapter,
    AgentsSdkPlan,
)

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


class _Adapter:
    def __init__(
        self,
        result: CodexAdapterResult | None = None,
        *,
        delay_seconds: float = 0,
        interrupt_confirmed: bool = True,
    ) -> None:
        self.result = result or CodexAdapterResult(
            status="completed",
            summary="Inspection complete.",
            changed_paths=(),
            test_evidence=(),
            unresolved_questions=(),
            thread_id="thread-001",
        )
        self.delay_seconds = delay_seconds
        self.interrupt_confirmed = interrupt_confirmed
        self.envelopes = []
        self.interrupted = []

    def invoke(self, envelope, *, deadline: float) -> CodexAdapterResult:
        self.envelopes.append(envelope)
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        return self.result

    def interrupt(self, request_id: str) -> bool:
        self.interrupted.append(request_id)
        return self.interrupt_confirmed


class _Inspector:
    def __init__(
        self,
        before: CodexWorkspaceSnapshot,
        after: CodexWorkspaceSnapshot,
    ) -> None:
        self._snapshots = iter((before, after))

    def snapshot(self, workspace: CodexWorkspace) -> CodexWorkspaceSnapshot:
        return next(self._snapshots)


class _ApprovalVerifier:
    def __init__(self, approved: bool = True) -> None:
        self.approved = approved
        self.checked = []

    def is_approved(
        self,
        proposal: CodexWorkspaceProposal,
        approval: CodexWorkspaceApproval,
    ) -> bool:
        self.checked.append((proposal, approval))
        return self.approved


def _snapshot(
    *,
    changed_paths: tuple[str, ...] = (),
    head: str = "abc123",
    remote_head: str = "abc123",
    forbidden_events: tuple[str, ...] = (),
    test_evidence: tuple[str, ...] = (),
) -> CodexWorkspaceSnapshot:
    return CodexWorkspaceSnapshot(
        head=head,
        remote_refs=(("origin/main", remote_head),),
        changed_paths=changed_paths,
        forbidden_events=forbidden_events,
        test_evidence=test_evidence,
    )


def _config(*, timeout_seconds: float = 300) -> CodexSpecialistConfig:
    return CodexSpecialistConfig(
        workspaces=(
            CodexWorkspace(
                name="jarvis",
                host="windows",
                cwd="C:/work/Jarvis-v2",
                write_paths=("src/", "tests/"),
            ),
        ),
        model="gpt-5.6-sol",
        reasoning="high",
        timeout_seconds=timeout_seconds,
    )


def _specialist(
    *,
    adapter: _Adapter | None = None,
    before: CodexWorkspaceSnapshot | None = None,
    after: CodexWorkspaceSnapshot | None = None,
    config: CodexSpecialistConfig | None = None,
    approval_verifier: _ApprovalVerifier | None = None,
) -> tuple[CodexSpecialist, _Adapter]:
    adapter = adapter or _Adapter()
    specialist = CodexSpecialist(
        config=config or _config(),
        adapter=adapter,
        inspector=_Inspector(before or _snapshot(), after or _snapshot()),
        approval_verifier=approval_verifier or _ApprovalVerifier(),
    )
    return specialist, adapter


def test_read_only_invocation_freezes_the_complete_execution_envelope() -> None:
    specialist, adapter = _specialist()

    result = specialist.invoke(
        CodexInvocation(
            request_id="request-001",
            workspace="jarvis",
            operation="review",
            task="Review the current diff.",
        )
    )

    envelope = adapter.envelopes[0]
    assert envelope.host == "windows"
    assert envelope.cwd == "C:/work/Jarvis-v2"
    assert envelope.model == "gpt-5.6-sol"
    assert envelope.reasoning == "high"
    assert envelope.sandbox == "read-only"
    assert envelope.approval_policy == "on-request"
    assert envelope.timeout_seconds == 300
    assert envelope.operation == "review"
    assert result.verified is True
    assert result.changed_paths == ()


def test_danger_full_access_and_never_approve_are_not_configurable() -> None:
    with pytest.raises(CodexPolicyError, match="danger-full-access"):
        CodexSpecialistConfig(
            workspaces=(
                CodexWorkspace(name="jarvis", host="ubuntu", cwd="/srv/jarvis"),
            ),
            model="gpt-5.6-sol",
            reasoning="high",
            sandbox="danger-full-access",
        )

    with pytest.raises(CodexPolicyError, match="never-approve"):
        CodexSpecialistConfig(
            workspaces=(
                CodexWorkspace(name="jarvis", host="ubuntu", cwd="/srv/jarvis"),
            ),
            model="gpt-5.6-sol",
            reasoning="high",
            approval_policy="never-approve",
        )


@pytest.mark.parametrize("cwd", ["relative/repo", "C:relative/repo"])
def test_workspace_requires_an_absolute_canonical_cwd(cwd: str) -> None:
    with pytest.raises(ValueError, match="absolute canonical path"):
        CodexWorkspace(name="jarvis", host="windows", cwd=cwd)


def test_workspace_preparation_requires_an_exact_approved_proposal() -> None:
    specialist, _adapter = _specialist()

    with pytest.raises(CodexPolicyError, match="approved exact proposal"):
        specialist.invoke(
            CodexInvocation(
                request_id="request-001",
                workspace="jarvis",
                operation="workspace_prepare",
                task="Prepare the requested source change.",
            )
        )


def test_workspace_preparation_accepts_only_matching_approval_and_allowed_paths() -> (
    None
):
    proposal = CodexWorkspaceProposal.create(
        action_id="action-001",
        request_id="request-001",
        workspace="jarvis",
        task="Prepare the requested source change.",
        allowed_paths=("src/", "tests/"),
    )
    approval = CodexWorkspaceApproval(
        action_id=proposal.action_id,
        request_id=proposal.request_id,
        proposal_digest=proposal.digest,
    )
    adapter = _Adapter(
        CodexAdapterResult(
            status="completed",
            summary="Prepared and tested the change.",
            changed_paths=("src/feature.py", "tests/test_feature.py"),
            test_evidence=("2 passed",),
            unresolved_questions=(),
            thread_id="thread-002",
        )
    )
    specialist, adapter = _specialist(
        adapter=adapter,
        after=_snapshot(
            changed_paths=("src/feature.py", "tests/test_feature.py"),
            test_evidence=("2 passed",),
        ),
    )

    result = specialist.invoke(
        CodexInvocation(
            request_id="request-001",
            workspace="jarvis",
            operation="workspace_prepare",
            task="Prepare the requested source change.",
            proposal=proposal,
            approval=approval,
        )
    )

    assert adapter.envelopes[0].sandbox == "workspace-write"
    assert adapter.envelopes[0].proposal_digest == proposal.digest
    assert result.changed_paths == ("src/feature.py", "tests/test_feature.py")
    assert result.test_evidence == ("2 passed",)


def test_workspace_preparation_rejects_a_forged_approval_object() -> None:
    proposal = CodexWorkspaceProposal.create(
        action_id="action-001",
        request_id="request-001",
        workspace="jarvis",
        task="Prepare the requested source change.",
        allowed_paths=("src/",),
    )
    specialist, adapter = _specialist(
        approval_verifier=_ApprovalVerifier(approved=False)
    )

    with pytest.raises(CodexPolicyError, match="authoritative approval"):
        specialist.invoke(
            CodexInvocation(
                request_id="request-001",
                workspace="jarvis",
                operation="workspace_prepare",
                task=proposal.task,
                proposal=proposal,
                approval=CodexWorkspaceApproval(
                    action_id=proposal.action_id,
                    request_id=proposal.request_id,
                    proposal_digest=proposal.digest,
                ),
            )
        )

    assert adapter.envelopes == []


def test_independent_verification_rejects_read_only_mutation_and_false_claims() -> None:
    adapter = _Adapter()
    specialist, _adapter = _specialist(
        adapter=adapter,
        after=_snapshot(changed_paths=("README.md",)),
    )

    with pytest.raises(CodexVerificationError, match="read-only"):
        specialist.invoke(
            CodexInvocation(
                request_id="request-001",
                workspace="jarvis",
                operation="inspect",
                task="Inspect the repository.",
            )
        )


def test_independent_verification_rejects_a_read_only_head_change() -> None:
    specialist, _adapter = _specialist(after=_snapshot(head="def456"))

    with pytest.raises(CodexVerificationError, match="read-only"):
        specialist.invoke(
            CodexInvocation(
                request_id="request-001",
                workspace="jarvis",
                operation="inspect",
                task="Inspect the repository.",
            )
        )


def test_independent_verification_rejects_out_of_scope_workspace_changes() -> None:
    proposal = CodexWorkspaceProposal.create(
        action_id="action-001",
        request_id="request-001",
        workspace="jarvis",
        task="Prepare the requested source change.",
        allowed_paths=("src/",),
    )
    specialist, _adapter = _specialist(
        after=_snapshot(changed_paths=("deployment/service.ini",)),
    )

    with pytest.raises(CodexVerificationError, match="outside the approved paths"):
        specialist.invoke(
            CodexInvocation(
                request_id="request-001",
                workspace="jarvis",
                operation="workspace_prepare",
                task=proposal.task,
                proposal=proposal,
                approval=CodexWorkspaceApproval(
                    action_id=proposal.action_id,
                    request_id=proposal.request_id,
                    proposal_digest=proposal.digest,
                ),
            )
        )


def test_independent_verification_rejects_unverified_test_claims() -> None:
    adapter = _Adapter(
        CodexAdapterResult(
            status="completed",
            summary="Tests passed.",
            changed_paths=(),
            test_evidence=("999 passed",),
            unresolved_questions=(),
            thread_id="thread-claims",
        )
    )
    specialist, _adapter = _specialist(adapter=adapter)

    with pytest.raises(CodexVerificationError, match="test-evidence claims"):
        specialist.invoke(
            CodexInvocation(
                request_id="request-001",
                workspace="jarvis",
                operation="review",
                task="Run and verify tests.",
            )
        )


@pytest.mark.parametrize(
    ("before", "after", "message"),
    [
        (_snapshot(), _snapshot(remote_head="def456"), "push"),
        (_snapshot(), _snapshot(forbidden_events=("history_rewrite",)), "history"),
        (_snapshot(), _snapshot(forbidden_events=("approval_bypass",)), "approval"),
        (
            _snapshot(),
            _snapshot(forbidden_events=("trust_critical_activation",)),
            "trust-critical",
        ),
        (
            _snapshot(),
            _snapshot(forbidden_events=("danger_full_access",)),
            "danger-full-access",
        ),
    ],
)
def test_independent_verification_rejects_forbidden_specialist_effects(
    before: CodexWorkspaceSnapshot,
    after: CodexWorkspaceSnapshot,
    message: str,
) -> None:
    specialist, _adapter = _specialist(before=before, after=after)

    with pytest.raises(CodexVerificationError, match=message):
        specialist.invoke(
            CodexInvocation(
                request_id="request-001",
                workspace="jarvis",
                operation="review",
                task="Review the repository.",
            )
        )


def test_specialist_interrupts_the_adapter_at_the_frozen_deadline() -> None:
    adapter = _Adapter(delay_seconds=0.05)
    specialist, _adapter = _specialist(
        adapter=adapter,
        config=_config(timeout_seconds=0.01),
    )

    with pytest.raises(CodexTimeoutError, match="deadline"):
        specialist.invoke(
            CodexInvocation(
                request_id="request-timeout",
                workspace="jarvis",
                operation="inspect",
                task="Inspect the repository.",
            )
        )

    assert adapter.interrupted == ["request-timeout"]


def test_timeout_rejects_an_unconfirmed_interrupt_or_late_mutation() -> None:
    unconfirmed = _Adapter(delay_seconds=0.05, interrupt_confirmed=False)
    specialist, _adapter = _specialist(
        adapter=unconfirmed,
        config=_config(timeout_seconds=0.01),
    )
    with pytest.raises(CodexVerificationError, match="interrupt"):
        specialist.invoke(
            CodexInvocation(
                request_id="request-unconfirmed",
                workspace="jarvis",
                operation="inspect",
                task="Inspect the repository.",
            )
        )

    late_mutation = _Adapter(delay_seconds=0.05)
    specialist, _adapter = _specialist(
        adapter=late_mutation,
        after=_snapshot(changed_paths=("README.md",)),
        config=_config(timeout_seconds=0.01),
    )
    with pytest.raises(CodexVerificationError, match="read-only"):
        specialist.invoke(
            CodexInvocation(
                request_id="request-late-mutation",
                workspace="jarvis",
                operation="inspect",
                task="Inspect the repository.",
            )
        )


def test_agents_orchestration_exposes_only_the_closed_read_only_codex_tool() -> None:
    specialist, codex_adapter = _specialist()
    captured: dict[str, object] = {}

    class _Reasoning:
        def __init__(self, *, effort: str) -> None:
            self.effort = effort

    def agent_factory(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    def run_sync(_agent: object, _text: str, **_kwargs: object) -> object:
        tools = captured["tools"]
        codex_tool = next(
            tool for tool in tools if tool.name == "invoke_codex_specialist"
        )
        captured["codex_output"] = asyncio.run(
            codex_tool.on_invoke_tool(
                None,
                json.dumps(
                    {
                        "workspace": "jarvis",
                        "operation": "review",
                        "task": "Review the current diff.",
                    }
                ),
            )
        )
        return SimpleNamespace(
            final_output=AgentsSdkPlan(reply_text="Review complete.")
        )

    request = OrchestrationRequest(
        state=RequestState(
            request_id="request-agent",
            event_id="event-agent",
            message_id="message-agent",
            operator_id="operator.test",
            session_id="session-agent",
            chat_id="operator.test",
            created_at=NOW,
            updated_at=NOW,
            status="accepted",
            phase="orchestration",
            model="gpt-5.6-terra",
            reasoning="medium",
        ),
        text="Ask Codex to review the current diff.",
    )

    result = AgentsSdkOrchestrationAdapter(
        agent_factory=agent_factory,
        run_sync=run_sync,
        model_settings_factory=lambda **values: values,
        reasoning_factory=_Reasoning,
        run_config_factory=lambda **values: values,
        codex_specialist=specialist,
    ).run(request)

    tool = next(
        tool for tool in captured["tools"] if tool.name == "invoke_codex_specialist"
    )
    assert tool.needs_approval is False
    assert tool.params_json_schema["properties"]["operation"]["enum"] == [
        "inspect",
        "review",
    ]
    assert captured["codex_output"]["verified"] is True
    assert codex_adapter.envelopes[0].request_id == "request-agent"
    assert codex_adapter.envelopes[0].sandbox == "read-only"
    assert result.reply_text == "Review complete."


def test_mcp_adapter_maps_only_the_frozen_envelope_to_the_codex_tool() -> None:
    class _McpClient:
        def __init__(self) -> None:
            self.calls = []
            self.interrupted = []

        def call_tool(self, name: str, arguments: dict, *, deadline: float) -> dict:
            self.calls.append((name, arguments, deadline))
            return {
                "structuredContent": {
                    "threadId": "thread-mcp-001",
                    "content": json.dumps(
                        {
                            "status": "completed",
                            "summary": "Review complete.",
                            "changed_paths": [],
                            "test_evidence": ["12 passed"],
                            "unresolved_questions": [],
                        }
                    ),
                }
            }

        def interrupt(self, request_id: str) -> bool:
            self.interrupted.append(request_id)
            return True

    client = _McpClient()
    adapter = CodexMcpAdapter(client=client)
    specialist, _adapter = _specialist(
        adapter=adapter,
        after=_snapshot(test_evidence=("12 passed",)),
    )

    result = specialist.invoke(
        CodexInvocation(
            request_id="request-mcp",
            workspace="jarvis",
            operation="review",
            task="Review the current diff.",
        )
    )

    tool_name, arguments, _deadline = client.calls[0]
    assert tool_name == "codex"
    assert set(arguments) == {
        "prompt",
        "approval-policy",
        "cwd",
        "model",
        "sandbox",
        "config",
        "developer-instructions",
    }
    assert arguments["approval-policy"] == "on-request"
    assert arguments["cwd"] == "C:/work/Jarvis-v2"
    assert arguments["model"] == "gpt-5.6-sol"
    assert arguments["sandbox"] == "read-only"
    assert arguments["config"] == {"model_reasoning_effort": "high"}
    assert "push" in arguments["developer-instructions"]
    assert result.thread_id == "thread-mcp-001"
    assert result.test_evidence == ("12 passed",)


def test_mcp_adapter_rejects_untyped_or_extended_structured_content() -> None:
    class _McpClient:
        def call_tool(self, _name: str, _arguments: dict, *, deadline: float) -> dict:
            return {
                "structuredContent": {
                    "threadId": "thread-mcp-001",
                    "content": json.dumps(
                        {
                            "status": "completed",
                            "summary": "Review complete.",
                            "changed_paths": [],
                            "test_evidence": [],
                            "unresolved_questions": [],
                            "authority": "granted",
                        }
                    ),
                }
            }

        def interrupt(self, request_id: str) -> bool:
            return True

    specialist, _adapter = _specialist(adapter=CodexMcpAdapter(client=_McpClient()))

    with pytest.raises(CodexVerificationError, match="structured result"):
        specialist.invoke(
            CodexInvocation(
                request_id="request-mcp",
                workspace="jarvis",
                operation="inspect",
                task="Inspect the repository.",
            )
        )

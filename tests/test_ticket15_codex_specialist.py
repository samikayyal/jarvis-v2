from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from jarvis_control_plane.codex_specialist import (
    CodexAdapterResult,
    CodexExecutionEnvelope,
    CodexInterruption,
    CodexInvocation,
    CodexMcpAdapter,
    CodexMcpApprovalRequest,
    CodexPolicyError,
    CodexSpecialist,
    CodexSpecialistConfig,
    CodexVerificationError,
    CodexWorkspace,
    CodexWorkspaceApproval,
    CodexWorkspaceChange,
    CodexWorkspaceProposal,
    CodexWorkspaceSnapshot,
)
from jarvis_control_plane.models import OrchestrationRequest, RequestState
from jarvis_control_plane.orchestration import (
    AgentsSdkOrchestrationAdapter,
    AgentsSdkPlan,
)
from jarvis_control_plane.traces import DiagnosticTraceRecorder

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
SRC_BEFORE = "a" * 64
SRC_AFTER = "b" * 64
TEST_BEFORE = "c" * 64
TEST_AFTER = "d" * 64


def _patch(path: str, *, before: str = "before", after: str = "after") -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1 +1 @@\n"
        f"-{before}\n"
        f"+{after}\n"
    )


def _change(
    path: str, before_digest: str = SRC_BEFORE, after_digest: str = SRC_AFTER
) -> CodexWorkspaceChange:
    return CodexWorkspaceChange(
        path=path,
        before_digest=before_digest,
        after_digest=after_digest,
    )


def _proposal(
    *,
    action_id: str = "action-001",
    request_id: str = "request-001",
    workspace: str = "jarvis",
    task: str = "Prepare the requested source change.",
    allowed_paths: tuple[str, ...] = ("src/",),
    changes: tuple[CodexWorkspaceChange, ...] = (_change("src/feature.py"),),
    base_head: str = "abc123",
    base_remote_refs: tuple[tuple[str, str], ...] = (("origin/main", "abc123"),),
    patch: str = _patch("src/feature.py"),
) -> CodexWorkspaceProposal:
    return CodexWorkspaceProposal.create(
        action_id=action_id,
        request_id=request_id,
        workspace=workspace,
        task=task,
        allowed_paths=allowed_paths,
        base_head=base_head,
        base_remote_refs=base_remote_refs,
        changes=changes,
        patch=patch,
    )


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
        self._interrupted = Event()
        self.invoke_started = Event()
        self.invoke_finished = Event()

    def invoke(self, envelope, *, deadline: float) -> CodexAdapterResult:
        self.envelopes.append(envelope)
        self.invoke_started.set()
        try:
            if self.delay_seconds:
                self._interrupted.wait(self.delay_seconds)
            return self.result
        finally:
            self.invoke_finished.set()

    def interrupt(
        self, request_id: str, *, deadline: float
    ) -> CodexInterruption | bool:
        self.interrupted.append(request_id)
        if not self.interrupt_confirmed:
            return False
        self._interrupted.set()
        return CodexInterruption(result=self.result)


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


class _TraceRecorder(DiagnosticTraceRecorder):
    """Small write-only recorder double that preserves the admission seam."""

    def __init__(self, *, failure: Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.failure = failure

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        result = kwargs["operation"]()
        kwargs["captured_result"] = result
        return result


def _snapshot(
    *,
    changed_paths: tuple[str, ...] = (),
    file_digests: tuple[tuple[str, str | None], ...] = (),
    head: str = "abc123",
    remote_head: str = "abc123",
    forbidden_events: tuple[str, ...] = (),
    test_evidence: tuple[str, ...] = (),
) -> CodexWorkspaceSnapshot:
    return CodexWorkspaceSnapshot(
        head=head,
        remote_refs=(("origin/main", remote_head),),
        changed_paths=changed_paths,
        file_digests=file_digests,
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
    trace: _TraceRecorder | None = None,
) -> tuple[CodexSpecialist, _Adapter]:
    adapter = adapter or _Adapter()
    specialist = CodexSpecialist(
        config=config or _config(),
        adapter=adapter,
        inspector=_Inspector(before or _snapshot(), after or _snapshot()),
        trace=trace or _TraceRecorder(),
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


def test_specialist_admits_the_frozen_envelope_and_raw_result_to_trace() -> None:
    trace = _TraceRecorder()
    specialist, adapter = _specialist(trace=trace)

    specialist.invoke(
        CodexInvocation(
            request_id="request-traced",
            workspace="jarvis",
            operation="review",
            task="Review the current diff.",
        )
    )

    assert len(trace.calls) == 1
    call = trace.calls[0]
    assert call["operation_type"] == "codex"
    assert call["input_payload"] == adapter.envelopes[0]
    assert call["arguments"] == {
        "operation": "invoke",
        "workspace_approval": None,
    }
    assert call["captured_result"] == adapter.result


def test_trace_admission_failure_prevents_the_codex_adapter_from_starting() -> None:
    trace = _TraceRecorder(failure=RuntimeError("trace capacity unavailable"))
    specialist, adapter = _specialist(trace=trace)

    with pytest.raises(RuntimeError, match="trace capacity unavailable"):
        specialist.invoke(
            CodexInvocation(
                request_id="request-trace-rejected",
                workspace="jarvis",
                operation="inspect",
                task="Inspect the repository.",
            )
        )

    assert adapter.envelopes == []


@pytest.mark.parametrize(
    "before,error",
    [
        (_snapshot(head="stale-head"), "workspace base changed"),
        (_snapshot(remote_head="stale-remote"), "workspace base changed"),
        (
            _snapshot(file_digests=(("src/feature.py", TEST_BEFORE),)),
            "workspace base content changed",
        ),
    ],
)
def test_stale_approved_base_is_rejected_before_codex_starts(
    before: CodexWorkspaceSnapshot,
    error: str,
) -> None:
    proposal = _proposal(request_id="request-stale-base")
    approval = CodexWorkspaceApproval(
        action_id=proposal.action_id,
        request_id=proposal.request_id,
        proposal_digest=proposal.digest,
    )
    specialist, adapter = _specialist(before=before)

    with pytest.raises(CodexVerificationError, match=error):
        specialist.invoke(
            CodexInvocation(
                request_id=proposal.request_id,
                workspace=proposal.workspace,
                operation="workspace_prepare",
                task=proposal.task,
                proposal=proposal,
                approval=approval,
            )
        )

    assert adapter.envelopes == []


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


def test_workspace_proposal_digest_binds_base_patch_and_file_transition() -> None:
    proposal = _proposal()

    payload = proposal._payload()
    assert payload["base_head"] == "abc123"
    assert payload["base_remote_refs"] == [["origin/main", "abc123"]]
    assert payload["patch"] == proposal.patch
    assert payload["changes"] == [
        {
            "path": "src/feature.py",
            "before_digest": SRC_BEFORE,
            "after_digest": SRC_AFTER,
        }
    ]
    assert _proposal(patch=_patch("src/feature.py", after="changed")).digest != (
        proposal.digest
    )


def test_workspace_proposal_rejects_patch_outside_declared_changes() -> None:
    with pytest.raises(ValueError, match="patch paths"):
        _proposal(patch=_patch("deployment/service.ini"))


def test_workspace_proposal_rejects_file_mode_changes() -> None:
    patch = (
        "diff --git a/src/feature.py b/src/feature.py\n"
        "old mode 100644\n"
        "new mode 100755\n"
        "--- a/src/feature.py\n"
        "+++ b/src/feature.py\n"
        "@@ -1 +1 @@\n"
        "-before\n"
        "+after\n"
    )

    with pytest.raises(ValueError, match="text changes only"):
        _proposal(patch=patch)


@pytest.mark.parametrize(
    ("mode_header", "old_marker", "new_marker"),
    [
        ("new file mode 100755", "/dev/null", "b/src/feature.py"),
        ("new file mode 120000", "/dev/null", "b/src/feature.py"),
        ("deleted file mode 100755", "a/src/feature.py", "/dev/null"),
    ],
)
def test_workspace_proposal_rejects_create_delete_mode_headers(
    mode_header: str,
    old_marker: str,
    new_marker: str,
) -> None:
    patch = (
        "diff --git a/src/feature.py b/src/feature.py\n"
        f"{mode_header}\n"
        f"--- {old_marker}\n"
        f"+++ {new_marker}\n"
        "@@ -1 +1 @@\n"
        "-before\n"
        "+after\n"
    )

    with pytest.raises(ValueError, match="text changes only"):
        _proposal(patch=patch)


def test_workspace_preparation_accepts_only_matching_approval_and_allowed_paths() -> (
    None
):
    proposal = _proposal(
        allowed_paths=("src/", "tests/"),
        changes=(
            _change("src/feature.py"),
            _change("tests/test_feature.py", TEST_BEFORE, TEST_AFTER),
        ),
        patch=(
            _patch("src/feature.py")
            + _patch("tests/test_feature.py", before="old test", after="new test")
        ),
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
            file_digests=(
                ("src/feature.py", SRC_AFTER),
                ("tests/test_feature.py", TEST_AFTER),
            ),
            test_evidence=("2 passed",),
        ),
        before=_snapshot(
            file_digests=(
                ("src/feature.py", SRC_BEFORE),
                ("tests/test_feature.py", TEST_BEFORE),
            )
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
    proposal = _proposal()
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
    proposal = _proposal()
    specialist, _adapter = _specialist(
        before=_snapshot(file_digests=(("src/feature.py", SRC_BEFORE),)),
        after=_snapshot(changed_paths=("deployment/service.ini",)),
    )

    with pytest.raises(CodexVerificationError, match="exact approved paths"):
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


def test_independent_verification_rejects_content_that_is_not_in_the_proposal() -> None:
    proposal = _proposal()
    specialist, _adapter = _specialist(
        before=_snapshot(file_digests=(("src/feature.py", SRC_BEFORE),)),
        after=_snapshot(
            changed_paths=("src/feature.py",),
            file_digests=(("src/feature.py", "e" * 64),),
        ),
    )

    with pytest.raises(CodexVerificationError, match="content outside"):
        specialist.invoke(
            CodexInvocation(
                request_id=proposal.request_id,
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
    adapter = _Adapter(delay_seconds=0.2)
    specialist, _adapter = _specialist(
        adapter=adapter,
        config=_config(timeout_seconds=0.1),
    )

    result = specialist.invoke(
        CodexInvocation(
            request_id="request-timeout",
            workspace="jarvis",
            operation="inspect",
            task="Inspect the repository.",
        )
    )

    assert adapter.interrupted == ["request-timeout"]
    assert result.status == "incomplete"
    assert "frozen deadline" in result.summary


def test_timeout_rejects_an_unconfirmed_interrupt_or_late_mutation() -> None:
    unconfirmed = _Adapter(delay_seconds=0.2, interrupt_confirmed=False)
    specialist, _adapter = _specialist(
        adapter=unconfirmed,
        config=_config(timeout_seconds=0.1),
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
    assert unconfirmed.invoke_finished.is_set()

    late_mutation = _Adapter(delay_seconds=0.2)
    specialist, _adapter = _specialist(
        adapter=late_mutation,
        after=_snapshot(changed_paths=("README.md",)),
        config=_config(timeout_seconds=0.1),
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


def test_timeout_does_not_return_until_the_worker_is_quiescent() -> None:
    class _LateWorker(_Adapter):
        def __init__(self) -> None:
            super().__init__(delay_seconds=0.2)
            self.finished = False

        def invoke(self, envelope, *, deadline: float) -> CodexAdapterResult:
            result = super().invoke(envelope, deadline=deadline)
            self.finished = True
            return result

    adapter = _LateWorker()
    specialist, _adapter = _specialist(
        adapter=adapter,
        config=_config(timeout_seconds=0.1),
    )

    result = specialist.invoke(
        CodexInvocation(
            request_id="request-quiescent",
            workspace="jarvis",
            operation="inspect",
            task="Inspect the repository.",
        )
    )

    assert result.status == "incomplete"
    assert adapter.finished is True


def test_timeout_retains_ownership_until_a_non_quiescent_worker_stops() -> None:
    class _StuckAdapter(_Adapter):
        def interrupt(self, request_id: str, *, deadline: float) -> CodexInterruption:
            self.interrupted.append(request_id)
            return CodexInterruption(result=self.result)

    adapter = _StuckAdapter(delay_seconds=0.3)
    specialist, _adapter = _specialist(
        adapter=adapter,
        config=_config(timeout_seconds=0.05),
    )

    with pytest.raises(CodexVerificationError, match="quiescence"):
        specialist.invoke(
            CodexInvocation(
                request_id="request-stuck",
                workspace="jarvis",
                operation="inspect",
                task="Inspect the repository.",
            )
        )
    assert adapter.invoke_finished.is_set()


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
    assert tool.timeout_seconds is None
    assert tool.params_json_schema["properties"]["operation"]["enum"] == [
        "inspect",
        "review",
    ]
    assert captured["codex_output"]["verified"] is True
    assert codex_adapter.envelopes[0].request_id == "request-agent"
    assert codex_adapter.envelopes[0].sandbox == "read-only"
    assert result.reply_text == "Review complete."


def test_agents_orchestration_preserves_a_typed_incomplete_codex_result() -> None:
    specialist, _codex_adapter = _specialist(
        adapter=_Adapter(delay_seconds=0.2),
        config=_config(timeout_seconds=0.1),
    )
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
                        "operation": "inspect",
                        "task": "Inspect the repository.",
                    }
                ),
            )
        )
        return SimpleNamespace(
            final_output=AgentsSdkPlan(reply_text="Inspection timed out safely.")
        )

    request = OrchestrationRequest(
        state=RequestState(
            request_id="request-agent-timeout",
            event_id="event-agent-timeout",
            message_id="message-agent-timeout",
            operator_id="operator.test",
            session_id="session-agent-timeout",
            chat_id="operator.test",
            created_at=NOW,
            updated_at=NOW,
            status="accepted",
            phase="orchestration",
            model="gpt-5.6-terra",
            reasoning="medium",
        ),
        text="Ask Codex to inspect the repository.",
    )

    result = AgentsSdkOrchestrationAdapter(
        agent_factory=agent_factory,
        run_sync=run_sync,
        model_settings_factory=lambda **values: values,
        reasoning_factory=_Reasoning,
        run_config_factory=lambda **values: values,
        codex_specialist=specialist,
    ).run(request)

    assert captured["codex_output"]["status"] == "incomplete"
    assert result.reply_text == "Inspection timed out safely."


def test_agents_orchestration_propagates_request_cancellation_into_codex() -> None:
    codex_adapter = _Adapter(delay_seconds=30)
    specialist, _unused = _specialist(adapter=codex_adapter)
    outcomes: list[object] = []

    orchestration = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **_values: object(),
        run_sync=lambda *_args, **_kwargs: object(),
        model_settings_factory=lambda **values: values,
        reasoning_factory=lambda **values: values,
        run_config_factory=lambda **values: values,
        codex_specialist=specialist,
    )
    invocation = CodexInvocation(
        request_id="request-agent-cancel",
        workspace="jarvis",
        operation="inspect",
        task="Inspect the repository.",
    )
    runner = Thread(target=lambda: outcomes.append(specialist.invoke(invocation)))
    runner.start()
    assert codex_adapter.invoke_started.wait(timeout=5)

    assert orchestration.cancel(request_id=invocation.request_id) is True

    runner.join(timeout=5)
    assert not runner.is_alive()
    assert codex_adapter.interrupted == [invocation.request_id]
    assert len(outcomes) == 1


def test_agents_orchestration_cancellation_prevents_a_queued_codex_start() -> None:
    specialist, codex_adapter = _specialist()
    orchestration = AgentsSdkOrchestrationAdapter(
        agent_factory=lambda **_values: object(),
        run_sync=lambda *_args, **_kwargs: object(),
        model_settings_factory=lambda **values: values,
        reasoning_factory=lambda **values: values,
        run_config_factory=lambda **values: values,
        codex_specialist=specialist,
    )
    request_id = "request-agent-cancelled-before-start"

    assert orchestration.cancel(request_id=request_id) is False
    with pytest.raises(CodexPolicyError, match="cancelled"):
        specialist.invoke(
            CodexInvocation(
                request_id=request_id,
                workspace="jarvis",
                operation="inspect",
                task="Inspect the repository.",
            ),
            is_cancelled=lambda: orchestration._request_is_cancelled(request_id),
        )

    assert codex_adapter.envelopes == []


def test_mcp_adapter_maps_only_the_frozen_envelope_to_the_codex_tool() -> None:
    class _McpClient:
        def __init__(self) -> None:
            self.calls = []
            self.interrupted = []

        def call_tool(
            self,
            name: str,
            arguments: dict,
            *,
            deadline: float,
            approval_callback,
        ) -> dict:
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
    adapter = CodexMcpAdapter(
        client=client,
        approval_handler=lambda _envelope, _request: "deny",
    )
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


def test_mcp_workspace_prompt_contains_the_frozen_approved_patch() -> None:
    proposal = _proposal(request_id="request-prompt-patch")
    envelope = CodexExecutionEnvelope(
        request_id=proposal.request_id,
        task=proposal.task,
        host="windows",
        cwd="C:/work/Jarvis-v2",
        model="gpt-5.6-sol",
        reasoning="high",
        sandbox="workspace-write",
        approval_policy="on-request",
        timeout_seconds=300,
        operation="workspace_prepare",
        allowed_paths=proposal.allowed_paths,
        proposal_digest=proposal.digest,
        proposal_base_head=proposal.base_head,
        proposal_remote_refs=proposal.base_remote_refs,
        proposal_changes=proposal.changes,
        proposal_patch=proposal.patch,
    )

    prompt = CodexMcpAdapter._prompt(envelope)

    assert proposal.patch in prompt
    assert "<approved_patch>" in prompt


def test_mcp_adapter_rejects_untyped_or_extended_structured_content() -> None:
    class _McpClient:
        def call_tool(
            self,
            _name: str,
            _arguments: dict,
            *,
            deadline: float,
            approval_callback,
        ) -> dict:
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

    specialist, _adapter = _specialist(
        adapter=CodexMcpAdapter(
            client=_McpClient(),
            approval_handler=lambda _envelope, _request: "deny",
        )
    )

    with pytest.raises(CodexVerificationError, match="structured result"):
        specialist.invoke(
            CodexInvocation(
                request_id="request-mcp",
                workspace="jarvis",
                operation="inspect",
                task="Inspect the repository.",
            )
        )


def test_mcp_adapter_rejects_oversized_content_before_json_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed = []
    monkeypatch.setattr(
        json,
        "loads",
        lambda _content: (
            parsed.append(True) or pytest.fail("oversized JSON was parsed")
        ),
    )

    with pytest.raises(CodexVerificationError, match="structured result"):
        CodexMcpAdapter._parse_result(
            {
                "structuredContent": {
                    "threadId": "thread-oversized",
                    "content": "x" * (512 * 1024 + 1),
                }
            }
        )

    assert parsed == []


def test_mcp_adapter_surfaces_approval_context_and_denies_read_only_escalation() -> (
    None
):
    callback_context = []
    decisions = []

    class _McpClient:
        def call_tool(
            self,
            _name: str,
            _arguments: dict,
            *,
            deadline: float,
            approval_callback,
        ) -> dict:
            decisions.append(
                approval_callback(
                    CodexMcpApprovalRequest(
                        thread_id="thread-mcp-approval",
                        request_id="call-001",
                        action="exec_command",
                        details={
                            "command": "git status",
                            "cwd": "C:/work/Jarvis-v2",
                        },
                    )
                )
            )
            return {
                "structuredContent": {
                    "threadId": "thread-mcp-approval",
                    "content": json.dumps(
                        {
                            "status": "completed",
                            "summary": "Review complete.",
                            "changed_paths": [],
                            "test_evidence": [],
                            "unresolved_questions": [],
                        }
                    ),
                }
            }

        def interrupt(self, _request_id: str) -> bool:
            return True

    def approval_handler(envelope, request):
        callback_context.append((envelope, request))
        return "allow"

    specialist, _adapter = _specialist(
        adapter=CodexMcpAdapter(
            client=_McpClient(),
            approval_handler=approval_handler,
        )
    )

    specialist.invoke(
        CodexInvocation(
            request_id="request-mcp-approval",
            workspace="jarvis",
            operation="review",
            task="Review the current diff.",
        )
    )

    assert decisions == ["deny"]
    envelope, request = callback_context[0]
    assert envelope.request_id == "request-mcp-approval"
    assert envelope.operation == "review"
    assert request.thread_id == "thread-mcp-approval"
    assert request.request_id == "call-001"
    assert request.action == "exec_command"
    assert request.details == {
        "command": "git status",
        "cwd": "C:/work/Jarvis-v2",
    }


def test_mcp_adapter_delegates_workspace_approval_with_frozen_envelope() -> None:
    proposal = _proposal(
        action_id="action-mcp-001",
        request_id="request-mcp-write",
        changes=(_change("src/feature.py"),),
        patch=_patch("src/feature.py"),
    )
    approval = CodexWorkspaceApproval(
        action_id=proposal.action_id,
        request_id=proposal.request_id,
        proposal_digest=proposal.digest,
    )
    decisions = []

    class _McpClient:
        def call_tool(
            self,
            _name: str,
            _arguments: dict,
            *,
            deadline: float,
            approval_callback,
        ) -> dict:
            decisions.append(
                approval_callback(
                    CodexMcpApprovalRequest(
                        thread_id="thread-mcp-write",
                        request_id="call-write-001",
                        action="apply_patch",
                        details={
                            "cwd": "C:/work/Jarvis-v2",
                            "fileChanges": [{"path": "src/feature.py"}],
                            "patch": proposal.patch,
                        },
                    )
                )
            )
            return {
                "structuredContent": {
                    "threadId": "thread-mcp-write",
                    "content": json.dumps(
                        {
                            "status": "completed",
                            "summary": "Prepared and tested the change.",
                            "changed_paths": ["src/feature.py"],
                            "test_evidence": [],
                            "unresolved_questions": [],
                        }
                    ),
                }
            }

        def interrupt(self, _request_id: str) -> bool:
            return True

    def approval_handler(envelope, request):
        assert envelope.request_id == proposal.request_id
        assert envelope.operation == "workspace_prepare"
        assert envelope.proposal_digest == proposal.digest
        assert envelope.allowed_paths == ("src",)
        assert request.thread_id == "thread-mcp-write"
        assert request.request_id == "call-write-001"
        assert request.action == "apply_patch"
        assert request.details == {
            "cwd": "C:/work/Jarvis-v2",
            "fileChanges": [{"path": "src/feature.py"}],
            "patch": proposal.patch,
        }
        return "allow"

    adapter = CodexMcpAdapter(
        client=_McpClient(),
        approval_handler=approval_handler,
    )
    specialist = CodexSpecialist(
        config=_config(),
        adapter=adapter,
        inspector=_Inspector(
            _snapshot(file_digests=(("src/feature.py", SRC_BEFORE),)),
            _snapshot(
                changed_paths=("src/feature.py",),
                file_digests=(("src/feature.py", SRC_AFTER),),
            ),
        ),
        trace=_TraceRecorder(),
        approval_verifier=_ApprovalVerifier(),
    )

    result = specialist.invoke(
        CodexInvocation(
            request_id=proposal.request_id,
            workspace="jarvis",
            operation="workspace_prepare",
            task=proposal.task,
            proposal=proposal,
            approval=approval,
        )
    )

    assert decisions == ["allow"]
    assert result.changed_paths == ("src/feature.py",)


def test_mcp_adapter_denies_forbidden_exec_command_before_operator_handler() -> None:
    proposal = _proposal(
        action_id="action-mcp-forbidden",
        request_id="request-mcp-forbidden",
    )
    approval = CodexWorkspaceApproval(
        action_id=proposal.action_id,
        request_id=proposal.request_id,
        proposal_digest=proposal.digest,
    )
    decisions = []
    handler_calls = []

    class _McpClient:
        def call_tool(
            self,
            _name: str,
            _arguments: dict,
            *,
            deadline: float,
            approval_callback,
        ) -> dict:
            decisions.append(
                approval_callback(
                    CodexMcpApprovalRequest(
                        thread_id="thread-mcp-forbidden",
                        request_id="call-forbidden-001",
                        action="exec_command",
                        details={
                            "command": "git push --force origin main",
                            "cwd": "C:/work/Jarvis-v2",
                        },
                    )
                )
            )
            return {
                "structuredContent": {
                    "threadId": "thread-mcp-forbidden",
                    "content": json.dumps(
                        {
                            "status": "completed",
                            "summary": "No workspace change was applied.",
                            "changed_paths": ["src/feature.py"],
                            "test_evidence": [],
                            "unresolved_questions": [],
                        }
                    ),
                }
            }

        def interrupt(self, _request_id: str) -> bool:
            return True

    adapter = CodexMcpAdapter(
        client=_McpClient(),
        approval_handler=lambda _envelope, _request: (
            handler_calls.append(True) or "allow"
        ),
    )
    specialist = CodexSpecialist(
        config=_config(),
        adapter=adapter,
        inspector=_Inspector(
            _snapshot(file_digests=(("src/feature.py", SRC_BEFORE),)),
            _snapshot(
                changed_paths=("src/feature.py",),
                file_digests=(("src/feature.py", SRC_AFTER),),
            ),
        ),
        trace=_TraceRecorder(),
        approval_verifier=_ApprovalVerifier(),
    )

    result = specialist.invoke(
        CodexInvocation(
            request_id=proposal.request_id,
            workspace="jarvis",
            operation="workspace_prepare",
            task=proposal.task,
            proposal=proposal,
            approval=approval,
        )
    )

    assert decisions == ["deny"]
    assert handler_calls == []
    assert result.changed_paths == ("src/feature.py",)


def test_mcp_adapter_denies_project_code_labeled_as_read_before_handler() -> None:
    proposal = _proposal(
        action_id="action-mcp-project-code",
        request_id="request-mcp-project-code",
    )
    envelope = CodexExecutionEnvelope(
        request_id=proposal.request_id,
        task=proposal.task,
        host="windows",
        cwd="C:/work/Jarvis-v2",
        model="gpt-5.6-sol",
        reasoning="high",
        sandbox="workspace-write",
        approval_policy="on-request",
        timeout_seconds=300,
        operation="workspace_prepare",
        allowed_paths=("src",),
        proposal_digest=proposal.digest,
        proposal_base_head=proposal.base_head,
        proposal_remote_refs=proposal.base_remote_refs,
        proposal_changes=proposal.changes,
        proposal_patch=proposal.patch,
    )
    handler_calls = []
    adapter = CodexMcpAdapter(
        client=SimpleNamespace(),
        approval_handler=lambda _envelope, _request: (
            handler_calls.append(True) or "allow"
        ),
    )

    decision = adapter._handle_approval(
        envelope,
        CodexMcpApprovalRequest(
            thread_id="thread-project-code",
            request_id="call-project-code",
            action="exec_command",
            details={
                "cwd": "C:/work/Jarvis-v2",
                "command": "python scripts/activate.py",
                "operation": "read",
            },
        ),
    )

    assert decision == "deny"
    assert handler_calls == []


@pytest.mark.parametrize(
    "argv",
    [
        ["/usr/bin/git", "status"],
        ["/usr/bin/git", "diff"],
        ["/usr/bin/git", "diff", "--ext-diff"],
        ["/usr/bin/git", "diff", "--textconv"],
        ["/usr/bin/git", "log", "-p"],
        ["/usr/bin/git", "show"],
    ],
)
def test_mcp_adapter_denies_git_commands_before_operator_handler(
    argv: list[str],
) -> None:
    proposal = _proposal()
    envelope = CodexExecutionEnvelope(
        request_id=proposal.request_id,
        task=proposal.task,
        host="ubuntu",
        cwd="/work/Jarvis-v2",
        model="gpt-5.6-sol",
        reasoning="high",
        sandbox="workspace-write",
        approval_policy="on-request",
        timeout_seconds=300,
        operation="workspace_prepare",
        allowed_paths=("src",),
        proposal_digest=proposal.digest,
        proposal_base_head=proposal.base_head,
        proposal_remote_refs=proposal.base_remote_refs,
        proposal_changes=proposal.changes,
        proposal_patch=proposal.patch,
    )
    handler_calls = []
    adapter = CodexMcpAdapter(
        client=SimpleNamespace(),
        approval_handler=lambda _envelope, request: (
            handler_calls.append(request.details) or "allow"
        ),
    )

    decision = adapter._handle_approval(
        envelope,
        CodexMcpApprovalRequest(
            thread_id="thread-git-read",
            request_id="call-git-read",
            action="exec_command",
            details={"cwd": envelope.cwd, "argv": argv},
        ),
    )

    assert decision == "deny"
    assert handler_calls == []


def test_mcp_adapter_allows_only_structured_authoritative_safe_read() -> None:
    proposal = _proposal()
    envelope = CodexExecutionEnvelope(
        request_id=proposal.request_id,
        task=proposal.task,
        host="ubuntu",
        cwd="/work/Jarvis-v2",
        model="gpt-5.6-sol",
        reasoning="high",
        sandbox="workspace-write",
        approval_policy="on-request",
        timeout_seconds=300,
        operation="workspace_prepare",
        allowed_paths=("src",),
        proposal_digest=proposal.digest,
        proposal_base_head=proposal.base_head,
        proposal_remote_refs=proposal.base_remote_refs,
        proposal_changes=proposal.changes,
        proposal_patch=proposal.patch,
    )
    handler_calls = []
    adapter = CodexMcpAdapter(
        client=SimpleNamespace(),
        approval_handler=lambda _envelope, request: (
            handler_calls.append(request.details) or "allow"
        ),
    )
    details = {
        "cwd": "/work/Jarvis-v2",
        "argv": ["/usr/bin/ls", "-la"],
    }

    decision = adapter._handle_approval(
        envelope,
        CodexMcpApprovalRequest(
            thread_id="thread-safe-read",
            request_id="call-safe-read",
            action="exec_command",
            details=details,
        ),
    )

    assert decision == "allow"
    assert handler_calls == [details]

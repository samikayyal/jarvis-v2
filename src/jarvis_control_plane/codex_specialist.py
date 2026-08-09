"""Closed, non-authoritative Codex specialist boundary.

Jarvis owns every security-relevant execution setting.  Callers select only an
allowlisted workspace, a closed operation, and task text.  Workspace mutation
additionally carries the exact frozen proposal and approval that authorized it.
Codex output is treated as a claim until an independent workspace inspector
verifies the resulting state.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from time import monotonic
from typing import Literal, Protocol

CodexHost = Literal["ubuntu", "windows"]
CodexOperation = Literal["inspect", "review", "workspace_prepare"]
CodexSandbox = Literal["read-only", "workspace-write"]
CodexApprovalPolicy = Literal["on-request"]
CodexApprovalDecision = Literal["allow", "deny"]
CodexMcpApprovalAction = Literal["apply_patch", "exec_command"]
CodexStatus = Literal["completed", "incomplete", "failed"]

_READ_ONLY_OPERATIONS = frozenset({"inspect", "review"})
_OPERATIONS = _READ_ONLY_OPERATIONS | {"workspace_prepare"}
_FORBIDDEN_EVENTS = frozenset(
    {
        "approval_bypass",
        "danger_full_access",
        "history_rewrite",
        "trust_critical_activation",
    }
)
_MAX_TIMEOUT_SECONDS = 15 * 60
_MAX_TASK_CHARS = 8_000
_MAX_SUMMARY_CHARS = 8_000
_MAX_RESULT_ITEMS = 128


class CodexSpecialistError(RuntimeError):
    """Base error for the bounded specialist boundary."""


class CodexPolicyError(CodexSpecialistError):
    """A request could not be frozen within Jarvis policy."""


class CodexTimeoutError(CodexSpecialistError):
    """The specialist did not return before its frozen deadline."""


class CodexVerificationError(CodexSpecialistError):
    """Independent evidence did not support accepting the specialist result."""


def _canonical_text(value: str, name: str, *, max_chars: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-blank")
    if value != value.strip():
        raise ValueError(f"{name} must be canonical")
    if len(value) > max_chars:
        raise ValueError(f"{name} is too long")
    return value


def _canonical_path(value: str, name: str) -> str:
    value = _canonical_text(value, name, max_chars=1_024).replace("\\", "/")
    if "//" in value or any(part in {"", ".", ".."} for part in value.split("/")):
        # A leading slash and a Windows drive separator are valid canonical roots.
        parts = value.split("/")
        invalid = [
            part
            for index, part in enumerate(parts)
            if part in {".", ".."} or (part == "" and index not in {0})
        ]
        if invalid or "//" in value:
            raise ValueError(f"{name} must be a resolved canonical path")
    return value


def _relative_path(value: str, name: str) -> str:
    if isinstance(value, str):
        value = value.rstrip("/\\")
    value = _canonical_path(value, name)
    if value.startswith("/") or (len(value) >= 2 and value[1] == ":"):
        raise ValueError(f"{name} must be workspace-relative")
    return value


def _canonical_cwd(value: str) -> str:
    value = _canonical_path(value, "workspace cwd")
    is_posix_absolute = value.startswith("/")
    is_windows_absolute = (
        len(value) >= 3 and value[0].isalpha() and value[1] == ":" and value[2] == "/"
    )
    if not (is_posix_absolute or is_windows_absolute):
        raise ValueError("workspace cwd must be an absolute canonical path")
    return value


def _canonical_paths(values: tuple[str, ...], name: str) -> tuple[str, ...]:
    paths = tuple(_relative_path(value, name) for value in values)
    if len(set(paths)) != len(paths):
        raise ValueError(f"{name} contains duplicates")
    return tuple(sorted(paths))


def _path_is_allowed(path: str, allowed: tuple[str, ...]) -> bool:
    for prefix in allowed:
        normalized_prefix = prefix.rstrip("/")
        if path == normalized_prefix or path.startswith(f"{normalized_prefix}/"):
            return True
    return False


@dataclass(frozen=True, slots=True)
class CodexWorkspace:
    """One configured host/cwd pair available to the specialist."""

    name: str
    host: CodexHost
    cwd: str
    write_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _canonical_text(self.name, "workspace name", max_chars=64)
        if self.host not in {"ubuntu", "windows"}:
            raise ValueError("workspace host is not canonical")
        object.__setattr__(self, "cwd", _canonical_cwd(self.cwd))
        object.__setattr__(
            self,
            "write_paths",
            _canonical_paths(tuple(self.write_paths), "workspace write path"),
        )


@dataclass(frozen=True, slots=True)
class CodexSpecialistConfig:
    """Jarvis-owned immutable settings used to freeze every Codex turn."""

    workspaces: tuple[CodexWorkspace, ...]
    model: str
    reasoning: str
    timeout_seconds: float = 300
    sandbox: str = "read-only"
    write_sandbox: str = "workspace-write"
    approval_policy: str = "on-request"

    def __post_init__(self) -> None:
        workspaces = tuple(self.workspaces)
        if not workspaces or any(
            not isinstance(workspace, CodexWorkspace) for workspace in workspaces
        ):
            raise CodexPolicyError("at least one typed workspace is required")
        if len({workspace.name for workspace in workspaces}) != len(workspaces):
            raise CodexPolicyError("workspace names must be unique")
        _canonical_text(self.model, "Codex model", max_chars=128)
        _canonical_text(self.reasoning, "Codex reasoning", max_chars=32)
        if self.sandbox == "danger-full-access":
            raise CodexPolicyError("danger-full-access is forbidden for Codex")
        if self.sandbox != "read-only" or self.write_sandbox != "workspace-write":
            raise CodexPolicyError("Codex sandbox must be read-only or workspace-write")
        if self.approval_policy == "never-approve":
            raise CodexPolicyError("never-approve cannot authorize mutating Codex work")
        if self.approval_policy != "on-request":
            raise CodexPolicyError("Codex approval policy must be on-request")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 0 < self.timeout_seconds <= _MAX_TIMEOUT_SECONDS
        ):
            raise CodexPolicyError(
                f"Codex timeout must be within 0 and {_MAX_TIMEOUT_SECONDS} seconds"
            )
        object.__setattr__(self, "workspaces", workspaces)

    def workspace(self, name: str) -> CodexWorkspace:
        for workspace in self.workspaces:
            if workspace.name == name:
                return workspace
        raise CodexPolicyError("Codex workspace is not allowlisted")


def _proposal_digest(payload: dict[str, object]) -> str:
    material = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CodexWorkspaceProposal:
    """Exact frozen authority for one workspace-preparation turn."""

    action_id: str
    request_id: str
    workspace: str
    task: str
    allowed_paths: tuple[str, ...]
    digest: str

    def __post_init__(self) -> None:
        for name in ("action_id", "request_id", "workspace"):
            _canonical_text(getattr(self, name), name, max_chars=128)
        _canonical_text(self.task, "Codex task", max_chars=_MAX_TASK_CHARS)
        paths = _canonical_paths(tuple(self.allowed_paths), "approved path")
        if not paths:
            raise ValueError("workspace proposal requires an approved path")
        object.__setattr__(self, "allowed_paths", paths)
        if self.digest != _proposal_digest(self._payload()):
            raise ValueError("workspace proposal digest does not match its payload")

    def _payload(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "request_id": self.request_id,
            "workspace": self.workspace,
            "task": self.task,
            "allowed_paths": list(self.allowed_paths),
        }

    @classmethod
    def create(
        cls,
        *,
        action_id: str,
        request_id: str,
        workspace: str,
        task: str,
        allowed_paths: tuple[str, ...],
    ) -> CodexWorkspaceProposal:
        paths = _canonical_paths(tuple(allowed_paths), "approved path")
        payload = {
            "action_id": action_id,
            "request_id": request_id,
            "workspace": workspace,
            "task": task,
            "allowed_paths": list(paths),
        }
        return cls(
            digest=_proposal_digest(payload),
            allowed_paths=paths,
            action_id=action_id,
            request_id=request_id,
            workspace=workspace,
            task=task,
        )


@dataclass(frozen=True, slots=True)
class CodexWorkspaceApproval:
    """Deterministic approval proof bound to one exact proposal digest."""

    action_id: str
    request_id: str
    proposal_digest: str

    def __post_init__(self) -> None:
        for name in ("action_id", "request_id", "proposal_digest"):
            _canonical_text(getattr(self, name), name, max_chars=128)


@dataclass(frozen=True, slots=True)
class CodexInvocation:
    """Narrow caller input; execution authority is deliberately absent."""

    request_id: str
    workspace: str
    operation: CodexOperation
    task: str
    proposal: CodexWorkspaceProposal | None = None
    approval: CodexWorkspaceApproval | None = None

    def __post_init__(self) -> None:
        _canonical_text(self.request_id, "request_id", max_chars=128)
        _canonical_text(self.workspace, "workspace", max_chars=64)
        if self.operation not in _OPERATIONS:
            raise ValueError("Codex operation is outside the closed operation set")
        _canonical_text(self.task, "Codex task", max_chars=_MAX_TASK_CHARS)


@dataclass(frozen=True, slots=True)
class CodexExecutionEnvelope:
    """Complete immutable adapter request frozen by Jarvis policy."""

    request_id: str
    task: str
    host: CodexHost
    cwd: str
    model: str
    reasoning: str
    sandbox: CodexSandbox
    approval_policy: CodexApprovalPolicy
    timeout_seconds: float
    operation: CodexOperation
    allowed_paths: tuple[str, ...]
    proposal_digest: str | None


@dataclass(frozen=True, slots=True)
class CodexMcpApprovalRequest:
    """One server-to-client Codex approval request.

    The managed MCP client translates the protocol's conversation/thread ID,
    request/call ID, and action-specific payload into this typed value before
    invoking Jarvis's approval callback.  ``details`` retains the exact
    command or file-change context needed for the callback to compare the
    request with the frozen Jarvis envelope and approval.
    """

    thread_id: str
    request_id: str
    action: CodexMcpApprovalAction
    details: Mapping[str, object]

    def __post_init__(self) -> None:
        _canonical_text(self.thread_id, "Codex approval thread_id", max_chars=256)
        _canonical_text(self.request_id, "Codex approval request_id", max_chars=256)
        if self.action not in {"apply_patch", "exec_command"}:
            raise ValueError("Codex approval action is not canonical")
        if not isinstance(self.details, Mapping):
            raise TypeError("Codex approval details must be a mapping")
        object.__setattr__(self, "details", dict(self.details))


CodexMcpApprovalCallback = Callable[[CodexMcpApprovalRequest], CodexApprovalDecision]
CodexMcpApprovalHandler = Callable[
    [CodexExecutionEnvelope, CodexMcpApprovalRequest], CodexApprovalDecision
]


@dataclass(frozen=True, slots=True)
class CodexAdapterResult:
    """Bounded claims returned by a replaceable Codex adapter."""

    status: CodexStatus
    summary: str
    changed_paths: tuple[str, ...]
    test_evidence: tuple[str, ...]
    unresolved_questions: tuple[str, ...]
    thread_id: str

    def __post_init__(self) -> None:
        if self.status not in {"completed", "incomplete", "failed"}:
            raise ValueError("Codex status is not canonical")
        _canonical_text(self.summary, "Codex summary", max_chars=_MAX_SUMMARY_CHARS)
        _canonical_text(self.thread_id, "Codex thread_id", max_chars=256)
        object.__setattr__(
            self,
            "changed_paths",
            _canonical_paths(tuple(self.changed_paths), "reported changed path"),
        )
        for field_name in ("test_evidence", "unresolved_questions"):
            values = tuple(getattr(self, field_name))
            if len(values) > _MAX_RESULT_ITEMS:
                raise ValueError(f"Codex {field_name} is too large")
            for value in values:
                _canonical_text(value, f"Codex {field_name} item", max_chars=1_024)
            object.__setattr__(self, field_name, values)


@dataclass(frozen=True, slots=True)
class CodexWorkspaceSnapshot:
    """Evidence captured by a Jarvis-controlled inspector, not by Codex."""

    head: str
    remote_refs: tuple[tuple[str, str], ...]
    changed_paths: tuple[str, ...]
    forbidden_events: tuple[str, ...] = ()
    test_evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _canonical_text(self.head, "workspace head", max_chars=256)
        refs = tuple(sorted(self.remote_refs))
        if len({name for name, _value in refs}) != len(refs):
            raise ValueError("workspace remote refs contain duplicates")
        for name, value in refs:
            _canonical_text(name, "remote ref", max_chars=256)
            _canonical_text(value, "remote ref value", max_chars=256)
        events = tuple(sorted(set(self.forbidden_events)))
        unknown = set(events) - _FORBIDDEN_EVENTS
        if unknown:
            raise ValueError("workspace snapshot contains an unknown security event")
        object.__setattr__(self, "remote_refs", refs)
        object.__setattr__(
            self,
            "changed_paths",
            _canonical_paths(tuple(self.changed_paths), "observed changed path"),
        )
        object.__setattr__(self, "forbidden_events", events)
        evidence = tuple(self.test_evidence)
        if len(evidence) > _MAX_RESULT_ITEMS:
            raise ValueError("workspace test evidence is too large")
        for value in evidence:
            _canonical_text(value, "workspace test evidence", max_chars=1_024)
        object.__setattr__(self, "test_evidence", evidence)


@dataclass(frozen=True, slots=True)
class CodexSpecialistResult:
    """Jarvis-owned result accepted only after independent verification."""

    status: CodexStatus
    summary: str
    changed_paths: tuple[str, ...]
    test_evidence: tuple[str, ...]
    unresolved_questions: tuple[str, ...]
    thread_id: str
    verified: Literal[True] = True


class CodexAdapter(Protocol):
    def invoke(
        self, envelope: CodexExecutionEnvelope, *, deadline: float
    ) -> CodexAdapterResult: ...

    def interrupt(self, request_id: str) -> bool: ...


class CodexWorkspaceInspector(Protocol):
    def snapshot(self, workspace: CodexWorkspace) -> CodexWorkspaceSnapshot: ...


class CodexApprovalVerifier(Protocol):
    """Authoritative approval-state check outside caller-controlled input."""

    def is_approved(
        self,
        proposal: CodexWorkspaceProposal,
        approval: CodexWorkspaceApproval,
    ) -> bool: ...


class CodexMcpClient(Protocol):
    """Managed-client surface for the official Codex MCP server.

    A concrete client must dispatch every server-initiated approval request to
    ``approval_callback`` and send its returned decision back over JSON-RPC.
    It must not hide, auto-allow, or silently drop an approval request.
    """

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, object],
        *,
        deadline: float,
        approval_callback: CodexMcpApprovalCallback,
    ) -> Mapping[str, object]: ...

    def interrupt(self, request_id: str) -> bool: ...


class CodexMcpAdapter:
    """Map a frozen envelope to the official ``codex`` MCP tool."""

    def __init__(
        self,
        *,
        client: CodexMcpClient,
        approval_handler: CodexMcpApprovalHandler,
    ) -> None:
        self._client = client
        if not callable(approval_handler):
            raise TypeError("Codex MCP approval_handler must be callable")
        self._approval_handler = approval_handler

    def invoke(
        self, envelope: CodexExecutionEnvelope, *, deadline: float
    ) -> CodexAdapterResult:
        if not isinstance(envelope, CodexExecutionEnvelope):
            raise CodexPolicyError("Codex MCP adapter requires a frozen envelope")
        arguments: dict[str, object] = {
            "prompt": self._prompt(envelope),
            "approval-policy": envelope.approval_policy,
            "cwd": envelope.cwd,
            "model": envelope.model,
            "sandbox": envelope.sandbox,
            "config": {"model_reasoning_effort": envelope.reasoning},
            "developer-instructions": self._developer_instructions(envelope),
        }
        raw_result = self._client.call_tool(
            "codex",
            arguments,
            deadline=deadline,
            approval_callback=lambda request: self._handle_approval(envelope, request),
        )
        return self._parse_result(raw_result)

    def interrupt(self, request_id: str) -> bool:
        return self._client.interrupt(request_id)

    def _handle_approval(
        self,
        envelope: CodexExecutionEnvelope,
        request: CodexMcpApprovalRequest,
    ) -> CodexApprovalDecision:
        if not isinstance(request, CodexMcpApprovalRequest):
            raise CodexVerificationError(
                "Codex MCP approval callback received an untyped request"
            )
        decision = self._approval_handler(envelope, request)
        if decision not in {"allow", "deny"}:
            raise CodexVerificationError(
                "Codex MCP approval callback returned an invalid decision"
            )
        if envelope.operation in _READ_ONLY_OPERATIONS:
            return "deny"
        return decision

    @staticmethod
    def _developer_instructions(envelope: CodexExecutionEnvelope) -> str:
        common = (
            "You are a bounded, non-authoritative Codex specialist. Do not push, "
            "rewrite Git history, bypass or hide approvals, use danger-full-access, "
            "or activate trust-critical Jarvis components. Do not claim success from "
            "prose; report the workspace paths you actually changed and the tests you "
            "actually observed. Return only the requested JSON object."
        )
        if envelope.operation != "workspace_prepare":
            return f"{common} This is read-only work; do not change any workspace path."
        paths = ", ".join(envelope.allowed_paths)
        return (
            f"{common} Workspace preparation is limited to these approved paths: "
            f"{paths}. The approval digest is {envelope.proposal_digest}."
        )

    @staticmethod
    def _prompt(envelope: CodexExecutionEnvelope) -> str:
        return (
            f"Allowed operation: {envelope.operation}.\n"
            f"Task: {envelope.task}\n\n"
            "Return exactly one JSON object with these keys: status (completed, "
            "incomplete, or failed), summary (string), changed_paths (array of "
            "workspace-relative strings), test_evidence (array of strings), and "
            "unresolved_questions (array of strings). Do not wrap the JSON in markdown."
        )

    @staticmethod
    def _parse_result(raw_result: Mapping[str, object]) -> CodexAdapterResult:
        try:
            structured = raw_result["structuredContent"]
            if not isinstance(structured, Mapping):
                raise TypeError
            thread_id = structured["threadId"]
            content = structured["content"]
            if not isinstance(thread_id, str) or not isinstance(content, str):
                raise TypeError
            payload = json.loads(content)
            if not isinstance(payload, dict) or set(payload) != {
                "status",
                "summary",
                "changed_paths",
                "test_evidence",
                "unresolved_questions",
            }:
                raise TypeError
            for name in ("changed_paths", "test_evidence", "unresolved_questions"):
                values = payload[name]
                if not isinstance(values, list) or any(
                    not isinstance(value, str) for value in values
                ):
                    raise TypeError
            return CodexAdapterResult(
                status=payload["status"],
                summary=payload["summary"],
                changed_paths=tuple(payload["changed_paths"]),
                test_evidence=tuple(payload["test_evidence"]),
                unresolved_questions=tuple(payload["unresolved_questions"]),
                thread_id=thread_id,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CodexVerificationError(
                "Codex MCP returned an invalid structured result"
            ) from exc


class CodexSpecialist:
    """Freeze, run, and independently verify one bounded Codex turn."""

    def __init__(
        self,
        *,
        config: CodexSpecialistConfig,
        adapter: CodexAdapter,
        inspector: CodexWorkspaceInspector,
        approval_verifier: CodexApprovalVerifier | None = None,
    ) -> None:
        self._config = config
        self._adapter = adapter
        self._inspector = inspector
        self._approval_verifier = approval_verifier

    @property
    def timeout_seconds(self) -> float:
        """The specialist-owned hard deadline for one Codex turn."""

        return float(self._config.timeout_seconds)

    def invoke(self, invocation: CodexInvocation) -> CodexSpecialistResult:
        workspace = self._config.workspace(invocation.workspace)
        envelope = self._freeze(invocation, workspace)
        before = self._inspector.snapshot(workspace)
        deadline = monotonic() + envelope.timeout_seconds
        executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="codex-specialist"
        )
        future = executor.submit(self._adapter.invoke, envelope, deadline=deadline)
        try:
            adapter_result = future.result(timeout=envelope.timeout_seconds)
        except FutureTimeout as exc:
            interrupted = self._adapter.interrupt(invocation.request_id)
            future.cancel()
            if interrupted is not True:
                raise CodexVerificationError(
                    "Codex interrupt could not be independently confirmed"
                ) from exc
            after = self._inspector.snapshot(workspace)
            self._verify_transition(envelope, before, after)
            raise CodexTimeoutError(
                "Codex specialist exceeded its frozen deadline"
            ) from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        if not isinstance(adapter_result, CodexAdapterResult):
            raise CodexVerificationError("Codex adapter returned an untyped result")
        after = self._inspector.snapshot(workspace)
        changed_paths, test_evidence = self._verify(
            envelope, adapter_result, before, after
        )
        return CodexSpecialistResult(
            status=adapter_result.status,
            summary=adapter_result.summary,
            changed_paths=changed_paths,
            test_evidence=test_evidence,
            unresolved_questions=adapter_result.unresolved_questions,
            thread_id=adapter_result.thread_id,
        )

    def _freeze(
        self, invocation: CodexInvocation, workspace: CodexWorkspace
    ) -> CodexExecutionEnvelope:
        proposal_digest: str | None = None
        allowed_paths: tuple[str, ...] = ()
        if invocation.operation in _READ_ONLY_OPERATIONS:
            if invocation.proposal is not None or invocation.approval is not None:
                raise CodexPolicyError("read-only Codex work cannot consume approval")
            sandbox: CodexSandbox = "read-only"
        else:
            proposal = invocation.proposal
            approval = invocation.approval
            if proposal is None or approval is None:
                raise CodexPolicyError(
                    "workspace preparation requires an approved exact proposal"
                )
            if (
                proposal.request_id != invocation.request_id
                or proposal.workspace != invocation.workspace
                or proposal.task != invocation.task
                or approval.action_id != proposal.action_id
                or approval.request_id != proposal.request_id
                or approval.proposal_digest != proposal.digest
            ):
                raise CodexPolicyError(
                    "workspace approval does not match the exact proposal"
                )
            if (
                self._approval_verifier is None
                or not self._approval_verifier.is_approved(proposal, approval)
            ):
                raise CodexPolicyError(
                    "workspace preparation lacks authoritative approval"
                )
            if any(
                not _path_is_allowed(path.rstrip("/"), workspace.write_paths)
                for path in proposal.allowed_paths
            ):
                raise CodexPolicyError(
                    "proposal contains a path outside the workspace allowlist"
                )
            allowed_paths = proposal.allowed_paths
            proposal_digest = proposal.digest
            sandbox = "workspace-write"
        return CodexExecutionEnvelope(
            request_id=invocation.request_id,
            task=invocation.task,
            host=workspace.host,
            cwd=workspace.cwd,
            model=self._config.model,
            reasoning=self._config.reasoning,
            sandbox=sandbox,
            approval_policy="on-request",
            timeout_seconds=self._config.timeout_seconds,
            operation=invocation.operation,
            allowed_paths=allowed_paths,
            proposal_digest=proposal_digest,
        )

    @staticmethod
    def _verify(
        envelope: CodexExecutionEnvelope,
        result: CodexAdapterResult,
        before: CodexWorkspaceSnapshot,
        after: CodexWorkspaceSnapshot,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        changed_paths = CodexSpecialist._verify_transition(envelope, before, after)
        if result.changed_paths != changed_paths:
            raise CodexVerificationError(
                "Codex changed-path claims do not match independent evidence"
            )
        if result.test_evidence != after.test_evidence:
            raise CodexVerificationError(
                "Codex test-evidence claims do not match independent evidence"
            )
        return changed_paths, after.test_evidence

    @staticmethod
    def _verify_transition(
        envelope: CodexExecutionEnvelope,
        before: CodexWorkspaceSnapshot,
        after: CodexWorkspaceSnapshot,
    ) -> tuple[str, ...]:
        if dict(before.remote_refs) != dict(after.remote_refs):
            raise CodexVerificationError("independent verification detected a push")
        messages = {
            "history_rewrite": "independent verification detected history rewriting",
            "approval_bypass": "independent verification detected an approval bypass",
            "trust_critical_activation": (
                "independent verification detected trust-critical activation"
            ),
            "danger_full_access": (
                "independent verification detected danger-full-access"
            ),
        }
        for event in after.forbidden_events:
            raise CodexVerificationError(messages[event])
        changed_paths = after.changed_paths
        if envelope.operation in _READ_ONLY_OPERATIONS and (
            changed_paths or before.head != after.head
        ):
            raise CodexVerificationError(
                "independent verification detected mutation during read-only work"
            )
        if envelope.operation == "workspace_prepare" and any(
            not _path_is_allowed(path, envelope.allowed_paths) for path in changed_paths
        ):
            raise CodexVerificationError(
                "independent verification found changes outside the approved paths"
            )
        return changed_paths

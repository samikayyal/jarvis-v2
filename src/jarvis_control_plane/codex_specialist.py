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
import shlex
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from itertools import pairwise
from time import monotonic
from typing import Literal, Protocol

from .terminal_policy import (
    TerminalAction,
    TerminalDisposition,
    authorize_terminal_action,
)

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
_MAX_PROPOSAL_PATCH_CHARS = 256_000
_CONTENT_DIGEST_LENGTH = 64
_MAX_INTERRUPT_GRACE_SECONDS = 5.0


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


def _canonical_digest(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != _CONTENT_DIGEST_LENGTH
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _canonical_patch(value: str, name: str = "workspace proposal patch") -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > _MAX_PROPOSAL_PATCH_CHARS
        or "\x00" in value
    ):
        raise ValueError(f"{name} must be a bounded non-empty patch")
    return value


def _patch_file_marker(value: str, prefix: str) -> str | None:
    try:
        tokens = shlex.split(value, posix=True)
    except ValueError as exc:
        raise ValueError("workspace proposal patch has an invalid file marker") from exc
    if len(tokens) != 1:
        raise ValueError("workspace proposal patch file markers must be canonical")
    marker = tokens[0]
    if marker == "/dev/null":
        return None
    if not marker.startswith(prefix):
        raise ValueError("workspace proposal patch has an invalid file prefix")
    return _relative_path(marker[len(prefix) :], "workspace proposal patch path")


def _canonical_patch_changes(
    patch: str,
) -> tuple[tuple[str, bool, bool], ...]:
    """Return exact text-file transitions from one strict canonical Git patch."""

    patch = _canonical_patch(patch)
    if not patch.endswith("\n"):
        raise ValueError("workspace proposal patch must end with a newline")
    lines = patch.splitlines()
    starts = [
        index for index, line in enumerate(lines) if line.startswith("diff --git ")
    ]
    if not starts or any(line for line in lines[: starts[0]]):
        raise ValueError("workspace proposal patch requires canonical Git diff headers")
    starts.append(len(lines))
    transitions: list[tuple[str, bool, bool]] = []
    for start, end in pairwise(starts):
        segment = lines[start:end]
        try:
            header = shlex.split(segment[0][len("diff --git ") :], posix=True)
        except ValueError as exc:
            raise ValueError(
                "workspace proposal patch has an invalid diff header"
            ) from exc
        if (
            len(header) != 2
            or not header[0].startswith("a/")
            or not header[1].startswith("b/")
        ):
            raise ValueError("workspace proposal patch has an invalid diff header")
        old_path = _relative_path(header[0][2:], "workspace proposal patch path")
        new_path = _relative_path(header[1][2:], "workspace proposal patch path")
        if old_path != new_path:
            raise ValueError("workspace proposal patch cannot rename or copy files")
        if any(
            line.startswith(
                (
                    "rename from ",
                    "rename to ",
                    "copy from ",
                    "copy to ",
                    "Binary files ",
                    "old mode ",
                    "new mode ",
                )
            )
            or line in {"GIT binary patch"}
            for line in segment[1:]
        ):
            raise ValueError("workspace proposal patch supports text changes only")
        try:
            hunk_index = next(
                index
                for index, line in enumerate(segment[1:], start=1)
                if line.startswith("@@ ")
            )
        except StopIteration as exc:
            raise ValueError("workspace proposal patch requires a text hunk") from exc
        old_markers = [
            line[4:] for line in segment[1:hunk_index] if line.startswith("--- ")
        ]
        new_markers = [
            line[4:] for line in segment[1:hunk_index] if line.startswith("+++ ")
        ]
        if len(old_markers) != 1 or len(new_markers) != 1:
            raise ValueError("workspace proposal patch requires exact file markers")
        marked_old = _patch_file_marker(old_markers[0], "a/")
        marked_new = _patch_file_marker(new_markers[0], "b/")
        if (marked_old is not None and marked_old != old_path) or (
            marked_new is not None and marked_new != new_path
        ):
            raise ValueError("workspace proposal patch paths are inconsistent")
        transitions.append((old_path, marked_old is not None, marked_new is not None))
    if len({path for path, _before, _after in transitions}) != len(transitions):
        raise ValueError("workspace proposal patch contains duplicate paths")
    return tuple(sorted(transitions))


def _canonical_remote_refs(
    values: tuple[tuple[str, str], ...], name: str = "remote ref"
) -> tuple[tuple[str, str], ...]:
    refs = tuple(sorted(values))
    if len({ref_name for ref_name, _value in refs}) != len(refs):
        raise ValueError(f"{name} contains duplicates")
    for ref_name, value in refs:
        _canonical_text(ref_name, name, max_chars=256)
        _canonical_text(value, f"{name} value", max_chars=256)
    return refs


def _canonical_file_digests(
    values: tuple[tuple[str, str | None], ...], name: str
) -> tuple[tuple[str, str | None], ...]:
    digests = tuple(
        (
            _relative_path(path, f"{name} path"),
            _canonical_digest(digest, f"{name} value"),
        )
        for path, digest in values
    )
    if len({path for path, _digest in digests}) != len(digests):
        raise ValueError(f"{name} contains duplicate paths")
    return tuple(sorted(digests))


def _path_is_allowed(path: str, allowed: tuple[str, ...]) -> bool:
    for prefix in allowed:
        normalized_prefix = prefix.rstrip("/")
        if path == normalized_prefix or path.startswith(f"{normalized_prefix}/"):
            return True
    return False


@dataclass(frozen=True, slots=True)
class CodexWorkspaceChange:
    """One exact file-content transition authorized by a workspace proposal."""

    path: str
    before_digest: str | None
    after_digest: str | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "path", _relative_path(self.path, "workspace change path")
        )
        before = _canonical_digest(self.before_digest, "workspace before digest")
        after = _canonical_digest(self.after_digest, "workspace after digest")
        if before == after:
            raise ValueError("workspace change must alter file content or existence")
        object.__setattr__(self, "before_digest", before)
        object.__setattr__(self, "after_digest", after)

    def as_payload(self) -> dict[str, object]:
        return {
            "path": self.path,
            "before_digest": self.before_digest,
            "after_digest": self.after_digest,
        }


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
    base_head: str
    base_remote_refs: tuple[tuple[str, str], ...]
    changes: tuple[CodexWorkspaceChange, ...]
    patch: str
    digest: str

    def __post_init__(self) -> None:
        for name in ("action_id", "request_id", "workspace"):
            _canonical_text(getattr(self, name), name, max_chars=128)
        _canonical_text(self.task, "Codex task", max_chars=_MAX_TASK_CHARS)
        paths = _canonical_paths(tuple(self.allowed_paths), "approved path")
        if not paths:
            raise ValueError("workspace proposal requires an approved path")
        _canonical_text(self.base_head, "workspace proposal base head", max_chars=256)
        remote_refs = _canonical_remote_refs(
            tuple(self.base_remote_refs), "workspace proposal remote ref"
        )
        changes = tuple(self.changes)
        if not changes or any(
            not isinstance(change, CodexWorkspaceChange) for change in changes
        ):
            raise ValueError("workspace proposal requires exact file changes")
        if tuple(change.path for change in changes) != tuple(
            sorted(change.path for change in changes)
        ):
            raise ValueError("workspace proposal changes must be sorted canonically")
        if len({change.path for change in changes}) != len(changes):
            raise ValueError("workspace proposal changes must be unique")
        if any(not _path_is_allowed(change.path, paths) for change in changes):
            raise ValueError("workspace proposal change is outside its allowed paths")
        patch = _canonical_patch(self.patch)
        patch_changes = _canonical_patch_changes(patch)
        expected_patch_changes = tuple(
            sorted(
                (
                    change.path,
                    change.before_digest is not None,
                    change.after_digest is not None,
                )
                for change in changes
            )
        )
        if patch_changes != expected_patch_changes:
            raise ValueError(
                "workspace proposal patch paths do not match its exact file changes"
            )
        object.__setattr__(self, "allowed_paths", paths)
        object.__setattr__(self, "base_remote_refs", remote_refs)
        object.__setattr__(self, "changes", changes)
        object.__setattr__(self, "patch", patch)
        if self.digest != _proposal_digest(self._payload()):
            raise ValueError("workspace proposal digest does not match its payload")

    def _payload(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "request_id": self.request_id,
            "workspace": self.workspace,
            "task": self.task,
            "allowed_paths": list(self.allowed_paths),
            "base_head": self.base_head,
            "base_remote_refs": [list(ref) for ref in self.base_remote_refs],
            "changes": [change.as_payload() for change in self.changes],
            "patch": self.patch,
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
        base_head: str,
        base_remote_refs: tuple[tuple[str, str], ...],
        changes: tuple[CodexWorkspaceChange, ...],
        patch: str,
    ) -> CodexWorkspaceProposal:
        paths = _canonical_paths(tuple(allowed_paths), "approved path")
        remote_refs = _canonical_remote_refs(
            tuple(base_remote_refs), "workspace proposal remote ref"
        )
        normalized_changes = tuple(sorted(changes, key=lambda change: change.path))
        patch = _canonical_patch(patch)
        payload = {
            "action_id": action_id,
            "request_id": request_id,
            "workspace": workspace,
            "task": task,
            "allowed_paths": list(paths),
            "base_head": base_head,
            "base_remote_refs": [list(ref) for ref in remote_refs],
            "changes": [change.as_payload() for change in normalized_changes],
            "patch": patch,
        }
        return cls(
            digest=_proposal_digest(payload),
            allowed_paths=paths,
            action_id=action_id,
            request_id=request_id,
            workspace=workspace,
            task=task,
            base_head=base_head,
            base_remote_refs=remote_refs,
            changes=normalized_changes,
            patch=patch,
        )


@dataclass(frozen=True, slots=True)
class CodexWorkspaceApproval:
    """Deterministic approval proof bound to one exact proposal digest."""

    action_id: str
    request_id: str
    proposal_digest: str

    def __post_init__(self) -> None:
        for name in ("action_id", "request_id"):
            _canonical_text(getattr(self, name), name, max_chars=128)
        _canonical_digest(self.proposal_digest, "proposal digest")


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
    proposal_base_head: str | None = None
    proposal_remote_refs: tuple[tuple[str, str], ...] = ()
    proposal_changes: tuple[CodexWorkspaceChange, ...] = ()
    proposal_patch: str | None = None


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
class CodexInterruption:
    """Terminal adapter result issued only after complete process-scope quiescence."""

    result: CodexAdapterResult
    quiescent: Literal[True] = True

    def __post_init__(self) -> None:
        if (
            not isinstance(self.result, CodexAdapterResult)
            or self.quiescent is not True
        ):
            raise TypeError("Codex interruption requires a quiescent typed result")


@dataclass(frozen=True, slots=True)
class CodexWorkspaceSnapshot:
    """Evidence captured by a Jarvis-controlled inspector, not by Codex."""

    head: str
    remote_refs: tuple[tuple[str, str], ...]
    changed_paths: tuple[str, ...]
    file_digests: tuple[tuple[str, str | None], ...] = ()
    forbidden_events: tuple[str, ...] = ()
    test_evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _canonical_text(self.head, "workspace head", max_chars=256)
        refs = _canonical_remote_refs(tuple(self.remote_refs))
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
        object.__setattr__(
            self,
            "file_digests",
            _canonical_file_digests(tuple(self.file_digests), "observed file digest"),
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

    def interrupt(self, request_id: str, *, deadline: float) -> CodexInterruption:
        """Stop the complete turn scope before deadline and return terminal evidence."""

        ...


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

    def interrupt(self, request_id: str, *, deadline: float) -> CodexInterruption: ...


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

    def interrupt(self, request_id: str, *, deadline: float) -> CodexInterruption:
        interruption = self._client.interrupt(request_id, deadline=deadline)
        if not isinstance(interruption, CodexInterruption):
            raise CodexVerificationError(
                "Codex MCP interrupt returned no typed quiescence evidence"
            )
        return interruption

    def _handle_approval(
        self,
        envelope: CodexExecutionEnvelope,
        request: CodexMcpApprovalRequest,
    ) -> CodexApprovalDecision:
        if not isinstance(request, CodexMcpApprovalRequest):
            raise CodexVerificationError(
                "Codex MCP approval callback received an untyped request"
            )
        if envelope.operation in _READ_ONLY_OPERATIONS:
            decision = self._approval_handler(envelope, request)
            if decision not in {"allow", "deny"}:
                raise CodexVerificationError(
                    "Codex MCP approval callback returned an invalid decision"
                )
            return "deny"
        if request.action == "apply_patch":
            if not self._matches_exact_patch(envelope, request.details):
                return "deny"
        elif request.action == "exec_command":
            if not self._is_safe_read_command(envelope, request.details):
                return "deny"
        else:  # pragma: no cover - CodexMcpApprovalRequest validates the action.
            return "deny"
        decision = self._approval_handler(envelope, request)
        if decision not in {"allow", "deny"}:
            raise CodexVerificationError(
                "Codex MCP approval callback returned an invalid decision"
            )
        return decision

    @staticmethod
    def _request_cwd(details: Mapping[str, object]) -> str | None:
        value = details.get("cwd")
        if not isinstance(value, str):
            return None
        try:
            return _canonical_cwd(value)
        except ValueError:
            return None

    @staticmethod
    def _request_paths(details: Mapping[str, object]) -> tuple[str, ...] | None:
        raw_paths: object | None = None
        if "paths" in details:
            raw_paths = details["paths"]
        elif "affected_paths" in details:
            raw_paths = details["affected_paths"]
        elif "fileChanges" in details:
            raw_changes = details["fileChanges"]
            if not isinstance(raw_changes, list):
                return None
            paths: list[str] = []
            for change in raw_changes:
                if not isinstance(change, Mapping) or set(change) != {"path"}:
                    return None
                path = change.get("path")
                if not isinstance(path, str):
                    return None
                paths.append(path)
            raw_paths = paths
        if raw_paths is None:
            return ()
        if not isinstance(raw_paths, (list, tuple)) or any(
            not isinstance(path, str) for path in raw_paths
        ):
            return None
        try:
            return _canonical_paths(tuple(raw_paths), "Codex approval path")
        except ValueError:
            return None

    @classmethod
    def _matches_exact_patch(
        cls, envelope: CodexExecutionEnvelope, details: Mapping[str, object]
    ) -> bool:
        if envelope.operation != "workspace_prepare":
            return False
        if set(details) != {"cwd", "fileChanges", "patch"}:
            return False
        if envelope.proposal_patch is None or not envelope.proposal_changes:
            return False
        if cls._request_cwd(details) != envelope.cwd:
            return False
        patch = details.get("patch")
        if not isinstance(patch, str) or patch != envelope.proposal_patch:
            return False
        paths = cls._request_paths(details)
        expected_paths = tuple(change.path for change in envelope.proposal_changes)
        return paths == expected_paths

    @classmethod
    def _is_safe_read_command(
        cls, envelope: CodexExecutionEnvelope, details: Mapping[str, object]
    ) -> bool:
        if envelope.operation != "workspace_prepare":
            return False
        if set(details) != {"argv", "cwd"}:
            return False
        if cls._request_cwd(details) != envelope.cwd:
            return False
        argv = details.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(value, str) or not value for value in argv)
        ):
            return False
        try:
            action = TerminalAction(
                host=envelope.host,
                executable=argv[0],
                arguments=tuple(argv[1:]),
                cwd=envelope.cwd,
            )
        except ValueError:
            return False
        return (
            authorize_terminal_action(action).disposition
            is TerminalDisposition.SAFE_READ
        )

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
            f"{paths}. Apply only the exact approved patch, whose digest is "
            f"{envelope.proposal_digest}; command approvals are read-only."
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
        interrupt_grace = min(
            _MAX_INTERRUPT_GRACE_SECONDS, envelope.timeout_seconds / 2
        )
        execution_timeout = envelope.timeout_seconds - interrupt_grace
        executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="codex-specialist"
        )
        future = executor.submit(self._adapter.invoke, envelope, deadline=deadline)
        try:
            adapter_result = future.result(timeout=execution_timeout)
        except FutureTimeout as exc:
            interruption = self._adapter.interrupt(
                invocation.request_id, deadline=deadline
            )
            if not isinstance(interruption, CodexInterruption):
                raise CodexVerificationError(
                    "Codex interrupt provided no typed quiescence evidence"
                ) from exc
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise CodexVerificationError(
                    "Codex process scope did not establish quiescence before its deadline"
                ) from exc
            try:
                worker_result = future.result(timeout=remaining)
            except FutureTimeout as error:
                raise CodexVerificationError(
                    "Codex process scope did not establish quiescence before its deadline"
                ) from error
            except Exception as error:
                raise CodexVerificationError(
                    "Codex interrupt stopped without a typed result"
                ) from error
            if worker_result != interruption.result:
                raise CodexVerificationError(
                    "Codex interrupt result did not match the quiescent worker result"
                ) from exc
            after = self._inspector.snapshot(workspace)
            changed_paths, test_evidence = self._verify(
                envelope, worker_result, before, after
            )
            unresolved_questions = tuple(
                dict.fromkeys(
                    (
                        *worker_result.unresolved_questions,
                        "Codex specialist did not complete before its frozen deadline.",
                    )
                )
            )
            return CodexSpecialistResult(
                status="incomplete",
                summary="Codex specialist did not complete before its frozen deadline.",
                changed_paths=changed_paths,
                test_evidence=test_evidence,
                unresolved_questions=unresolved_questions[:_MAX_RESULT_ITEMS],
                thread_id=worker_result.thread_id,
            )
        finally:
            # A verification failure must not release request/workspace ownership
            # while the adapter can still mutate it. A conforming adapter makes
            # this wait bounded by establishing process-scope quiescence before
            # its deadline; a violating adapter fails closed until it is terminal.
            executor.shutdown(wait=True, cancel_futures=True)
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
        proposal: CodexWorkspaceProposal | None = None
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
            proposal_base_head=proposal.base_head if proposal is not None else None,
            proposal_remote_refs=(
                proposal.base_remote_refs if proposal is not None else ()
            ),
            proposal_changes=proposal.changes if proposal is not None else (),
            proposal_patch=proposal.patch if proposal is not None else None,
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
        if envelope.operation == "workspace_prepare":
            expected_changes = envelope.proposal_changes
            expected_paths = tuple(change.path for change in expected_changes)
            if (
                envelope.proposal_base_head is None
                or dict(before.remote_refs) != dict(envelope.proposal_remote_refs)
                or before.head != envelope.proposal_base_head
            ):
                raise CodexVerificationError(
                    "workspace base changed before the approved proposal executed"
                )
            if before.head != after.head:
                raise CodexVerificationError(
                    "independent verification detected a workspace commit"
                )
            if changed_paths != expected_paths:
                raise CodexVerificationError(
                    "independent verification did not match the exact approved paths"
                )
            before_digests = dict(before.file_digests)
            after_digests = dict(after.file_digests)
            if set(before_digests) != set(expected_paths) or set(after_digests) != set(
                expected_paths
            ):
                raise CodexVerificationError(
                    "independent verification lacks exact file-content evidence"
                )
            for change in expected_changes:
                if before_digests[change.path] != change.before_digest:
                    raise CodexVerificationError(
                        "workspace base content changed before the approved proposal"
                    )
                if after_digests[change.path] != change.after_digest:
                    raise CodexVerificationError(
                        "independent verification found content outside the approved transition"
                    )
            if any(
                not _path_is_allowed(path, envelope.allowed_paths)
                for path in changed_paths
            ):
                raise CodexVerificationError(
                    "independent verification found changes outside the approved paths"
                )
        return changed_paths

"""Exact, approval-gated Markdown writes to the dedicated knowledge vault."""

from __future__ import annotations

import difflib
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from threading import RLock
from time import monotonic
from typing import Literal, NoReturn, Protocol

from .knowledge_vault import (
    VaultRemoteUnavailable,
    VaultRepositoryConflict,
)
from .knowledge_vault_common import _EXCLUDED_TOP_LEVEL_DIRECTORIES, _remaining_seconds
from .models import FrozenActionProposal
from .ports import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ActionDispatcherError,
    ActionDispatchHandle,
)

KNOWLEDGE_VAULT_WRITE_KIND = "knowledge_vault_write"
KNOWLEDGE_VAULT_WRITE_SCHEMA = "knowledge_vault_write_v1"
DEFAULT_VAULT_COMMIT_NAME = "Jarvis"
DEFAULT_VAULT_COMMIT_EMAIL = "jarvis@samikayyal.com"
DEFAULT_VAULT_COMMIT_SUBJECT = "jarvis: update knowledge vault"

_MAX_WRITE_PATHS = 16
_MAX_WRITE_CONTENT_BYTES = 64 * 1024
_MAX_WRITE_DIFF_CHARS = 256 * 1024
_MAX_COMMIT_SUBJECT_CHARS = 200
_MAX_COMMIT_BODY_CHARS = 2_000
_COMMIT_HASH = r"^[0-9a-fA-F]{40,64}$"


class VaultWriteError(Exception):
    """A vault write was invalid, unavailable, or outside its boundary."""


class VaultWriteRemoteUnavailable(VaultWriteError):
    """The remote could not be reached while a write was being prepared."""


class VaultWriteConflict(VaultWriteError):
    """The exact approved write no longer matches the repository state."""


class VaultWriteRepositoryError(VaultWriteError):
    """The repository edge could not safely complete a write operation."""


class VaultWriteRepository(Protocol):
    """The narrow Git edge used by the write connector."""

    def is_clean(self, root: Path, *, deadline: float | None = None) -> bool: ...

    def synchronize(
        self, root: Path, *, now: datetime, deadline: float | None = None
    ) -> datetime: ...

    def current_commit(self, root: Path, *, deadline: float | None = None) -> str: ...

    def fetch_remote_commit(
        self, root: Path, *, deadline: float | None = None
    ) -> str: ...

    def stage(
        self, root: Path, paths: Sequence[str], *, deadline: float | None = None
    ) -> None: ...

    def staged_diff(
        self, root: Path, paths: Sequence[str], *, deadline: float | None = None
    ) -> str | None: ...

    def commit(
        self,
        root: Path,
        *,
        author_name: str,
        author_email: str,
        subject: str,
        body: str,
        deadline: float | None = None,
    ) -> str: ...

    def push(
        self,
        root: Path,
        *,
        expected_base: str,
        commit_id: str,
        deadline: float | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class VaultCommitIdentity:
    """The configured, repository-local identity used for vault commits."""

    name: str = DEFAULT_VAULT_COMMIT_NAME
    email: str = DEFAULT_VAULT_COMMIT_EMAIL

    def __post_init__(self) -> None:
        _canonical_text(self.name, "commit identity name", max_length=100)
        _canonical_email(self.email)

    def as_payload(self) -> dict[str, str]:
        return {"name": self.name, "email": self.email}


@dataclass(frozen=True, slots=True)
class VaultWriteChange:
    """One allowed Markdown create or modification in a frozen proposal."""

    path: str
    operation: Literal["create", "modify"]
    content: str

    def __post_init__(self) -> None:
        _canonical_note_path(self.path)
        if not isinstance(self.operation, str) or self.operation not in {
            "create",
            "modify",
        }:
            raise ValueError("vault write operation must be create or modify")
        _validate_note_content(self.content)

    def as_payload(self) -> dict[str, str]:
        return {
            "path": self.path,
            "operation": self.operation,
            "content": self.content,
        }


@dataclass(frozen=True, slots=True)
class VaultWriteRequest:
    """The complete typed request reconstructed from a frozen action."""

    base_commit: str
    changes: tuple[VaultWriteChange, ...]
    patch: str
    commit_identity: VaultCommitIdentity
    commit_subject: str
    commit_body: str

    def __post_init__(self) -> None:
        _commit_hash(self.base_commit)
        changes = tuple(self.changes)
        if not changes or len(changes) > _MAX_WRITE_PATHS:
            raise ValueError("vault write must contain between one and 16 changes")
        if any(not isinstance(change, VaultWriteChange) for change in changes):
            raise TypeError("changes must contain VaultWriteChange values")
        if tuple(change.path for change in changes) != tuple(
            sorted(change.path for change in changes)
        ):
            raise ValueError("vault write paths must be sorted canonically")
        if len({change.path for change in changes}) != len(changes):
            raise ValueError("vault write paths must be unique")
        if not isinstance(self.commit_identity, VaultCommitIdentity):
            raise TypeError("commit_identity must be a VaultCommitIdentity")
        _validate_patch(self.patch)
        _validate_commit_subject(self.commit_subject)
        _validate_commit_body(self.commit_body)
        object.__setattr__(self, "changes", changes)

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(change.path for change in self.changes)

    @classmethod
    def from_proposal(cls, proposal: FrozenActionProposal) -> VaultWriteRequest:
        payload = _parse_write_payload(proposal)
        raw_changes = payload["changes"]
        if not isinstance(raw_changes, list):
            raise TypeError("knowledge-vault write changes must be a list")
        changes = tuple(_change_from_payload(item) for item in raw_changes)
        paths = payload["paths"]
        if paths != [change.path for change in changes]:
            raise ValueError("knowledge-vault write paths do not match changes")
        identity_payload = payload["commit_identity"]
        if not isinstance(identity_payload, dict) or set(identity_payload) != {
            "name",
            "email",
        }:
            raise ValueError("knowledge-vault commit identity is malformed")
        try:
            identity = VaultCommitIdentity(
                name=identity_payload["name"], email=identity_payload["email"]
            )
            request = cls(
                base_commit=payload["base_commit"],
                changes=changes,
                patch=payload["diff"],
                commit_identity=identity,
                commit_subject=payload["commit_subject"],
                commit_body=payload["commit_body"],
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("knowledge-vault write payload is invalid") from exc
        if proposal.preview != render_vault_write_preview(request):
            raise ValueError("knowledge-vault write preview does not match payload")
        return request


class VaultWriteProposal:
    """Factory for the exact action stored by the generic approval flow."""

    @classmethod
    def create(
        cls,
        *,
        action_id: str,
        request_id: str,
        base_commit: str,
        changes: Sequence[VaultWriteChange | Mapping[str, object]],
        diff: str,
        commit_subject: str = DEFAULT_VAULT_COMMIT_SUBJECT,
        commit_body: str,
        commit_identity: VaultCommitIdentity | Mapping[str, object] | None = None,
    ) -> FrozenActionProposal:
        normalized_changes = tuple(_coerce_change(change) for change in changes)
        normalized_identity = _coerce_identity(commit_identity)
        request = VaultWriteRequest(
            base_commit=base_commit,
            changes=normalized_changes,
            patch=diff,
            commit_identity=normalized_identity,
            commit_subject=commit_subject,
            commit_body=commit_body,
        )
        return FrozenActionProposal.create(
            action_id=action_id,
            request_id=request_id,
            kind=KNOWLEDGE_VAULT_WRITE_KIND,
            preview=render_vault_write_preview(request),
            payload={
                "schema": KNOWLEDGE_VAULT_WRITE_SCHEMA,
                "base_commit": request.base_commit,
                "paths": list(request.paths),
                "changes": [change.as_payload() for change in request.changes],
                "diff": request.patch,
                "commit_identity": request.commit_identity.as_payload(),
                "commit_subject": request.commit_subject,
                "commit_body": request.commit_body,
            },
        )

    @classmethod
    def from_proposal(cls, proposal: FrozenActionProposal) -> VaultWriteRequest:
        """Parse a frozen proposal through the same public type as Calendar/Gmail."""

        return VaultWriteRequest.from_proposal(proposal)


def _parse_write_payload(proposal: FrozenActionProposal) -> dict[str, object]:
    if proposal.kind != KNOWLEDGE_VAULT_WRITE_KIND:
        raise ValueError("proposal is not a knowledge-vault write")
    try:
        payload = json.loads(proposal.payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("knowledge-vault write payload is malformed") from exc
    if not isinstance(payload, dict):
        raise TypeError("knowledge-vault write payload must be an object")
    expected = {
        "schema",
        "base_commit",
        "paths",
        "changes",
        "diff",
        "commit_identity",
        "commit_subject",
        "commit_body",
    }
    if set(payload) != expected:
        raise ValueError("knowledge-vault write payload has an unexpected shape")
    if payload["schema"] != KNOWLEDGE_VAULT_WRITE_SCHEMA:
        raise ValueError("knowledge-vault write schema is unsupported")
    return payload


@dataclass(frozen=True, slots=True)
class VaultWriteDispatchResult:
    """The non-sensitive local result of one committed and pushed patch."""

    commit_id: str
    paths: tuple[str, ...]
    pushed: bool = True


@dataclass(slots=True)
class _VaultWriteProgress:
    """Track the side-effect boundary for conservative failure handling."""

    write_started: bool = False
    commit_attempted: bool = False
    commit_id: str | None = None


class KnowledgeVaultWriteConnector:
    """Prepare and dispatch one exact Markdown-only vault patch."""

    def __init__(
        self,
        *,
        root: Path,
        repository: VaultWriteRepository,
        now: Callable[[], datetime],
        allowed_note_directories: Sequence[str] = (".",),
        commit_identity: VaultCommitIdentity | None = None,
        commit_subject: str = DEFAULT_VAULT_COMMIT_SUBJECT,
        timeout_seconds: float = 20.0,
    ) -> None:
        resolved_root = root.resolve(strict=True)
        if not resolved_root.is_dir() or root.is_symlink():
            raise ValueError("knowledge-vault root must be a real directory")
        if not callable(now):
            raise TypeError("now must be callable")
        for name in (
            "is_clean",
            "synchronize",
            "current_commit",
            "fetch_remote_commit",
            "stage",
            "staged_diff",
            "commit",
            "push",
        ):
            if not callable(getattr(repository, name, None)):
                raise TypeError(f"repository must provide {name}")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= 120.0
        ):
            raise ValueError("timeout_seconds must be within 0 and 120 seconds")
        self._root = resolved_root
        self._repository = repository
        self._now = now
        self._allowed_directories = _canonical_allowed_directories(
            allowed_note_directories
        )
        self._commit_identity = commit_identity or VaultCommitIdentity()
        _validate_commit_subject(commit_subject)
        self._commit_subject = commit_subject
        self._timeout_seconds = float(timeout_seconds)
        self._prepared_lock = RLock()
        self._prepared: dict[str, _VaultWriteDispatch] = {}
        self._write_blocked = False

    def propose(
        self,
        *,
        request_id: str,
        changes: Mapping[str, str] | Sequence[VaultWriteChange],
        commit_subject: str | None = None,
        deadline: float | None = None,
    ) -> FrozenActionProposal:
        """Synchronize and freeze a complete patch without changing the clone."""

        self._ensure_not_blocked()
        _canonical_text(request_id, "request_id", max_length=256)
        deadline = self._deadline(deadline)
        self._require_clean(deadline=deadline)
        try:
            self._repository.synchronize(self._root, now=self._now(), deadline=deadline)
        except VaultRemoteUnavailable as exc:
            raise VaultWriteRemoteUnavailable(
                "knowledge-vault synchronization was unavailable"
            ) from exc
        except VaultWriteRemoteUnavailable as exc:
            raise VaultWriteRemoteUnavailable(
                "knowledge-vault synchronization was unavailable"
            ) from exc
        except VaultRepositoryConflict as exc:
            raise VaultWriteConflict(
                "knowledge-vault synchronization requires manual recovery"
            ) from exc
        except VaultWriteError:
            raise
        except Exception as exc:
            raise VaultWriteRemoteUnavailable(
                "knowledge-vault synchronization was unavailable"
            ) from exc
        self._require_clean(deadline=deadline)
        base_commit = self._current_commit(deadline=deadline)
        normalized, originals = self._prepare_changes(changes)
        patch = render_vault_unified_diff(originals, normalized)
        subject = self._commit_subject if commit_subject is None else commit_subject
        _validate_commit_subject(subject)
        body = _commit_body(request_id, normalized)
        return VaultWriteProposal.create(
            action_id=f"{request_id}:proposal",
            request_id=request_id,
            base_commit=base_commit,
            changes=normalized,
            diff=patch,
            commit_identity=self._commit_identity,
            commit_subject=subject,
            commit_body=body,
        )

    def bind_proposal(self, action: FrozenActionProposal) -> FrozenActionProposal:
        if action.kind != KNOWLEDGE_VAULT_WRITE_KIND:
            return action
        self._validate_request(VaultWriteRequest.from_proposal(action))
        return action

    def validate_pending_action(self, action: FrozenActionProposal) -> None:
        if action.kind != KNOWLEDGE_VAULT_WRITE_KIND:
            return
        request = self._validate_request(VaultWriteRequest.from_proposal(action))
        try:
            self._verify_base(request, deadline=self._deadline(None))
        except VaultWriteError as exc:
            raise ActionDispatcherError(
                "knowledge-vault base changed after the proposal was frozen"
            ) from exc

    def prepare(self, action: FrozenActionProposal) -> ActionDispatchHandle:
        if action.kind != KNOWLEDGE_VAULT_WRITE_KIND:
            raise ActionDispatcherError("proposal is not a knowledge-vault write")
        self._validate_request(VaultWriteRequest.from_proposal(action))
        handle = _VaultWriteDispatch(self, action)
        with self._prepared_lock:
            if action.action_id in self._prepared:
                raise ActionDispatcherError(
                    f"action {action.action_id} is already prepared",
                    may_have_dispatched=True,
                )
            self._prepared[action.action_id] = handle
        return handle

    def dispatch(
        self, action: FrozenActionProposal, *, deadline: float | None = None
    ) -> VaultWriteDispatchResult:
        """Apply, independently verify, commit, and normally push the exact patch."""

        if action.kind != KNOWLEDGE_VAULT_WRITE_KIND:
            raise ActionDispatcherError("proposal is not a knowledge-vault write")
        self._ensure_not_blocked()
        request = self._validate_request(VaultWriteRequest.from_proposal(action))
        deadline = self._deadline(deadline)
        progress = _VaultWriteProgress()
        try:
            return self._dispatch_exact(request, deadline=deadline, progress=progress)
        except VaultWriteConflict as exc:
            self._raise_dispatch_failure(exc, progress=progress)
        except VaultWriteRemoteUnavailable as exc:
            self._raise_dispatch_failure(exc, progress=progress, unknown_push=True)
        except VaultWriteError as exc:
            self._raise_dispatch_failure(exc, progress=progress)
        except Exception as exc:  # noqa: BLE001 - unknown repository edge outcome
            self._raise_dispatch_failure(exc, progress=progress)

    def _raise_dispatch_failure(
        self,
        error: Exception,
        *,
        progress: _VaultWriteProgress,
        unknown_push: bool = False,
    ) -> NoReturn:
        if progress.commit_attempted:
            self._write_blocked = True
            message = (
                "knowledge-vault push outcome is unknown; manual recovery is required"
                if unknown_push
                else "knowledge-vault write stopped for explicit manual recovery"
            )
            raise ActionDispatcherError(message, may_have_dispatched=True) from error
        if isinstance(error, VaultWriteError):
            raise ActionDispatcherError(
                str(error), may_have_dispatched=progress.write_started
            ) from error
        raise ActionDispatcherError(
            "knowledge-vault write was unavailable",
            may_have_dispatched=progress.write_started,
        ) from error

    def _dispatch_exact(
        self,
        request: VaultWriteRequest,
        *,
        deadline: float,
        progress: _VaultWriteProgress,
    ) -> VaultWriteDispatchResult:
        originals = self._verify_base(request, deadline=deadline)
        current_patch = render_vault_unified_diff(originals, request.changes)
        if current_patch != request.patch:
            raise VaultWriteConflict(
                "knowledge-vault content changed after the proposal was frozen"
            )
        progress.write_started = True
        self._apply_changes(request.changes, originals)
        self._repository.stage(self._root, request.paths, deadline=deadline)
        self._verify_applied_patch(request, originals=originals, deadline=deadline)
        progress.commit_attempted = True
        progress.commit_id = _commit_hash(
            self._repository.commit(
                self._root,
                author_name=request.commit_identity.name,
                author_email=request.commit_identity.email,
                subject=request.commit_subject,
                body=request.commit_body,
                deadline=deadline,
            )
        )
        self._push_same_commit(request, commit_id=progress.commit_id, deadline=deadline)
        return VaultWriteDispatchResult(
            commit_id=progress.commit_id, paths=request.paths
        )

    def _verify_applied_patch(
        self,
        request: VaultWriteRequest,
        *,
        originals: Mapping[str, str | None],
        deadline: float,
    ) -> None:
        resulting = self._read_change_contents(request.changes)
        resulting_patch = render_vault_unified_diff(originals, resulting)
        if resulting_patch != request.patch:
            raise VaultWriteConflict(
                "applied knowledge-vault diff did not match the proposal"
            )
        staged_patch = self._repository.staged_diff(
            self._root, request.paths, deadline=deadline
        )
        if staged_patch is not None and _normalise_patch(
            staged_patch
        ) != _normalise_patch(request.patch):
            raise VaultWriteConflict(
                "staged knowledge-vault diff did not match the proposal"
            )

    def cancel(self, *, action_id: str) -> ActionCancellationResult:
        with self._prepared_lock:
            handle = self._prepared.get(action_id)
        if handle is None:
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
        return handle.cancel()

    def finalize(self, *, action_id: str) -> None:
        with self._prepared_lock:
            self._prepared.pop(action_id, None)

    def _forget(self, action_id: str, handle: _VaultWriteDispatch) -> None:
        with self._prepared_lock:
            if self._prepared.get(action_id) is handle:
                del self._prepared[action_id]

    def clear_manual_recovery_block(self) -> None:
        """Clear the post-conflict stop only after an administrator recovers Git."""

        with self._prepared_lock:
            self._write_blocked = False

    def _push_same_commit(
        self, request: VaultWriteRequest, *, commit_id: str, deadline: float
    ) -> None:
        for attempt in range(2):
            try:
                self._repository.push(
                    self._root,
                    expected_base=request.base_commit,
                    commit_id=commit_id,
                    deadline=deadline,
                )
                return
            except (VaultRemoteUnavailable, VaultWriteRemoteUnavailable) as exc:
                if attempt == 0:
                    _remaining_seconds(deadline, VaultWriteRemoteUnavailable)
                    continue
                raise VaultWriteRemoteUnavailable(
                    "knowledge-vault push was unavailable"
                ) from exc
            except VaultRepositoryConflict as exc:
                raise VaultWriteConflict(
                    "knowledge-vault push encountered a repository conflict"
                ) from exc

    def _prepare_changes(
        self, changes: Mapping[str, str] | Sequence[VaultWriteChange]
    ) -> tuple[tuple[VaultWriteChange, ...], dict[str, str | None]]:
        raw_items = self._raw_change_items(changes)
        if not raw_items or len(raw_items) > _MAX_WRITE_PATHS:
            raise VaultWriteError(
                "knowledge-vault write must contain between one and 16 changes"
            )
        normalized: list[VaultWriteChange] = []
        originals: dict[str, str | None] = {}
        seen: set[str] = set()
        for raw_path, content in raw_items:
            path = self._canonical_allowed_path(raw_path)
            if path in seen:
                raise VaultWriteError("knowledge-vault write paths must be unique")
            seen.add(path)
            candidate = self._root / Path(*PurePosixPath(path).parts)
            original = self._read_note(candidate, path, allow_missing=True)
            _validate_note_content(content)
            if original == content:
                continue
            normalized.append(
                VaultWriteChange(
                    path=path,
                    operation="modify" if original is not None else "create",
                    content=content,
                )
            )
            originals[path] = original
        if not normalized:
            raise VaultWriteError("knowledge-vault write contains no changes")
        normalized.sort(key=lambda change: change.path)
        originals = {change.path: originals[change.path] for change in normalized}
        return tuple(normalized), originals

    @staticmethod
    def _raw_change_items(
        changes: Mapping[str, str] | Sequence[VaultWriteChange],
    ) -> tuple[tuple[object, object], ...]:
        if isinstance(changes, Mapping):
            return tuple(changes.items())
        if not isinstance(changes, Sequence) or isinstance(changes, (str, bytes)):
            raise VaultWriteError("knowledge-vault changes must be a mapping")
        items: list[tuple[object, object]] = []
        for change in changes:
            if isinstance(change, VaultWriteChange):
                items.append((change.path, change.content))
            elif isinstance(change, Mapping) and set(change) == {"path", "content"}:
                items.append((change["path"], change["content"]))
            else:
                raise VaultWriteError(
                    "knowledge-vault changes must contain path and content"
                )
        return tuple(items)

    def _read_change_contents(
        self, changes: Sequence[VaultWriteChange]
    ) -> tuple[VaultWriteChange, ...]:
        result: list[VaultWriteChange] = []
        for change in changes:
            candidate = self._root / Path(*PurePosixPath(change.path).parts)
            content = self._read_note(candidate, change.path, allow_missing=False)
            if content is None:
                raise VaultWriteConflict(
                    "knowledge-vault note disappeared during write"
                )
            result.append(
                VaultWriteChange(
                    path=change.path, operation=change.operation, content=content
                )
            )
        return tuple(result)

    def _apply_changes(
        self,
        changes: Sequence[VaultWriteChange],
        originals: Mapping[str, str | None],
    ) -> None:
        for change in changes:
            candidate = self._root / Path(*PurePosixPath(change.path).parts)
            before = self._read_note(candidate, change.path, allow_missing=True)
            if before != originals[change.path]:
                raise VaultWriteConflict("knowledge-vault note changed during apply")
            try:
                candidate.write_text(change.content, encoding="utf-8", newline="")
            except OSError as exc:
                raise VaultWriteRepositoryError(
                    "knowledge-vault note could not be written"
                ) from exc

    def _verify_base(
        self, request: VaultWriteRequest, *, deadline: float
    ) -> dict[str, str | None]:
        self._require_clean(deadline=deadline)
        try:
            remote_commit = _commit_hash(
                self._repository.fetch_remote_commit(self._root, deadline=deadline)
            )
            local_commit = self._current_commit(deadline=deadline)
        except VaultWriteError:
            raise
        except Exception as exc:
            raise VaultWriteRemoteUnavailable(
                "knowledge-vault remote verification was unavailable"
            ) from exc
        if remote_commit != request.base_commit or local_commit != request.base_commit:
            raise VaultWriteConflict(
                "knowledge-vault remote or local base changed after the proposal"
            )
        originals = {
            change.path: self._read_note(
                self._root / Path(*PurePosixPath(change.path).parts),
                change.path,
                allow_missing=True,
            )
            for change in request.changes
        }
        for change in request.changes:
            expected_operation = (
                "modify" if originals[change.path] is not None else "create"
            )
            if change.operation != expected_operation:
                raise VaultWriteConflict(
                    f"knowledge-vault {change.path} operation does not match its base"
                )
        return originals

    def _validate_request(self, request: VaultWriteRequest) -> VaultWriteRequest:
        if request.commit_identity != self._commit_identity:
            raise ActionDispatcherError(
                "knowledge-vault commit identity changed after the proposal"
            )
        if not request.commit_subject.startswith(
            self._commit_subject.split(":", 1)[0] + ":"
        ):
            raise ActionDispatcherError(
                "knowledge-vault commit subject is outside the configured prefix"
            )
        for change in request.changes:
            self._canonical_allowed_path(change.path)
        return request

    def _canonical_allowed_path(self, value: object) -> str:
        path = _canonical_note_path(value)
        parts = PurePosixPath(path).parts
        if not any(
            directory == () or parts[: len(directory)] == directory
            for directory in self._allowed_directories
        ):
            raise VaultWriteError("path is outside configured note directories")
        candidate = self._root / Path(*parts)
        if any(
            os.path.lexists(self._root.joinpath(*parts[:index], ".git"))
            for index in range(1, len(parts))
        ):
            raise VaultWriteError("path is not an ordinary knowledge-vault note")
        if any(
            parent.is_symlink() for parent in candidate.parents if parent != self._root
        ):
            raise VaultWriteError("path is not an ordinary knowledge-vault note")
        if candidate.exists() and candidate.is_symlink():
            raise VaultWriteError("path is not an ordinary knowledge-vault note")
        if candidate.exists() and not candidate.is_file():
            raise VaultWriteError("path is not an ordinary knowledge-vault note")
        if not candidate.parent.is_dir() or candidate.parent.is_symlink():
            raise VaultWriteError("path is not an ordinary knowledge-vault note")
        if os.path.lexists(candidate) and candidate.is_symlink():
            raise VaultWriteError("path is not an ordinary knowledge-vault note")
        return path

    def _read_note(
        self, candidate: Path, path: str, *, allow_missing: bool
    ) -> str | None:
        if not os.path.lexists(candidate):
            if allow_missing:
                return None
            raise VaultWriteConflict(f"knowledge-vault note {path} is missing")
        if candidate.is_symlink() or not candidate.is_file():
            raise VaultWriteError("path is not an ordinary knowledge-vault note")
        try:
            raw = candidate.read_bytes()
        except OSError as exc:
            raise VaultWriteRepositoryError(
                "knowledge-vault note could not be read"
            ) from exc
        if len(raw) > _MAX_WRITE_CONTENT_BYTES:
            raise VaultWriteError("knowledge-vault note exceeds the write byte limit")
        try:
            content = raw.decode("utf-8").replace("\r\n", "\n")
        except UnicodeDecodeError as exc:
            raise VaultWriteError("knowledge-vault note is not valid UTF-8") from exc
        _validate_note_content(content)
        return content

    def _require_clean(self, *, deadline: float) -> None:
        try:
            clean = self._repository.is_clean(self._root, deadline=deadline)
        except Exception as exc:
            raise VaultWriteRepositoryError(
                "knowledge-vault cleanliness could not be verified"
            ) from exc
        if not clean:
            raise VaultWriteConflict("knowledge-vault clone must be clean")

    def _current_commit(self, *, deadline: float) -> str:
        try:
            commit = self._repository.current_commit(self._root, deadline=deadline)
        except Exception as exc:
            raise VaultWriteRepositoryError(
                "knowledge-vault base commit could not be read"
            ) from exc
        return _commit_hash(commit)

    def _deadline(self, deadline: float | None) -> float:
        selected = (
            deadline if deadline is not None else monotonic() + self._timeout_seconds
        )
        _remaining_seconds(selected, VaultWriteRepositoryError)
        return selected

    def _ensure_not_blocked(self) -> None:
        if self._write_blocked:
            raise VaultWriteError(
                "knowledge-vault writes are blocked pending manual recovery"
            )


class _VaultWriteDispatch:
    """Prepared vault write that can be cancelled before application begins."""

    def __init__(
        self, owner: KnowledgeVaultWriteConnector, action: FrozenActionProposal
    ):
        self._owner = owner
        self._action = action
        self._lock = RLock()
        self._started = False
        self._cancelled = False

    def run(self) -> VaultWriteDispatchResult:
        with self._lock:
            if self._cancelled:
                self._owner._forget(self._action.action_id, self)
                raise ActionDispatcherError("knowledge-vault write was cancelled")
            self._started = True
        try:
            return self._owner.dispatch(self._action)
        finally:
            self._owner._forget(self._action.action_id, self)

    def cancel(self) -> ActionCancellationResult:
        with self._lock:
            if not self._started:
                self._cancelled = True
                self._owner._forget(self._action.action_id, self)
                return ActionCancellationResult(ActionCancellationStatus.NOT_STARTED)
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)


def render_vault_unified_diff(
    originals: Mapping[str, str | None], changes: Sequence[VaultWriteChange]
) -> str:
    """Render a deterministic, complete unified diff for the approved paths."""

    chunks: list[str] = []
    for change in sorted(changes, key=lambda item: item.path):
        old = originals.get(change.path)
        old_lines = [] if old is None else old.splitlines(keepends=True)
        new_lines = change.content.splitlines(keepends=True)
        from_file = "/dev/null" if old is None else f"a/{change.path}"
        diff = list(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=from_file,
                tofile=f"b/{change.path}",
                n=3,
                lineterm="\n",
            )
        )
        if not diff:
            continue
        chunks.append("".join(diff).rstrip("\n") + "\n")
    patch = "".join(chunks)
    _validate_patch(patch)
    return patch


def render_vault_write_preview(request: VaultWriteRequest) -> str:
    """Render the complete human approval envelope, including every diff line."""

    lines = [
        "Knowledge-vault write proposal",
        f"Base commit: {request.base_commit}",
        "Paths:",
    ]
    lines.extend(f"- {change.path} ({change.operation})" for change in request.changes)
    lines.extend(
        (
            f"Commit identity: {request.commit_identity.name} <{request.commit_identity.email}>",
            f"Commit subject: {request.commit_subject}",
            "Commit body:",
            request.commit_body,
            "approval will commit and push precisely this patch.",
            "Complete unified diff:",
            request.patch,
            "End of complete unified diff.",
        )
    )
    return "\n".join(lines).removesuffix("\n")


def _commit_body(request_id: str, changes: Sequence[VaultWriteChange]) -> str:
    return "\n".join(
        (
            "Changed knowledge-vault note paths:",
            *(f"- {change.path}" for change in changes),
            f"Request ID: {request_id}",
        )
    )


def _coerce_change(value: VaultWriteChange | Mapping[str, object]) -> VaultWriteChange:
    if isinstance(value, VaultWriteChange):
        return value
    if not isinstance(value, Mapping) or set(value) != {"path", "operation", "content"}:
        raise ValueError("vault write change has an unexpected shape")
    return VaultWriteChange(
        path=value["path"],
        operation=value["operation"],
        content=value["content"],  # type: ignore[arg-type]
    )


def _change_from_payload(value: object) -> VaultWriteChange:
    try:
        return _coerce_change(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError("vault write change is invalid") from exc


def _coerce_identity(
    value: VaultCommitIdentity | Mapping[str, object] | None,
) -> VaultCommitIdentity:
    if value is None:
        return VaultCommitIdentity()
    if isinstance(value, VaultCommitIdentity):
        return value
    if not isinstance(value, Mapping) or set(value) != {"name", "email"}:
        raise ValueError("commit identity has an unexpected shape")
    return VaultCommitIdentity(name=value["name"], email=value["email"])  # type: ignore[arg-type]


def _canonical_allowed_directories(value: Sequence[str]) -> tuple[tuple[str, ...], ...]:
    if isinstance(value, (str, bytes)) or not value or len(value) > _MAX_WRITE_PATHS:
        raise ValueError("allowed note directories must be a bounded sequence")
    result: list[tuple[str, ...]] = []
    for raw in value:
        if not isinstance(raw, str) or not raw or "\\" in raw:
            raise ValueError("allowed note directories must be canonical paths")
        path = PurePosixPath(raw)
        if raw != "." and (
            path.is_absolute()
            or path.as_posix() != raw
            or any(part in {".", ".."} for part in path.parts)
        ):
            raise ValueError("allowed note directories must be canonical paths")
        parts = () if raw == "." else path.parts
        if any(part.startswith(".") for part in parts) or any(
            part in _EXCLUDED_TOP_LEVEL_DIRECTORIES for part in parts
        ):
            raise ValueError("allowed note directories include an excluded path")
        result.append(parts)
    if len(set(result)) != len(result):
        raise ValueError("allowed note directories must be unique")
    return tuple(sorted(result, key=lambda item: (len(item), item)))


def _canonical_note_path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > 512
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise VaultWriteError("path is not an ordinary knowledge-vault note")
    raw = PurePosixPath(value)
    if (
        "\\" in value
        or raw.is_absolute()
        or PureWindowsPath(value).is_absolute()
        or raw.as_posix() != value
        or any(part in {".", ".."} for part in raw.parts)
        or value.endswith("/")
        or raw.suffix != ".md"
        or any(part.startswith(".") for part in raw.parts)
        or any(part in _EXCLUDED_TOP_LEVEL_DIRECTORIES for part in raw.parts)
    ):
        raise VaultWriteError("path is not an ordinary knowledge-vault note")
    return value


def _validate_note_content(value: object) -> str:
    if not isinstance(value, str):
        raise VaultWriteError("knowledge-vault note content must be text")
    if "\x00" in value or "\r" in value:
        raise VaultWriteError(
            "knowledge-vault note content is not canonical UTF-8 text"
        )
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise VaultWriteError(
            "knowledge-vault note content is not valid UTF-8"
        ) from exc
    if len(encoded) > _MAX_WRITE_CONTENT_BYTES:
        raise VaultWriteError("knowledge-vault note exceeds the write byte limit")
    return value


def _validate_patch(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_WRITE_DIFF_CHARS:
        raise ValueError("knowledge-vault write diff must be bounded and non-blank")
    if "\r" in value or "\x00" in value:
        raise ValueError("knowledge-vault write diff contains invalid bytes")
    return value


def _validate_commit_subject(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > _MAX_COMMIT_SUBJECT_CHARS
        or "\n" in value
        or "\r" in value
        or not value.startswith("jarvis:")
    ):
        raise ValueError("vault commit subject must be a concise jarvis: subject")
    return value


def _validate_commit_body(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > _MAX_COMMIT_BODY_CHARS
    ):
        raise ValueError("vault commit body must be bounded and non-blank")
    if "\x00" in value or "\r" in value:
        raise ValueError("vault commit body contains invalid bytes")
    return value


def _canonical_text(value: object, name: str, *, max_length: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > max_length
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(f"{name} must be a canonical bounded string")
    return value


def _canonical_email(value: object) -> str:
    value = _canonical_text(value, "commit identity email", max_length=254)
    if value.count("@") != 1 or any(char.isspace() for char in value):
        raise ValueError("commit identity email is invalid")
    return value


def _commit_hash(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(_COMMIT_HASH, value):
        raise ValueError("vault base commit must be a Git commit hash")
    return value.lower()


def _normalise_patch(value: str) -> str:
    return (
        "\n".join(
            line
            for line in value.replace("\r\n", "\n").splitlines()
            if line != r"\ No newline at end of file"
        ).rstrip("\n")
        + "\n"
    )


class ControlledVaultWriteRepository:
    """Deterministic repository double for connector and broker contract tests."""

    def __init__(
        self,
        *,
        current_commit: str,
        remote_commit: str,
        clean: bool = True,
        synchronize_failure: str | None = None,
        fetch_failure: str | None = None,
        stage_failure: str | None = None,
        staged_diff_override: str | None = None,
        commit_failure: str | None = None,
        push_failure: str | None = None,
        push_remote_unavailable: bool = False,
    ) -> None:
        self._current_commit = _commit_hash(current_commit)
        self.remote_commit = _commit_hash(remote_commit)
        self.clean = clean
        self.synchronize_failure = synchronize_failure
        self.fetch_failure = fetch_failure
        self.stage_failure = stage_failure
        self.staged_diff_override = staged_diff_override
        self.commit_failure = commit_failure
        self.push_failure = push_failure
        self.push_remote_unavailable = push_remote_unavailable
        self.synchronize_calls = 0
        self.fetch_calls = 0
        self.stage_calls: list[tuple[str, ...]] = []
        self.commit_calls: list[dict[str, object]] = []
        self.push_calls: list[dict[str, object]] = []
        self._next_commit_number = 1

    def is_clean(self, _root: Path, *, deadline: float | None = None) -> bool:
        return self.clean

    def synchronize(
        self, _root: Path, *, now: datetime, deadline: float | None = None
    ) -> datetime:
        self.synchronize_calls += 1
        if not self.clean:
            raise VaultWriteConflict("knowledge-vault clone must be clean")
        if self.synchronize_failure is not None:
            raise VaultWriteRemoteUnavailable(self.synchronize_failure)
        self._current_commit = self.remote_commit
        return now

    def current_commit(self, _root: Path, *, deadline: float | None = None) -> str:
        return self._current_commit

    def fetch_remote_commit(self, _root: Path, *, deadline: float | None = None) -> str:
        self.fetch_calls += 1
        if self.fetch_failure is not None:
            raise VaultWriteRemoteUnavailable(self.fetch_failure)
        return self.remote_commit

    def stage(
        self, _root: Path, paths: Sequence[str], *, deadline: float | None = None
    ) -> None:
        if self.stage_failure is not None:
            raise VaultWriteRepositoryError(self.stage_failure)
        self.stage_calls.append(tuple(paths))
        self.clean = False

    def staged_diff(
        self, _root: Path, _paths: Sequence[str], *, deadline: float | None = None
    ) -> str | None:
        return self.staged_diff_override

    def commit(
        self,
        _root: Path,
        *,
        author_name: str,
        author_email: str,
        subject: str,
        body: str,
        deadline: float | None = None,
    ) -> str:
        if self.commit_failure is not None:
            raise VaultWriteRepositoryError(self.commit_failure)
        self.commit_calls.append(
            {
                "author_name": author_name,
                "author_email": author_email,
                "subject": subject,
                "body": body,
            }
        )
        commit_id = f"{self._next_commit_number:040x}"
        self._next_commit_number += 1
        self._current_commit = commit_id
        self.clean = True
        return commit_id

    def push(
        self,
        _root: Path,
        *,
        expected_base: str,
        commit_id: str,
        deadline: float | None = None,
    ) -> None:
        self.push_calls.append({"expected_base": expected_base, "commit_id": commit_id})
        if self.push_remote_unavailable:
            raise VaultWriteRemoteUnavailable("remote push was unavailable")
        if self.push_failure is not None:
            raise VaultWriteConflict(self.push_failure)
        if self.remote_commit != expected_base:
            raise VaultWriteConflict("remote rejected the non-fast-forward push")
        self.remote_commit = commit_id

    def advance_remote(self, commit_id: str) -> None:
        self.remote_commit = _commit_hash(commit_id)


VaultWriteConnector = KnowledgeVaultWriteConnector

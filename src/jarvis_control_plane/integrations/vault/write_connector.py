"""Approval-aware vault connector that owns the exact write dispatch flow."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import NoReturn

from ...models import FrozenActionProposal
from ...ports import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ActionDispatcherError,
    ActionDispatchHandle,
)
from .common import _remaining_seconds
from .errors import (
    VaultPushPreDispatchFailure,
    VaultPushUnknownOutcome,
    VaultRemoteUnavailable,
    VaultRepositoryConflict,
    VaultWriteConflict,
    VaultWriteError,
    VaultWritePushPreDispatchFailure,
    VaultWriteRemoteUnavailable,
)
from .write_dispatch import (
    _VaultWriteDispatch,
    _VaultWriteProgress,
    _WriteExecutionMixin,
)
from .write_models import (
    DEFAULT_VAULT_COMMIT_SUBJECT,
    KNOWLEDGE_VAULT_WRITE_KIND,
    VaultCommitIdentity,
    VaultWriteChange,
    VaultWriteDispatchResult,
    VaultWriteProposal,
    VaultWriteRepository,
    VaultWriteRequest,
)
from .write_policy import _canonical_text, _commit_body, _validate_commit_subject


class KnowledgeVaultWriteConnector(_WriteExecutionMixin):
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
            "render_diff",
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
        from .write_policy import canonical_allowed_note_directories

        self._allowed_directories = canonical_allowed_note_directories(
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
        patch = self._render_patch(originals, normalized, deadline=deadline)
        body = _commit_body(request_id, normalized)
        return VaultWriteProposal.create(
            action_id=f"{request_id}:proposal",
            request_id=request_id,
            base_commit=base_commit,
            changes=normalized,
            diff=patch,
            commit_identity=self._commit_identity,
            commit_subject=self._commit_subject,
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
        except VaultPushUnknownOutcome as exc:
            self._raise_dispatch_failure(exc, progress=progress, unknown_push=True)
        except VaultWritePushPreDispatchFailure as exc:
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
            except VaultPushPreDispatchFailure as exc:
                if attempt == 0:
                    _remaining_seconds(deadline, VaultWriteRemoteUnavailable)
                    continue
                raise VaultWritePushPreDispatchFailure(
                    "knowledge-vault push failed before dispatch; manual recovery is required"
                ) from exc
            except VaultPushUnknownOutcome:
                raise
            except (VaultRemoteUnavailable, VaultWriteRemoteUnavailable) as exc:
                raise VaultPushUnknownOutcome(
                    "knowledge-vault push outcome is unknown"
                ) from exc
            except VaultRepositoryConflict as exc:
                raise VaultWriteConflict(
                    "knowledge-vault push encountered a repository conflict"
                ) from exc


VaultWriteConnector = KnowledgeVaultWriteConnector


__all__ = ["KnowledgeVaultWriteConnector", "VaultWriteConnector"]

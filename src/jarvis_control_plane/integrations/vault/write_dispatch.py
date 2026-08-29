"""Exact write application, verification, and prepared-dispatch lifecycle."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from threading import RLock
from time import monotonic
from typing import TYPE_CHECKING

from ...ports import ActionCancellationResult, ActionCancellationStatus
from .common import _remaining_seconds
from .errors import (
    VaultWriteConflict,
    VaultWriteError,
    VaultWriteRemoteUnavailable,
    VaultWriteRepositoryError,
)
from .write_models import (
    _MAX_WRITE_PATHS,
    VaultWriteChange,
    VaultWriteDispatchResult,
    VaultWriteRequest,
)
from .write_policy import (
    _canonical_note_path,
    _commit_hash,
    _validate_note_content,
    _validate_patch,
)

if TYPE_CHECKING:
    from .write_connector import KnowledgeVaultWriteConnector


@dataclass(slots=True)
class _VaultWriteProgress:
    """Track the side-effect boundary for conservative failure handling."""

    write_started: bool = False
    commit_attempted: bool = False
    commit_id: str | None = None


class _WriteExecutionMixin:
    """Implementation shared by proposal preparation and exact dispatch."""

    def _dispatch_exact(
        self,
        request: VaultWriteRequest,
        *,
        deadline: float,
        progress: _VaultWriteProgress,
    ) -> VaultWriteDispatchResult:
        originals = self._verify_base(request, deadline=deadline)
        current_patch = self._render_patch(
            originals, request.changes, deadline=deadline
        )
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
        resulting_patch = self._render_patch(originals, resulting, deadline=deadline)
        if resulting_patch != request.patch:
            raise VaultWriteConflict(
                "applied knowledge-vault diff did not match the proposal"
            )
        staged_patch = self._repository.staged_diff(self._root, deadline=deadline)
        if staged_patch != request.patch:
            raise VaultWriteConflict(
                "staged knowledge-vault diff did not match the proposal"
            )

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
            from ...ports import ActionDispatcherError

            raise ActionDispatcherError(
                "knowledge-vault commit identity changed after the proposal"
            )
        if request.commit_subject != self._commit_subject:
            from ...ports import ActionDispatcherError

            raise ActionDispatcherError(
                "knowledge-vault commit subject changed after the proposal"
            )
        for change in request.changes:
            self._canonical_allowed_path(change.path)
        return request

    def _render_patch(
        self,
        originals: Mapping[str, str | None],
        changes: Sequence[VaultWriteChange],
        *,
        deadline: float,
    ) -> str:
        try:
            patch = self._repository.render_diff(
                self._root,
                originals,
                {change.path: change.content for change in changes},
                deadline=deadline,
            )
            _validate_patch(patch)
            return patch
        except VaultWriteError:
            raise
        except Exception as exc:
            raise VaultWriteRepositoryError(
                "knowledge-vault diff could not be rendered"
            ) from exc

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
        from .write_models import _MAX_WRITE_CONTENT_BYTES

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

    def __init__(self, owner: KnowledgeVaultWriteConnector, action: object) -> None:
        self._owner = owner
        self._action = action
        self._lock = RLock()
        self._started = False
        self._cancelled = False

    def run(self) -> VaultWriteDispatchResult:
        with self._lock:
            if self._cancelled:
                self._owner._forget(self._action.action_id, self)
                from ...ports import ActionDispatcherError

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


__all__ = ["_VaultWriteDispatch", "_VaultWriteProgress", "_WriteExecutionMixin"]

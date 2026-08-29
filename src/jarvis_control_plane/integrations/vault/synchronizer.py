"""Bounded Git synchronization and the legacy-injectable repository edge."""

from __future__ import annotations

import os
import shlex
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from subprocess import DEVNULL, CompletedProcess, TimeoutExpired, run
from tempfile import TemporaryDirectory

from .common import _remaining_seconds
from .errors import (
    VaultPushPreDispatchFailure,
    VaultPushUnknownOutcome,
    VaultRemoteUnavailable,
    VaultRepositoryConflict,
    VaultSynchronizationError,
)
from .read_models import VaultSynchronizationMetadataStore
from .write_policy import _normalise_staged_diff


class SubprocessVaultSynchronizer:
    """No-shell Git synchronizer for the dedicated service-account clone.

    The class retains the historical ``run_process`` injection point because
    the control-plane contract tests use it to model precise Git outcomes.
    """

    def __init__(
        self,
        *,
        git_executable: Path = Path("/usr/bin/git"),
        ssh_executable: Path,
        ssh_config_path: Path,
        known_hosts_path: Path,
        synchronization_state: VaultSynchronizationMetadataStore,
        run_process: Callable[..., CompletedProcess[str]] = run,
    ) -> None:
        for path, name in (
            (git_executable, "git_executable"),
            (ssh_executable, "ssh_executable"),
            (ssh_config_path, "ssh_config_path"),
            (known_hosts_path, "known_hosts_path"),
        ):
            if not path.is_absolute():
                raise ValueError(f"{name} must be an absolute deployment path")
        self._git_executable = git_executable
        self._run_process = run_process
        self._synchronization_state = synchronization_state
        self._environment = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_SSH_COMMAND": shlex.join(
                (
                    str(ssh_executable),
                    "-F",
                    str(ssh_config_path),
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "IdentitiesOnly=yes",
                    "-o",
                    "StrictHostKeyChecking=yes",
                    "-o",
                    f"UserKnownHostsFile={known_hosts_path}",
                    "-o",
                    "GlobalKnownHostsFile=/dev/null",
                )
            ),
        }

    @property
    def last_synchronized_at(self) -> datetime | None:
        synchronized_at = (
            self._synchronization_state.load_knowledge_vault_synchronized_at()
        )
        if synchronized_at is not None and synchronized_at.tzinfo is None:
            raise VaultRepositoryConflict(
                "knowledge-vault synchronization metadata is invalid"
            )
        return synchronized_at

    def is_clean(self, root: Path, *, deadline: float | None = None) -> bool:
        return (
            self._git(
                root,
                "status",
                "--porcelain",
                failure_type=VaultRepositoryConflict,
                deadline=deadline,
            ).stdout.strip()
            == ""
        )

    def synchronize(
        self, root: Path, *, now: datetime, deadline: float | None = None
    ) -> datetime:
        if not self.is_clean(root, deadline=deadline):
            raise VaultRepositoryConflict("knowledge-vault clone is not clean")
        self._git(
            root,
            "fetch",
            "--prune",
            "--no-tags",
            "origin",
            failure_type=VaultRemoteUnavailable,
            deadline=deadline,
        )
        self._git(
            root,
            "merge",
            "--ff-only",
            "FETCH_HEAD",
            failure_type=VaultRepositoryConflict,
            deadline=deadline,
        )
        self._synchronization_state.save_knowledge_vault_synchronized_at(now)
        return now

    def current_commit(self, root: Path, *, deadline: float | None = None) -> str:
        """Return the dedicated clone's exact checked-out commit."""

        return self._git(
            root,
            "rev-parse",
            "HEAD",
            failure_type=VaultRepositoryConflict,
            deadline=deadline,
        ).stdout.strip()

    def fetch_remote_commit(self, root: Path, *, deadline: float | None = None) -> str:
        """Fetch without merging, then return the exact fetched remote base."""

        self._git(
            root,
            "fetch",
            "--prune",
            "--no-tags",
            "origin",
            failure_type=VaultRemoteUnavailable,
            deadline=deadline,
        )
        return self._git(
            root,
            "rev-parse",
            "FETCH_HEAD",
            failure_type=VaultRepositoryConflict,
            deadline=deadline,
        ).stdout.strip()

    def render_diff(
        self,
        root: Path,
        _originals: Mapping[str, str | None],
        changes: Mapping[str, str],
        *,
        deadline: float | None = None,
    ) -> str:
        """Render a canonical patch through a temporary Git index."""

        if not changes:
            raise VaultRepositoryConflict("knowledge-vault write has no changes")
        ordered_changes = tuple(sorted(changes.items()))
        with TemporaryDirectory(prefix="jarvis-vault-diff-") as directory:
            temporary_index = Path(directory) / "index"
            temporary_objects = Path(directory) / "objects"
            temporary_objects.mkdir()
            source_objects = self._git(
                root,
                "rev-parse",
                "--git-path",
                "objects",
                failure_type=VaultRepositoryConflict,
                deadline=deadline,
            ).stdout.strip()
            if not source_objects:
                raise VaultRepositoryConflict(
                    "knowledge-vault Git object database is unavailable"
                )
            source_objects_path = Path(source_objects)
            if not source_objects_path.is_absolute():
                source_objects_path = root / source_objects_path
            alternate_objects = str(source_objects_path.resolve())
            configured_alternates = self._environment.get(
                "GIT_ALTERNATE_OBJECT_DIRECTORIES"
            )
            if configured_alternates:
                alternate_objects = os.pathsep.join(
                    (alternate_objects, configured_alternates)
                )
            environment = {
                **self._environment,
                "GIT_INDEX_FILE": str(temporary_index),
                "GIT_OBJECT_DIRECTORY": str(temporary_objects),
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": alternate_objects,
            }
            self._git(
                root,
                "read-tree",
                "HEAD",
                failure_type=VaultRepositoryConflict,
                deadline=deadline,
                environment=environment,
            )
            content_path = Path(directory) / "content"
            for path, content in ordered_changes:
                content_path.write_bytes(content.encode("utf-8"))
                blob = self._git(
                    root,
                    "hash-object",
                    "-w",
                    f"--path={path}",
                    str(content_path),
                    failure_type=VaultRepositoryConflict,
                    deadline=deadline,
                    environment=environment,
                ).stdout.strip()
                self._git(
                    root,
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"100644,{blob},{path}",
                    failure_type=VaultRepositoryConflict,
                    deadline=deadline,
                    environment=environment,
                )
            output = self._git(
                root,
                "diff",
                "--cached",
                "--no-ext-diff",
                "--no-renames",
                "--no-color",
                "--unified=3",
                "--",
                *(path for path, _content in ordered_changes),
                failure_type=VaultRepositoryConflict,
                deadline=deadline,
                environment=environment,
            ).stdout
        return _normalise_staged_diff(output)

    def stage(
        self, root: Path, paths: Sequence[str], *, deadline: float | None = None
    ) -> None:
        """Stage only the already validated Markdown paths."""

        if not paths:
            raise VaultRepositoryConflict("knowledge-vault write has no paths")
        self._git(
            root,
            "add",
            "--",
            *paths,
            failure_type=VaultRepositoryConflict,
            deadline=deadline,
        )

    def staged_diff(self, root: Path, *, deadline: float | None = None) -> str:
        """Return the complete canonical diff for the current Git index."""

        output = self._git(
            root,
            "diff",
            "--cached",
            "--no-ext-diff",
            "--no-renames",
            "--no-color",
            "--unified=3",
            "--",
            failure_type=VaultRepositoryConflict,
            deadline=deadline,
        ).stdout
        return _normalise_staged_diff(output)

    def commit(
        self,
        root: Path,
        *,
        author_name: str,
        author_email: str,
        subject: str,
        body: str,
        deadline: float | None = None,
    ) -> str:
        """Create one normal commit with the frozen configured identity."""

        self._git(
            root,
            "-c",
            f"user.name={author_name}",
            "-c",
            f"user.email={author_email}",
            "commit",
            "--no-verify",
            "-m",
            subject,
            "-m",
            body,
            failure_type=VaultRepositoryConflict,
            deadline=deadline,
        )
        return self.current_commit(root, deadline=deadline)

    def push(
        self,
        root: Path,
        *,
        expected_base: str,
        commit_id: str,
        deadline: float | None = None,
    ) -> None:
        """Push the checked-out branch normally, without history rewriting."""

        branch = self._git(
            root,
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
            failure_type=VaultRepositoryConflict,
            deadline=deadline,
        ).stdout.strip()
        if not branch or branch == "HEAD":
            raise VaultRepositoryConflict("knowledge-vault clone is detached")
        local = self.current_commit(root, deadline=deadline)
        if local != commit_id:
            raise VaultRepositoryConflict("knowledge-vault commit changed before push")
        try:
            self._git(
                root,
                "push",
                "--porcelain",
                "origin",
                f"HEAD:refs/heads/{branch}",
                failure_type=VaultRepositoryConflict,
                pre_dispatch_failure_type=VaultPushPreDispatchFailure,
                started_failure_type=VaultPushUnknownOutcome,
                deadline=deadline,
            )
        except VaultPushPreDispatchFailure:
            raise
        except VaultPushUnknownOutcome:
            raise
        except VaultRepositoryConflict as exc:
            message = str(exc).casefold()
            if "non-fast-forward" in message or "rejected" in message:
                raise VaultRepositoryConflict(
                    "knowledge-vault remote rejected a non-fast-forward push"
                ) from exc
            raise VaultPushUnknownOutcome(
                "knowledge-vault push outcome is unknown"
            ) from exc

    def _git(
        self,
        root: Path,
        *arguments: str,
        failure_type: type[VaultSynchronizationError],
        pre_dispatch_failure_type: type[VaultSynchronizationError] | None = None,
        started_failure_type: type[VaultSynchronizationError] | None = None,
        deadline: float | None,
        environment: Mapping[str, str] | None = None,
    ) -> CompletedProcess[str]:
        timeout = 15.0
        if deadline is not None:
            timeout = _remaining_seconds(deadline, failure_type)
        try:
            completed = self._run_process(
                [str(self._git_executable), "-C", str(root), *arguments],
                check=False,
                stdin=DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._environment if environment is None else environment,
            )
        except OSError as exc:
            failure = pre_dispatch_failure_type or failure_type
            raise failure("knowledge-vault Git is unavailable") from exc
        except (TimeoutError, TimeoutExpired) as exc:
            failure = started_failure_type or failure_type
            raise failure("knowledge-vault synchronization timed out") from exc
        if deadline is not None:
            _remaining_seconds(deadline, failure_type)
        if completed.returncode != 0:
            detail = " ".join(
                part.strip()
                for part in (completed.stderr, completed.stdout)
                if part.strip()
            )[:200]
            message = "knowledge-vault synchronization failed"
            if detail:
                message = f"{message}: {detail}"
            raise failure_type(message)
        return completed


class ControlledVaultSynchronizer:
    """Deterministic synchronizer used by the control-plane contract tests."""

    def __init__(
        self,
        *,
        last_synchronized_at: datetime | None = None,
        failure: str | VaultSynchronizationError | None = None,
        clean: bool = True,
    ) -> None:
        self._last_synchronized_at = last_synchronized_at
        self.failure = (
            VaultRemoteUnavailable(failure) if isinstance(failure, str) else failure
        )
        self.clean = clean
        self.calls = 0

    @property
    def last_synchronized_at(self) -> datetime | None:
        return self._last_synchronized_at

    def is_clean(self, _root: Path, *, deadline: float | None = None) -> bool:
        return self.clean

    def synchronize(
        self, _root: Path, *, now: datetime, deadline: float | None = None
    ) -> datetime:
        if deadline is not None:
            _remaining_seconds(deadline, VaultRepositoryConflict)
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        self._last_synchronized_at = now
        return now


__all__ = ["ControlledVaultSynchronizer", "SubprocessVaultSynchronizer"]

"""Repository adapters for synchronized vault reads and exact writes."""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired, run
from tempfile import TemporaryDirectory

from .common import _remaining_seconds
from .errors import (
    VaultPushPreDispatchFailure,
    VaultPushUnknownOutcome,
    VaultRemoteUnavailable,
    VaultRepositoryConflict,
    VaultWriteConflict,
    VaultWriteRemoteUnavailable,
    VaultWriteRepositoryError,
)
from .write_models import VaultWriteChange
from .write_policy import (
    _commit_hash,
    render_vault_unified_diff,
)


class SubprocessVaultRepository:
    """Bounded Git implementation used by the production vault service."""

    def __init__(
        self,
        *,
        ssh_executable: Path,
        ssh_config_path: Path,
        known_hosts_path: Path,
        git_executable: Path = Path("/usr/bin/git"),
        proxy_command: Sequence[str] | None = None,
    ) -> None:
        for path, name in (
            (git_executable, "git_executable"),
            (ssh_executable, "ssh_executable"),
            (ssh_config_path, "ssh_config_path"),
            (known_hosts_path, "known_hosts_path"),
        ):
            if not path.is_absolute():
                raise ValueError(f"{name} must be an absolute path")
        if proxy_command is not None and (
            not proxy_command or any(not item for item in proxy_command)
        ):
            raise ValueError("vault proxy command must be a non-empty argument list")
        self._git = git_executable
        ssh_arguments = [
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
        ]
        if proxy_command is not None:
            ssh_arguments.extend(("-o", f"ProxyCommand={shlex.join(proxy_command)}"))
        self._environment = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": "/dev/null",
            "GIT_SSH_COMMAND": shlex.join(ssh_arguments),
        }
        self._last_synchronized_at: datetime | None = None

    @property
    def last_synchronized_at(self) -> datetime | None:
        return self._last_synchronized_at

    def is_clean(self, root: Path, *, deadline: float | None = None) -> bool:
        result = self._run(
            root, ("status", "--porcelain=v1", "--untracked-files=all"), deadline
        )
        return not result.stdout.strip()

    def synchronize(
        self, root: Path, *, now: datetime, deadline: float | None = None
    ) -> datetime:
        if not self.is_clean(root, deadline=deadline):
            raise VaultRepositoryConflict("knowledge-vault clone must be clean")
        try:
            self._run(root, ("fetch", "--prune", "origin"), deadline)
            self._run(root, ("merge", "--ff-only", "@{upstream}"), deadline)
        except VaultWriteRepositoryError as exc:
            raise VaultRemoteUnavailable(
                "knowledge-vault synchronization failed"
            ) from exc
        self._last_synchronized_at = now
        return now

    def current_commit(self, root: Path, *, deadline: float | None = None) -> str:
        return self._run(root, ("rev-parse", "HEAD"), deadline).stdout.strip()

    def validate_remote(
        self, root: Path, expected_remote: str, *, deadline: float | None = None
    ) -> None:
        if not expected_remote or expected_remote.strip() != expected_remote:
            raise ValueError("expected vault remote must be canonical")
        actual_remote = self._run(
            root, ("remote", "get-url", "origin"), deadline
        ).stdout.strip()
        if actual_remote != expected_remote:
            raise VaultRepositoryConflict(
                "knowledge-vault origin differs from active configuration"
            )

    def fetch_remote_commit(self, root: Path, *, deadline: float | None = None) -> str:
        try:
            self._run(root, ("fetch", "--prune", "origin"), deadline)
            return self._run(
                root, ("rev-parse", "@{upstream}"), deadline
            ).stdout.strip()
        except VaultWriteRepositoryError as exc:
            raise VaultWriteRemoteUnavailable("vault remote is unavailable") from exc

    def render_diff(
        self,
        root: Path,
        originals: Mapping[str, str | None],
        changes: Mapping[str, str],
        *,
        deadline: float | None = None,
    ) -> str:
        with TemporaryDirectory(prefix="jarvis-vault-diff-") as directory:
            temporary = Path(directory) / "vault"
            self._run(
                Path(directory),
                ("clone", "--shared", "--quiet", str(root), str(temporary)),
                deadline,
            )
            for relative, content in changes.items():
                path = temporary / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8", newline="")
            self.stage(temporary, tuple(changes), deadline=deadline)
            return self.staged_diff(temporary, deadline=deadline) or ""

    def stage(
        self, root: Path, paths: Sequence[str], *, deadline: float | None = None
    ) -> None:
        self._run(root, ("add", "--", *paths), deadline)

    def staged_diff(self, root: Path, *, deadline: float | None = None) -> str | None:
        result = self._run(
            root,
            ("diff", "--cached", "--no-ext-diff", "--no-color", "--"),
            deadline,
        )
        return result.stdout or None

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
        environment = {
            **self._environment,
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
        }
        self._run(
            root,
            ("commit", "-m", subject, "-m", body),
            deadline,
            env=environment,
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
        if self.fetch_remote_commit(root, deadline=deadline) != expected_base:
            raise VaultWriteConflict("remote changed after approval")
        upstream = self._run(
            root, ("rev-parse", "--abbrev-ref", "@{upstream}"), deadline
        ).stdout.strip()
        branch = upstream.removeprefix("origin/")
        try:
            self._run(
                root,
                ("push", "origin", f"{commit_id}:refs/heads/{branch}"),
                deadline,
                push=True,
            )
        except VaultPushPreDispatchFailure:
            raise
        except VaultPushUnknownOutcome:
            raise
        except VaultWriteRepositoryError as exc:
            raise VaultWriteConflict("vault push was rejected") from exc

    def _run_process(
        self, command: Sequence[str], **kwargs: object
    ) -> CompletedProcess[str]:
        check = kwargs.pop("check", False)
        return run(command, check=check, **kwargs)  # type: ignore[arg-type]

    def _run(
        self,
        root: Path,
        arguments: Sequence[str],
        deadline: float | None,
        *,
        env: Mapping[str, str] | None = None,
        push: bool = False,
    ) -> CompletedProcess[str]:
        timeout = (
            20.0
            if deadline is None
            else _remaining_seconds(deadline, VaultWriteRepositoryError)
        )
        try:
            completed = self._run_process(
                (str(self._git), "-C", str(root), *arguments),
                stdin=None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
                shell=False,
                env=dict(env or self._environment),
            )
        except OSError as exc:
            if push:
                raise VaultPushPreDispatchFailure("vault push did not start") from exc
            raise VaultWriteRepositoryError("vault Git process did not start") from exc
        except TimeoutExpired as exc:
            if push:
                raise VaultPushUnknownOutcome("vault push outcome is unknown") from exc
            raise VaultWriteRepositoryError("vault Git process timed out") from exc
        if completed.returncode != 0:
            raise VaultWriteRepositoryError(
                f"vault Git command failed with exit code {completed.returncode}"
            )
        return completed


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
        self._rendered_diff: str | None = None
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

    def render_diff(
        self,
        _root: Path,
        originals: Mapping[str, str | None],
        changes: Mapping[str, str],
        *,
        deadline: float | None = None,
    ) -> str:
        normalized = tuple(
            VaultWriteChange(
                path=path,
                operation="modify" if originals[path] is not None else "create",
                content=content,
            )
            for path, content in sorted(changes.items())
        )
        self._rendered_diff = render_vault_unified_diff(originals, normalized)
        return self._rendered_diff

    def stage(
        self, _root: Path, paths: Sequence[str], *, deadline: float | None = None
    ) -> None:
        if self.stage_failure is not None:
            raise VaultWriteRepositoryError(self.stage_failure)
        self.stage_calls.append(tuple(paths))
        self.clean = False

    def staged_diff(self, _root: Path, *, deadline: float | None = None) -> str | None:
        return (
            self.staged_diff_override
            if self.staged_diff_override is not None
            else self._rendered_diff
        )

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
            raise VaultPushPreDispatchFailure("remote push was unavailable")
        if self.push_failure is not None:
            raise VaultWriteConflict(self.push_failure)
        if self.remote_commit != expected_base:
            raise VaultWriteConflict("remote rejected the non-fast-forward push")
        self.remote_commit = commit_id

    def advance_remote(self, commit_id: str) -> None:
        self.remote_commit = _commit_hash(commit_id)


__all__ = ["ControlledVaultWriteRepository", "SubprocessVaultRepository"]

"""Production no-shell Git edge for the isolated knowledge-vault service."""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired, run
from tempfile import TemporaryDirectory

from .knowledge_vault import (
    VaultPushPreDispatchFailure,
    VaultPushUnknownOutcome,
    VaultRemoteUnavailable,
    VaultRepositoryConflict,
)
from .knowledge_vault_common import _remaining_seconds
from .knowledge_vault_writes import (
    VaultWriteConflict,
    VaultWriteRemoteUnavailable,
    VaultWriteRepositoryError,
)


class SubprocessVaultRepository:
    """Bounded Git implementation shared by vault reads and exact writes."""

    def __init__(
        self,
        *,
        ssh_executable: Path,
        ssh_config_path: Path,
        known_hosts_path: Path,
        git_executable: Path = Path("/usr/bin/git"),
    ) -> None:
        for path, name in (
            (git_executable, "git_executable"),
            (ssh_executable, "ssh_executable"),
            (ssh_config_path, "ssh_config_path"),
            (known_hosts_path, "known_hosts_path"),
        ):
            if not path.is_absolute():
                raise ValueError(f"{name} must be an absolute path")
        self._git = git_executable
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
            root, ("commit", "-m", subject, "-m", body), deadline, env=environment
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
            completed = run(
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

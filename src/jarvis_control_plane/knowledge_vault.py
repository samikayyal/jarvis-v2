"""Bounded, deterministic reads from the dedicated knowledge-vault clone.

The connector deliberately owns neither Git credentials nor write authority.  A
deployment supplies a dedicated clone and a narrowly configured synchronizer;
this module only permits ordinary Markdown reads below that canonical root.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from subprocess import DEVNULL, CompletedProcess, TimeoutExpired, run
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .knowledge_vault_common import (
    _EXCLUDED_TOP_LEVEL_DIRECTORIES,
    _remaining_seconds,
)

_MAX_QUERY_CHARS = 200
_MAX_RETURNED_EXCERPTS = 8
_MAX_EXCERPT_CHARS = 600
_MAX_NOTES_INSPECTED = 128
_MAX_BYTES_PER_NOTE = 64 * 1024
_MAX_TOTAL_BYTES_SCANNED = 512 * 1024
_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_UNIFIED_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


class VaultReadError(Exception):
    """A vault read was invalid, unavailable, or outside the configured boundary."""

    _default_code = "read_failed"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        selected_code = code or self._default_code
        if not selected_code or selected_code.strip() != selected_code:
            raise ValueError("vault read error code must be canonical")
        self.code = selected_code


class VaultSynchronizationError(VaultReadError):
    """The dedicated clone could not be synchronized safely."""

    _default_code = "synchronization_failed"


class VaultRemoteUnavailable(VaultSynchronizationError):
    """The remote could not be reached; a known clean clone may be read stale."""

    _default_code = "remote_unavailable"


class VaultRepositoryConflict(VaultSynchronizationError):
    """The local clone requires explicit administrator recovery."""

    _default_code = "recovery_required"


class VaultPushPreDispatchFailure(VaultSynchronizationError):
    """The push process did not start, so no remote update could have occurred."""

    _default_code = "push_not_started"


class VaultPushUnknownOutcome(VaultSynchronizationError):
    """The push process started, but its remote side effect cannot be established."""

    _default_code = "push_outcome_unknown"


class VaultSynchronizationMetadataStore(Protocol):
    """Authoritative durable metadata for the last successful vault sync."""

    def load_knowledge_vault_synchronized_at(self) -> datetime | None: ...

    def save_knowledge_vault_synchronized_at(
        self, synchronized_at: datetime
    ) -> None: ...


class VaultReadInput(BaseModel):
    """One closed exact-note or deterministic-search request."""

    model_config = ConfigDict(extra="forbid")

    query: str | None = Field(default=None, min_length=1, max_length=_MAX_QUERY_CHARS)
    path: str | None = Field(default=None, min_length=1, max_length=512)
    title: str | None = Field(default=None, min_length=1, max_length=_MAX_QUERY_CHARS)

    @model_validator(mode="after")
    def exactly_one_selector(self) -> VaultReadInput:
        selectors = (self.query, self.path, self.title)
        if sum(value is not None for value in selectors) != 1:
            raise ValueError("exactly one of query, path, or title is required")
        for value in selectors:
            if value is not None and value.strip() != value:
                raise ValueError("vault read selectors must be canonical strings")
        return self


class VaultExcerpt(BaseModel):
    """A bounded, line-addressable note excerpt safe to give the planner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=512)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=_MAX_EXCERPT_CHARS)
    complete: bool = False
    ends_with_newline: bool | None = None


class KnowledgeVaultReadResult(BaseModel):
    """The only vault content shape available through the orchestration tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["knowledge_vault"] = "knowledge_vault"
    synchronized_at: datetime
    stale_warning: str | None = Field(default=None, max_length=200)
    excerpts: tuple[VaultExcerpt, ...] = Field(max_length=_MAX_RETURNED_EXCERPTS)


class VaultSynchronizer(Protocol):
    """The bounded Git process that owns synchronization, not content reads."""

    @property
    def last_synchronized_at(self) -> datetime | None: ...

    def is_clean(self, root: Path, *, deadline: float | None = None) -> bool: ...

    def synchronize(
        self, root: Path, *, now: datetime, deadline: float | None = None
    ) -> datetime: ...


class SubprocessVaultSynchronizer:
    """A no-shell Git synchronizer for the dedicated service-account clone.

    The configured SSH config must contain the repository-scoped identity. This
    process forces that config, disables interactive prompts, and pins host-key
    verification instead of inheriting a possibly permissive user Git setup.
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
        """Render a canonical patch through the same Git edge as verification.

        The temporary index keeps proposal creation side-effect free while making
        Git, rather than a second diff algorithm, the authority for hunk headers,
        newline markers, and diff attributes.
        """

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
        """Push the checked-out branch normally; force and history rewrites are absent."""

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

    def is_clean(self, root: Path, *, deadline: float | None = None) -> bool:
        return self.clean

    def synchronize(
        self, root: Path, *, now: datetime, deadline: float | None = None
    ) -> datetime:
        if deadline is not None:
            _remaining_seconds(deadline, VaultRepositoryConflict)
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        self._last_synchronized_at = now
        return now


class KnowledgeVaultConnector:
    """Read-only connector for one configured, synchronized vault clone."""

    def __init__(
        self,
        *,
        root: Path,
        synchronizer: VaultSynchronizer,
        now: Callable[[], datetime],
        read_timeout_seconds: float = 2.0,
    ) -> None:
        resolved_root = root.resolve(strict=True)
        if not resolved_root.is_dir() or root.is_symlink():
            raise ValueError("knowledge-vault root must be a real directory")
        self._root = resolved_root
        self._synchronizer = synchronizer
        self._now = now
        if (
            isinstance(read_timeout_seconds, bool)
            or not isinstance(read_timeout_seconds, (float, int))
            or not 0 < read_timeout_seconds <= 2.0
        ):
            raise ValueError("read_timeout_seconds must be within 0 and 2 seconds")
        self._read_timeout_seconds = float(read_timeout_seconds)

    def read(
        self, request: VaultReadInput, *, deadline: float | None = None
    ) -> KnowledgeVaultReadResult:
        """Synchronize first, then perform one deterministic, bounded local read."""

        deadline = (
            deadline
            if deadline is not None
            else monotonic() + self._read_timeout_seconds
        )
        _remaining_seconds(deadline, VaultRepositoryConflict)
        self._require_clean_clone(deadline=deadline)
        now = self._now()
        try:
            synchronized_at = self._synchronizer.synchronize(
                self._root, now=now, deadline=deadline
            )
            stale_warning = None
        except VaultRemoteUnavailable as exc:
            synchronized_at = self._synchronizer.last_synchronized_at
            if synchronized_at is None:
                raise VaultReadError(
                    "knowledge-vault reads require a clean synchronized clone",
                    code="clean_snapshot_unavailable",
                ) from exc
            self._require_clean_clone(deadline=deadline)
            stale_warning = _stale_warning(now - synchronized_at)
        except VaultSynchronizationError as exc:
            raise VaultReadError(
                "knowledge-vault synchronization requires explicit recovery",
                code="recovery_required",
            ) from exc

        read_budget = _VaultReadBudget(deadline=deadline)
        excerpts = self._select_excerpts(request, read_budget)
        return KnowledgeVaultReadResult(
            synchronized_at=synchronized_at,
            stale_warning=stale_warning,
            excerpts=tuple(excerpts[:_MAX_RETURNED_EXCERPTS]),
        )

    def _require_clean_clone(self, *, deadline: float) -> None:
        try:
            is_clean = self._synchronizer.is_clean(self._root, deadline=deadline)
        except VaultSynchronizationError as exc:
            raise VaultReadError(
                "knowledge-vault synchronization requires explicit recovery",
                code="recovery_required",
            ) from exc
        if not is_clean:
            raise VaultReadError(
                "knowledge-vault reads require a clean synchronized clone",
                code="dirty_snapshot",
            )

    def as_bounded_read_tool(self):
        """Return the closed orchestration tool without importing Agents at module load."""

        from .orchestration import BoundedReadTool

        def handler(
            _request: object, typed_input: BaseModel, deadline: float
        ) -> BaseModel:
            if not isinstance(typed_input, VaultReadInput):
                raise TypeError("read_knowledge_vault received an invalid input model")
            return self.read(typed_input, deadline=deadline)

        return BoundedReadTool(
            name="read_knowledge_vault",
            description=(
                "Read or search the configured knowledge vault locally. Results are "
                "bounded excerpts; exact-path small-note results explicitly report "
                "whether the content is complete and whether it ends with a newline."
            ),
            input_model=VaultReadInput,
            output_model=KnowledgeVaultReadResult,
            handler=handler,
            timeout_seconds=self._read_timeout_seconds,
        )

    def _select_excerpts(
        self, request: VaultReadInput, budget: _VaultReadBudget
    ) -> list[VaultExcerpt]:
        if request.path is not None:
            note = self._ordinary_note_for_path(request.path)
            return [self._path_excerpt(note, request.path, budget)]

        notes = self._ordinary_notes(budget)
        if request.title is not None:
            matches = [
                note
                for note in notes
                if _note_title(budget.read(note)) == request.title
            ]
            if len(matches) != 1:
                raise VaultReadError(
                    "knowledge-vault title is not unambiguous",
                    code="ambiguous_selector",
                )
            return [self._excerpt(matches[0], request.title, budget)]

        assert request.query is not None
        query = request.query.casefold()
        matches: list[Path] = []
        for note in notes:
            content = budget.read(note)
            if query in self._relative(note).casefold() or query in content.casefold():
                matches.append(note)
                matches.extend(self._link_targets(note, query, budget))
        return [
            self._excerpt(note, request.query, budget)
            for note in _unique_paths(matches)[:_MAX_RETURNED_EXCERPTS]
        ]

    def _ordinary_notes(self, budget: _VaultReadBudget) -> list[Path]:
        notes: list[Path] = []
        for directory, directories, filenames in os.walk(self._root):
            budget.require_time()
            directory_path = Path(directory)
            directories[:] = [
                name
                for name in directories
                if not name.startswith(".")
                and name not in _EXCLUDED_TOP_LEVEL_DIRECTORIES
                and not os.path.lexists(directory_path / name / ".git")
            ]
            for filename in sorted(filenames):
                budget.require_time()
                path = directory_path / filename
                if path.suffix == ".md" and self._is_ordinary_note(path):
                    notes.append(path)
                    if len(notes) > _MAX_NOTES_INSPECTED:
                        raise VaultReadError(
                            "knowledge-vault search exceeds the note-inspection limit"
                        )
        return sorted(notes, key=self._relative)

    def _ordinary_note_for_path(self, value: str) -> Path:
        raw_path = PurePosixPath(value)
        if (
            "\\" in value
            or raw_path.is_absolute()
            or PureWindowsPath(value).is_absolute()
            or any(part in {".", ".."} for part in value.split("/"))
            or raw_path.as_posix() != value
        ):
            raise VaultReadError(
                "path is not an ordinary knowledge-vault note",
                code="outside_root",
            )
        candidate = self._root.joinpath(*raw_path.parts)
        if not self._is_ordinary_note(candidate) or self._relative(candidate) != value:
            parts = raw_path.parts
            if any(part.startswith(".") for part in parts) or any(
                part in _EXCLUDED_TOP_LEVEL_DIRECTORIES for part in parts
            ):
                code = "excluded_path"
            elif raw_path.suffix != ".md":
                code = "unsupported_file_type"
            elif candidate.is_symlink() or any(
                parent.is_symlink()
                for parent in candidate.parents
                if parent != self._root
            ):
                code = "excluded_path"
            else:
                code = "path_not_found"
            raise VaultReadError(
                "path is not an ordinary knowledge-vault note", code=code
            )
        return candidate

    def _is_ordinary_note(self, path: Path) -> bool:
        try:
            resolved = path.resolve(strict=True)
            relative = resolved.relative_to(self._root)
        except (OSError, ValueError):
            return False
        if path.is_symlink() or any(
            parent.is_symlink() for parent in path.parents if parent != self._root
        ):
            return False
        if path.suffix != ".md" or not path.is_file():
            return False
        parts = relative.parts
        if not parts or any(part.startswith(".") for part in parts):
            return False
        if any(part in _EXCLUDED_TOP_LEVEL_DIRECTORIES for part in parts):
            return False
        return not any(
            os.path.lexists(directory / ".git")
            for directory in (
                self._root.joinpath(*parts[:index]) for index in range(1, len(parts))
            )
        )

    def _relative(self, path: Path) -> str:
        return path.resolve(strict=True).relative_to(self._root).as_posix()

    def _excerpt(
        self, note: Path, needle: str, budget: _VaultReadBudget
    ) -> VaultExcerpt:
        lines = budget.read(note).splitlines()
        needle_folded = needle.casefold()
        match_index = next(
            (
                index
                for index, line in enumerate(lines)
                if needle_folded in line.casefold()
            ),
            0,
        )
        start = max(0, match_index - 1)
        selected = lines[start : min(len(lines), match_index + 4)]
        text = "\n".join(selected)[:_MAX_EXCERPT_CHARS]
        if not text:
            text = "(empty Markdown note)"
        return VaultExcerpt(
            path=self._relative(note),
            start_line=start + 1,
            end_line=start + len(selected),
            text=text,
        )

    def _path_excerpt(
        self, note: Path, path: str, budget: _VaultReadBudget
    ) -> VaultExcerpt:
        """Return a complete small exact-path note without widening searches."""

        content = budget.read(note)
        if content and len(content) <= _MAX_EXCERPT_CHARS:
            return VaultExcerpt(
                path=self._relative(note),
                start_line=1,
                end_line=max(1, len(content.splitlines())),
                text=content,
                complete=True,
                ends_with_newline=content.endswith("\n"),
            )
        return self._excerpt(note, path, budget)

    def _link_targets(
        self, note: Path, query: str, budget: _VaultReadBudget
    ) -> list[Path]:
        """Resolve only matching ordinary local links; external targets are ignored."""

        targets: list[Path] = []
        for line in budget.read(note).splitlines():
            if query not in line.casefold():
                continue
            for match in _WIKILINK.finditer(line):
                if query not in match.group(0).casefold():
                    continue
                target = match.group(1).split("|", 1)[0].split("#", 1)[0]
                targets.extend(self._resolve_link_target(note, target, wikilink=True))
            for match in _MARKDOWN_LINK.finditer(line):
                if query not in match.group(0).casefold():
                    continue
                target = match.group(1).split("#", 1)[0]
                targets.extend(self._resolve_link_target(note, target, wikilink=False))
        return targets

    def _resolve_link_target(
        self, source: Path, target: str, *, wikilink: bool
    ) -> list[Path]:
        if not target or ":" in target or "\\" in target:
            return []
        raw_target = PurePosixPath(target)
        if raw_target.is_absolute():
            return []
        suffix_target = (
            raw_target if raw_target.suffix == ".md" else raw_target.with_suffix(".md")
        )
        candidates = (
            [self._root / suffix_target]
            if not wikilink or len(suffix_target.parts) > 1
            else [self._root / suffix_target, source.parent / suffix_target]
        )
        if not wikilink:
            candidates = [source.parent / suffix_target]
        return [
            candidate for candidate in candidates if self._is_ordinary_note(candidate)
        ]


@dataclass(slots=True)
class _VaultReadBudget:
    """Per-read byte, note, and deadline limits for untrusted vault content."""

    deadline: float
    inspected_notes: int = 0
    scanned_bytes: int = 0
    contents: dict[Path, str] = field(default_factory=dict)

    def require_time(self) -> None:
        _remaining_seconds(self.deadline, VaultReadError)

    def read(self, note: Path) -> str:
        cached = self.contents.get(note)
        if cached is not None:
            return cached
        self.require_time()
        if self.inspected_notes >= _MAX_NOTES_INSPECTED:
            raise VaultReadError(
                "knowledge-vault search exceeds the note-inspection limit"
            )
        self.inspected_notes += 1
        try:
            with note.open("rb") as stream:
                content = stream.read(_MAX_BYTES_PER_NOTE + 1)
        except OSError as exc:
            raise VaultReadError("knowledge-vault note could not be read") from exc
        self.require_time()
        if len(content) > _MAX_BYTES_PER_NOTE:
            raise VaultReadError("knowledge-vault note exceeds the per-note byte limit")
        if self.scanned_bytes + len(content) > _MAX_TOTAL_BYTES_SCANNED:
            raise VaultReadError("knowledge-vault search exceeds the total byte limit")
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise VaultReadError("knowledge-vault note is not valid UTF-8") from exc
        self.scanned_bytes += len(content)
        self.contents[note] = decoded
        return decoded


def _note_title(content: str) -> str | None:
    lines = content.splitlines()
    if lines and lines[0] == "---":
        for line in lines[1:]:
            if line == "---":
                break
            if line.startswith("title:"):
                return line.removeprefix("title:").strip().strip("\"'")
    for line in lines:
        if line.startswith("# "):
            return line.removeprefix("# ").strip()
    return None


def _stale_warning(age) -> str:
    minutes = max(0, int(age.total_seconds() // 60))
    hours, minutes = divmod(minutes, 60)
    age_text = f"{hours}h {minutes}m" if hours else f"{minutes}m"
    return (
        "Knowledge-vault synchronization is unavailable; results may be stale "
        f"(age: {age_text})."
    )


def _unique_paths(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _normalise_staged_diff(output: str) -> str:
    """Remove Git-only headers while retaining every unified-diff line."""

    chunks: list[list[str]] = []
    current: list[str] = []
    ignored_prefixes = (
        "diff --git ",
        "index ",
        "new file mode ",
        "old mode ",
        "new mode ",
        "deleted file mode ",
        "similarity index ",
        "dissimilarity index ",
        "rename from ",
        "rename to ",
    )
    lines = output.replace("\r\n", "\n").splitlines()
    hunk_lines_remaining = 0
    for line in lines:
        is_file_header = line.startswith("--- ") and hunk_lines_remaining == 0
        if is_file_header:
            if current:
                chunks.append(current)
            current = [line]
            continue
        if not current:
            continue
        if line.startswith(ignored_prefixes):
            continue
        current.append(line)
        if line.startswith("@@ "):
            hunk = _UNIFIED_HUNK.match(line)
            if hunk is not None:
                old_count = int(hunk.group(1) or "1")
                new_count = int(hunk.group(2) or "1")
                hunk_lines_remaining = old_count + new_count
        elif hunk_lines_remaining and line and line[0] in " +-":
            hunk_lines_remaining -= 1
    if current:
        chunks.append(current)
    if not chunks:
        return ""
    return "\n".join("\n".join(chunk) for chunk in chunks).rstrip("\n") + "\n"

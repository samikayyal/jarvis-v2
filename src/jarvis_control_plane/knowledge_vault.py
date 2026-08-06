"""Bounded, deterministic reads from the dedicated knowledge-vault clone.

The connector deliberately owns neither Git credentials nor write authority.  A
deployment supplies a dedicated clone and a narrowly configured synchronizer;
this module only permits ordinary Markdown reads below that canonical root.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from subprocess import DEVNULL, CompletedProcess, TimeoutExpired, run
from time import monotonic
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

_MAX_QUERY_CHARS = 200
_MAX_RETURNED_EXCERPTS = 8
_MAX_EXCERPT_CHARS = 600
_MAX_NOTES_INSPECTED = 128
_MAX_BYTES_PER_NOTE = 64 * 1024
_MAX_TOTAL_BYTES_SCANNED = 512 * 1024
_EXCLUDED_TOP_LEVEL_DIRECTORIES = frozenset(
    {"attachments", "plugins", "templates", "themes", "trash"}
)
_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


class VaultReadError(Exception):
    """A vault read was invalid, unavailable, or outside the configured boundary."""


class VaultSynchronizationError(VaultReadError):
    """The dedicated clone could not be synchronized safely."""


class VaultRemoteUnavailable(VaultSynchronizationError):
    """The remote could not be reached; a known clean clone may be read stale."""


class VaultRepositoryConflict(VaultSynchronizationError):
    """The local clone requires explicit administrator recovery."""


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

    def _git(
        self,
        root: Path,
        *arguments: str,
        failure_type: type[VaultSynchronizationError],
        deadline: float | None,
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
                env=self._environment,
            )
        except OSError as exc:
            raise failure_type("knowledge-vault Git is unavailable") from exc
        except (TimeoutError, TimeoutExpired) as exc:
            raise failure_type("knowledge-vault synchronization timed out") from exc
        if deadline is not None:
            _remaining_seconds(deadline, failure_type)
        if completed.returncode != 0:
            raise failure_type("knowledge-vault synchronization failed")
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
                    "knowledge-vault reads require a clean synchronized clone"
                ) from exc
            self._require_clean_clone(deadline=deadline)
            stale_warning = _stale_warning(now - synchronized_at)
        except VaultSynchronizationError as exc:
            raise VaultReadError(
                "knowledge-vault synchronization requires explicit recovery"
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
                "knowledge-vault synchronization requires explicit recovery"
            ) from exc
        if not is_clean:
            raise VaultReadError(
                "knowledge-vault reads require a clean synchronized clone"
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
                "bounded excerpts and may include a visible stale-read warning."
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
            return [self._excerpt(note, request.path, budget)]

        notes = self._ordinary_notes(budget)
        if request.title is not None:
            matches = [
                note
                for note in notes
                if _note_title(budget.read(note)) == request.title
            ]
            if len(matches) != 1:
                raise VaultReadError("knowledge-vault title is not unambiguous")
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
            raise VaultReadError("path is not an ordinary knowledge-vault note")
        candidate = self._root.joinpath(*raw_path.parts)
        if not self._is_ordinary_note(candidate) or self._relative(candidate) != value:
            raise VaultReadError("path is not an ordinary knowledge-vault note")
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


def _remaining_seconds(deadline: float, error_type: type[Exception]) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise error_type("knowledge-vault read exceeded its overall deadline")
    return remaining


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

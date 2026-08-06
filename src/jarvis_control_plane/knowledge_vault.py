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
from datetime import datetime
from pathlib import Path, PurePosixPath
from subprocess import DEVNULL, CompletedProcess, run
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

_MAX_QUERY_CHARS = 200
_MAX_RESULTS = 8
_MAX_EXCERPT_CHARS = 600
_EXCLUDED_TOP_LEVEL_DIRECTORIES = frozenset(
    {"attachments", "plugins", "templates", "themes", "trash"}
)
_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


class VaultReadError(Exception):
    """A vault read was invalid, unavailable, or outside the configured boundary."""


class VaultSynchronizationError(VaultReadError):
    """The dedicated clone could not be synchronized safely."""


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
    excerpts: tuple[VaultExcerpt, ...] = Field(max_length=_MAX_RESULTS)


class VaultSynchronizer(Protocol):
    """The bounded Git process that owns synchronization, not content reads."""

    @property
    def last_synchronized_at(self) -> datetime | None: ...

    def is_clean(self, root: Path) -> bool: ...

    def synchronize(self, root: Path, *, now: datetime) -> datetime: ...


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

    def is_clean(self, root: Path) -> bool:
        return self._git(root, "status", "--porcelain").stdout.strip() == ""

    def synchronize(self, root: Path, *, now: datetime) -> datetime:
        if not self.is_clean(root):
            raise VaultSynchronizationError("knowledge-vault clone is not clean")
        self._git(root, "fetch", "--prune", "--no-tags", "origin")
        self._git(root, "merge", "--ff-only", "FETCH_HEAD")
        self._last_synchronized_at = now
        return now

    def _git(self, root: Path, *arguments: str):
        try:
            completed = self._run_process(
                [str(self._git_executable), "-C", str(root), *arguments],
                check=False,
                stdin=DEVNULL,
                capture_output=True,
                text=True,
                timeout=15,
                env=self._environment,
            )
        except OSError as exc:
            raise VaultSynchronizationError(
                "knowledge-vault Git is unavailable"
            ) from exc
        except TimeoutError as exc:
            raise VaultSynchronizationError(
                "knowledge-vault synchronization timed out"
            ) from exc
        if completed.returncode != 0:
            raise VaultSynchronizationError("knowledge-vault synchronization failed")
        return completed


class ControlledVaultSynchronizer:
    """Deterministic synchronizer used by the control-plane contract tests."""

    def __init__(
        self,
        *,
        last_synchronized_at: datetime | None = None,
        failure: str | None = None,
        clean: bool = True,
    ) -> None:
        self._last_synchronized_at = last_synchronized_at
        self.failure = failure
        self.clean = clean
        self.calls = 0

    @property
    def last_synchronized_at(self) -> datetime | None:
        return self._last_synchronized_at

    def is_clean(self, root: Path) -> bool:
        return self.clean

    def synchronize(self, root: Path, *, now: datetime) -> datetime:
        self.calls += 1
        if self.failure is not None:
            raise VaultSynchronizationError(self.failure)
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
    ) -> None:
        resolved_root = root.resolve(strict=True)
        if not resolved_root.is_dir() or root.is_symlink():
            raise ValueError("knowledge-vault root must be a real directory")
        self._root = resolved_root
        self._synchronizer = synchronizer
        self._now = now

    def read(self, request: VaultReadInput) -> KnowledgeVaultReadResult:
        """Synchronize first, then perform one deterministic, bounded local read."""

        if not self._synchronizer.is_clean(self._root):
            raise VaultReadError(
                "knowledge-vault reads require a clean synchronized clone"
            )
        now = self._now()
        try:
            synchronized_at = self._synchronizer.synchronize(self._root, now=now)
            stale_warning = None
        except VaultSynchronizationError as exc:
            synchronized_at = self._synchronizer.last_synchronized_at
            if synchronized_at is None or not self._synchronizer.is_clean(self._root):
                raise VaultReadError(
                    "knowledge-vault reads require a clean synchronized clone"
                ) from exc
            stale_warning = _stale_warning(now - synchronized_at)

        excerpts = self._select_excerpts(request)
        return KnowledgeVaultReadResult(
            synchronized_at=synchronized_at,
            stale_warning=stale_warning,
            excerpts=tuple(excerpts[:_MAX_RESULTS]),
        )

    def as_bounded_read_tool(self):
        """Return the closed orchestration tool without importing Agents at module load."""

        from .orchestration import BoundedReadTool

        def handler(_request: object, typed_input: BaseModel) -> BaseModel:
            if not isinstance(typed_input, VaultReadInput):
                raise TypeError("read_knowledge_vault received an invalid input model")
            return self.read(typed_input)

        return BoundedReadTool(
            name="read_knowledge_vault",
            description=(
                "Read or search the configured knowledge vault locally. Results are "
                "bounded excerpts and may include a visible stale-read warning."
            ),
            input_model=VaultReadInput,
            output_model=KnowledgeVaultReadResult,
            handler=handler,
        )

    def _select_excerpts(self, request: VaultReadInput) -> list[VaultExcerpt]:
        if request.path is not None:
            note = self._ordinary_note_for_path(request.path)
            return [self._excerpt(note, request.path)]

        notes = self._ordinary_notes()
        if request.title is not None:
            matches = [note for note in notes if _note_title(note) == request.title]
            if len(matches) != 1:
                raise VaultReadError("knowledge-vault title is not unambiguous")
            return [self._excerpt(matches[0], request.title)]

        assert request.query is not None
        query = request.query.casefold()
        matches: list[Path] = []
        for note in notes:
            content = note.read_text(encoding="utf-8")
            if query in self._relative(note).casefold() or query in content.casefold():
                matches.append(note)
                matches.extend(self._link_targets(note, query))
        return [self._excerpt(note, request.query) for note in _unique_paths(matches)]

    def _ordinary_notes(self) -> list[Path]:
        return sorted(
            (path for path in self._root.rglob("*.md") if self._is_ordinary_note(path)),
            key=self._relative,
        )

    def _ordinary_note_for_path(self, value: str) -> Path:
        candidate = self._root.joinpath(*PurePosixPath(value).parts)
        if not self._is_ordinary_note(candidate):
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
        return parts[0] not in _EXCLUDED_TOP_LEVEL_DIRECTORIES

    def _relative(self, path: Path) -> str:
        return path.resolve(strict=True).relative_to(self._root).as_posix()

    def _excerpt(self, note: Path, needle: str) -> VaultExcerpt:
        lines = note.read_text(encoding="utf-8").splitlines()
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

    def _link_targets(self, note: Path, query: str) -> list[Path]:
        """Resolve only matching ordinary local links; external targets are ignored."""

        targets: list[Path] = []
        for line in note.read_text(encoding="utf-8").splitlines():
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


def _note_title(note: Path) -> str | None:
    lines = note.read_text(encoding="utf-8").splitlines()
    for line in lines:
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

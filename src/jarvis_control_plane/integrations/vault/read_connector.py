"""Read-only connector for a clean, synchronized knowledge-vault clone."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from time import monotonic

from pydantic import BaseModel

from .common import _EXCLUDED_TOP_LEVEL_DIRECTORIES, _remaining_seconds
from .errors import (
    VaultReadError,
    VaultRemoteUnavailable,
    VaultRepositoryConflict,
    VaultSynchronizationError,
)
from .read_models import (
    KnowledgeVaultReadResult,
    VaultReadInput,
    VaultSynchronizer,
)
from .read_policy import (
    _MARKDOWN_LINK,
    _MAX_COMPLETE_NOTE_CHARS,
    _MAX_EXCERPT_CHARS,
    _MAX_NOTES_INSPECTED,
    _MAX_RETURNED_EXCERPTS,
    _WIKILINK,
    _note_title,
    _stale_warning,
    _unique_paths,
    _VaultReadBudget,
)


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
        """Synchronize first, then perform one deterministic bounded local read."""

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
        """Return the closed orchestration tool without loading Agents at import."""

        from ...orchestration import BoundedReadTool

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
    ) -> list:
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
                "path is not an ordinary knowledge-vault note", code="outside_root"
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

    def _excerpt(self, note: Path, needle: str, budget: _VaultReadBudget):
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
        from .read_models import VaultExcerpt

        return VaultExcerpt(
            path=self._relative(note),
            start_line=start + 1,
            end_line=start + len(selected),
            text=text,
        )

    def _path_excerpt(self, note: Path, path: str, budget: _VaultReadBudget):
        """Return a complete bounded exact-path note without widening searches."""

        content = budget.read(note)
        if content and len(content) <= _MAX_COMPLETE_NOTE_CHARS:
            from .read_models import VaultExcerpt

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


__all__ = ["KnowledgeVaultConnector"]

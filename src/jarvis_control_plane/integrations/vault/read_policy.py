"""Bounds and content helpers for deterministic knowledge-vault reads."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .common import _remaining_seconds
from .errors import VaultReadError
from .read_models import _MAX_COMPLETE_NOTE_CHARS, _MAX_RETURNED_EXCERPTS

_MAX_NOTES_INSPECTED = 128
_MAX_BYTES_PER_NOTE = 64 * 1024
_MAX_TOTAL_BYTES_SCANNED = 512 * 1024
_MAX_EXCERPT_CHARS = 600
_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


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


__all__ = [
    "_MARKDOWN_LINK",
    "_MAX_BYTES_PER_NOTE",
    "_MAX_COMPLETE_NOTE_CHARS",
    "_MAX_EXCERPT_CHARS",
    "_MAX_NOTES_INSPECTED",
    "_MAX_RETURNED_EXCERPTS",
    "_MAX_TOTAL_BYTES_SCANNED",
    "_WIKILINK",
    "_VaultReadBudget",
    "_note_title",
    "_stale_warning",
    "_unique_paths",
]

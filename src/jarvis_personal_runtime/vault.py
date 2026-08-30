"""One bounded read-only prepared tool for the configured Markdown vault."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path, PurePosixPath, PureWindowsPath

_MAX_QUERY_CHARS = 200
_MAX_PATH_CHARS = 512
_MAX_MATCHES = 8
_MAX_EXCERPT_CHARS = 600
_MAX_NOTES_INSPECTED = 128
_MAX_BYTES_PER_NOTE = 64 * 1024
_MAX_TOTAL_BYTES_SCANNED = 512 * 1024


class VaultToolError(ValueError):
    """A deterministic rejection of an invalid or over-budget vault read."""


class ReadVaultTool:
    """Search or read ordinary Markdown files beneath one fixed vault root."""

    definitions: tuple[dict[str, object], ...] = (
        {
            "type": "function",
            "name": "read_vault",
            "description": (
                "Search the configured Markdown vault or read one exact Markdown "
                "file. Use a vault-relative POSIX path for read mode."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["search", "read"]},
                    "value": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _MAX_PATH_CHARS,
                    },
                },
                "required": ["mode", "value"],
                "additionalProperties": False,
            },
        },
    )

    def __init__(self, root: Path, *, max_result_chars: int = 65_536) -> None:
        try:
            resolved = root.resolve(strict=True)
        except OSError as exc:
            raise ValueError("vault root must be a readable directory") from exc
        if root.is_symlink() or not resolved.is_dir():
            raise ValueError("vault root must be a real directory")
        if (
            isinstance(max_result_chars, bool)
            or not isinstance(max_result_chars, int)
            or max_result_chars <= 0
        ):
            raise ValueError("max_result_chars must be positive")
        self._root = resolved
        self._max_result_chars = max_result_chars

    async def execute(self, name: str, arguments: dict[str, object]) -> str:
        if name != "read_vault":
            raise VaultToolError(f"unknown prepared tool: {name}")
        if set(arguments) != {"mode", "value"}:
            raise VaultToolError("read_vault arguments must be exactly mode and value")
        mode = arguments["mode"]
        value = arguments["value"]
        if not isinstance(mode, str) or mode not in {"search", "read"}:
            raise VaultToolError("read_vault mode must be search or read")
        if not isinstance(value, str) or not value or len(value) > _MAX_PATH_CHARS:
            raise VaultToolError("read_vault value must be a non-empty bounded string")
        if mode == "read":
            result = self._read(value)
        else:
            result = self._search(value)
        encoded = json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if len(encoded) > self._max_result_chars:
            raise VaultToolError("read_vault result exceeds the configured limit")
        return encoded

    def _read(self, value: str) -> dict[str, object]:
        note = self._exact_note(value)
        content = self._read_utf8(note)
        return {"mode": "read", "path": value, "content": content}

    def _search(self, query: str) -> dict[str, object]:
        if not query.strip() or len(query) > _MAX_QUERY_CHARS:
            raise VaultToolError(
                "read_vault search query must be non-empty and bounded"
            )
        folded = query.casefold()
        matches: list[dict[str, str]] = []
        inspected = 0
        scanned_bytes = 0
        for note in self._ordinary_notes():
            inspected += 1
            if inspected > _MAX_NOTES_INSPECTED:
                raise VaultToolError("read_vault search exceeds the note limit")
            raw = self._read_bytes(note)
            scanned_bytes += len(raw)
            if scanned_bytes > _MAX_TOTAL_BYTES_SCANNED:
                raise VaultToolError("read_vault search exceeds the byte limit")
            content = self._decode_utf8(raw)
            relative = note.relative_to(self._root).as_posix()
            if folded not in relative.casefold() and folded not in content.casefold():
                continue
            matches.append(
                {"path": relative, "excerpt": self._excerpt(content, folded)}
            )
            if len(matches) == _MAX_MATCHES:
                break
        return {"mode": "search", "query": query, "matches": matches}

    def _ordinary_notes(self) -> Iterator[Path]:
        for directory, directories, filenames in os.walk(self._root):
            directory_path = Path(directory)
            directories[:] = sorted(
                name
                for name in directories
                if not name.startswith(".") and not (directory_path / name).is_symlink()
            )
            for filename in sorted(filenames):
                candidate = directory_path / filename
                if candidate.suffix == ".md" and self._is_ordinary_note(candidate):
                    yield candidate

    def _exact_note(self, value: str) -> Path:
        raw = PurePosixPath(value)
        if (
            "\\" in value
            or raw.is_absolute()
            or PureWindowsPath(value).is_absolute()
            or raw.as_posix() != value
            or any(part in {".", ".."} or part.startswith(".") for part in raw.parts)
            or raw.suffix != ".md"
        ):
            raise VaultToolError(
                "read_vault path must be an ordinary relative .md file"
            )
        candidate = self._root.joinpath(*raw.parts)
        if not self._is_ordinary_note(candidate):
            raise VaultToolError("read_vault Markdown file was not found")
        return candidate

    def _is_ordinary_note(self, candidate: Path) -> bool:
        try:
            resolved = candidate.resolve(strict=True)
            relative = resolved.relative_to(self._root)
        except (OSError, ValueError):
            return False
        if (
            candidate.is_symlink()
            or not candidate.is_file()
            or candidate.suffix != ".md"
        ):
            return False
        if any(part.startswith(".") for part in relative.parts):
            return False
        return not any(
            parent.is_symlink() for parent in candidate.parents if parent != self._root
        )

    def _read_bytes(self, note: Path) -> bytes:
        try:
            with note.open("rb") as stream:
                raw = stream.read(_MAX_BYTES_PER_NOTE + 1)
        except OSError as exc:
            raise VaultToolError("read_vault Markdown file could not be read") from exc
        if len(raw) > _MAX_BYTES_PER_NOTE:
            raise VaultToolError("read_vault Markdown file exceeds the byte limit")
        return raw

    def _read_utf8(self, note: Path) -> str:
        return self._decode_utf8(self._read_bytes(note))

    @staticmethod
    def _decode_utf8(raw: bytes) -> str:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise VaultToolError("read_vault Markdown file is not valid UTF-8") from exc

    @staticmethod
    def _excerpt(content: str, folded_query: str) -> str:
        lines = content.splitlines()
        match_index = next(
            (
                index
                for index, line in enumerate(lines)
                if folded_query in line.casefold()
            ),
            0,
        )
        start = max(0, match_index - 1)
        excerpt = "\n".join(lines[start : match_index + 3])
        return excerpt[:_MAX_EXCERPT_CHARS] or "(empty Markdown note)"


__all__ = ["ReadVaultTool", "VaultToolError"]

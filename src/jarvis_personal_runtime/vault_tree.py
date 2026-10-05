"""Read-only, paginated path inventory shared by both vault configurations."""

from __future__ import annotations

import json
import os
from pathlib import Path

_MAX_ENTRIES = 20_000
_PAGE_ENTRIES = 200

TREE_DEFINITION: dict[str, object] = {
    "type": "function",
    "name": "list_vault_tree",
    "description": (
        "List the full recursive folder and file tree of the configured Obsidian "
        "note directories, including attachment paths, without reading contents. "
        "Hidden entries, symlinks, and nested repositories are excluded. Start "
        "with offset 0 and follow next_offset until null for the complete tree. "
        "Paths are vault-relative. If base_revision changes between pages, "
        "restart at 0. Use this tool, not terminal commands, to discover paths."
    ),
    "strict": True,
    "parameters": {
        "type": "object",
        "properties": {"offset": {"type": "integer", "minimum": 0}},
        "required": ["offset"],
        "additionalProperties": False,
    },
}


def validate_tree_arguments(arguments: dict[str, object]) -> int:
    if set(arguments) != {"offset"}:
        raise ValueError("list_vault_tree arguments must be exactly offset")
    offset = arguments["offset"]
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("list_vault_tree offset must be a non-negative integer")
    return offset


def list_tree(
    root: Path,
    directories: tuple[str, ...],
    offset: int,
    *,
    max_result_chars: int,
    metadata: dict[str, object] | None = None,
) -> str:
    """Inventory visible ordinary paths without opening file contents.

    Walk from the vault root so a configured nested directory cannot bypass
    ancestor symlink/repository checks. Fail explicitly on scan errors or limits
    rather than presenting an incomplete inventory as a complete tree.
    """
    entries: list[dict[str, str]] = []
    visited = 0

    def in_scope(path: str, *, ancestor: bool = False) -> bool:
        return any(
            directory == "."
            or path == directory
            or path.startswith(directory + "/")
            or (ancestor and directory.startswith(path + "/"))
            for directory in directories
        )

    def scan_error(error: OSError) -> None:
        raise ValueError("list_vault_tree could not inspect a directory") from error

    for current, children, files in os.walk(
        root, onerror=scan_error, followlinks=False
    ):
        current_path = Path(current)
        kept: list[str] = []
        for name in sorted(children + files):
            visited += 1
            if visited > _MAX_ENTRIES:
                raise ValueError("list_vault_tree exceeds the filesystem entry limit")
            path = current_path / name
            if name.startswith(".") or path.is_symlink() or path.is_junction():
                continue
            relative = path.relative_to(root).as_posix()
            try:
                path.resolve(strict=True).relative_to(root)
                is_directory = path.is_dir()
                if is_directory:
                    if (
                        not in_scope(relative, ancestor=True)
                        or (path / ".git").exists()
                    ):
                        continue
                    kept.append(name)
                elif not path.is_file() or not in_scope(relative):
                    continue
            except (OSError, ValueError) as exc:
                raise ValueError("list_vault_tree could not inspect an entry") from exc
            entries.append(
                {"path": relative, "type": "directory" if is_directory else "file"}
            )
        children[:] = kept
    entries.sort(key=lambda entry: entry["path"])
    if offset > len(entries):
        raise ValueError("list_vault_tree offset exceeds the tree size; restart at 0")
    result: dict[str, object] = {
        **(metadata or {}),
        "scope": list(directories),
        "entries": [],
        "total_entries": len(entries),
        "next_offset": None,
    }

    def encode() -> str:
        return json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    end = min(offset + _PAGE_ENTRIES, len(entries))
    while True:
        result["entries"] = entries[offset:end]
        result["next_offset"] = end if end < len(entries) else None
        encoded = encode()
        if len(encoded) <= max_result_chars and (
            end > offset or offset == len(entries)
        ):
            return encoded
        if end <= offset + 1:
            raise ValueError(
                "list_vault_tree entry exceeds the configured result limit"
            )
        end -= 1

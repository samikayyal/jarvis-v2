"""Prepared tools for a bounded, Git-backed Obsidian Markdown vault.

The module deliberately owns the complete vault boundary.  The model can ask
for a read or prepare one exact edit batch, but it cannot choose a repository,
branch, or arbitrary Git command.  A prepared edit is immutable until the
operator approves it through the normal runtime approval continuation.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import itertools
import json
import os
import shlex
import signal
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from .permissions import TomlPermissionStore

if TYPE_CHECKING:
    from .runtime import ApprovalRequired, RuntimeTrace


_MAX_PATH_CHARS = 512
_MAX_QUERY_CHARS = 200
_MAX_COMMIT_MESSAGE_CHARS = 500
_MAX_TEXT_CHARS = 128 * 1024
_MAX_BYTES_PER_NOTE = 64 * 1024
_MAX_TOTAL_BYTES_SCANNED = 512 * 1024
_MAX_NOTES_INSPECTED = 128
_MAX_MATCHES = 8
_MAX_EXCERPT_CHARS = 600
_GIT_TIMEOUT_SECONDS = 45.0


class VaultToolError(ValueError):
    """A deterministic rejection or unavailable vault operation."""


class VaultGitError(VaultToolError):
    """The configured clone or its Git remote cannot safely be used."""


def _required_text(value: object, name: str, *, maximum: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if maximum is not None and len(value) > maximum:
        raise ValueError(f"{name} is too long")
    if any(char in value for char in "\x00\r\n"):
        raise ValueError(f"{name} contains a forbidden control character")
    return value


def _canonical_directory(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("note_directories must contain non-empty strings")
    if (
        "\\" in value
        or len(value) > _MAX_PATH_CHARS
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("note directory must be a relative POSIX path")
    raw = PurePosixPath(value)
    if (
        raw.is_absolute()
        or raw.as_posix() != value
        or any(part in {"", ".", ".."} or part.startswith(".") for part in raw.parts)
    ):
        if value == ".":
            return value
        raise ValueError("note directory must be a visible relative POSIX path")
    return raw.as_posix()


@dataclass(frozen=True, slots=True)
class VaultGitSettings:
    """Fixed authority used by :class:`VaultTools`.

    ``remote`` and ``branch`` are configuration values, never model-provided
    tool arguments.  SSH paths are optional so local test repositories and
    administrators using an agent can omit them.
    """

    remote: str
    branch: str
    note_directories: tuple[str, ...]
    author_name: str = "Jarvis"
    author_email: str = "jarvis@samikayyal.com"
    ssh_identity_file: Path | None = None
    ssh_known_hosts_file: Path | None = None

    def __post_init__(self) -> None:
        remote = _required_text(self.remote, "remote", maximum=2048)
        if remote.startswith("-"):
            raise ValueError("remote must not begin with '-'")
        branch = _required_text(self.branch, "branch", maximum=256)
        components = branch.split("/")
        if (
            branch.startswith("-")
            or branch in {".", "..", "@"}
            or any(
                marker in branch for marker in ("..", "//", "\\", " ", "~", "^", ":")
            )
            or any(
                not component
                or component.startswith(".")
                or component.endswith((".", ".lock"))
                for component in components
            )
            or any(ord(char) < 32 or ord(char) == 127 for char in branch)
        ):
            raise ValueError("branch is not a safe Git branch name")
        if not isinstance(self.note_directories, tuple) or not self.note_directories:
            raise ValueError("note_directories must be a non-empty tuple")
        directories = tuple(
            _canonical_directory(item) for item in self.note_directories
        )
        if len(set(directories)) != len(directories):
            raise ValueError("note_directories must not contain duplicates")
        author_name = _required_text(self.author_name, "author_name", maximum=200)
        author_email = _required_text(self.author_email, "author_email", maximum=320)
        if self.ssh_identity_file is not None and not isinstance(
            self.ssh_identity_file, Path
        ):
            raise TypeError("ssh_identity_file must be a Path or None")
        if self.ssh_known_hosts_file is not None and not isinstance(
            self.ssh_known_hosts_file, Path
        ):
            raise TypeError("ssh_known_hosts_file must be a Path or None")
        object.__setattr__(self, "remote", remote)
        object.__setattr__(self, "branch", branch)
        object.__setattr__(self, "note_directories", directories)
        object.__setattr__(self, "author_name", author_name)
        object.__setattr__(self, "author_email", author_email)


@dataclass(frozen=True, slots=True)
class _GitResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    stdout_bytes: bytes = b""
    stderr_bytes: bytes = b""


@dataclass(frozen=True, slots=True)
class _SyncState:
    base_revision: str
    freshness: str
    last_successful_sync: datetime | None = None


@dataclass(frozen=True, slots=True)
class _PreparedChange:
    operation: str
    path: str
    old_bytes: bytes
    new_bytes: bytes
    patch: str


@dataclass(slots=True)
class _ContinuationState:
    resolved: bool = False
    approval_decision: str = "approved_once"
    preview_shown: bool = True


@dataclass(frozen=True, slots=True)
class _EditContinuation:
    base_revision: str
    commit_message: str
    changes: tuple[_PreparedChange, ...]
    patch: str
    state: _ContinuationState


def _json(payload: Mapping[str, object], *, limit: int | None = None) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if limit is not None and len(encoded) > limit:
        raise VaultToolError("vault result exceeds the configured limit")
    return encoded


def _decode_note(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VaultToolError("Markdown note is not valid UTF-8") from exc


def _encode_note(text: str) -> bytes:
    if not isinstance(text, str):
        raise VaultToolError("Markdown content must be text")
    if len(text) > _MAX_TEXT_CHARS:
        raise VaultToolError("Markdown content is too large")
    try:
        raw = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise VaultToolError("Markdown content is not valid UTF-8") from exc
    if len(raw) > _MAX_BYTES_PER_NOTE:
        raise VaultToolError("Markdown note exceeds the byte limit")
    return raw


def _display_diff(path: str, old: bytes, new: bytes, *, operation: str) -> str:
    # Byte diffs retain BOMs and the distinction between a final newline and
    # no final newline.  Markdown input is already validated as UTF-8, so the
    # resulting display remains safe text while the actual edit stays bytes.
    from_name = b"/dev/null" if operation == "create" else f"a/{path}".encode()
    to_name = f"b/{path}".encode()
    rendered = difflib.diff_bytes(
        difflib.unified_diff,
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=from_name,
        tofile=to_name,
        lineterm=b"\n",
    )
    result = b"".join(rendered).decode("utf-8", errors="replace")
    markers: list[str] = []
    if old and not old.endswith((b"\n", b"\r")):
        markers.append("old")
    if new and not new.endswith((b"\n", b"\r")):
        markers.append("new")
    if markers:
        result = f"{result}\n\\ No newline at end of file"
    return result


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _same_path(left: Path, right: Path) -> bool:
    """Compare canonical paths without relying on platform case semantics."""

    left_text = str(left.resolve(strict=False))
    right_text = str(right.resolve(strict=False))
    if os.name == "nt":
        return left_text.casefold() == right_text.casefold()
    return left_text == right_text


def _remote_key(value: str) -> str:
    # Local-path remotes have several equivalent spellings.  URLs and SCP
    # syntax are compared verbatim after removing a harmless trailing slash.
    if "://" not in value and not value.startswith("git@"):
        try:
            return (
                str(Path(value).expanduser().resolve(strict=False)).casefold()
                if os.name == "nt"
                else str(Path(value).expanduser().resolve(strict=False))
            )
        except (OSError, RuntimeError):
            return value.rstrip("/")
    return value.rstrip("/")


class VaultTools:
    """Expose synchronized ``read_vault`` and approval-gated ``edit_vault``."""

    definitions: tuple[dict[str, object], ...] = (
        {
            "type": "function",
            "name": "read_vault",
            "description": (
                "Synchronize the configured Git-backed Markdown vault, then "
                "search it or read one exact approved Markdown path."
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
        {
            "type": "function",
            "name": "edit_vault",
            "description": (
                "Prepare an exact Markdown note batch against a synchronized "
                "Git revision. The runtime shows a simple before/after preview "
                "and handles approval outside the model loop. Choice 2 remembers "
                "approval for that exact file; later requests for it can proceed "
                "without another prompt. Use one file per call when offering this "
                "choice. The result reports the actual approval and sync outcome. "
                "Tell the operator briefly what changed and whether it synced. "
                "Keep commit hashes, base revisions, commit messages, and Git "
                "details out of the preview and final reply."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "base_revision": {
                        "type": "string",
                        "minLength": 40,
                        "maxLength": 64,
                    },
                    "commit_message": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _MAX_COMMIT_MESSAGE_CHARS,
                    },
                    "changes": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 32,
                        "items": {
                            "anyOf": [
                                {
                                    "type": "object",
                                    "properties": {
                                        "operation": {
                                            "type": "string",
                                            "enum": ["update"],
                                        },
                                        "path": {
                                            "type": "string",
                                            "minLength": 1,
                                            "maxLength": _MAX_PATH_CHARS,
                                        },
                                        "replacements": {
                                            "type": "array",
                                            "minItems": 1,
                                            "maxItems": 64,
                                            "items": {
                                                "type": "object",
                                                "properties": {
                                                    "old_text": {
                                                        "type": "string",
                                                        "minLength": 1,
                                                        "maxLength": _MAX_TEXT_CHARS,
                                                    },
                                                    "new_text": {
                                                        "type": "string",
                                                        "maxLength": _MAX_TEXT_CHARS,
                                                    },
                                                },
                                                "required": ["old_text", "new_text"],
                                                "additionalProperties": False,
                                            },
                                        },
                                    },
                                    "required": ["operation", "path", "replacements"],
                                    "additionalProperties": False,
                                },
                                {
                                    "type": "object",
                                    "properties": {
                                        "operation": {
                                            "type": "string",
                                            "enum": ["create"],
                                        },
                                        "path": {
                                            "type": "string",
                                            "minLength": 1,
                                            "maxLength": _MAX_PATH_CHARS,
                                        },
                                        "content": {
                                            "type": "string",
                                            "maxLength": _MAX_TEXT_CHARS,
                                        },
                                    },
                                    "required": ["operation", "path", "content"],
                                    "additionalProperties": False,
                                },
                            ]
                        },
                    },
                },
                "required": ["base_revision", "commit_message", "changes"],
                "additionalProperties": False,
            },
        },
    )

    def __init__(
        self,
        root: Path,
        settings: VaultGitSettings,
        *,
        max_result_chars: int = 65_536,
        permission_store: TomlPermissionStore | None = None,
        trace: RuntimeTrace | None = None,
    ) -> None:
        if not isinstance(root, Path):
            raise TypeError("vault root must be a Path")
        try:
            resolved = root.resolve(strict=True)
        except OSError as exc:
            raise ValueError("vault root must be a readable directory") from exc
        if root.is_symlink() or not resolved.is_dir():
            raise ValueError("vault root must be a real directory")
        if not isinstance(settings, VaultGitSettings):
            raise TypeError("settings must be VaultGitSettings")
        if (
            isinstance(max_result_chars, bool)
            or not isinstance(max_result_chars, int)
            or max_result_chars <= 0
        ):
            raise ValueError("max_result_chars must be positive")
        self._root = resolved
        self._settings = settings
        self._max_result_chars = max_result_chars
        self._permission_store = permission_store
        self._trace = trace
        # A remembered file never grants access to another clone, remote, or branch.
        self._permission_host = "vault:" + _hash(
            json.dumps(
                [
                    os.path.normcase(str(resolved)),
                    _remote_key(settings.remote),
                    settings.branch,
                ]
            ).encode()
        )
        self._lock = asyncio.Lock()
        self._last_successful_revision: str | None = None
        self._last_successful_sync: datetime | None = None

    async def execute(
        self, name: str, arguments: dict[str, object]
    ) -> str | ApprovalRequired:
        if name == "read_vault":
            return await self._execute_read(arguments)
        if name == "edit_vault":
            return await self._execute_edit(arguments)
        raise VaultToolError(f"unknown prepared tool: {name}")

    async def resume(self, continuation: object, *, approved: bool) -> str:
        if not isinstance(continuation, _EditContinuation):
            raise TypeError("invalid vault edit continuation")
        async with self._lock:
            if continuation.state.resolved:
                return _json({"status": "already_resolved"})
            continuation.state.resolved = True
            if not approved:
                return _json(
                    {
                        "status": "rejected",
                        "approval": {
                            "decision": "rejected",
                            "preview_shown": True,
                            "source": "operator_reply",
                        },
                        "paths": [change.path for change in continuation.changes],
                    },
                    limit=self._max_result_chars,
                )
            if self._has_saved_permissions(continuation.changes):
                continuation.state.approval_decision = "approved_and_saved"
            return await self._execute_approved(continuation)

    async def _execute_read(self, arguments: dict[str, object]) -> str:
        mode, value = self._validate_read_arguments(arguments)
        async with self._lock:
            sync = await self._synchronize(allow_stale=True)
            if mode == "read":
                result = self._read_note(value)
            else:
                result = self._search_notes(value)
        result.update(
            {
                "base_revision": sync.base_revision,
                "freshness": sync.freshness,
            }
        )
        if sync.freshness == "stale":
            result["warning"] = (
                "The remote could not be reached; this result may be stale."
            )
            if sync.last_successful_sync is not None:
                result["last_successful_sync"] = sync.last_successful_sync.isoformat()
        return _json(result, limit=self._max_result_chars)

    async def _execute_edit(
        self, arguments: dict[str, object]
    ) -> str | ApprovalRequired:
        base_revision, commit_message, raw_changes = self._validate_edit_arguments(
            arguments
        )
        async with self._lock:
            sync = await self._synchronize(allow_stale=False)
            if sync.base_revision != base_revision:
                raise VaultToolError(
                    "edit_vault base_revision does not match the synchronized vault"
                )
            changes = self._prepare_changes(raw_changes)
            actual_changes = tuple(
                change
                for change in changes
                if change.operation == "create" or change.old_bytes != change.new_bytes
            )
            if not actual_changes:
                return _json(
                    {
                        "status": "no_change",
                        "paths": [change.path for change in changes],
                    },
                    limit=self._max_result_chars,
                )
            patch = "\n\n".join(change.patch for change in actual_changes)
            display = self._approval_display(actual_changes)
            if len(display) > self._max_result_chars:
                raise VaultToolError(
                    "edit_vault proposal is larger than the configured result limit"
                )
            from .runtime import ApprovalRequired, PendingAction

            continuation = _EditContinuation(
                base_revision=base_revision,
                commit_message=commit_message,
                changes=actual_changes,
                patch=patch,
                state=_ContinuationState(),
            )
            if self._has_saved_permissions(actual_changes):
                continuation.state.resolved = True
                continuation.state.approval_decision = "saved_file_permission"
                continuation.state.preview_shown = False
                return await self._execute_approved(continuation)
            can_save = self._permission_store is not None and len(actual_changes) == 1
            return ApprovalRequired(
                PendingAction(
                    host=self._permission_host,
                    prefix=actual_changes[0].path,
                    display=display,
                    allow_save_permission=can_save,
                    approval_suffix=(
                        "Reply 1 to approve once, 2 to always approve future edits "
                        "to this file, or 9 to reject."
                        if can_save
                        else None
                    ),
                ),
                continuation,
            )

    def _has_saved_permissions(self, changes: tuple[_PreparedChange, ...]) -> bool:
        if self._permission_store is None:
            return False
        rules = self._permission_store.list_rules()
        return all(
            any(rule.matches(self._permission_host, change.path) for rule in rules)
            for change in changes
        )

    def _validate_read_arguments(self, arguments: dict[str, object]) -> tuple[str, str]:
        if not isinstance(arguments, dict) or set(arguments) != {"mode", "value"}:
            raise VaultToolError("read_vault arguments must be exactly mode and value")
        mode = arguments["mode"]
        value = arguments["value"]
        if not isinstance(mode, str) or mode not in {"read", "search"}:
            raise VaultToolError("read_vault mode must be read or search")
        if not isinstance(value, str) or not value or len(value) > _MAX_PATH_CHARS:
            raise VaultToolError("read_vault value must be non-empty and bounded")
        if mode == "search" and len(value) > _MAX_QUERY_CHARS:
            raise VaultToolError("read_vault search query is too long")
        return mode, value

    def _validate_edit_arguments(
        self, arguments: dict[str, object]
    ) -> tuple[str, str, tuple[Mapping[str, object], ...]]:
        if not isinstance(arguments, dict) or set(arguments) != {
            "base_revision",
            "commit_message",
            "changes",
        }:
            raise VaultToolError(
                "edit_vault arguments must be exactly base_revision, commit_message, and changes"
            )
        base_revision = arguments["base_revision"]
        if (
            not isinstance(base_revision, str)
            or len(base_revision) != 40
            or any(char not in "0123456789abcdefABCDEF" for char in base_revision)
        ):
            raise VaultToolError("base_revision must be a full 40-character Git SHA")
        base_revision = base_revision.lower()
        commit_message = arguments["commit_message"]
        if not isinstance(commit_message, str) or not commit_message.strip():
            raise VaultToolError("commit_message must be non-empty")
        if len(commit_message) > _MAX_COMMIT_MESSAGE_CHARS:
            raise VaultToolError("commit_message is too long")
        if any(char in commit_message for char in "\x00\r"):
            raise VaultToolError(
                "commit_message contains a forbidden control character"
            )
        raw_changes = arguments["changes"]
        if (
            not isinstance(raw_changes, list)
            or not raw_changes
            or len(raw_changes) > 32
            or not all(isinstance(item, Mapping) for item in raw_changes)
        ):
            raise VaultToolError("changes must be a non-empty bounded list of objects")
        return base_revision, commit_message, tuple(raw_changes)  # type: ignore[arg-type]

    def _prepare_changes(
        self, raw_changes: tuple[Mapping[str, object], ...]
    ) -> tuple[_PreparedChange, ...]:
        prepared: list[_PreparedChange] = []
        seen: set[str] = set()
        for raw in raw_changes:
            operation = raw.get("operation")
            path_value = raw.get("path")
            if operation not in {"update", "create"} or not isinstance(path_value, str):
                raise VaultToolError("each change needs operation and path")
            path = self._validate_note_path(path_value)
            if path in seen:
                raise VaultToolError("each Markdown path may appear only once")
            seen.add(path)
            candidate = self._root.joinpath(*PurePosixPath(path).parts)
            if operation == "create":
                if set(raw) != {"operation", "path", "content"}:
                    raise VaultToolError(
                        "create changes require exactly operation, path, and content"
                    )
                if candidate.exists() or candidate.is_symlink():
                    raise VaultToolError(f"Markdown note already exists: {path}")
                old_bytes = b""
                new_bytes = _encode_note(raw["content"])  # type: ignore[arg-type]
            else:
                if set(raw) != {"operation", "path", "replacements"}:
                    raise VaultToolError(
                        "update changes require exactly operation, path, and replacements"
                    )
                if not self._is_ordinary_note(candidate, path):
                    raise VaultToolError(f"Markdown note was not found: {path}")
                old_bytes = self._read_note_bytes(candidate)
                original = _decode_note(old_bytes)
                replacements = raw["replacements"]
                new_text = self._apply_replacements(original, replacements)
                new_bytes = _encode_note(new_text)
            prepared.append(
                _PreparedChange(
                    operation=operation,
                    path=path,
                    old_bytes=old_bytes,
                    new_bytes=new_bytes,
                    patch=_display_diff(
                        path, old_bytes, new_bytes, operation=operation
                    ),
                )
            )
        return tuple(prepared)

    def _apply_replacements(self, original: str, replacements: object) -> str:
        if (
            not isinstance(replacements, list)
            or not replacements
            or len(replacements) > 64
            or not all(isinstance(item, Mapping) for item in replacements)
        ):
            raise VaultToolError("replacements must be a non-empty bounded list")
        spans: list[tuple[int, int, str]] = []
        for item in replacements:
            if set(item) != {"old_text", "new_text"}:
                raise VaultToolError(
                    "each replacement needs exactly old_text and new_text"
                )
            old_text = item["old_text"]
            new_text = item["new_text"]
            if not isinstance(old_text, str) or not old_text:
                raise VaultToolError("old_text must be non-empty text")
            if not isinstance(new_text, str):
                raise VaultToolError("new_text must be text")
            if len(old_text) > _MAX_TEXT_CHARS or len(new_text) > _MAX_TEXT_CHARS:
                raise VaultToolError("replacement text is too large")
            first = original.find(old_text)
            second = original.find(old_text, first + 1) if first >= 0 else -1
            if first < 0 or second >= 0:
                raise VaultToolError("each old_text must match exactly once")
            start = first
            spans.append((start, start + len(old_text), new_text))
        spans.sort(key=lambda span: span[0])
        for previous, current in itertools.pairwise(spans):
            if current[0] < previous[1]:
                raise VaultToolError("replacement ranges must not overlap")
        newline = "\r\n" if "\r\n" in original else "\n"
        result = original
        for start, end, new_text in reversed(spans):
            if newline == "\r\n":
                new_text = (
                    new_text.replace("\r\n", "\n")
                    .replace("\r", "\n")
                    .replace("\n", "\r\n")
                )
            else:
                new_text = new_text.replace("\r\n", "\n").replace("\r", "\n")
            result = result[:start] + new_text + result[end:]
        return result

    def _approval_display(self, changes: Iterable[_PreparedChange]) -> str:
        sections = ["Proposed note changes:"]
        for change in changes:
            sections.append(f"File: {change.path}")
            old = _decode_note(change.old_bytes)
            new = _decode_note(change.new_bytes)
            if change.operation == "create":
                sections.append("Create this note:\n" + (new or "(empty note)"))
                continue
            before, after = old.splitlines(keepends=True), new.splitlines(keepends=True)
            matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
            for tag, i, j, k, l in matcher.get_opcodes():
                if tag == "equal":
                    continue
                previous = "\n".join(
                    line.rstrip("\r\n") or "(blank line)" for line in before[i:j]
                )
                replacement = "\n".join(
                    line.rstrip("\r\n") or "(blank line)" for line in after[k:l]
                )
                if tag == "insert":
                    sections.append("Add:\n" + (replacement or "(blank line)"))
                elif tag == "delete":
                    sections.append("Remove:\n" + (previous or "(blank line)"))
                else:
                    sections.append(
                        "Replace:\n"
                        + (previous or "(blank line)")
                        + "\nWith:\n"
                        + (replacement or "(blank line)")
                    )
            if old.endswith("\n") != new.endswith("\n"):
                sections.append(
                    "Add the final newline."
                    if new.endswith("\n")
                    else "Remove the final newline."
                )
        sections.append("Approval saves and syncs these changes.")
        return "\n\n".join(sections)

    def _validate_note_path(self, value: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > _MAX_PATH_CHARS
            or "\\" in value
            or ":" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise VaultToolError(
                "note path must be a bounded relative POSIX Markdown path"
            )
        raw = PurePosixPath(value)
        if (
            raw.is_absolute()
            or raw.as_posix() != value
            or raw.suffix != ".md"
            or any(
                part in {"", ".", ".."} or part.startswith(".") for part in raw.parts
            )
        ):
            raise VaultToolError("note path must be a visible relative .md path")
        if not any(
            directory == "." or value == directory or value.startswith(f"{directory}/")
            for directory in self._settings.note_directories
        ):
            raise VaultToolError("note path is outside the configured note directories")
        candidate = self._root.joinpath(*raw.parts)
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(self._root)
        except ValueError as exc:
            raise VaultToolError("note path resolves outside the vault") from exc
        if any(
            parent.is_symlink() for parent in candidate.parents if parent != self._root
        ):
            raise VaultToolError("note path may not traverse a symlink")
        if any(
            (parent / ".git").exists()
            for parent in candidate.parents
            if parent != self._root
        ):
            raise VaultToolError("note path may not traverse a nested Git repository")
        return value

    def _is_ordinary_note(self, candidate: Path, relative: str) -> bool:
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self._root)
        except (OSError, ValueError):
            return False
        if candidate.is_symlink() or not candidate.is_file():
            return False
        try:
            self._validate_note_path(relative)
        except VaultToolError:
            return False
        return not any(
            parent.is_symlink() for parent in candidate.parents if parent != self._root
        )

    def _read_note_bytes(self, candidate: Path) -> bytes:
        try:
            with candidate.open("rb") as stream:
                raw = stream.read(_MAX_BYTES_PER_NOTE + 1)
        except OSError as exc:
            raise VaultToolError("Markdown note could not be read") from exc
        if len(raw) > _MAX_BYTES_PER_NOTE:
            raise VaultToolError("Markdown note exceeds the byte limit")
        _decode_note(raw)
        return raw

    def _read_note(self, value: str) -> dict[str, object]:
        path = self._validate_note_path(value)
        candidate = self._root.joinpath(*PurePosixPath(path).parts)
        if not self._is_ordinary_note(candidate, path):
            raise VaultToolError("read_vault Markdown file was not found")
        return {
            "mode": "read",
            "path": path,
            "content": _decode_note(self._read_note_bytes(candidate)),
        }

    def _search_notes(self, query: str) -> dict[str, object]:
        if not query.strip() or len(query) > _MAX_QUERY_CHARS:
            raise VaultToolError(
                "read_vault search query must be non-empty and bounded"
            )
        folded = query.casefold()
        matches: list[dict[str, str]] = []
        inspected = 0
        scanned = 0
        for note in self._ordinary_notes():
            inspected += 1
            if inspected > _MAX_NOTES_INSPECTED:
                raise VaultToolError("read_vault search exceeds the note limit")
            raw = self._read_note_bytes(note)
            scanned += len(raw)
            if scanned > _MAX_TOTAL_BYTES_SCANNED:
                raise VaultToolError("read_vault search exceeds the byte limit")
            content = _decode_note(raw)
            relative = note.relative_to(self._root).as_posix()
            if folded not in relative.casefold() and folded not in content.casefold():
                continue
            matches.append(
                {"path": relative, "excerpt": self._excerpt(content, folded)}
            )
            if len(matches) >= _MAX_MATCHES:
                break
        return {"mode": "search", "query": query, "matches": matches}

    def _ordinary_notes(self) -> Iterable[Path]:
        yielded: set[Path] = set()
        for directory in self._settings.note_directories:
            root = (
                self._root
                if directory == "."
                else self._root.joinpath(*PurePosixPath(directory).parts)
            )
            if not root.is_dir() or root.is_symlink():
                continue
            for current, directories, filenames in os.walk(
                root, topdown=True, followlinks=False
            ):
                current_path = Path(current)
                directories[:] = sorted(
                    name
                    for name in directories
                    if not name.startswith(".")
                    and not (current_path / name).is_symlink()
                    and not (current_path / name / ".git").exists()
                )
                for filename in sorted(filenames):
                    if not filename.endswith(".md") or filename.startswith("."):
                        continue
                    candidate = current_path / filename
                    relative = candidate.relative_to(self._root).as_posix()
                    if candidate in yielded:
                        continue
                    if self._is_ordinary_note(candidate, relative):
                        yielded.add(candidate)
                        yield candidate

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

    async def _synchronize(self, *, allow_stale: bool) -> _SyncState:
        await self._require_clean()
        branch = await self._git_text(["symbolic-ref", "--quiet", "--short", "HEAD"])
        if branch != self._settings.branch:
            raise VaultGitError("vault clone is not on the configured branch")
        local = await self._git_text(["rev-parse", "HEAD"])
        fetched = await self._run_git(
            ["fetch", "--no-tags", self._settings.remote, self._settings.branch]
        )
        if fetched.timed_out or fetched.exit_code != 0:
            if (
                allow_stale
                and self._last_successful_revision is not None
                and local == self._last_successful_revision
            ):
                return _SyncState(
                    local,
                    "stale",
                    self._last_successful_sync,
                )
            raise VaultGitError("vault remote could not be synchronized")
        remote = await self._git_text(["rev-parse", "FETCH_HEAD^{commit}"])
        if local != remote:
            ancestor = await self._run_git(
                ["merge-base", "--is-ancestor", "HEAD", "FETCH_HEAD"]
            )
            if ancestor.timed_out or ancestor.exit_code != 0:
                raise VaultGitError("vault clone and remote have diverged")
            merged = await self._run_git(["merge", "--ff-only", "FETCH_HEAD"])
            if merged.timed_out or merged.exit_code != 0:
                raise VaultGitError("vault clone could not fast-forward to the remote")
        head = await self._git_text(["rev-parse", "HEAD"])
        if head != remote:
            raise VaultGitError("vault clone did not reach the fetched remote revision")
        await self._require_clean()
        now = datetime.now(UTC)
        self._last_successful_revision = head
        self._last_successful_sync = now
        return _SyncState(head, "synced", now)

    async def _revalidate_approval(self, continuation: _EditContinuation) -> str | None:
        await self._require_clean()
        branch = await self._git_text(["symbolic-ref", "--quiet", "--short", "HEAD"])
        if branch != self._settings.branch:
            return "stale_base"
        head = await self._git_text(["rev-parse", "HEAD"])
        if head != continuation.base_revision:
            return "stale_base"
        fetched = await self._run_git(
            ["fetch", "--no-tags", self._settings.remote, self._settings.branch]
        )
        if fetched.timed_out or fetched.exit_code != 0:
            return "sync_unavailable"
        remote = await self._git_text(["rev-parse", "FETCH_HEAD^{commit}"])
        if remote != continuation.base_revision:
            return "stale_base"
        return None

    async def _execute_approved(self, continuation: _EditContinuation) -> str:
        try:
            problem = await self._revalidate_approval(continuation)
        except VaultGitError:
            problem = "execution_failed"
        if problem is not None:
            return self._outcome(problem, continuation)
        try:
            for change in continuation.changes:
                self._validate_note_path(change.path)
                candidate = self._root.joinpath(*PurePosixPath(change.path).parts)
                if change.operation == "update":
                    if not self._is_ordinary_note(candidate, change.path):
                        return self._outcome("stale_base", continuation)
                    current = self._read_note_bytes(candidate)
                    if current != change.old_bytes:
                        return self._outcome("stale_base", continuation)
                elif candidate.exists() or candidate.is_symlink():
                    return self._outcome("stale_base", continuation)
                else:
                    self._ensure_parent_directory(candidate)
            for change in continuation.changes:
                candidate = self._root.joinpath(*PurePosixPath(change.path).parts)
                self._write_note_bytes(
                    candidate, change.new_bytes, create=change.operation == "create"
                )
            for change in continuation.changes:
                candidate = self._root.joinpath(*PurePosixPath(change.path).parts)
                if self._read_note_bytes(candidate) != change.new_bytes:
                    return self._outcome(
                        "execution_failed", continuation, local_changes=True
                    )
            actual_patch = "\n\n".join(
                _display_diff(
                    change.path,
                    change.old_bytes,
                    self._read_note_bytes(
                        self._root.joinpath(*PurePosixPath(change.path).parts)
                    ),
                    operation=change.operation,
                )
                for change in continuation.changes
            )
            if actual_patch != continuation.patch:
                return self._outcome(
                    "execution_failed", continuation, local_changes=True
                )
            staged = await self._run_git(
                ["add", "--", *(change.path for change in continuation.changes)]
            )
            if staged.timed_out or staged.exit_code != 0:
                return self._outcome("stage_failed", continuation, local_changes=True)
            staged_names = await self._run_git(
                ["diff", "--cached", "--name-only", "-z", "--"]
            )
            if staged_names.timed_out or staged_names.exit_code != 0:
                return self._outcome(
                    "execution_failed", continuation, local_changes=True
                )
            staged_paths = [path for path in staged_names.stdout.split("\x00") if path]
            expected_paths = sorted(change.path for change in continuation.changes)
            if sorted(staged_paths) != expected_paths:
                return self._outcome(
                    "execution_failed", continuation, local_changes=True
                )
            for change in continuation.changes:
                staged_blob = await self._run_git(
                    ["cat-file", "blob", f":{change.path}"]
                )
                if (
                    staged_blob.timed_out
                    or staged_blob.exit_code != 0
                    or staged_blob.stdout_bytes != change.new_bytes
                ):
                    return self._outcome(
                        "execution_failed", continuation, local_changes=True
                    )
            committed = await self._run_git(
                [
                    "-c",
                    f"user.name={self._settings.author_name}",
                    "-c",
                    f"user.email={self._settings.author_email}",
                    "commit",
                    "--no-verify",
                    "--no-gpg-sign",
                    "--author",
                    f"{self._settings.author_name} <{self._settings.author_email}>",
                    "-m",
                    continuation.commit_message,
                ]
            )
            if committed.timed_out or committed.exit_code != 0:
                return self._outcome("commit_failed", continuation, local_changes=True)
            commit = await self._git_text(["rev-parse", "HEAD"])
            return await self._push_and_verify(commit, continuation)
        except (OSError, VaultToolError, VaultGitError):
            return self._outcome("execution_failed", continuation, local_changes=True)

    def _ensure_parent_directory(self, candidate: Path) -> None:
        try:
            relative_parent = candidate.parent.resolve(strict=False).relative_to(
                self._root
            )
        except ValueError as exc:
            raise VaultToolError("note parent resolves outside the vault") from exc
        current = self._root
        for part in relative_parent.parts:
            current = current / part
            if current.exists() and (current.is_symlink() or not current.is_dir()):
                raise VaultToolError("note parent must contain only real directories")
            if not current.exists():
                current.mkdir()
            if current.is_symlink() or not current.is_dir():
                raise VaultToolError("note parent must contain only real directories")

    @staticmethod
    def _write_note_bytes(candidate: Path, content: bytes, *, create: bool) -> None:
        flags = os.O_WRONLY
        if create:
            flags |= os.O_CREAT | os.O_EXCL
        else:
            flags |= os.O_TRUNC
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        flags |= nofollow
        descriptor = os.open(candidate, flags, 0o600 if create else 0o644)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(content)
        finally:
            if descriptor != -1:
                os.close(descriptor)

    async def _push_and_verify(
        self, commit: str, continuation: _EditContinuation
    ) -> str:
        pushed = await self._run_git(
            [
                "push",
                self._settings.remote,
                f"{commit}:refs/heads/{self._settings.branch}",
            ]
        )
        try:
            remote_contains = await self._remote_contains(commit)
        except VaultGitError:
            remote_contains = None
        if remote_contains is True:
            status = "synced"
        elif remote_contains is None:
            status = "sync_unknown"
        else:
            status = "committed_not_synced"
        if pushed.timed_out and remote_contains is not True:
            status = "sync_unknown"
        elif pushed.exit_code != 0 and remote_contains is not True:
            status = (
                "committed_not_synced" if remote_contains is False else "sync_unknown"
            )
        return self._outcome(status, continuation, commit=commit)

    async def _remote_contains(self, commit: str) -> bool | None:
        result = await self._run_git(
            ["ls-remote", self._settings.remote, f"refs/heads/{self._settings.branch}"]
        )
        if result.timed_out or result.exit_code != 0:
            return None
        line = next((line for line in result.stdout.splitlines() if line.strip()), "")
        fields = line.split()
        if len(fields) < 2:
            return None
        if fields[0] == commit:
            return True
        # Obtain the remote object without changing the checked-out branch, so
        # a second writer can advance the remote after our push and still be
        # recognized when our commit is an ancestor of that new head.
        fetched = await self._run_git(
            ["fetch", "--no-tags", self._settings.remote, self._settings.branch]
        )
        if fetched.timed_out or fetched.exit_code != 0:
            return None
        fetched_head = await self._run_git(["rev-parse", "FETCH_HEAD^{commit}"])
        if fetched_head.timed_out or fetched_head.exit_code != 0:
            return None
        if fetched_head.stdout.strip() == commit:
            return True
        ancestor = await self._run_git(
            ["merge-base", "--is-ancestor", commit, "FETCH_HEAD"]
        )
        if ancestor.timed_out:
            return None
        return ancestor.exit_code == 0

    def _outcome(
        self,
        status: str,
        continuation: _EditContinuation,
        *,
        commit: str | None = None,
        local_changes: bool = False,
    ) -> str:
        payload: dict[str, object] = {
            "status": status,
            "approval": {
                "decision": continuation.state.approval_decision,
                "preview_shown": continuation.state.preview_shown,
                "source": "operator_reply"
                if continuation.state.preview_shown
                else "saved_permission",
            },
            "paths": [change.path for change in continuation.changes],
        }
        if self._trace is not None:
            self._trace.record(
                "vault_edit_outcome",
                {
                    **payload,
                    "base_revision": continuation.base_revision,
                    "commit": commit,
                    "commit_message": continuation.commit_message,
                    "local_changes_left": local_changes,
                },
            )
        if local_changes:
            payload["local_changes_left"] = True
        return _json(payload, limit=self._max_result_chars)

    async def _require_clean(self) -> None:
        await self._require_repository()
        result = await self._run_git(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"]
        )
        if result.timed_out or result.exit_code != 0:
            raise VaultGitError("vault Git status could not be read")
        if result.stdout.strip():
            raise VaultGitError("vault clone has local changes; recover it before use")

    async def _require_repository(self) -> None:
        root = await self._git_text(["rev-parse", "--show-toplevel"])
        if not _same_path(Path(root), self._root):
            raise VaultGitError("vault root is not the dedicated Git worktree")
        git_dir = self._root / ".git"
        if git_dir.is_symlink() or not git_dir.is_dir():
            raise VaultGitError("vault clone must use a real .git directory")
        if not (git_dir / "index").is_file():
            raise VaultGitError("vault Git index is missing")
        state_paths = (
            "MERGE_HEAD",
            "CHERRY_PICK_HEAD",
            "REVERT_HEAD",
            "BISECT_LOG",
            "rebase-merge",
            "rebase-apply",
        )
        if any((git_dir / name).exists() for name in state_paths):
            raise VaultGitError("vault clone has an unfinished Git operation")
        origin = await self._run_git(["remote", "get-url", "origin"])
        if (
            origin.exit_code == 0
            and origin.stdout.strip()
            and _remote_key(origin.stdout.strip()) != _remote_key(self._settings.remote)
        ):
            raise VaultGitError("vault origin does not match the configured remote")

    async def _git_text(self, arguments: list[str]) -> str:
        result = await self._run_git(arguments)
        if result.timed_out or result.exit_code != 0:
            raise VaultGitError("vault Git operation failed")
        return result.stdout.strip()

    async def _git_lines(self, arguments: list[str]) -> list[str]:
        return [line for line in (await self._git_text(arguments)).splitlines() if line]

    async def _run_git(
        self,
        arguments: list[str],
        *,
        timeout: float = _GIT_TIMEOUT_SECONDS,
    ) -> _GitResult:
        command = [
            "git",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "diff.external=",
            "-c",
            "credential.helper=",
            "-c",
            "core.quotePath=false",
            "--no-pager",
            "--no-optional-locks",
            *arguments,
        ]
        environment = os.environ.copy()
        for key in tuple(environment):
            if key.startswith("GIT_"):
                environment.pop(key, None)
        environment.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
            }
        )
        ssh_command: list[str] = ["ssh", "-F", os.devnull, "-o", "BatchMode=yes"]
        if self._settings.ssh_identity_file is not None:
            ssh_command.extend(
                [
                    "-i",
                    str(self._settings.ssh_identity_file),
                    "-o",
                    "IdentitiesOnly=yes",
                ]
            )
        if self._settings.ssh_known_hosts_file is not None:
            ssh_command.extend(
                [
                    "-o",
                    "StrictHostKeyChecking=yes",
                    "-o",
                    f"UserKnownHostsFile={self._settings.ssh_known_hosts_file}",
                ]
            )
        environment["GIT_SSH_COMMAND"] = " ".join(
            shlex.quote(part) for part in ssh_command
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self._root,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
                start_new_session=os.name != "nt",
            )
        except OSError as exc:
            raise VaultGitError("Git executable could not be started") from exc
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except TimeoutError:
            await self._quiesce(process)
            return _GitResult(None, "", "", timed_out=True)
        except asyncio.CancelledError:
            await self._quiesce(process)
            raise
        return _GitResult(
            process.returncode,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
            stdout_bytes=stdout,
            stderr_bytes=stderr,
        )

    @staticmethod
    async def _quiesce(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except (OSError, ProcessLookupError):
            pass
        try:
            await asyncio.wait_for(process.wait(), 2.0)
        except (TimeoutError, asyncio.CancelledError):
            try:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except (OSError, ProcessLookupError):
                pass
            await process.wait()


__all__ = ["VaultGitError", "VaultGitSettings", "VaultToolError", "VaultTools"]

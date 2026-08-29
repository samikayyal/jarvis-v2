"""Canonical Markdown path, content, commit, and diff validation policy."""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath, PureWindowsPath

from .common import _EXCLUDED_TOP_LEVEL_DIRECTORIES
from .errors import VaultWriteError
from .write_models import (
    _COMMIT_HASH,
    _MAX_COMMIT_BODY_CHARS,
    _MAX_COMMIT_SUBJECT_CHARS,
    _MAX_WRITE_CONTENT_BYTES,
    _MAX_WRITE_DIFF_CHARS,
    VaultWriteChange,
    VaultWriteRequest,
)

_UNIFIED_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


def render_vault_unified_diff(
    originals: Mapping[str, str | None], changes: Sequence[VaultWriteChange]
) -> str:
    """Render the deterministic diff retained for controlled repositories."""

    chunks: list[str] = []
    for change in sorted(changes, key=lambda item: item.path):
        old = originals.get(change.path)
        old_lines = [] if old is None else old.splitlines(keepends=True)
        new_lines = change.content.splitlines(keepends=True)
        from_file = "/dev/null" if old is None else f"a/{change.path}"
        raw_diff = list(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=from_file,
                tofile=f"b/{change.path}",
                n=3,
                lineterm="\n",
            )
        )
        if not raw_diff:
            continue
        diff: list[str] = []
        for line in raw_diff:
            diff.append(line)
            if not line.endswith("\n"):
                diff.append(r"\ No newline at end of file" + "\n")
        chunks.append("".join(diff).rstrip("\n") + "\n")
    patch = "".join(chunks)
    _validate_patch(patch)
    return patch


def render_vault_write_preview(request: VaultWriteRequest) -> str:
    """Render the complete human approval envelope, including every diff line."""

    lines = [
        "Knowledge-vault write proposal",
        f"Base commit: {request.base_commit}",
        "Paths:",
    ]
    lines.extend(f"- {change.path} ({change.operation})" for change in request.changes)
    lines.extend(
        (
            f"Commit identity: {request.commit_identity.name} <{request.commit_identity.email}>",
            f"Commit subject: {request.commit_subject}",
            "Commit body:",
            request.commit_body,
            "approval will commit and push precisely this patch.",
            "Complete unified diff:",
            request.patch,
            "End of complete unified diff.",
        )
    )
    return "\n".join(lines).removesuffix("\n")


def _commit_body(request_id: str, changes: Sequence[VaultWriteChange]) -> str:
    return "\n".join(
        (
            "Changed knowledge-vault note paths:",
            *(f"- {change.path}" for change in changes),
            f"Request ID: {request_id}",
        )
    )


def canonical_allowed_note_directories(
    value: Sequence[str],
) -> tuple[tuple[str, ...], ...]:
    if isinstance(value, (str, bytes)) or not value or len(value) > 16:
        raise ValueError("allowed note directories must be a bounded sequence")
    result: list[tuple[str, ...]] = []
    for raw in value:
        if not isinstance(raw, str) or not raw or "\\" in raw:
            raise ValueError("allowed note directories must be canonical paths")
        path = PurePosixPath(raw)
        if raw != "." and (
            path.is_absolute()
            or path.as_posix() != raw
            or any(part in {".", ".."} for part in path.parts)
        ):
            raise ValueError("allowed note directories must be canonical paths")
        parts = () if raw == "." else path.parts
        if any(part.startswith(".") for part in parts) or any(
            part in _EXCLUDED_TOP_LEVEL_DIRECTORIES for part in parts
        ):
            raise ValueError("allowed note directories include an excluded path")
        result.append(parts)
    if len(set(result)) != len(result):
        raise ValueError("allowed note directories must be unique")
    return tuple(sorted(result, key=lambda item: (len(item), item)))


def _canonical_note_path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > 512
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise VaultWriteError("path is not an ordinary knowledge-vault note")
    raw = PurePosixPath(value)
    if (
        "\\" in value
        or raw.is_absolute()
        or PureWindowsPath(value).is_absolute()
        or raw.as_posix() != value
        or any(part in {".", ".."} for part in raw.parts)
        or value.endswith("/")
        or raw.suffix != ".md"
        or any(part.startswith(".") for part in raw.parts)
        or any(part in _EXCLUDED_TOP_LEVEL_DIRECTORIES for part in raw.parts)
    ):
        raise VaultWriteError("path is not an ordinary knowledge-vault note")
    return value


def _validate_note_content(value: object) -> str:
    if not isinstance(value, str):
        raise VaultWriteError("knowledge-vault note content must be text")
    if "\x00" in value or "\r" in value:
        raise VaultWriteError(
            "knowledge-vault note content is not canonical UTF-8 text"
        )
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise VaultWriteError(
            "knowledge-vault note content is not valid UTF-8"
        ) from exc
    if len(encoded) > _MAX_WRITE_CONTENT_BYTES:
        raise VaultWriteError("knowledge-vault note exceeds the write byte limit")
    return value


def _validate_patch(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_WRITE_DIFF_CHARS:
        raise ValueError("knowledge-vault write diff must be bounded and non-blank")
    if "\r" in value or "\x00" in value:
        raise ValueError("knowledge-vault write diff contains invalid bytes")
    return value


def _validate_commit_subject(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > _MAX_COMMIT_SUBJECT_CHARS
        or "\n" in value
        or "\r" in value
        or not value.startswith("jarvis:")
    ):
        raise ValueError("vault commit subject must be a concise jarvis: subject")
    return value


def _validate_commit_body(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > _MAX_COMMIT_BODY_CHARS
    ):
        raise ValueError("vault commit body must be bounded and non-blank")
    if "\x00" in value or "\r" in value:
        raise ValueError("vault commit body contains invalid bytes")
    return value


def _canonical_text(value: object, name: str, *, max_length: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > max_length
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(f"{name} must be a canonical bounded string")
    return value


def _canonical_email(value: object) -> str:
    value = _canonical_text(value, "commit identity email", max_length=254)
    if value.count("@") != 1 or any(char.isspace() for char in value):
        raise ValueError("commit identity email is invalid")
    return value


def _commit_hash(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(_COMMIT_HASH, value):
        raise ValueError("vault base commit must be a Git commit hash")
    return value.lower()


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


__all__ = [
    "_UNIFIED_HUNK",
    "_canonical_email",
    "_canonical_note_path",
    "_canonical_text",
    "_commit_body",
    "_commit_hash",
    "_normalise_staged_diff",
    "_validate_commit_body",
    "_validate_commit_subject",
    "_validate_note_content",
    "_validate_patch",
    "canonical_allowed_note_directories",
    "render_vault_unified_diff",
    "render_vault_write_preview",
]

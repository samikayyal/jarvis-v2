"""Approval-frozen models and the narrow repository protocol for vault writes."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from ...models import FrozenActionProposal

KNOWLEDGE_VAULT_WRITE_KIND = "knowledge_vault_write"
KNOWLEDGE_VAULT_WRITE_SCHEMA = "knowledge_vault_write_v1"
DEFAULT_VAULT_COMMIT_NAME = "Jarvis"
DEFAULT_VAULT_COMMIT_EMAIL = "jarvis@samikayyal.com"
DEFAULT_VAULT_COMMIT_SUBJECT = "jarvis: update knowledge vault"

_MAX_WRITE_PATHS = 16
_MAX_WRITE_CONTENT_BYTES = 64 * 1024
_MAX_WRITE_DIFF_CHARS = 256 * 1024
_MAX_COMMIT_SUBJECT_CHARS = 200
_MAX_COMMIT_BODY_CHARS = 2_000
_COMMIT_HASH = r"^[0-9a-fA-F]{40,64}$"


class VaultWriteRepository(Protocol):
    """The narrow Git edge used by the write connector."""

    def is_clean(self, root: Path, *, deadline: float | None = None) -> bool: ...

    def synchronize(
        self, root: Path, *, now: datetime, deadline: float | None = None
    ) -> datetime: ...

    def current_commit(self, root: Path, *, deadline: float | None = None) -> str: ...

    def fetch_remote_commit(
        self, root: Path, *, deadline: float | None = None
    ) -> str: ...

    def render_diff(
        self,
        root: Path,
        originals: Mapping[str, str | None],
        changes: Mapping[str, str],
        *,
        deadline: float | None = None,
    ) -> str: ...

    def stage(
        self, root: Path, paths: Sequence[str], *, deadline: float | None = None
    ) -> None: ...

    def staged_diff(
        self, root: Path, *, deadline: float | None = None
    ) -> str | None: ...

    def commit(
        self,
        root: Path,
        *,
        author_name: str,
        author_email: str,
        subject: str,
        body: str,
        deadline: float | None = None,
    ) -> str: ...

    def push(
        self,
        root: Path,
        *,
        expected_base: str,
        commit_id: str,
        deadline: float | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class VaultCommitIdentity:
    """The configured, repository-local identity used for vault commits."""

    name: str = DEFAULT_VAULT_COMMIT_NAME
    email: str = DEFAULT_VAULT_COMMIT_EMAIL

    def __post_init__(self) -> None:
        from .write_policy import _canonical_email, _canonical_text

        _canonical_text(self.name, "commit identity name", max_length=100)
        _canonical_email(self.email)

    def as_payload(self) -> dict[str, str]:
        return {"name": self.name, "email": self.email}


@dataclass(frozen=True, slots=True)
class VaultWriteChange:
    """One allowed Markdown create or modification in a frozen proposal."""

    path: str
    operation: Literal["create", "modify"]
    content: str

    def __post_init__(self) -> None:
        from .write_policy import _canonical_note_path, _validate_note_content

        _canonical_note_path(self.path)
        if not isinstance(self.operation, str) or self.operation not in {
            "create",
            "modify",
        }:
            raise ValueError("vault write operation must be create or modify")
        _validate_note_content(self.content)

    def as_payload(self) -> dict[str, str]:
        return {
            "path": self.path,
            "operation": self.operation,
            "content": self.content,
        }


@dataclass(frozen=True, slots=True)
class VaultWriteRequest:
    """The complete typed request reconstructed from a frozen action."""

    base_commit: str
    changes: tuple[VaultWriteChange, ...]
    patch: str
    commit_identity: VaultCommitIdentity
    commit_subject: str
    commit_body: str

    def __post_init__(self) -> None:
        from .write_policy import (
            _commit_hash,
            _validate_commit_body,
            _validate_commit_subject,
            _validate_patch,
        )

        _commit_hash(self.base_commit)
        changes = tuple(self.changes)
        if not changes or len(changes) > _MAX_WRITE_PATHS:
            raise ValueError("vault write must contain between one and 16 changes")
        if any(not isinstance(change, VaultWriteChange) for change in changes):
            raise TypeError("changes must contain VaultWriteChange values")
        if tuple(change.path for change in changes) != tuple(
            sorted(change.path for change in changes)
        ):
            raise ValueError("vault write paths must be sorted canonically")
        if len({change.path for change in changes}) != len(changes):
            raise ValueError("vault write paths must be unique")
        if not isinstance(self.commit_identity, VaultCommitIdentity):
            raise TypeError("commit_identity must be a VaultCommitIdentity")
        _validate_patch(self.patch)
        _validate_commit_subject(self.commit_subject)
        _validate_commit_body(self.commit_body)
        object.__setattr__(self, "changes", changes)

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(change.path for change in self.changes)

    @classmethod
    def from_proposal(cls, proposal: FrozenActionProposal) -> VaultWriteRequest:
        payload = _parse_write_payload(proposal)
        raw_changes = payload["changes"]
        if not isinstance(raw_changes, list):
            raise TypeError("knowledge-vault write changes must be a list")
        changes = tuple(_change_from_payload(item) for item in raw_changes)
        paths = payload["paths"]
        if paths != [change.path for change in changes]:
            raise ValueError("knowledge-vault write paths do not match changes")
        identity_payload = payload["commit_identity"]
        if not isinstance(identity_payload, dict) or set(identity_payload) != {
            "name",
            "email",
        }:
            raise ValueError("knowledge-vault commit identity is malformed")
        try:
            identity = VaultCommitIdentity(
                name=identity_payload["name"], email=identity_payload["email"]
            )
            request = cls(
                base_commit=payload["base_commit"],
                changes=changes,
                patch=payload["diff"],
                commit_identity=identity,
                commit_subject=payload["commit_subject"],
                commit_body=payload["commit_body"],
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("knowledge-vault write payload is invalid") from exc
        from .write_policy import render_vault_write_preview

        if proposal.preview != render_vault_write_preview(request):
            raise ValueError("knowledge-vault write preview does not match payload")
        return request


class VaultWriteProposal:
    """Factory for the exact action stored by the generic approval flow."""

    @classmethod
    def create(
        cls,
        *,
        action_id: str,
        request_id: str,
        base_commit: str,
        changes: Sequence[VaultWriteChange | Mapping[str, object]],
        diff: str,
        commit_subject: str = DEFAULT_VAULT_COMMIT_SUBJECT,
        commit_body: str,
        commit_identity: VaultCommitIdentity | Mapping[str, object] | None = None,
    ) -> FrozenActionProposal:
        normalized_changes = tuple(_coerce_change(change) for change in changes)
        normalized_identity = _coerce_identity(commit_identity)
        request = VaultWriteRequest(
            base_commit=base_commit,
            changes=normalized_changes,
            patch=diff,
            commit_identity=normalized_identity,
            commit_subject=commit_subject,
            commit_body=commit_body,
        )
        from .write_policy import render_vault_write_preview

        return FrozenActionProposal.create(
            action_id=action_id,
            request_id=request_id,
            kind=KNOWLEDGE_VAULT_WRITE_KIND,
            preview=render_vault_write_preview(request),
            payload={
                "schema": KNOWLEDGE_VAULT_WRITE_SCHEMA,
                "base_commit": request.base_commit,
                "paths": list(request.paths),
                "changes": [change.as_payload() for change in request.changes],
                "diff": request.patch,
                "commit_identity": request.commit_identity.as_payload(),
                "commit_subject": request.commit_subject,
                "commit_body": request.commit_body,
            },
        )

    @classmethod
    def from_proposal(cls, proposal: FrozenActionProposal) -> VaultWriteRequest:
        """Parse a frozen proposal through the same public type as Gmail."""

        return VaultWriteRequest.from_proposal(proposal)


def _parse_write_payload(proposal: FrozenActionProposal) -> dict[str, object]:
    if proposal.kind != KNOWLEDGE_VAULT_WRITE_KIND:
        raise ValueError("proposal is not a knowledge-vault write")
    try:
        payload = json.loads(proposal.payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("knowledge-vault write payload is malformed") from exc
    if not isinstance(payload, dict):
        raise TypeError("knowledge-vault write payload must be an object")
    expected = {
        "schema",
        "base_commit",
        "paths",
        "changes",
        "diff",
        "commit_identity",
        "commit_subject",
        "commit_body",
    }
    if set(payload) != expected:
        raise ValueError("knowledge-vault write payload has an unexpected shape")
    if payload["schema"] != KNOWLEDGE_VAULT_WRITE_SCHEMA:
        raise ValueError("knowledge-vault write schema is unsupported")
    return payload


@dataclass(frozen=True, slots=True)
class VaultWriteDispatchResult:
    """The non-sensitive local result of one committed and pushed patch."""

    commit_id: str
    paths: tuple[str, ...]
    pushed: bool = True


def _coerce_change(value: VaultWriteChange | Mapping[str, object]) -> VaultWriteChange:
    if isinstance(value, VaultWriteChange):
        return value
    if not isinstance(value, Mapping) or set(value) != {"path", "operation", "content"}:
        raise ValueError("vault write change has an unexpected shape")
    return VaultWriteChange(
        path=value["path"],
        operation=value["operation"],
        content=value["content"],  # type: ignore[arg-type]
    )


def _change_from_payload(value: object) -> VaultWriteChange:
    try:
        return _coerce_change(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError("vault write change is invalid") from exc


def _coerce_identity(
    value: VaultCommitIdentity | Mapping[str, object] | None,
) -> VaultCommitIdentity:
    if value is None:
        return VaultCommitIdentity()
    if isinstance(value, VaultCommitIdentity):
        return value
    if not isinstance(value, Mapping) or set(value) != {"name", "email"}:
        raise ValueError("commit identity has an unexpected shape")
    return VaultCommitIdentity(name=value["name"], email=value["email"])  # type: ignore[arg-type]


__all__ = [
    "DEFAULT_VAULT_COMMIT_EMAIL",
    "DEFAULT_VAULT_COMMIT_NAME",
    "DEFAULT_VAULT_COMMIT_SUBJECT",
    "KNOWLEDGE_VAULT_WRITE_KIND",
    "KNOWLEDGE_VAULT_WRITE_SCHEMA",
    "VaultCommitIdentity",
    "VaultWriteChange",
    "VaultWriteDispatchResult",
    "VaultWriteProposal",
    "VaultWriteRepository",
    "VaultWriteRequest",
]

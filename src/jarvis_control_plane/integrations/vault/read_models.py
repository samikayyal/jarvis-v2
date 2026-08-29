"""Typed request, result, and synchronization contracts for vault reads."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .errors import (
    VaultPushPreDispatchFailure,
    VaultPushUnknownOutcome,
    VaultReadError,
    VaultRemoteUnavailable,
    VaultRepositoryConflict,
    VaultSynchronizationError,
)

_MAX_QUERY_CHARS = 200
_MAX_RETURNED_EXCERPTS = 8
_MAX_EXCERPT_CHARS = 600
_MAX_COMPLETE_NOTE_CHARS = 64 * 1024


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
    text: str = Field(min_length=1, max_length=_MAX_COMPLETE_NOTE_CHARS)
    complete: bool = False
    ends_with_newline: bool | None = None


class KnowledgeVaultReadResult(BaseModel):
    """The only vault content shape available through orchestration."""

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


__all__ = [
    "KnowledgeVaultReadResult",
    "VaultExcerpt",
    "VaultPushPreDispatchFailure",
    "VaultPushUnknownOutcome",
    "VaultReadError",
    "VaultReadInput",
    "VaultRemoteUnavailable",
    "VaultRepositoryConflict",
    "VaultSynchronizationError",
    "VaultSynchronizationMetadataStore",
    "VaultSynchronizer",
]

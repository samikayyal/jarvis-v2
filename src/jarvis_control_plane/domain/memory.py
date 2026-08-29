"""Explicit durable assistant-memory records and bounded selections."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .ingress_messaging import (
    _contains_credential_like_text,
    _non_empty_identifier,
    ensure_utc,
)


class MemoryLifecycle(str, Enum):
    """The durable lifecycle state of one explicitly saved memory."""

    ACTIVE = "active"
    REPLACED = "replaced"
    FORGOTTEN = "forgotten"


@dataclass(frozen=True, slots=True)
class DurableMemory:
    """One explicit assistant memory with content-free terminal states."""

    memory_id: str
    content: str | None
    created_at: datetime
    updated_at: datetime
    source_message_id: str | None = None
    status: MemoryLifecycle | str = MemoryLifecycle.ACTIVE
    credential_like: bool | None = None
    replaced_by_memory_id: str | None = None

    def __post_init__(self) -> None:
        _non_empty_identifier(self.memory_id, "memory_id")
        status = MemoryLifecycle(self.status)
        object.__setattr__(self, "status", status)
        if self.content is not None and (
            not isinstance(self.content, str) or not self.content.strip()
        ):
            raise ValueError("memory content must be non-blank when present")
        if status is MemoryLifecycle.ACTIVE and self.content is None:
            raise ValueError("active memories must retain their content")
        if status is not MemoryLifecycle.ACTIVE and self.content is not None:
            raise ValueError("terminal memories must remove their content")
        if self.source_message_id is not None:
            _non_empty_identifier(self.source_message_id, "source_message_id")
        if self.replaced_by_memory_id is not None:
            _non_empty_identifier(self.replaced_by_memory_id, "replaced_by_memory_id")
        if status is not MemoryLifecycle.REPLACED and self.replaced_by_memory_id:
            raise ValueError(
                "only replaced memories may reference a replacement memory"
            )
        if self.credential_like is not None and not isinstance(
            self.credential_like, bool
        ):
            raise TypeError("credential_like must be a boolean when provided")
        detected = (
            _contains_credential_like_text(self.content)
            if self.content is not None
            else False
        )
        object.__setattr__(
            self,
            "credential_like",
            bool(self.credential_like) or detected,
        )
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))
        object.__setattr__(self, "updated_at", ensure_utc(self.updated_at))

    @property
    def lifecycle(self) -> MemoryLifecycle:
        """Compatibility name for the explicit lifecycle state."""

        return self.status

    @property
    def is_active(self) -> bool:
        return self.status is MemoryLifecycle.ACTIVE

    @property
    def is_terminal(self) -> bool:
        return self.status is not MemoryLifecycle.ACTIVE

    @property
    def revision_digest(self) -> str:
        """Stable content-free revision token used by exact mutations."""

        material = "\x1f".join(
            (
                self.memory_id,
                self.content or "",
                self.created_at.isoformat(),
                self.updated_at.isoformat(),
                self.source_message_id or "",
                self.status.value,
                str(bool(self.credential_like)),
                self.replaced_by_memory_id or "",
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @property
    def provenance(self) -> str:
        """Bounded provenance text that never includes the saved content."""

        source = self.source_message_id or "none"
        return (
            f"memory {self.memory_id}; source message {source}; "
            f"created {self.created_at.isoformat()}; "
            f"updated {self.updated_at.isoformat()}; status {self.status.value}"
        )


# The domain phrase is useful to callers that prefer an assistant-facing name.
AssistantMemory = DurableMemory


@dataclass(frozen=True, slots=True)
class MemorySelection:
    """Bounded memory selected for orchestration context.

    Automatic retrieval is always non-secret.  The broker may instead create
    an explicit exact-record selection, which is the only path that can carry
    a credential-like memory into an orchestration request.
    """

    memories: tuple[DurableMemory, ...]
    explicit: bool = False

    def __post_init__(self) -> None:
        memories = tuple(self.memories)
        if any(not isinstance(memory, DurableMemory) for memory in memories):
            raise TypeError("memories must contain DurableMemory values")
        if any(not memory.is_active for memory in memories):
            raise ValueError(
                "automatic memory selection cannot include terminal records"
            )
        if not isinstance(self.explicit, bool):
            raise TypeError("memory selection explicit flag must be boolean")
        if self.explicit and len(memories) != 1:
            raise ValueError(
                "explicit memory selection requires exactly one active memory"
            )
        if not self.explicit and any(memory.credential_like for memory in memories):
            raise ValueError("automatic memory selection cannot include credentials")
        object.__setattr__(self, "memories", memories)

    @property
    def provenance_disclosure(self) -> str | None:
        if not self.memories:
            return None
        pointers = "; ".join(
            f"{memory.memory_id} from message {memory.source_message_id or 'none'}"
            for memory in self.memories
        )
        return f"Memory used: {pointers}."

"""Closed, bounded read tools for the orchestration agent."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ...models import OrchestrationRequest
from ...ports import OrchestrationAdapterError

__all__ = [
    "BoundedReadInput",
    "BoundedReadOutput",
    "BoundedReadTool",
    "_ToolInvocationBudget",
    "_excluded_capability_refusal",
    "_safe_unavailable_read_reason",
    "_unavailable_read_reply",
]

_MAX_READ_CHARS = 1_000
_READ_TOOL_TIMEOUT_SECONDS = 20.0
_MAX_READ_TOOL_SECONDS = _READ_TOOL_TIMEOUT_SECONDS
_READ_UNAVAILABLE_RESULT = {
    "unavailable": True,
    "message": (
        "The connected service is unavailable or not authorized. "
        "Explain that the requested read could not be completed, "
        "do not claim any retrieved data, and do not retry."
    ),
}
_READ_DEPENDENCY_NAMES = {
    "read_request_context": "request context",
    "read_gmail": "Gmail",
    "read_google_drive": "Google Drive",
    "read_knowledge_vault": "knowledge vault",
}
_GOOGLE_READ_FAILURE_REASONS = {
    "google_read_disconnected": "Google is disconnected",
    "google_read_unavailable": "Google is unavailable",
    "google_read_timeout": "Google timed out",
    "google_read_rate_limited": "Google rate limiting prevented the read",
    "missing_scope": "Google authorization is missing the required scope",
    "wrong_identity": "Google authorization uses the wrong identity",
}
_GOOGLE_CONTENT_UNAVAILABLE_REASONS = {
    "unsupported_mime_type": "Google Drive does not support reading binary file content",
}
_SERVICE_UNAVAILABLE_MESSAGE = "owned service is unavailable"
_VAULT_READ_FAILURE_REASONS = {
    "unsupported_file_type": "the requested path is not an ordinary Markdown note",
    "excluded_path": "the requested path is excluded from the knowledge-vault read boundary",
    "outside_root": "the requested path is outside the knowledge-vault read boundary",
    "path_not_found": "the requested path is not an ordinary note in the vault",
    "dirty_snapshot": "the knowledge vault clone is dirty",
    "clean_snapshot_unavailable": "the knowledge vault has no clean synchronized snapshot",
    "recovery_required": "the knowledge vault requires explicit recovery",
    "ambiguous_selector": "the vault selector did not identify one note",
}

_CALENDAR_REQUEST = re.compile(r"\b(?:google\s+)?calendar\b", re.IGNORECASE)
_GMAIL_DESTRUCTIVE_ACTION = (
    r"(?:delete|deleting|remove|removing|trash|trashing|purge|purging|"
    r"erase|erasing|destroy|destroying)"
)
_GMAIL_SERVICE = r"(?:gmail|e-?mail|mailbox|inbox)"
_GMAIL_DESTRUCTIVE_REQUESTS = (
    re.compile(
        rf"\b{_GMAIL_DESTRUCTIVE_ACTION}\b.{{0,120}}\b{_GMAIL_SERVICE}\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        rf"\b{_GMAIL_SERVICE}\b.{{0,120}}\b{_GMAIL_DESTRUCTIVE_ACTION}\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        rf"\b(?:deletion|removal|erasure|destruction)\s+of\b"
        rf".{{0,120}}\b{_GMAIL_SERVICE}\b",
        re.IGNORECASE | re.DOTALL,
    ),
)


def _excluded_capability_refusal(text: str) -> str | None:
    """Refuse closed v1 exclusions before a model can invoke any read tool."""

    if _CALENDAR_REQUEST.search(text):
        return (
            "Calendar is not available in Jarvis v1. "
            "No tool, proposal, pending action, or provider dispatch was created."
        )
    if any(pattern.search(text) for pattern in _GMAIL_DESTRUCTIVE_REQUESTS):
        return (
            "Destructive Gmail operations are not available in Jarvis v1. "
            "No Gmail read, proposal, pending action, or provider dispatch was created."
        )
    return None


def _safe_unavailable_read_reason(exc: Exception) -> str | None:
    if (
        type(exc).__module__ == "jarvis_control_plane.google_reads"
        and type(exc).__name__ == "GoogleReadError"
    ):
        code = str(exc)
        return _GOOGLE_READ_FAILURE_REASONS.get(code)
    from ...knowledge_vault import VaultReadError
    from ...service_protocol import (
        RemoteServiceError,
        ServiceAuthenticationError,
        ServiceProtocolError,
    )

    if isinstance(exc, VaultReadError):
        return _VAULT_READ_FAILURE_REASONS.get(exc.code)

    if isinstance(exc, RemoteServiceError):
        if exc.error_type == "GoogleReadError":
            return _GOOGLE_READ_FAILURE_REASONS.get(str(exc))
        if exc.error_type == "VaultReadError":
            return _VAULT_READ_FAILURE_REASONS.get(exc.code)
        return None
    if isinstance(exc, ServiceAuthenticationError):
        return "the service identity could not be verified"
    if type(exc) is ServiceProtocolError and str(exc) == _SERVICE_UNAVAILABLE_MESSAGE:
        return "the service could not be reached"
    return None


def _unavailable_read_reply(tool_name: str, reason: str) -> str:
    dependency = _READ_DEPENDENCY_NAMES[tool_name]
    return (
        f"The requested {dependency} read could not be completed because {reason}. "
        "I did not retry the unavailable read."
    )


_CLOSED_READ_TOOL_NAMES = frozenset(
    {
        "read_request_context",
        "read_gmail",
        "read_google_drive",
        "read_knowledge_vault",
    }
)


class BoundedReadInput(BaseModel):
    """Strict input schema for the one read-only context tool in Ticket 14."""

    model_config = ConfigDict(extra="forbid")

    max_chars: int = Field(default=_MAX_READ_CHARS, ge=1, le=_MAX_READ_CHARS)


class BoundedReadOutput(BaseModel):
    """Strict bounded output returned by a read tool."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["authorized_request"]
    text: str = Field(min_length=1, max_length=_MAX_READ_CHARS)


@dataclass(frozen=True, slots=True)
class BoundedReadTool:
    """Closed, typed read capability with no mutation or dispatch authority."""

    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: Callable[[OrchestrationRequest, BaseModel, float], BaseModel]
    timeout_seconds: float = _MAX_READ_TOOL_SECONDS

    def __post_init__(self) -> None:
        if self.name not in _CLOSED_READ_TOOL_NAMES:
            raise ValueError("bounded read tool is outside the closed tool set")
        if not self.description.strip():
            raise ValueError("bounded read tool description must be non-blank")
        if not isinstance(self.input_model, type) or not issubclass(
            self.input_model, BaseModel
        ):
            raise TypeError("bounded read input_model must be a Pydantic model")
        if not isinstance(self.output_model, type) or not issubclass(
            self.output_model, BaseModel
        ):
            raise TypeError("bounded read output_model must be a Pydantic model")
        if not callable(self.handler):
            raise TypeError("bounded read handler must be callable")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (float, int))
            or not 0 < self.timeout_seconds <= _MAX_READ_TOOL_SECONDS
        ):
            raise ValueError(
                f"bounded read timeout must be within 0 and {_MAX_READ_TOOL_SECONDS} seconds"
            )


@dataclass(slots=True)
class _ToolInvocationBudget:
    limit: int
    used: int = 0

    def consume(self) -> None:
        if self.used >= self.limit:
            raise OrchestrationAdapterError(
                "bounded read tool invocation limit exceeded"
            )
        self.used += 1

"""Typed Google read requests, results, bounds, and allowlisted operations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from ...ports import OrchestrationAdapterError

GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
DRIVE_READ_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
GOOGLE_READ_SCOPES = frozenset({GMAIL_READ_SCOPE, DRIVE_READ_SCOPE})

DEFAULT_MAX_RESULT_ITEMS = 20
MAX_RESULT_ITEMS = 50
DEFAULT_MAX_ITEM_BYTES = 16 * 1024
MAX_ITEM_BYTES = 64 * 1024
DEFAULT_MAX_RESULT_BYTES = 256 * 1024
MAX_RESULT_BYTES = 1024 * 1024
GOOGLE_READ_TRACE_PAYLOAD_LIMIT_BYTES = 2 * 1024 * 1024

GoogleReadOperation = Literal[
    "gmail_messages_list",
    "gmail_messages_get",
    "gmail_threads_list",
    "gmail_threads_get",
    "drive_files_list",
    "drive_files_get",
    "drive_files_export",
]

_OPERATION_SCOPES: dict[str, frozenset[str]] = {
    "gmail_messages_list": frozenset({GMAIL_READ_SCOPE}),
    "gmail_messages_get": frozenset({GMAIL_READ_SCOPE}),
    "gmail_threads_list": frozenset({GMAIL_READ_SCOPE}),
    "gmail_threads_get": frozenset({GMAIL_READ_SCOPE}),
    "drive_files_list": frozenset({DRIVE_READ_SCOPE}),
    "drive_files_get": frozenset({DRIVE_READ_SCOPE}),
    "drive_files_export": frozenset({DRIVE_READ_SCOPE}),
}
_SERVICE_BY_OPERATION = {
    operation: "gmail" if operation.startswith("gmail_") else "drive"
    for operation in _OPERATION_SCOPES
}


class GoogleReadError(OrchestrationAdapterError):
    """A stable, content-free Google-read outcome safe for orchestration."""


class GoogleReadProviderError(RuntimeError):
    """Private provider-edge failure; its details never cross the connector."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        super().__init__(detail or code)


@dataclass(frozen=True, slots=True)
class GoogleReadProviderResult:
    """Private provider output before connector truncation and sanitization."""

    items: tuple[str, ...] = ()
    continuation_token: str | None = None

    def __post_init__(self) -> None:
        if not all(isinstance(item, str) for item in self.items):
            raise TypeError("Google provider items must be text")
        if self.continuation_token is not None and (
            not isinstance(self.continuation_token, str)
            or not self.continuation_token.strip()
        ):
            raise ValueError("Google continuation token must be non-blank when present")


@dataclass(frozen=True, slots=True)
class GoogleReadRequest:
    """One internal, already allowlisted provider request without credentials."""

    operation: GoogleReadOperation
    arguments: Mapping[str, str]
    max_results: int


@dataclass(frozen=True, slots=True)
class GoogleReadTracePayload:
    """The full connector evidence retained before a sanitized result is returned."""

    provider_result: GoogleReadProviderResult
    result: GoogleReadResult


@dataclass(frozen=True, slots=True)
class GoogleReadResult:
    """Sanitized output allowed into orchestration request working data."""

    service: Literal["gmail", "drive"]
    operation: GoogleReadOperation
    items: tuple[str, ...]
    truncated: bool
    continuation_available: bool
    # Connector-owned evidence used only by the server-side orchestration seam.
    # It is deliberately excluded from the model-facing GoogleReadOutput.
    connection_generation: int = field(repr=False)
    content_available: bool | None = None
    content_unavailable_reason: Literal["unsupported_mime_type"] | None = None

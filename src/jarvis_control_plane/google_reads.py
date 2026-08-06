"""Closed, bounded Google read connector for the Ticket 17 capability surface.

The connector owns credential validation and only accepts the eight read
operations named by the V1 specification.  Its caller receives typed, bounded
content; credentials, provider payloads, and provider exception details remain
inside this boundary.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .google_oauth import GoogleCredentialStore, OAuthCredentialRecord
from .models import AuditEvidence, OrchestrationRequest
from .orchestration import BoundedReadTool
from .ports import (
    AuditBoundary,
    AuditWriteError,
    Clock,
    IdGenerator,
    OrchestrationAdapterError,
)

GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
CALENDAR_LIST_READ_SCOPE = (
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly"
)
CALENDAR_EVENTS_READ_SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"
DRIVE_READ_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
GOOGLE_READ_SCOPES = frozenset(
    {
        GMAIL_READ_SCOPE,
        CALENDAR_LIST_READ_SCOPE,
        CALENDAR_EVENTS_READ_SCOPE,
        DRIVE_READ_SCOPE,
    }
)

DEFAULT_MAX_RESULT_ITEMS = 20
MAX_RESULT_ITEMS = 50
DEFAULT_MAX_ITEM_BYTES = 16 * 1024
MAX_ITEM_BYTES = 64 * 1024
DEFAULT_MAX_RESULT_BYTES = 256 * 1024
MAX_RESULT_BYTES = 1024 * 1024

GoogleReadOperation = Literal[
    "gmail_messages_list",
    "gmail_messages_get",
    "gmail_threads_list",
    "gmail_threads_get",
    "calendar_list",
    "calendar_events_list",
    "calendar_events_get",
    "drive_files_list",
    "drive_files_get",
    "drive_files_export",
]

_OPERATION_SCOPES: dict[str, frozenset[str]] = {
    "gmail_messages_list": frozenset({GMAIL_READ_SCOPE}),
    "gmail_messages_get": frozenset({GMAIL_READ_SCOPE}),
    "gmail_threads_list": frozenset({GMAIL_READ_SCOPE}),
    "gmail_threads_get": frozenset({GMAIL_READ_SCOPE}),
    "calendar_list": frozenset({CALENDAR_LIST_READ_SCOPE}),
    "calendar_events_list": frozenset({CALENDAR_EVENTS_READ_SCOPE}),
    "calendar_events_get": frozenset({CALENDAR_EVENTS_READ_SCOPE}),
    "drive_files_list": frozenset({DRIVE_READ_SCOPE}),
    "drive_files_get": frozenset({DRIVE_READ_SCOPE}),
    "drive_files_export": frozenset({DRIVE_READ_SCOPE}),
}
_SERVICE_BY_OPERATION = {
    operation: "gmail"
    if operation.startswith("gmail_")
    else "calendar"
    if operation.startswith("calendar_")
    else "drive"
    for operation in _OPERATION_SCOPES
}


class GoogleReadError(OrchestrationAdapterError):
    """A stable, content-free Google-read outcome safe for orchestration."""


class GoogleReadProviderError(RuntimeError):
    """Private provider-edge failure; its details never cross the connector."""


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


class GoogleReadProvider(Protocol):
    """The connector-only provider edge; no generic HTTP surface is available."""

    def read(
        self, *, request: GoogleReadRequest, credential: OAuthCredentialRecord
    ) -> GoogleReadProviderResult: ...


class ControlledGoogleReadProvider:
    """Deterministic provider double used by contract and broker-seam tests."""

    def __init__(
        self,
        *,
        result: GoogleReadProviderResult | None = None,
        failure: str | None = None,
    ) -> None:
        self.result = result or GoogleReadProviderResult()
        self.failure = failure
        self.calls: list[tuple[str, dict[str, str], int]] = []

    def read(
        self, *, request: GoogleReadRequest, credential: OAuthCredentialRecord
    ) -> GoogleReadProviderResult:
        self.calls.append(
            (request.operation, dict(request.arguments), request.max_results)
        )
        if self.failure is not None:
            raise GoogleReadProviderError(self.failure)
        return self.result


@dataclass(frozen=True, slots=True)
class GoogleReadResult:
    """Sanitized output allowed into orchestration request working data."""

    service: Literal["gmail", "calendar", "drive"]
    operation: GoogleReadOperation
    items: tuple[str, ...]
    truncated: bool
    continuation_available: bool


class GoogleReadConnector:
    """Closed Gmail, Calendar, and Drive read capability for one Google subject."""

    def __init__(
        self,
        *,
        configured_identity: str,
        credential_store: GoogleCredentialStore,
        provider: GoogleReadProvider,
        audit: AuditBoundary,
        clock: Clock,
        ids: IdGenerator,
        max_result_items: int = DEFAULT_MAX_RESULT_ITEMS,
        max_item_bytes: int = DEFAULT_MAX_ITEM_BYTES,
        max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES,
    ) -> None:
        self._configured_identity = _non_blank(
            configured_identity, "configured_identity"
        )
        self._credential_store = credential_store
        self._provider = provider
        self._audit = audit
        self._clock = clock
        self._ids = ids
        self._max_result_items = _limit(
            max_result_items,
            MAX_RESULT_ITEMS,
            "max_result_items",
        )
        self._max_item_bytes = _limit(max_item_bytes, MAX_ITEM_BYTES, "max_item_bytes")
        self._max_result_bytes = _limit(
            max_result_bytes,
            MAX_RESULT_BYTES,
            "max_result_bytes",
        )

    def gmail_messages_list(
        self,
        *,
        request_id: str,
        query: str,
        max_results: int = DEFAULT_MAX_RESULT_ITEMS,
    ) -> GoogleReadResult:
        return self._read(
            request_id,
            "gmail_messages_list",
            {"query": _text(query, "query")},
            max_results,
        )

    def gmail_messages_get(
        self, *, request_id: str, message_id: str
    ) -> GoogleReadResult:
        return self._read(
            request_id,
            "gmail_messages_get",
            {"message_id": _non_blank(message_id, "message_id")},
            1,
        )

    def gmail_threads_list(
        self,
        *,
        request_id: str,
        query: str,
        max_results: int = DEFAULT_MAX_RESULT_ITEMS,
    ) -> GoogleReadResult:
        return self._read(
            request_id,
            "gmail_threads_list",
            {"query": _text(query, "query")},
            max_results,
        )

    def gmail_threads_get(self, *, request_id: str, thread_id: str) -> GoogleReadResult:
        return self._read(
            request_id,
            "gmail_threads_get",
            {"thread_id": _non_blank(thread_id, "thread_id")},
            1,
        )

    def calendar_list(self, *, request_id: str) -> GoogleReadResult:
        return self._read(request_id, "calendar_list", {}, self._max_result_items)

    def calendar_events_list(
        self,
        *,
        request_id: str,
        calendar_id: str,
        query: str,
        max_results: int = DEFAULT_MAX_RESULT_ITEMS,
    ) -> GoogleReadResult:
        return self._read(
            request_id,
            "calendar_events_list",
            {
                "calendar_id": _non_blank(calendar_id, "calendar_id"),
                "query": _text(query, "query"),
            },
            max_results,
        )

    def calendar_events_get(
        self, *, request_id: str, calendar_id: str, event_id: str
    ) -> GoogleReadResult:
        return self._read(
            request_id,
            "calendar_events_get",
            {
                "calendar_id": _non_blank(calendar_id, "calendar_id"),
                "event_id": _non_blank(event_id, "event_id"),
            },
            1,
        )

    def drive_files_list(
        self,
        *,
        request_id: str,
        query: str,
        max_results: int = DEFAULT_MAX_RESULT_ITEMS,
    ) -> GoogleReadResult:
        return self._read(
            request_id,
            "drive_files_list",
            {"query": _text(query, "query")},
            max_results,
        )

    def drive_files_get(self, *, request_id: str, file_id: str) -> GoogleReadResult:
        return self._read(
            request_id,
            "drive_files_get",
            {"file_id": _non_blank(file_id, "file_id")},
            1,
        )

    def drive_files_export(
        self, *, request_id: str, file_id: str, mime_type: str
    ) -> GoogleReadResult:
        return self._read(
            request_id,
            "drive_files_export",
            {
                "file_id": _non_blank(file_id, "file_id"),
                "mime_type": _non_blank(mime_type, "mime_type"),
            },
            1,
        )

    def _read(
        self,
        request_id: str,
        operation: GoogleReadOperation,
        arguments: Mapping[str, str],
        max_results: int,
    ) -> GoogleReadResult:
        request_id = _non_blank(request_id, "request_id")
        requested_count = min(_requested_count(max_results), self._max_result_items)
        credential = self._credential(operation)
        self._append_audit(
            request_id=request_id,
            operation=operation,
            outcome="attempted",
            execution_status="attempted",
        )
        try:
            provider_result = self._provider.read(
                request=GoogleReadRequest(
                    operation=operation,
                    arguments=dict(arguments),
                    max_results=requested_count,
                ),
                credential=credential,
            )
        except GoogleReadProviderError as exc:
            self._record_failure(request_id, operation)
            raise GoogleReadError(_safe_provider_failure(str(exc))) from exc
        except Exception as exc:
            self._record_failure(request_id, operation)
            raise GoogleReadError("google_read_unavailable") from exc
        if not isinstance(provider_result, GoogleReadProviderResult):
            self._record_failure(request_id, operation)
            raise GoogleReadError("google_read_unavailable")

        items, item_truncated = _bounded_items(
            provider_result.items,
            max_items=min(requested_count, self._max_result_items),
            max_item_bytes=self._max_item_bytes,
        )
        result = GoogleReadResult(
            service=_SERVICE_BY_OPERATION[operation],
            operation=operation,
            items=items,
            truncated=item_truncated or len(provider_result.items) > len(items),
            continuation_available=(
                provider_result.continuation_token is not None
                or len(provider_result.items) > len(items)
            ),
        )
        if len(_serialized_result(result)) > self._max_result_bytes:
            self._record_failure(request_id, operation)
            raise GoogleReadError("google_read_oversized")
        self._append_audit(
            request_id=request_id,
            operation=operation,
            outcome="completed",
            execution_status="completed",
        )
        return result

    def _record_failure(self, request_id: str, operation: GoogleReadOperation) -> None:
        try:
            self._append_audit(
                request_id=request_id,
                operation=operation,
                outcome="failed",
                execution_status="failed",
            )
        except GoogleReadError:
            # The provider call has already started.  Preserve the original
            # safe outcome and let the broker's outer audit/trace gates stop
            # any reply while local administration repairs audit storage.
            pass

    def _append_audit(
        self,
        *,
        request_id: str,
        operation: GoogleReadOperation,
        outcome: str,
        execution_status: str,
    ) -> None:
        try:
            self._audit.append(
                AuditEvidence(
                    evidence_id=self._ids.new_id("audit"),
                    kind="google_read",
                    occurred_at=self._clock.now(),
                    request_id=request_id,
                    outcome=outcome,
                    actor="google_connector",
                    operation_type=operation,
                    target_category="google_connector",
                    execution_status=execution_status,
                )
            )
        except (AuditWriteError, ValueError, TypeError, RuntimeError) as exc:
            raise GoogleReadError("google_read_audit_unavailable") from exc

    def _credential(self, operation: GoogleReadOperation) -> OAuthCredentialRecord:
        try:
            credential = self._credential_store.current
        except Exception as exc:
            raise GoogleReadError("google_read_unavailable") from exc
        if credential is None:
            raise GoogleReadError("google_read_disconnected")
        if credential.subject != self._configured_identity:
            raise GoogleReadError("wrong_identity")
        required_scopes = _OPERATION_SCOPES[operation]
        # ``_credential`` is called from ``_read`` immediately after the operation
        # is selected; keeping scope validation in the connector makes it impossible
        # for tool schemas or the provider to widen an OAuth grant.
        if (
            required_scopes is not None
            and not required_scopes <= credential.granted_scopes
        ):
            raise GoogleReadError("missing_scope")
        return credential


class GmailReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["messages_list", "messages_get", "threads_list", "threads_get"]
    query: str | None = Field(default=None, max_length=1000)
    message_id: str | None = Field(default=None, max_length=512)
    thread_id: str | None = Field(default=None, max_length=512)
    max_results: int = Field(
        default=DEFAULT_MAX_RESULT_ITEMS, ge=1, le=MAX_RESULT_ITEMS
    )

    @model_validator(mode="after")
    def _validate_shape(self) -> GmailReadInput:
        single = {"messages_get": self.message_id, "threads_get": self.thread_id}
        if self.operation in single and not single[self.operation]:
            raise ValueError("single Gmail reads require the matching identifier")
        if self.operation.endswith("list") and self.query is None:
            raise ValueError("Gmail list reads require a query")
        return self


class CalendarReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["calendar_list", "events_list", "events_get"]
    calendar_id: str | None = Field(default=None, max_length=512)
    event_id: str | None = Field(default=None, max_length=512)
    query: str | None = Field(default=None, max_length=1000)
    max_results: int = Field(
        default=DEFAULT_MAX_RESULT_ITEMS, ge=1, le=MAX_RESULT_ITEMS
    )

    @model_validator(mode="after")
    def _validate_shape(self) -> CalendarReadInput:
        if self.operation != "calendar_list" and not self.calendar_id:
            raise ValueError("event reads require calendar_id")
        if self.operation == "events_get" and not self.event_id:
            raise ValueError("event lookup requires event_id")
        if self.operation == "events_list" and self.query is None:
            raise ValueError("event list reads require a query")
        return self


class DriveReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["files_list", "files_get", "files_export"]
    query: str | None = Field(default=None, max_length=1000)
    file_id: str | None = Field(default=None, max_length=512)
    mime_type: str | None = Field(default=None, max_length=256)
    max_results: int = Field(
        default=DEFAULT_MAX_RESULT_ITEMS, ge=1, le=MAX_RESULT_ITEMS
    )

    @model_validator(mode="after")
    def _validate_shape(self) -> DriveReadInput:
        if self.operation == "files_list" and self.query is None:
            raise ValueError("Drive list reads require a query")
        if self.operation != "files_list" and not self.file_id:
            raise ValueError("Drive item reads require file_id")
        if self.operation == "files_export" and not self.mime_type:
            raise ValueError("Drive export requires mime_type")
        return self


class GoogleReadOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service: Literal["gmail", "calendar", "drive"]
    operation: GoogleReadOperation
    items: tuple[str, ...] = Field(max_length=MAX_RESULT_ITEMS)
    truncated: bool
    continuation_available: bool


def _google_read_tools(connector: GoogleReadConnector) -> tuple[BoundedReadTool, ...]:
    """Return the three closed Google service tools for orchestration injection."""
    if not isinstance(connector, GoogleReadConnector):
        raise TypeError("connector must be a GoogleReadConnector")

    def gmail(_request: OrchestrationRequest, input: BaseModel) -> BaseModel:
        if not isinstance(input, GmailReadInput):
            raise TypeError("read_gmail received an invalid input model")
        result = {
            "messages_list": lambda: connector.gmail_messages_list(
                request_id=_request.state.request_id,
                query=input.query or "",
                max_results=input.max_results,
            ),
            "messages_get": lambda: connector.gmail_messages_get(
                request_id=_request.state.request_id, message_id=input.message_id or ""
            ),
            "threads_list": lambda: connector.gmail_threads_list(
                request_id=_request.state.request_id,
                query=input.query or "",
                max_results=input.max_results,
            ),
            "threads_get": lambda: connector.gmail_threads_get(
                request_id=_request.state.request_id, thread_id=input.thread_id or ""
            ),
        }[input.operation]()
        return _output(result)

    def calendar(_request: OrchestrationRequest, input: BaseModel) -> BaseModel:
        if not isinstance(input, CalendarReadInput):
            raise TypeError("read_google_calendar received an invalid input model")
        if input.operation == "calendar_list":
            result = connector.calendar_list(request_id=_request.state.request_id)
        elif input.operation == "events_list":
            result = connector.calendar_events_list(
                request_id=_request.state.request_id,
                calendar_id=input.calendar_id or "",
                query=input.query or "",
                max_results=input.max_results,
            )
        else:
            result = connector.calendar_events_get(
                request_id=_request.state.request_id,
                calendar_id=input.calendar_id or "",
                event_id=input.event_id or "",
            )
        return _output(result)

    def drive(_request: OrchestrationRequest, input: BaseModel) -> BaseModel:
        if not isinstance(input, DriveReadInput):
            raise TypeError("read_google_drive received an invalid input model")
        if input.operation == "files_list":
            result = connector.drive_files_list(
                request_id=_request.state.request_id,
                query=input.query or "",
                max_results=input.max_results,
            )
        elif input.operation == "files_get":
            result = connector.drive_files_get(
                request_id=_request.state.request_id, file_id=input.file_id or ""
            )
        else:
            result = connector.drive_files_export(
                request_id=_request.state.request_id,
                file_id=input.file_id or "",
                mime_type=input.mime_type or "",
            )
        return _output(result)

    return (
        BoundedReadTool(
            "read_gmail",
            "Read only bounded Gmail messages or threads.",
            GmailReadInput,
            GoogleReadOutput,
            gmail,
        ),
        BoundedReadTool(
            "read_google_calendar",
            "Read only bounded Google Calendar lists and events.",
            CalendarReadInput,
            GoogleReadOutput,
            calendar,
        ),
        BoundedReadTool(
            "read_google_drive",
            "Read only bounded Google Drive metadata, content, or exports.",
            DriveReadInput,
            GoogleReadOutput,
            drive,
        ),
    )


def _output(result: GoogleReadResult) -> GoogleReadOutput:
    return GoogleReadOutput(
        service=result.service,
        operation=result.operation,
        items=result.items,
        truncated=result.truncated,
        continuation_available=result.continuation_available,
    )


def _non_blank(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{name} must be a non-blank canonical string")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or value.strip() != value or len(value) > 1000:
        raise ValueError(f"{name} must be canonical text up to 1000 characters")
    return value


def _limit(value: object, maximum: int, name: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value <= 0
        or value > maximum
    ):
        raise ValueError(f"{name} must be a positive integer no greater than {maximum}")
    return value


def _requested_count(value: object) -> int:
    return _limit(value, MAX_RESULT_ITEMS, "max_results")


def _bounded_items(
    items: Sequence[str], *, max_items: int, max_item_bytes: int
) -> tuple[tuple[str, ...], bool]:
    bounded: list[str] = []
    truncated = False
    for item in items[:max_items]:
        encoded = item.encode("utf-8")
        if len(encoded) > max_item_bytes:
            item = encoded[:max_item_bytes].decode("utf-8", errors="ignore")
            truncated = True
        bounded.append(item)
    return tuple(bounded), truncated


def _serialized_result(result: GoogleReadResult) -> bytes:
    return json.dumps(
        {
            "service": result.service,
            "operation": result.operation,
            "items": result.items,
            "truncated": result.truncated,
            "continuation_available": result.continuation_available,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _safe_provider_failure(detail: str) -> str:
    if detail == "timeout":
        return "google_read_timeout"
    if detail == "rate_limited":
        return "google_read_rate_limited"
    if detail == "invalid_grant":
        return "google_read_disconnected"
    return "google_read_unavailable"

"""Closed Google read tool schemas and orchestration adapters."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from ...models import OrchestrationRequest
from ...orchestration import BoundedReadTool
from .drive_parser import _TEXT_EXPORT_MIME_TYPES
from .read_models import (
    DEFAULT_MAX_RESULT_ITEMS,
    MAX_RESULT_ITEMS,
    GoogleReadOperation,
    GoogleReadResult,
)


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
        if (
            self.operation == "files_export"
            and self.mime_type not in _TEXT_EXPORT_MIME_TYPES
        ):
            raise ValueError("Drive export must use an approved text mime_type")
        return self


class GoogleReadOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service: Literal["gmail", "drive"]
    operation: GoogleReadOperation
    items: tuple[str, ...] = Field(max_length=MAX_RESULT_ITEMS)
    truncated: bool
    continuation_available: bool
    content_available: bool | None = None
    content_unavailable_reason: Literal["unsupported_mime_type"] | None = None
    _connection_generation: int | None = PrivateAttr(default=None)


def _google_read_tools(connector: object) -> tuple[BoundedReadTool, ...]:
    """Return the two closed Google service tools exposed by Jarvis v1."""

    required_operations = (
        "gmail_messages_list",
        "gmail_messages_get",
        "gmail_threads_list",
        "gmail_threads_get",
        "drive_files_list",
        "drive_files_get",
        "drive_files_export",
    )
    if any(
        not callable(getattr(connector, name, None)) for name in required_operations
    ):
        raise TypeError("connector must provide the closed Google read surface")

    def gmail(
        request: OrchestrationRequest, input: BaseModel, _deadline: float
    ) -> BaseModel:
        if not isinstance(input, GmailReadInput):
            raise TypeError("read_gmail received an invalid input model")
        result = {
            "messages_list": lambda: connector.gmail_messages_list(
                request_id=request.state.request_id,
                query=input.query or "",
                max_results=input.max_results,
            ),
            "messages_get": lambda: connector.gmail_messages_get(
                request_id=request.state.request_id, message_id=input.message_id or ""
            ),
            "threads_list": lambda: connector.gmail_threads_list(
                request_id=request.state.request_id,
                query=input.query or "",
                max_results=input.max_results,
            ),
            "threads_get": lambda: connector.gmail_threads_get(
                request_id=request.state.request_id, thread_id=input.thread_id or ""
            ),
        }[input.operation]()
        return _output(result)

    def drive(
        request: OrchestrationRequest, input: BaseModel, _deadline: float
    ) -> BaseModel:
        if not isinstance(input, DriveReadInput):
            raise TypeError("read_google_drive received an invalid input model")
        if input.operation == "files_list":
            result = connector.drive_files_list(
                request_id=request.state.request_id,
                query=input.query or "",
                max_results=input.max_results,
            )
        elif input.operation == "files_get":
            result = connector.drive_files_get(
                request_id=request.state.request_id, file_id=input.file_id or ""
            )
        else:
            result = connector.drive_files_export(
                request_id=request.state.request_id,
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
            "read_google_drive",
            "Read only bounded Google Drive metadata, content, or exports.",
            DriveReadInput,
            GoogleReadOutput,
            drive,
        ),
    )


def _output(result: GoogleReadResult) -> GoogleReadOutput:
    output = GoogleReadOutput(
        service=result.service,
        operation=result.operation,
        items=result.items,
        truncated=result.truncated,
        continuation_available=result.continuation_available,
        content_available=result.content_available,
        content_unavailable_reason=result.content_unavailable_reason,
    )
    # PrivateAttr keeps connector provenance off the tool schema and dump.
    output._connection_generation = result.connection_generation
    return output

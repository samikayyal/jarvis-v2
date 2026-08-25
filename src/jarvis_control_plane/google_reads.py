"""Closed, bounded Gmail and Drive read connector for Jarvis v1.

The connector owns credential validation and only accepts the seven read
operations named by the V1 specification.  Its caller receives typed, bounded
content; credentials, provider payloads, and provider exception details remain
inside this boundary.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Literal, Protocol
from urllib.parse import quote, urlencode

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from .google_auth import (
    GoogleRefreshTokenExchanger,
    GoogleTokenExchangeError,
)
from .google_http import (
    GOOGLE_HTTP_TIMEOUT_SECONDS,
    MAX_GOOGLE_HTTP_RESPONSE_BYTES,
    GoogleHttpError,
    GoogleHttpResponse,
    GoogleHttpTransport,
    UrllibGoogleHttpTransport,
    ensure_bounded_response_body,
)
from .google_oauth import (
    GoogleConnectionBinding,
    GoogleCredentialStore,
    GoogleOAuthLifecycle,
    OAuthCredentialRecord,
)
from .models import AuditEvidence, OrchestrationRequest
from .orchestration import BoundedReadTool
from .ports import (
    AuditBoundary,
    AuditWriteError,
    Clock,
    DiagnosticTraceError,
    IdGenerator,
    OrchestrationAdapterError,
    TraceCapacityError,
    TraceWriteError,
)
from .sessions import ServiceReadiness
from .traces import DiagnosticTraceRecorder

GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
DRIVE_READ_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
GOOGLE_READ_SCOPES = frozenset({GMAIL_READ_SCOPE, DRIVE_READ_SCOPE})

DEFAULT_MAX_RESULT_ITEMS = 20
MAX_RESULT_ITEMS = 50
DEFAULT_MAX_ITEM_BYTES = 16 * 1024
MAX_ITEM_BYTES = 64 * 1024
DEFAULT_MAX_RESULT_BYTES = 256 * 1024
MAX_RESULT_BYTES = 1024 * 1024
MAX_PROVIDER_RESPONSE_BYTES = MAX_GOOGLE_HTTP_RESPONSE_BYTES
# A single blocking HTTPS exchange may take up to five seconds.  The
# orchestration tool owns the 20-second deadline for the complete, possibly
# multi-request read (token refresh plus one or more fixed Google calls).
GOOGLE_READ_TRACE_PAYLOAD_LIMIT_BYTES = 2 * 1024 * 1024

_GMAIL_API_ROOT = "https://gmail.googleapis.com/gmail/v1/users/me"
_DRIVE_API_ROOT = "https://www.googleapis.com/drive/v3"
_TEXT_EXPORT_MIME_TYPES = frozenset({"text/plain", "text/csv", "text/markdown"})
_TEXT_MEDIA_MIME_TYPES = frozenset(
    {
        "text/plain",
        "text/csv",
        "text/markdown",
        "application/json",
        "application/xml",
        "text/xml",
        "application/yaml",
        "application/x-yaml",
        "text/yaml",
    }
)
_GMAIL_TEXT_MIME_TYPES = frozenset({"text/plain", "text/html"})
_GMAIL_METADATA_HEADERS = frozenset({"from", "to", "cc", "subject", "date"})

# Kept as read-side aliases for existing callers; ownership now lives in the
# neutral Google HTTP module rather than in this connector.
GoogleReadHttpResponse = GoogleHttpResponse
GoogleReadHttpTransport = GoogleHttpTransport
UrllibGoogleReadHttpTransport = UrllibGoogleHttpTransport

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


class GoogleApiReadProvider:
    """Live, fixed-surface Google reader; it has no generic API operation."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        transport: GoogleReadHttpTransport | None = None,
        timeout_seconds: float = GOOGLE_HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self._token_exchange = GoogleRefreshTokenExchanger(
            client_id=client_id,
            client_secret=client_secret,
            transport=transport,
            timeout_seconds=timeout_seconds,
        )
        self._transport = self._token_exchange.transport
        self._timeout_seconds = self._token_exchange.timeout_seconds

    def read(
        self, *, request: GoogleReadRequest, credential: OAuthCredentialRecord
    ) -> GoogleReadProviderResult:
        access_token = self._refresh_access_token(credential.refresh_token)
        response = self._authorized_get(request, access_token)
        if request.operation == "drive_files_export":
            return GoogleReadProviderResult(items=(self._decode_text_export(response),))
        payload = self._json_response(response)
        if request.operation == "drive_files_get":
            return self._drive_file_result(request, payload, access_token)
        return GoogleReadProviderResult(
            items=_response_items(request.operation, payload),
            continuation_token=_continuation_token(payload),
        )

    def _drive_file_result(
        self,
        request: GoogleReadRequest,
        metadata: Mapping[str, object],
        access_token: str,
    ) -> GoogleReadProviderResult:
        """Return metadata and, only for an allowlisted text file, its media.

        Google Workspace files retain the existing explicit ``files.export``
        route.  For ordinary Drive files, metadata establishes the MIME type
        before this connector asks for ``alt=media``; binary and unknown types
        are never downloaded.
        """

        mime_type = metadata.get("mimeType")
        result: dict[str, object] = dict(metadata)
        if mime_type in _TEXT_MEDIA_MIME_TYPES:
            media = self._authorized_drive_media_get(
                request.arguments["file_id"], access_token
            )
            result["content"] = self._decode_text_media(media, _TEXT_MEDIA_MIME_TYPES)
        else:
            # Metadata is safe to retain, but it must not look like a
            # successful content read to the orchestration boundary.
            result["content_unavailable"] = "unsupported_mime_type"
        return GoogleReadProviderResult(
            items=(
                json.dumps(
                    result, ensure_ascii=False, separators=(",", ":"), sort_keys=True
                ),
            )
        )

    def _refresh_access_token(self, refresh_token: str) -> str:
        try:
            return self._token_exchange.exchange(refresh_token).access_token
        except GoogleTokenExchangeError as exc:
            raise GoogleReadProviderError(exc.code, str(exc)) from exc

    def _request(
        self,
        *,
        method: Literal["GET", "POST"],
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> GoogleReadHttpResponse:
        try:
            return self._transport.request(
                method=method,
                url=url,
                headers=dict(headers),
                body=body,
                timeout_seconds=self._timeout_seconds,
            )
        except GoogleHttpError as exc:
            raise GoogleReadProviderError(exc.code, str(exc)) from exc

    def _authorized_get(
        self, request: GoogleReadRequest, access_token: str
    ) -> GoogleReadHttpResponse:
        url = _google_read_url(request)
        return self._request(
            method="GET",
            url=url,
            headers={"Authorization": f"Bearer {access_token}"},
            body=None,
        )

    def _authorized_drive_media_get(
        self, file_id: str, access_token: str
    ) -> GoogleReadHttpResponse:
        return self._request(
            method="GET",
            url=_drive_media_url(file_id),
            headers={"Authorization": f"Bearer {access_token}"},
            body=None,
        )

    def _json_response(
        self,
        response: GoogleReadHttpResponse,
    ) -> Mapping[str, object]:
        _ensure_response_size(response.body)
        if response.status_code != 200:
            _raise_http_failure(response)
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GoogleReadProviderError(
                "unavailable", "Google returned invalid JSON"
            ) from error
        if not isinstance(payload, dict):
            raise GoogleReadProviderError(
                "unavailable", "Google returned a non-object JSON body"
            )
        return payload

    def _decode_text_export(self, response: GoogleReadHttpResponse) -> str:
        content_type = _response_mime_type(response)
        if content_type not in _TEXT_EXPORT_MIME_TYPES:
            raise GoogleReadProviderError("unavailable", "Google export was not text")
        return self._decode_text_media(response, _TEXT_EXPORT_MIME_TYPES)

    def _decode_text_media(
        self,
        response: GoogleReadHttpResponse,
        approved_mime_types: frozenset[str],
    ) -> str:
        _ensure_response_size(response.body)
        if response.status_code != 200:
            _raise_http_failure(response)
        content_type = _response_mime_type(response)
        if content_type not in approved_mime_types:
            raise GoogleReadProviderError("unavailable", "Google export was not text")
        try:
            return response.body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise GoogleReadProviderError(
                "unavailable", "Google export was not UTF-8 text"
            ) from error


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


class GoogleReadConnector:
    """Closed Gmail and Drive read capability for one Google subject."""

    def __init__(
        self,
        *,
        configured_identity: str,
        credential_store: GoogleCredentialStore,
        provider: GoogleReadProvider,
        audit: AuditBoundary,
        trace: DiagnosticTraceRecorder,
        clock: Clock,
        ids: IdGenerator,
        connection_binding: GoogleConnectionBinding,
        on_invalid_grant: Callable[[int], object] | None = None,
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
        if not isinstance(connection_binding, GoogleConnectionBinding):
            raise TypeError("connection_binding must be a GoogleConnectionBinding")
        self._connection_binding = connection_binding
        if not isinstance(trace, DiagnosticTraceRecorder):
            raise TypeError("trace must be a DiagnosticTraceRecorder")
        self._trace = trace
        self._clock = clock
        self._ids = ids
        self._on_invalid_grant = on_invalid_grant or self._discard_invalid_credential
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

    def current(self) -> ServiceReadiness:
        """Return only the safe connection projection used by `/status`."""

        try:
            snapshot = self._connection_binding.snapshot()
            credential = snapshot.credential
        except Exception:  # noqa: BLE001 - credential failures become safe unknown state
            return ServiceReadiness("google", "unknown")
        if (
            not snapshot.connection.connected
            or credential is None
            or credential.subject != self._configured_identity
            or credential.connection_generation != snapshot.connection.generation
        ):
            return ServiceReadiness("google", "unavailable")
        return ServiceReadiness("google", "ready")

    def current_connection_generation(self) -> int:
        """Return the connector-owned generation for the orchestration seam."""

        return self._connection_binding.snapshot().connection.generation

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
        self._append_audit(
            request_id=request_id,
            operation=operation,
            outcome="attempted",
            execution_status="attempted",
        )
        credential_validated = False
        try:
            # The lifecycle's synchronization lock keeps the credential and
            # connection generation bound together through the provider call.
            # A reconnect cannot occur between the read admission and the
            # provider result that is attached to this observation.
            with self._connection_binding.synchronization_lock:
                credential, connection_generation = self._credential(operation)
                credential_validated = True
                provider_request = GoogleReadRequest(
                    operation=operation,
                    arguments=dict(arguments),
                    max_results=requested_count,
                )
                trace_payload = self._trace.execute(
                    # The broker owns the parent request's full trace reservation
                    # while the model is running.  A deterministic child key gives
                    # this connector its own pre-reserved complete-payload budget
                    # in the same trace store without competing with that parent.
                    request_id=f"{request_id}:google:{operation}",
                    operation_id=f"{request_id}:connector:google:{operation}",
                    operation_type="google_read_connector",
                    input_payload=provider_request,
                    arguments={
                        "parent_request_id": request_id,
                        "operation": operation,
                        "arguments": dict(arguments),
                        "max_results": requested_count,
                    },
                    telemetry={"service": _SERVICE_BY_OPERATION[operation]},
                    operation=lambda: self._read_provider_result(
                        request=provider_request,
                        credential=credential,
                        connection_generation=connection_generation,
                    ),
                    result_limit_bytes=GOOGLE_READ_TRACE_PAYLOAD_LIMIT_BYTES,
                    error_limit_bytes=GOOGLE_READ_TRACE_PAYLOAD_LIMIT_BYTES,
                )
        except GoogleReadError:
            if credential_validated:
                self._record_failure(request_id, operation)
            else:
                # Admission failures happen before a provider attempt.  Keep
                # the historical audit contract: if the failure record cannot
                # be written, expose that bounded administrative failure.
                self._append_audit(
                    request_id=request_id,
                    operation=operation,
                    outcome="failed",
                    execution_status="failed",
                )
            raise
        except GoogleReadProviderError as exc:
            if exc.code == "invalid_grant":
                try:
                    self._on_invalid_grant(credential.connection_generation)
                except Exception as cleanup_error:
                    self._record_failure(request_id, operation)
                    raise GoogleReadError("google_read_unavailable") from cleanup_error
            self._record_failure(request_id, operation)
            raise GoogleReadError(_safe_provider_failure(exc.code)) from exc
        except (DiagnosticTraceError, TraceCapacityError, TraceWriteError) as exc:
            self._record_failure(request_id, operation)
            raise GoogleReadError("google_read_trace_unavailable") from exc
        except Exception as exc:
            self._record_failure(request_id, operation)
            raise GoogleReadError("google_read_unavailable") from exc
        self._append_audit(
            request_id=request_id,
            operation=operation,
            outcome="completed",
            execution_status="completed",
        )
        return trace_payload.result

    def _read_provider_result(
        self,
        *,
        request: GoogleReadRequest,
        credential: OAuthCredentialRecord,
        connection_generation: int,
    ) -> GoogleReadTracePayload:
        provider_result = self._provider.read(request=request, credential=credential)
        if not isinstance(provider_result, GoogleReadProviderResult):
            raise GoogleReadProviderError(
                "unavailable", "provider returned an invalid result"
            )
        items, item_truncated = _bounded_items(
            provider_result.items,
            max_items=min(request.max_results, self._max_result_items),
            max_item_bytes=self._max_item_bytes,
        )
        result = GoogleReadResult(
            service=_SERVICE_BY_OPERATION[request.operation],
            operation=request.operation,
            items=items,
            truncated=item_truncated or len(provider_result.items) > len(items),
            continuation_available=(
                provider_result.continuation_token is not None
                or len(provider_result.items) > len(items)
            ),
            content_available=_content_available(request.operation, items),
            content_unavailable_reason=_content_unavailable_reason(
                request.operation, items
            ),
            connection_generation=connection_generation,
        )
        if len(_serialized_result(result)) > self._max_result_bytes:
            raise GoogleReadError("google_read_oversized")
        return GoogleReadTracePayload(provider_result=provider_result, result=result)

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

    def _credential(
        self, operation: GoogleReadOperation
    ) -> tuple[OAuthCredentialRecord, int]:
        try:
            snapshot = self._connection_binding.snapshot()
            connection = snapshot.connection
            credential = snapshot.credential
        except Exception as exc:
            raise GoogleReadError("google_read_unavailable") from exc
        if not connection.connected or credential is None:
            raise GoogleReadError("google_read_disconnected")
        if credential.subject != self._configured_identity:
            raise GoogleReadError("wrong_identity")
        if credential.connection_generation != connection.generation:
            raise GoogleReadError("google_read_unavailable")
        required_scopes = _OPERATION_SCOPES[operation]
        # ``_credential`` is called from ``_read`` immediately after the operation
        # is selected; keeping scope validation in the connector makes it impossible
        # for tool schemas or the provider to widen an OAuth grant.
        if (
            required_scopes is not None
            and not required_scopes <= credential.granted_scopes
        ):
            raise GoogleReadError("missing_scope")
        return credential, connection.generation

    def _discard_invalid_credential(self, connection_generation: int) -> None:
        credential = self._credential_store.current
        if (
            credential is not None
            and credential.connection_generation == connection_generation
        ):
            self._credential_store.delete()


def build_live_google_read_connector(
    *,
    configured_identity: str,
    credential_store: GoogleCredentialStore,
    oauth_lifecycle: GoogleOAuthLifecycle,
    client_id: str,
    client_secret: str,
    audit: AuditBoundary,
    trace: DiagnosticTraceRecorder,
    clock: Clock,
    ids: IdGenerator,
    transport: GoogleReadHttpTransport | None = None,
) -> GoogleReadConnector:
    """Compose the live Google reader with the OAuth invalidation authority.

    This is the production construction path: it intentionally uses the real
    fixed-surface HTTP provider and delegates ``invalid_grant`` to the OAuth
    lifecycle, which deletes the credential and durably marks the connection
    disconnected before the read result returns to orchestration.
    """

    provider = GoogleApiReadProvider(
        client_id=client_id,
        client_secret=client_secret,
        transport=transport,
    )
    return GoogleReadConnector(
        configured_identity=configured_identity,
        credential_store=credential_store,
        provider=provider,
        audit=audit,
        trace=trace,
        clock=clock,
        ids=ids,
        connection_binding=oauth_lifecycle.connection_binding,
        on_invalid_grant=lambda connection_generation: (
            oauth_lifecycle.handle_refresh_failure(
                "invalid_grant", connection_generation=connection_generation
            )
        ),
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
        _request: OrchestrationRequest, input: BaseModel, _deadline: float
    ) -> BaseModel:
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

    def drive(
        _request: OrchestrationRequest, input: BaseModel, _deadline: float
    ) -> BaseModel:
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
    # Keep connector-owned provenance on the server-side model instance only.
    # PrivateAttr is absent from both the tool schema and model_dump output.
    output._connection_generation = result.connection_generation
    return output


def _non_blank(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{name} must be a non-blank canonical string")
    return value


def _content_available(
    operation: GoogleReadOperation, items: Sequence[str]
) -> bool | None:
    if operation != "drive_files_get" or not items:
        return None
    try:
        payload = json.loads(items[0])
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    if payload.get("content_unavailable") == "unsupported_mime_type":
        return False
    if "content" in payload:
        return True
    return False if isinstance(payload.get("mimeType"), str) else None


def _content_unavailable_reason(
    operation: GoogleReadOperation, items: Sequence[str]
) -> Literal["unsupported_mime_type"] | None:
    if operation != "drive_files_get" or not items:
        return None
    try:
        payload = json.loads(items[0])
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    reason = payload.get("content_unavailable")
    if reason == "unsupported_mime_type":
        return reason
    if "content" not in payload and isinstance(payload.get("mimeType"), str):
        return "unsupported_mime_type"
    return None


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
            "content_available": result.content_available,
            "content_unavailable_reason": result.content_unavailable_reason,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _ensure_response_size(body: bytes) -> None:
    try:
        ensure_bounded_response_body(body)
    except GoogleHttpError as exc:
        raise GoogleReadProviderError(exc.code, str(exc)) from exc


def _raise_http_failure(response: GoogleReadHttpResponse) -> None:
    detail = response.body.decode("utf-8", errors="replace")
    code = "unavailable"
    if response.status_code == 429:
        code = "rate_limited"
    raise GoogleReadProviderError(code, detail)


def _google_read_url(request: GoogleReadRequest) -> str:
    operation = request.operation
    arguments = request.arguments
    if operation == "gmail_messages_list":
        return _url(
            f"{_GMAIL_API_ROOT}/messages",
            {
                "q": arguments["query"],
                "maxResults": request.max_results,
                "fields": "messages(id,threadId),nextPageToken,resultSizeEstimate",
            },
        )
    if operation == "gmail_messages_get":
        return _url(
            f"{_GMAIL_API_ROOT}/messages/{quote(arguments['message_id'], safe='')}",
            {
                "format": "full",
                "metadataHeaders": ("From", "To", "Cc", "Subject", "Date"),
                "fields": _gmail_full_fields(),
            },
        )
    if operation == "gmail_threads_list":
        return _url(
            f"{_GMAIL_API_ROOT}/threads",
            {
                "q": arguments["query"],
                "maxResults": request.max_results,
                "fields": "threads(id,historyId,snippet),nextPageToken,resultSizeEstimate",
            },
        )
    if operation == "gmail_threads_get":
        return _url(
            f"{_GMAIL_API_ROOT}/threads/{quote(arguments['thread_id'], safe='')}",
            {
                "format": "full",
                "metadataHeaders": ("From", "To", "Cc", "Subject", "Date"),
                "fields": "id,historyId,messages(" + _gmail_message_fields() + ")",
            },
        )
    if operation == "drive_files_list":
        return _url(
            f"{_DRIVE_API_ROOT}/files",
            {
                "q": arguments["query"],
                "pageSize": request.max_results,
                "fields": "files(id,name,mimeType,description,modifiedTime,size,webViewLink),nextPageToken",
            },
        )
    if operation == "drive_files_get":
        return _url(
            f"{_DRIVE_API_ROOT}/files/{quote(arguments['file_id'], safe='')}",
            {"fields": "id,name,mimeType,description,modifiedTime,size,webViewLink"},
        )
    if operation == "drive_files_export":
        mime_type = arguments["mime_type"]
        if mime_type not in _TEXT_EXPORT_MIME_TYPES:
            raise GoogleReadProviderError(
                "unavailable", "Drive export mime type was not allowed"
            )
        return _url(
            f"{_DRIVE_API_ROOT}/files/{quote(arguments['file_id'], safe='')}/export",
            {"mimeType": mime_type},
        )
    raise AssertionError(f"unsupported Google read operation: {operation}")


def _url(endpoint: str, query: Mapping[str, object]) -> str:
    flattened: list[tuple[str, str]] = []
    for key, value in query.items():
        if isinstance(value, tuple):
            flattened.extend((key, str(item)) for item in value)
        else:
            flattened.append((key, str(value)))
    return f"{endpoint}?{urlencode(flattened)}"


def _response_items(
    operation: GoogleReadOperation, payload: Mapping[str, object]
) -> tuple[str, ...]:
    collection_key = {
        "gmail_messages_list": "messages",
        "gmail_threads_list": "threads",
        "drive_files_list": "files",
    }.get(operation)
    if operation in {"gmail_messages_get", "gmail_threads_get"}:
        return _gmail_response_items(operation, payload)
    values: object = (
        payload if collection_key is None else payload.get(collection_key, ())
    )
    if not isinstance(values, (Mapping, list, tuple)):
        raise GoogleReadProviderError(
            "unavailable", "Google response had an invalid result shape"
        )
    rows = (values,) if isinstance(values, Mapping) else values
    if not all(isinstance(row, Mapping) for row in rows):
        raise GoogleReadProviderError(
            "unavailable", "Google response had an invalid item"
        )
    return tuple(
        json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        for row in rows
    )


def _gmail_full_fields() -> str:
    return _gmail_message_fields()


def _gmail_message_fields() -> str:
    return (
        "id,threadId,internalDate,labelIds,sizeEstimate,snippet,"
        "payload(headers,mimeType,filename,body(size,data,attachmentId),"
        "parts(headers,mimeType,filename,body(size,data,attachmentId),"
        "parts(headers,mimeType,filename,body(size,data,attachmentId))))"
    )


def _gmail_response_items(
    operation: GoogleReadOperation, payload: Mapping[str, object]
) -> tuple[str, ...]:
    if operation == "gmail_messages_get":
        messages: object = (payload,)
    else:
        messages = payload.get("messages", ())
    if not isinstance(messages, tuple | list) or not all(
        isinstance(message, Mapping) for message in messages
    ):
        raise GoogleReadProviderError(
            "unavailable", "Gmail response had invalid messages"
        )
    if operation == "gmail_threads_get" and not messages:
        return (
            json.dumps(
                {key: payload[key] for key in ("id", "historyId") if key in payload},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
    return tuple(
        json.dumps(
            _gmail_message_with_text(message),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        for message in messages
    )


def _gmail_message_with_text(message: Mapping[str, object]) -> dict[str, object]:
    result = {
        key: message[key]
        for key in (
            "id",
            "threadId",
            "internalDate",
            "labelIds",
            "sizeEstimate",
            "snippet",
        )
        if key in message
    }
    payload = message.get("payload")
    if not isinstance(payload, Mapping):
        return result
    headers = _gmail_headers(payload)
    if headers:
        result["headers"] = headers
    text = _gmail_payload_text(payload)
    if text:
        result["body"] = text
    return result


def _gmail_headers(payload: Mapping[str, object]) -> dict[str, str]:
    raw_headers = payload.get("headers", ())
    if not isinstance(raw_headers, tuple | list):
        return {}
    headers: dict[str, str] = {}
    for raw_header in raw_headers:
        if not isinstance(raw_header, Mapping):
            continue
        name = raw_header.get("name")
        value = raw_header.get("value")
        if (
            isinstance(name, str)
            and isinstance(value, str)
            and name.lower() in _GMAIL_METADATA_HEADERS
        ):
            headers[name] = value
    return headers


def _gmail_payload_text(payload: Mapping[str, object]) -> str:
    """Extract only inline approved text; attachment bytes stay at Google."""

    text_parts: list[str] = []
    pending: list[Mapping[str, object]] = [payload]
    while pending:
        part = pending.pop()
        nested = part.get("parts", ())
        if isinstance(nested, tuple | list):
            # ``pending`` is LIFO; reverse nested parts so the returned text
            # stays in the message's original MIME order.
            pending.extend(
                reversed([child for child in nested if isinstance(child, Mapping)])
            )
        mime_type = part.get("mimeType")
        if mime_type not in _GMAIL_TEXT_MIME_TYPES or _is_attachment_part(part):
            continue
        body = part.get("body")
        if not isinstance(body, Mapping) or not isinstance(body.get("data"), str):
            continue
        decoded = _decode_gmail_part(body["data"])
        if decoded is None:
            continue
        if mime_type == "text/html":
            decoded = _html_to_text(decoded)
        if decoded:
            text_parts.append(decoded)
    return "\n\n".join(text_parts)


def _is_attachment_part(part: Mapping[str, object]) -> bool:
    if isinstance(part.get("filename"), str) and part["filename"]:
        return True
    body = part.get("body")
    if isinstance(body, Mapping) and body.get("attachmentId") is not None:
        return True
    for header in (
        part.get("headers", ()) if isinstance(part.get("headers"), tuple | list) else ()
    ):
        if not isinstance(header, Mapping):
            continue
        name = header.get("name")
        value = header.get("value")
        if (
            isinstance(name, str)
            and isinstance(value, str)
            and name.lower() == "content-disposition"
            and value.lower().lstrip().startswith("attachment")
        ):
            return True
    return False


def _decode_gmail_part(data: str) -> str | None:
    try:
        padding = "=" * (-len(data) % 4)
        raw = base64.b64decode(data + padding, altchars=b"-_", validate=True)
        return raw.decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return None


class _HtmlTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._ignored_depth += 1
        if tag in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def _html_to_text(value: str) -> str:
    parser = _HtmlTextExtractor()
    parser.feed(value)
    parser.close()
    return " ".join("".join(parser.parts).split())


def _response_mime_type(response: GoogleReadHttpResponse) -> str:
    # HTTP header names are case-insensitive.  urllib normally preserves the
    # provider spelling, while gateways and test transports may normalize it.
    # Looking up one exact spelling would turn a valid text export into the
    # generic Google-unavailable outcome.
    for name, value in response.headers.items():
        if name.lower() == "content-type":
            return value.split(";", 1)[0].strip().lower()
    return ""


def _drive_media_url(file_id: str) -> str:
    return _url(f"{_DRIVE_API_ROOT}/files/{quote(file_id, safe='')}", {"alt": "media"})


def _continuation_token(payload: Mapping[str, object]) -> str | None:
    token = payload.get("nextPageToken")
    if token is None:
        return None
    if not isinstance(token, str) or not token:
        raise GoogleReadProviderError(
            "unavailable", "Google response had an invalid page token"
        )
    return token


def _safe_provider_failure(detail: str) -> str:
    if detail == "timeout":
        return "google_read_timeout"
    if detail == "rate_limited":
        return "google_read_rate_limited"
    if detail == "invalid_grant":
        return "google_read_disconnected"
    if detail == "oversized":
        return "google_read_oversized"
    return "google_read_unavailable"

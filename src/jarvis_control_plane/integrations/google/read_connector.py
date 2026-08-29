"""Bounded Google read connector and its lifecycle-aware composition root."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from ...models import AuditEvidence
from ...ports import (
    AuditBoundary,
    AuditWriteError,
    Clock,
    DiagnosticTraceError,
    IdGenerator,
    TraceCapacityError,
    TraceWriteError,
)
from ...sessions import ServiceReadiness
from ...traces import DiagnosticTraceRecorder
from .credentials import GoogleConnectionBinding, GoogleCredentialStore
from .http import GoogleHttpTransport
from .oauth_lifecycle import GoogleOAuthLifecycle
from .oauth_models import OAuthCredentialRecord
from .read_models import (
    _OPERATION_SCOPES,
    _SERVICE_BY_OPERATION,
    DEFAULT_MAX_ITEM_BYTES,
    DEFAULT_MAX_RESULT_BYTES,
    DEFAULT_MAX_RESULT_ITEMS,
    GOOGLE_READ_TRACE_PAYLOAD_LIMIT_BYTES,
    MAX_ITEM_BYTES,
    MAX_RESULT_BYTES,
    MAX_RESULT_ITEMS,
    GoogleReadError,
    GoogleReadOperation,
    GoogleReadProviderError,
    GoogleReadProviderResult,
    GoogleReadRequest,
    GoogleReadResult,
    GoogleReadTracePayload,
)
from .read_policy import (
    _bounded_items,
    _content_available,
    _content_unavailable_reason,
    _limit,
    _non_blank,
    _requested_count,
    _safe_provider_failure,
    _serialized_result,
    _text,
)
from .read_provider import GoogleApiReadProvider, GoogleReadProvider


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
            max_result_items, MAX_RESULT_ITEMS, "max_result_items"
        )
        self._max_item_bytes = _limit(max_item_bytes, MAX_ITEM_BYTES, "max_item_bytes")
        self._max_result_bytes = _limit(
            max_result_bytes, MAX_RESULT_BYTES, "max_result_bytes"
        )

    def current(self) -> ServiceReadiness:
        """Return only the safe connection projection used by /status."""

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
            # The synchronization lock binds credential and generation through
            # the provider call so a reconnect cannot interleave.
            with self._connection_binding.synchronization_lock:
                credential, connection_generation = self._credential(operation)
                credential_validated = True
                provider_request = GoogleReadRequest(
                    operation=operation,
                    arguments=dict(arguments),
                    max_results=requested_count,
                )
                trace_payload = self._trace.execute(
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
            # Preserve the original safe outcome while the outer audit gate
            # prevents a reply until local administration repairs storage.
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
        if not required_scopes <= credential.granted_scopes:
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
    transport: GoogleHttpTransport | None = None,
) -> GoogleReadConnector:
    """Compose the live reader with the OAuth invalidation authority."""

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

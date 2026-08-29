"""Closed Gmail new-send and typed-reply connector for Ticket 18.

Only a fully frozen :class:`FrozenActionProposal` reaches this module.  The
proposal parser recreates the canonical delivery request and its preview before
the provider edge is contacted, so model-controlled prose cannot hide a
recipient, MIME field, reply header, or threading decision.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from threading import RLock

from ....acceptance_failpoints import (
    ReviewedPostDispatchFailpoint,
    ReviewedPostDispatchFailure,
)
from ....google_http import GoogleHttpTransport
from ....google_oauth import (
    GoogleConnectionBinding,
    GoogleConnectionSnapshot,
    GoogleConnectionState,
    GoogleCredentialStore,
    GoogleOAuthLifecycle,
    OAuthCredentialRecord,
)
from ....models import AuditEvidence, FrozenActionProposal
from ....ports import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ActionDispatcherError,
    ActionDispatchHandle,
    AuditBoundary,
    AuditWriteError,
    Clock,
    DiagnosticTraceError,
    IdGenerator,
    TraceCapacityError,
    TraceWriteError,
)
from ....traces import DiagnosticTraceRecorder
from .actions import (
    GMAIL_SEND_SCOPE,
    GmailReplyRequest,
    GmailWriteRequest,
    gmail_proposal_payload,
    gmail_proposal_preview,
    gmail_write_request_from_proposal,
)
from .write_provider import (
    _GMAIL_SEND_URL,  # noqa: F401
    _IDENTIFIER,  # noqa: F401
    GMAIL_WRITE_TIMEOUT_SECONDS,  # noqa: F401
    ControlledGmailWriteProvider,  # noqa: F401
    GmailApiWriteProvider,
    GmailDeliveryResult,  # noqa: F401
    GmailWriteProvider,
    GmailWriteProviderError,
    GmailWriteProviderResult,
    _canonical_string,
    _encode_rfc822,  # noqa: F401
    _identifier,  # noqa: F401
    _json_object,  # noqa: F401
    _response_trace,  # noqa: F401
    _token_request_trace,  # noqa: F401
)

GMAIL_WRITE_TRACE_PAYLOAD_LIMIT_BYTES = 2 * 1024 * 1024


class _GmailWriteDispatch:
    """Prepared Gmail write that can be cancelled before the provider call."""

    def __init__(
        self, owner: GmailWriteConnector, action: FrozenActionProposal
    ) -> None:
        self._owner = owner
        self._action = action
        self._lock = RLock()
        self._started = False
        self._cancelled = False

    def run(self) -> object | None:
        with self._lock:
            if self._cancelled:
                self._owner._forget(self._action.action_id, self)
                raise ActionDispatcherError(
                    "Gmail action was cancelled before dispatch"
                )
            self._started = True
        try:
            self._owner.dispatch(self._action)
        finally:
            self._owner._forget(self._action.action_id, self)

    def cancel(self) -> ActionCancellationResult:
        with self._lock:
            if not self._started:
                self._cancelled = True
                result = ActionCancellationResult(ActionCancellationStatus.NOT_STARTED)
                self._owner._forget(self._action.action_id, self)
                return result
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)


class GmailWriteConnector:
    """Complete action lifecycle for exactly Gmail sends and typed replies."""

    def __init__(
        self,
        *,
        configured_identity: str,
        credential_store: GoogleCredentialStore,
        provider: GmailWriteProvider,
        audit: AuditBoundary,
        trace: DiagnosticTraceRecorder,
        clock: Clock,
        ids: IdGenerator,
        connection_state: Callable[[], GoogleConnectionState] | None = None,
        connection_binding: GoogleConnectionBinding | None = None,
        on_invalid_grant: Callable[[], object] | None = None,
        acceptance_failpoint: ReviewedPostDispatchFailpoint | None = None,
    ) -> None:
        self._configured_identity = _canonical_string(
            configured_identity, "configured_identity"
        )
        self._credential_store = credential_store
        self._provider = provider
        self._audit = audit
        if not isinstance(trace, DiagnosticTraceRecorder):
            raise TypeError("trace must be a DiagnosticTraceRecorder")
        self._trace = trace
        self._clock = clock
        self._ids = ids
        if connection_binding is None and not callable(connection_state):
            raise TypeError("connection_state or connection_binding must be configured")
        if connection_binding is not None and connection_state is not None:
            raise ValueError(
                "connection_state and connection_binding are mutually exclusive"
            )
        self._connection_state = connection_state
        self._connection_binding = connection_binding
        self._connection_lock = (
            connection_binding.synchronization_lock
            if connection_binding is not None
            else RLock()
        )
        self._prepared_lock = RLock()
        self._prepared: dict[str, _GmailWriteDispatch] = {}
        self._on_invalid_grant = on_invalid_grant or credential_store.delete
        if acceptance_failpoint is not None and not isinstance(
            acceptance_failpoint, ReviewedPostDispatchFailpoint
        ):
            raise TypeError(
                "acceptance_failpoint must be a reviewed post-dispatch failpoint"
            )
        if (
            acceptance_failpoint is not None
            and acceptance_failpoint.spec.service != "gmail"
        ):
            raise ValueError("Gmail connector requires a Gmail acceptance failpoint")
        self._acceptance_failpoint = acceptance_failpoint

    def prepare(self, action: FrozenActionProposal) -> ActionDispatchHandle:
        """Prepare one Gmail write without beginning the provider exchange."""

        handle = _GmailWriteDispatch(self, action)
        with self._prepared_lock:
            if action.action_id in self._prepared:
                raise ActionDispatcherError(
                    f"action {action.action_id} is already prepared",
                    may_have_dispatched=True,
                )
            self._prepared[action.action_id] = handle
        return handle

    def cancel(self, *, action_id: str) -> ActionCancellationResult:
        """Cancel a prepared Gmail write before it reaches the provider edge."""

        with self._prepared_lock:
            handle = self._prepared.get(action_id)
        if handle is None:
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
        return handle.cancel()

    def _forget(self, action_id: str, handle: _GmailWriteDispatch) -> None:
        with self._prepared_lock:
            if self._prepared.get(action_id) is handle:
                del self._prepared[action_id]

    def bind_proposal(self, action: FrozenActionProposal) -> FrozenActionProposal:
        """Freeze the current Google connection generation before presentation."""

        with self._connection_lock:
            request = gmail_write_request_from_proposal(action)
            if request.google_subject is not None:
                raise ValueError("only the Gmail connector may bind a Google action")
            connection = self._connection_snapshot().connection
            self._require_usable_connection(connection)
            bound_request = replace(
                request,
                google_subject=self._configured_identity,
                connection_generation=connection.generation,
            )
            return FrozenActionProposal.create(
                action_id=action.action_id,
                request_id=action.request_id,
                kind=action.kind,
                preview=gmail_proposal_preview(bound_request),
                payload=gmail_proposal_payload(bound_request),
            )

    def validate_pending_action(self, action: FrozenActionProposal) -> None:
        """Refuse a frozen Gmail action if its OAuth connection changed."""

        with self._connection_lock:
            request = gmail_write_request_from_proposal(action, require_binding=True)
            self._validate_connection_snapshot(request, self._connection_snapshot())

    def dispatch(self, action: FrozenActionProposal) -> None:
        """Run the one-shot Gmail lifecycle with explicit security phases."""

        # Hold one boundary from final snapshot through the provider attempt.
        with self._connection_lock:
            request, credential = self._prepare_dispatch(action)
            try:
                result = self._attempt_provider_once(
                    action=action,
                    request=request,
                    credential=credential,
                )
                self._classify_provider_result(request, result)
            except GmailWriteProviderError as exc:
                self._raise_provider_failure(action, exc)
            except TraceCapacityError as exc:
                self._raise_trace_failure(action, exc, may_have_dispatched=False)
            except TraceWriteError as exc:
                self._raise_trace_failure(
                    action, exc, may_have_dispatched=exc.operation_started
                )
            except DiagnosticTraceError as exc:
                self._raise_unknown_provider_failure(action, exc)
            except Exception as exc:  # noqa: BLE001 - unknown provider failures are ambiguous
                self._raise_unknown_provider_failure(action, exc)
            self._record_completed_delivery(action)

    def _prepare_dispatch(
        self, action: FrozenActionProposal
    ) -> tuple[GmailWriteRequest, OAuthCredentialRecord]:
        """Reconstruct, admit, and revalidate before any provider attempt."""

        try:
            request = gmail_write_request_from_proposal(action, require_binding=True)
            snapshot = self._connection_snapshot()
            credential = self._validate_connection_snapshot(request, snapshot)
            self._append_audit(
                action, outcome="attempted", execution_status="attempted"
            )
            # Re-read the pair after audit while the shared lock is still held.
            credential = self._validate_connection_snapshot(
                request, self._connection_snapshot()
            )
        except (ActionDispatcherError, ValueError, TypeError, AuditWriteError) as exc:
            raise ActionDispatcherError(
                str(exc) or "Gmail dispatch was blocked"
            ) from exc

        return request, credential

    def _attempt_provider_once(
        self,
        *,
        action: FrozenActionProposal,
        request: GmailWriteRequest,
        credential: OAuthCredentialRecord,
    ) -> GmailWriteProviderResult:
        """Make the single traced provider attempt for this frozen action."""

        result = self._trace.execute(
            request_id=f"{action.request_id}:gmail:{action.action_id}",
            operation_id=f"{action.request_id}:connector:gmail:{action.action_id}",
            operation_type="gmail_write_connector",
            input_payload={"action": action, "request": request},
            arguments={"operation": request.operation},
            telemetry={"service": "gmail"},
            operation=lambda: self._provider.send(
                request=request, credential=credential
            ),
            result_limit_bytes=GMAIL_WRITE_TRACE_PAYLOAD_LIMIT_BYTES,
            error_limit_bytes=GMAIL_WRITE_TRACE_PAYLOAD_LIMIT_BYTES,
        )
        if self._acceptance_failpoint is not None:
            try:
                self._acceptance_failpoint.raise_if_armed(
                    service="gmail",
                    operation=request.operation,
                    action_id=action.action_id,
                )
            except ReviewedPostDispatchFailure as exc:
                raise ActionDispatcherError(
                    "Gmail delivery outcome is unknown", may_have_dispatched=True
                ) from exc
        return result

    @staticmethod
    def _classify_provider_result(request: GmailWriteRequest, result: object) -> None:
        """Classify acknowledgement validity and frozen reply-thread integrity."""

        if not isinstance(result, GmailWriteProviderResult):
            raise GmailWriteProviderError("invalid_response", may_have_sent=True)
        if isinstance(request, GmailReplyRequest) and (
            result.delivery.thread_id != request.thread_id
        ):
            raise GmailWriteProviderError("thread_mismatch", may_have_sent=True)

    def _raise_provider_failure(
        self, action: FrozenActionProposal, error: GmailWriteProviderError
    ) -> None:
        """Record a definite or unknown provider outcome and stop dispatch."""

        if error.code == "invalid_grant":
            try:
                self._on_invalid_grant()
            except Exception as cleanup_error:
                self._record_terminal(action, outcome="failed")
                raise ActionDispatcherError(
                    "Gmail connection could not be invalidated safely"
                ) from cleanup_error
        self._record_terminal(
            action, outcome="unknown" if error.may_have_sent else "failed"
        )
        raise ActionDispatcherError(
            "Gmail delivery outcome is unknown"
            if error.may_have_sent
            else "Gmail delivery was not accepted",
            may_have_dispatched=error.may_have_sent,
        ) from error

    def _raise_unknown_provider_failure(
        self, action: FrozenActionProposal, error: Exception
    ) -> None:
        """Fail closed when trace or an unexpected provider edge is ambiguous."""

        self._record_terminal(action, outcome="unknown")
        raise ActionDispatcherError(
            "Gmail delivery outcome is unknown", may_have_dispatched=True
        ) from error

    def _raise_trace_failure(
        self,
        action: FrozenActionProposal,
        error: DiagnosticTraceError,
        *,
        may_have_dispatched: bool,
    ) -> None:
        """Preserve whether tracing reached the external operation boundary."""

        outcome = "unknown" if may_have_dispatched else "failed"
        self._record_terminal(action, outcome=outcome)
        raise ActionDispatcherError(
            "Gmail delivery outcome is unknown"
            if may_have_dispatched
            else "Gmail delivery was not attempted",
            may_have_dispatched=may_have_dispatched,
        ) from error

    def _record_completed_delivery(self, action: FrozenActionProposal) -> None:
        """Record terminal success; missing evidence keeps the outcome unknown."""

        try:
            self._append_audit(
                action, outcome="completed", execution_status="completed"
            )
        except AuditWriteError as exc:
            # Missing terminal evidence must close the durable outbox as unknown.
            raise ActionDispatcherError(
                "Gmail delivery outcome is unknown", may_have_dispatched=True
            ) from exc

    def _connection_snapshot(self) -> GoogleConnectionSnapshot:
        if self._connection_binding is not None:
            try:
                return self._connection_binding.snapshot()
            except Exception as exc:
                raise ActionDispatcherError(
                    "Google connection snapshot is unavailable"
                ) from exc
        try:
            connection = self._connection_state()  # type: ignore[misc]
            credential = self._credential_store.current
        except Exception as exc:
            raise ActionDispatcherError(
                "Google connection snapshot is unavailable"
            ) from exc
        if not isinstance(connection, GoogleConnectionState):
            raise ActionDispatcherError("Google connection state is unavailable")
        return GoogleConnectionSnapshot(connection=connection, credential=credential)

    def _validate_connection_snapshot(
        self,
        request: GmailWriteRequest,
        snapshot: GoogleConnectionSnapshot,
    ) -> OAuthCredentialRecord:
        self._require_usable_connection(snapshot.connection)
        if (
            request.google_subject != self._configured_identity
            or request.connection_generation != snapshot.connection.generation
        ):
            raise ActionDispatcherError("Google connection changed after proposal")
        credential = snapshot.credential
        if credential is None:
            raise ValueError("Gmail is disconnected")
        if credential.subject != self._configured_identity:
            raise ValueError("Gmail identity does not match the configured account")
        if GMAIL_SEND_SCOPE not in credential.granted_scopes:
            raise ValueError("Gmail send scope is unavailable")
        return credential

    @staticmethod
    def _require_usable_connection(connection: GoogleConnectionState) -> None:
        if (
            not connection.connected
            or GMAIL_SEND_SCOPE not in connection.granted_scopes
        ):
            raise ActionDispatcherError("Gmail connection is unavailable")

    def _record_terminal(self, action: FrozenActionProposal, *, outcome: str) -> None:
        try:
            self._append_audit(
                action,
                outcome=outcome,
                execution_status="unknown" if outcome == "unknown" else "failed",
            )
        except AuditWriteError as exc:
            # Preserve unknown outcome when the connector lacks terminal evidence.
            raise ActionDispatcherError(
                "Gmail terminal audit evidence is unavailable",
                may_have_dispatched=outcome == "unknown",
            ) from exc

    def _append_audit(
        self,
        action: FrozenActionProposal,
        *,
        outcome: str,
        execution_status: str,
    ) -> None:
        try:
            self._audit.append(
                AuditEvidence(
                    evidence_id=self._ids.new_id("audit"),
                    kind="gmail_write",
                    occurred_at=self._clock.now(),
                    request_id=action.request_id,
                    outcome=outcome,
                    actor="google_connector",
                    operation_type=action.kind,
                    target_category="gmail",
                    execution_status=execution_status,
                )
            )
        except (AuditWriteError, ValueError, TypeError, RuntimeError) as exc:
            raise AuditWriteError("Gmail audit evidence is unavailable") from exc


def build_live_gmail_write_connector(
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
    acceptance_failpoint: ReviewedPostDispatchFailpoint | None = None,
) -> GmailWriteConnector:
    """Compose Gmail's fixed write connector with OAuth invalidation ownership."""

    return GmailWriteConnector(
        configured_identity=configured_identity,
        credential_store=credential_store,
        provider=GmailApiWriteProvider(
            client_id=client_id, client_secret=client_secret, transport=transport
        ),
        audit=audit,
        trace=trace,
        clock=clock,
        ids=ids,
        connection_binding=oauth_lifecycle.connection_binding,
        on_invalid_grant=lambda: oauth_lifecycle.handle_refresh_failure(
            "invalid_grant"
        ),
        acceptance_failpoint=acceptance_failpoint,
    )

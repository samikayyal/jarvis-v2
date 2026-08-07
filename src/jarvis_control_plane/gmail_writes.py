"""Closed Gmail new-send and typed-reply connector for Ticket 18.

Only a fully frozen :class:`FrozenActionProposal` reaches this module.  The
proposal parser recreates the canonical delivery request and its preview before
the provider edge is contacted, so model-controlled prose cannot hide a
recipient, MIME field, reply header, or threading decision.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from email.message import EmailMessage
from email.policy import SMTP
from threading import RLock
from typing import Literal, Protocol

from .gmail_actions import (
    GMAIL_SEND_SCOPE,
    GmailReplyRequest,
    GmailWriteRequest,
    gmail_proposal_payload,
    gmail_proposal_preview,
    gmail_write_request_from_proposal,
)
from .google_auth import (
    GoogleRefreshTokenExchanger,
    GoogleTokenExchangeError,
    GoogleTokenExchangeRequest,
)
from .google_http import (
    GOOGLE_HTTP_TIMEOUT_SECONDS,
    GoogleHttpError,
    GoogleHttpResponse,
    GoogleHttpTransport,
    ensure_bounded_response_body,
)
from .google_oauth import (
    GoogleConnectionBinding,
    GoogleConnectionSnapshot,
    GoogleConnectionState,
    GoogleCredentialStore,
    GoogleOAuthLifecycle,
    OAuthCredentialRecord,
)
from .models import AuditEvidence, FrozenActionProposal
from .ports import (
    ActionDispatcherError,
    AuditBoundary,
    AuditWriteError,
    Clock,
    DiagnosticTraceError,
    IdGenerator,
    TraceCapacityError,
    TraceWriteError,
)
from .traces import DiagnosticTraceRecorder

GMAIL_WRITE_TRACE_PAYLOAD_LIMIT_BYTES = 2 * 1024 * 1024
GMAIL_WRITE_TIMEOUT_SECONDS = GOOGLE_HTTP_TIMEOUT_SECONDS

_GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")


class GmailWriteProviderError(RuntimeError):
    """Private provider-edge error with whether an external send may exist."""

    def __init__(
        self,
        code: str,
        *,
        may_have_sent: bool = False,
        trace_payload: Mapping[str, object] | None = None,
    ) -> None:
        self.code = code
        self.may_have_sent = may_have_sent
        self.trace_payload = dict(trace_payload or {})
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class GmailDeliveryResult:
    """Minimal provider acknowledgement needed to verify the frozen thread."""

    message_id: str
    thread_id: str

    def __post_init__(self) -> None:
        _identifier(self.message_id, "message_id")
        _identifier(self.thread_id, "thread_id")


@dataclass(frozen=True, slots=True)
class GmailWriteProviderResult:
    """Delivery acknowledgement plus complete provider evidence for the trace."""

    delivery: GmailDeliveryResult
    provider_trace: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.delivery, GmailDeliveryResult):
            raise TypeError("delivery must be a GmailDeliveryResult")
        object.__setattr__(self, "provider_trace", dict(self.provider_trace))


class GmailWriteProvider(Protocol):
    """The deliberately narrow provider edge: it only sends frozen mail."""

    def send(
        self, *, request: GmailWriteRequest, credential: OAuthCredentialRecord
    ) -> GmailWriteProviderResult: ...


class ControlledGmailWriteProvider:
    """Deterministic provider double that records exactly one typed send request."""

    def __init__(
        self,
        *,
        result: GmailDeliveryResult | None = None,
        failure: str | None = None,
        may_have_sent: bool = False,
    ) -> None:
        self.result = result or GmailDeliveryResult("sent-controlled", "thread-new")
        self.failure = failure
        self.may_have_sent = may_have_sent
        self.calls: list[GmailWriteRequest] = []

    def send(
        self, *, request: GmailWriteRequest, credential: OAuthCredentialRecord
    ) -> GmailWriteProviderResult:
        self.calls.append(request)
        if self.failure is not None:
            raise GmailWriteProviderError(
                self.failure,
                may_have_sent=self.may_have_sent,
                trace_payload={"credential": credential, "request": request},
            )
        return GmailWriteProviderResult(
            delivery=self.result,
            provider_trace={"credential": credential, "request": request},
        )


class GmailApiWriteProvider:
    """Production HTTPS edge for exactly Gmail's ``messages.send`` operation."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        transport: GoogleHttpTransport | None = None,
        timeout_seconds: float = GMAIL_WRITE_TIMEOUT_SECONDS,
    ) -> None:
        self._token_exchange = GoogleRefreshTokenExchanger(
            client_id=client_id,
            client_secret=client_secret,
            transport=transport,
            timeout_seconds=timeout_seconds,
        )
        self._transport = self._token_exchange.transport
        self._timeout_seconds = self._token_exchange.timeout_seconds

    def send(
        self, *, request: GmailWriteRequest, credential: OAuthCredentialRecord
    ) -> GmailWriteProviderResult:
        provider_trace: dict[str, object] = {
            "credential": credential,
            "request": request,
        }
        try:
            token = self._refresh_access_token(credential.refresh_token, provider_trace)
            envelope: dict[str, str] = {"raw": _encode_rfc822(request)}
            if isinstance(request, GmailReplyRequest):
                envelope["threadId"] = request.thread_id
            body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            provider_trace["gmail_request"] = {
                "method": "POST",
                "url": _GMAIL_SEND_URL,
                "headers": headers,
                "body": body,
            }
            response = self._request(
                method="POST",
                url=_GMAIL_SEND_URL,
                headers=headers,
                body=body,
                may_have_sent=True,
            )
            provider_trace["gmail_response"] = _response_trace(response)
            payload = _json_object(response, may_have_sent=True)
            message_id = payload.get("id")
            thread_id = payload.get("threadId")
            if not isinstance(message_id, str) or not isinstance(thread_id, str):
                raise GmailWriteProviderError("invalid_response", may_have_sent=True)
            try:
                delivery = GmailDeliveryResult(
                    message_id=message_id, thread_id=thread_id
                )
            except ValueError as exc:
                raise GmailWriteProviderError(
                    "invalid_response", may_have_sent=True
                ) from exc
            return GmailWriteProviderResult(
                delivery=delivery, provider_trace=provider_trace
            )
        except GmailWriteProviderError as exc:
            exc.trace_payload = provider_trace
            raise

    def _refresh_access_token(
        self, refresh_token: str, provider_trace: dict[str, object]
    ) -> str:
        try:
            exchange = self._token_exchange.exchange(refresh_token)
        except GoogleTokenExchangeError as exc:
            provider_trace["token_request"] = _token_request_trace(exc.request)
            if exc.response is not None:
                provider_trace["token_response"] = _response_trace(exc.response)
            code = (
                "invalid_token_response" if exc.code == "invalid_response" else exc.code
            )
            raise GmailWriteProviderError(code) from exc
        provider_trace["token_request"] = _token_request_trace(exchange.request)
        provider_trace["token_response"] = _response_trace(exchange.response)
        return exchange.access_token

    def _request(
        self,
        *,
        method: Literal["POST"],
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        may_have_sent: bool,
    ) -> GoogleHttpResponse:
        try:
            return self._transport.request(
                method=method,
                url=url,
                headers=headers,
                body=body,
                timeout_seconds=self._timeout_seconds,
            )
        except GoogleHttpError as exc:
            raise GmailWriteProviderError(
                exc.code, may_have_sent=may_have_sent
            ) from exc
        except Exception as exc:
            raise GmailWriteProviderError(
                "unavailable", may_have_sent=may_have_sent
            ) from exc


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
        self._on_invalid_grant = on_invalid_grant or credential_store.delete

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

        # Keep the same boundary held from the final snapshot through the
        # provider attempt.  OAuth replacement/disconnect cannot change the
        # credential or generation in that interval.
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
            # The audit write is part of the preflight boundary.  Re-read the
            # pair before returning, while the shared lock is still held.
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

        return self._trace.execute(
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
            # Gmail may already have accepted the message.  The outer broker
            # must close the durable outbox as unknown rather than permit a
            # second dispatch attempt when this terminal evidence is missing.
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
        except AuditWriteError:
            pass

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
    )


def _encode_rfc822(request: GmailWriteRequest) -> str:
    message = EmailMessage(policy=SMTP)
    for name, value in request.message.mime_headers():
        message[name] = value
    if isinstance(request, GmailReplyRequest):
        for name, value in request.threading_mime_headers():
            message[name] = value
    message.set_content(
        request.message.body,
        subtype=request.message.mime_subtype,
        charset="utf-8",
    )
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")


def _json_object(
    response: GoogleHttpResponse, *, may_have_sent: bool
) -> Mapping[str, object]:
    try:
        ensure_bounded_response_body(response.body)
    except GoogleHttpError as exc:
        raise GmailWriteProviderError(exc.code, may_have_sent=may_have_sent) from exc
    except TypeError as exc:
        raise GmailWriteProviderError(
            "invalid_response", may_have_sent=may_have_sent
        ) from exc
    if response.status_code != 200:
        raise GmailWriteProviderError("unavailable", may_have_sent=may_have_sent)
    try:
        value = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GmailWriteProviderError(
            "invalid_response", may_have_sent=may_have_sent
        ) from exc
    if not isinstance(value, dict):
        raise GmailWriteProviderError("invalid_response", may_have_sent=may_have_sent)
    return value


def _token_request_trace(request: GoogleTokenExchangeRequest) -> dict[str, object]:
    return {
        "method": request.method,
        "url": request.url,
        "headers": dict(request.headers),
        "body": request.body,
        # The exact wire body is retained above; this decoded form keeps the
        # credential-bearing OAuth inputs directly inspectable in manual traces.
        "form": dict(request.form),
    }


def _response_trace(response: GoogleHttpResponse) -> dict[str, object]:
    trace: dict[str, object] = {
        "status_code": response.status_code,
        "headers": dict(response.headers),
        "body": response.body,
    }
    try:
        trace["body_text"] = response.body.decode("utf-8")
    except UnicodeDecodeError:
        # The bytes above remain the lossless evidence for a non-text result.
        pass
    return trace


def _canonical_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{name} must be a non-blank canonical string")
    return value


def _identifier(value: object, name: str) -> str:
    value = _canonical_string(value, name)
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must be a canonical identifier")
    return value

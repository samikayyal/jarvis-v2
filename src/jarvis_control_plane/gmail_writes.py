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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from email.message import EmailMessage
from email.policy import SMTP
from typing import Literal, Protocol
from urllib.parse import urlencode

from .google_oauth import (
    GoogleConnectionState,
    GoogleCredentialStore,
    GoogleOAuthLifecycle,
    OAuthCredentialRecord,
)
from .google_reads import (
    GOOGLE_HTTP_TIMEOUT_SECONDS,
    GoogleReadHttpResponse,
    GoogleReadHttpTransport,
    UrllibGoogleReadHttpTransport,
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

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_WRITE_TRACE_PAYLOAD_LIMIT_BYTES = 2 * 1024 * 1024
GMAIL_WRITE_TIMEOUT_SECONDS = GOOGLE_HTTP_TIMEOUT_SECONDS

_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
_MIME_TYPES = frozenset({"text/plain", "text/html"})
_MAILBOX = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")

GmailWriteOperation = Literal["gmail_send", "gmail_reply"]


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
class GmailSendRequest:
    """The complete delivery-affecting Gmail request reconstructed from a proposal."""

    operation: GmailWriteOperation
    to: tuple[str, ...]
    cc: tuple[str, ...]
    bcc: tuple[str, ...]
    subject: str
    body: str
    mime_type: Literal["text/plain", "text/html"]
    threading: Literal["new_message", "gmail_threaded_reply"]
    thread_id: str | None = None
    source_message_id: str | None = None
    source_thread_id: str | None = None
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()
    google_subject: str | None = None
    connection_generation: int | None = None

    def __post_init__(self) -> None:
        if self.operation == "gmail_send":
            if (
                self.threading != "new_message"
                or any(
                    value is not None
                    for value in (
                        self.thread_id,
                        self.source_message_id,
                        self.source_thread_id,
                        self.in_reply_to,
                    )
                )
                or self.references
            ):
                raise ValueError("new Gmail sends cannot carry reply threading fields")
        elif self.operation == "gmail_reply":
            if self.threading != "gmail_threaded_reply":
                raise ValueError("Gmail replies require gmail_threaded_reply")
            if (
                self.thread_id is None
                or self.source_message_id is None
                or self.source_thread_id is None
                or self.in_reply_to is None
                or not self.references
            ):
                raise ValueError(
                    "Gmail replies require complete source and threading fields"
                )
            if self.thread_id != self.source_thread_id:
                raise ValueError(
                    "Gmail reply thread must match its frozen source thread"
                )
            if self.references[-1] != self.in_reply_to:
                raise ValueError("Gmail reply references must end with In-Reply-To")
        else:
            raise ValueError("Gmail operation is not allowed")
        if (self.google_subject is None) != (self.connection_generation is None):
            raise ValueError("Google action bindings require subject and generation")
        if self.google_subject is not None:
            _canonical_string(self.google_subject, "google_subject")
            if (
                not isinstance(self.connection_generation, int)
                or self.connection_generation < 0
            ):
                raise ValueError("connection_generation must be non-negative")


@dataclass(frozen=True, slots=True)
class GmailSendProviderResult:
    """Minimal provider acknowledgement needed to verify the frozen thread."""

    message_id: str
    thread_id: str

    def __post_init__(self) -> None:
        _identifier(self.message_id, "message_id")
        _identifier(self.thread_id, "thread_id")


@dataclass(frozen=True, slots=True)
class GmailWriteProviderResult:
    """Delivery acknowledgement plus complete provider evidence for the trace."""

    delivery: GmailSendProviderResult
    provider_trace: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.delivery, GmailSendProviderResult):
            raise TypeError("delivery must be a GmailSendProviderResult")
        object.__setattr__(self, "provider_trace", dict(self.provider_trace))


class GmailWriteProvider(Protocol):
    """The deliberately narrow provider edge: it only sends frozen mail."""

    def send(
        self, *, request: GmailSendRequest, credential: OAuthCredentialRecord
    ) -> GmailWriteProviderResult: ...


class ControlledGmailWriteProvider:
    """Deterministic provider double that records exactly one typed send request."""

    def __init__(
        self,
        *,
        result: GmailSendProviderResult | None = None,
        failure: str | None = None,
        may_have_sent: bool = False,
    ) -> None:
        self.result = result or GmailSendProviderResult("sent-controlled", "thread-new")
        self.failure = failure
        self.may_have_sent = may_have_sent
        self.calls: list[GmailSendRequest] = []

    def send(
        self, *, request: GmailSendRequest, credential: OAuthCredentialRecord
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
        transport: GoogleReadHttpTransport | None = None,
        timeout_seconds: float = GMAIL_WRITE_TIMEOUT_SECONDS,
    ) -> None:
        self._client_id = _canonical_string(client_id, "client_id")
        self._client_secret = _canonical_string(client_secret, "client_secret")
        self._transport = transport or UrllibGoogleReadHttpTransport()
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= GMAIL_WRITE_TIMEOUT_SECONDS
        ):
            raise ValueError("timeout_seconds must be positive and no greater than 5")
        self._timeout_seconds = float(timeout_seconds)

    def send(
        self, *, request: GmailSendRequest, credential: OAuthCredentialRecord
    ) -> GmailWriteProviderResult:
        provider_trace: dict[str, object] = {
            "credential": credential,
            "request": request,
        }
        try:
            token = self._refresh_access_token(credential.refresh_token, provider_trace)
            envelope: dict[str, str] = {"raw": _encode_rfc822(request)}
            if request.thread_id is not None:
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
                delivery = GmailSendProviderResult(
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
        form = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        body = urlencode(form).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        provider_trace["token_request"] = {
            "method": "POST",
            "url": _GOOGLE_TOKEN_URL,
            "headers": headers,
            "body": body,
            # Keep a decoded representation beside the exact wire body.  The
            # diagnostic encoder stores bytes losslessly as base64, whereas
            # manual incident review needs the controlled credential inputs
            # directly inspectable without reconstituting the request.
            "form": form,
        }
        response = self._request(
            method="POST",
            url=_GOOGLE_TOKEN_URL,
            headers=headers,
            body=body,
            may_have_sent=False,
        )
        provider_trace["token_response"] = _response_trace(response)
        payload = _json_object(response, token_exchange=True, may_have_sent=False)
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise GmailWriteProviderError("invalid_token_response")
        return token

    def _request(
        self,
        *,
        method: Literal["POST"],
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        may_have_sent: bool,
    ) -> GoogleReadHttpResponse:
        try:
            return self._transport.request(
                method=method,
                url=url,
                headers=headers,
                body=body,
                timeout_seconds=self._timeout_seconds,
            )
        except Exception as exc:
            raise GmailWriteProviderError(
                "unavailable", may_have_sent=may_have_sent
            ) from exc


class GmailWriteConnector:
    """Action dispatcher that permits exactly Gmail new-send and typed replies."""

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
        connection_state: Callable[[], GoogleConnectionState],
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
        if not callable(connection_state):
            raise TypeError("connection_state must return GoogleConnectionState")
        self._connection_state = connection_state
        self._on_invalid_grant = on_invalid_grant or credential_store.delete

    def bind_proposal(self, action: FrozenActionProposal) -> FrozenActionProposal:
        """Freeze the current Google connection generation before presentation."""

        request = gmail_send_request_from_proposal(action)
        if request.google_subject is not None:
            raise ValueError("only the Gmail connector may bind a Google action")
        connection = self._current_connection()
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
            preview=_preview(bound_request),
            payload=_payload(bound_request),
        )

    def validate_pending_action(self, action: FrozenActionProposal) -> None:
        """Refuse a frozen Gmail action if its OAuth connection changed."""

        request = gmail_send_request_from_proposal(action, require_binding=True)
        connection = self._current_connection()
        self._require_usable_connection(connection)
        if (
            request.google_subject != self._configured_identity
            or request.connection_generation != connection.generation
        ):
            raise ActionDispatcherError("Google connection changed after proposal")

    def dispatch(self, action: FrozenActionProposal) -> None:
        try:
            request = gmail_send_request_from_proposal(action, require_binding=True)
            self.validate_pending_action(action)
            credential = self._credential()
            self._append_audit(
                action, outcome="attempted", execution_status="attempted"
            )
            self.validate_pending_action(action)
        except (ActionDispatcherError, ValueError, TypeError, AuditWriteError) as exc:
            raise ActionDispatcherError(
                str(exc) or "Gmail dispatch was blocked"
            ) from exc
        try:
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
            if not isinstance(result, GmailWriteProviderResult):
                raise GmailWriteProviderError("invalid_response", may_have_sent=True)
            if (
                request.operation == "gmail_reply"
                and result.delivery.thread_id != request.thread_id
            ):
                raise GmailWriteProviderError("thread_mismatch", may_have_sent=True)
        except GmailWriteProviderError as exc:
            if exc.code == "invalid_grant":
                try:
                    self._on_invalid_grant()
                except (OSError, RuntimeError, ValueError) as cleanup_error:
                    self._record_terminal(action, outcome="failed")
                    raise ActionDispatcherError(
                        "Gmail connection could not be invalidated safely"
                    ) from cleanup_error
            self._record_terminal(
                action, outcome="unknown" if exc.may_have_sent else "failed"
            )
            raise ActionDispatcherError(
                "Gmail delivery outcome is unknown"
                if exc.may_have_sent
                else "Gmail delivery was not accepted",
                may_have_dispatched=exc.may_have_sent,
            ) from exc
        except (DiagnosticTraceError, TraceCapacityError, TraceWriteError) as exc:
            self._record_terminal(action, outcome="unknown")
            raise ActionDispatcherError(
                "Gmail delivery outcome is unknown", may_have_dispatched=True
            ) from exc
        except Exception as exc:
            self._record_terminal(action, outcome="unknown")
            raise ActionDispatcherError(
                "Gmail delivery outcome is unknown", may_have_dispatched=True
            ) from exc
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

    def _credential(self) -> OAuthCredentialRecord:
        try:
            credential = self._credential_store.current
        except Exception as exc:
            raise ValueError("Gmail is unavailable") from exc
        if credential is None:
            raise ValueError("Gmail is disconnected")
        if credential.subject != self._configured_identity:
            raise ValueError("Gmail identity does not match the configured account")
        if GMAIL_SEND_SCOPE not in credential.granted_scopes:
            raise ValueError("Gmail send scope is unavailable")
        return credential

    def _current_connection(self) -> GoogleConnectionState:
        try:
            connection = self._connection_state()
        except Exception as exc:
            raise ActionDispatcherError(
                "Google connection state is unavailable"
            ) from exc
        if not isinstance(connection, GoogleConnectionState):
            raise ActionDispatcherError("Google connection state is unavailable")
        return connection

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


def create_gmail_send_proposal(
    *,
    action_id: str,
    request_id: str,
    to: Sequence[str],
    cc: Sequence[str] = (),
    bcc: Sequence[str] = (),
    subject: str,
    body: str,
    mime_type: str,
    source_message_id: str | None = None,
    source_thread_id: str | None = None,
    in_reply_to: str | None = None,
    references: Sequence[str] = (),
    google_subject: str | None = None,
    connection_generation: int | None = None,
) -> FrozenActionProposal:
    """Create the only canonical Gmail action proposal accepted by the dispatcher."""

    is_reply = any(
        value is not None
        for value in (source_message_id, source_thread_id, in_reply_to)
    ) or bool(references)
    request = GmailSendRequest(
        operation="gmail_reply" if is_reply else "gmail_send",
        to=_recipients(to, "to"),
        cc=_recipients(cc, "cc", allow_empty=True),
        bcc=_recipients(bcc, "bcc", allow_empty=True),
        subject=_subject(subject),
        body=_body(body),
        mime_type=_mime_type(mime_type),
        threading="gmail_threaded_reply" if is_reply else "new_message",
        thread_id=_identifier(source_thread_id, "source_thread_id")
        if is_reply
        else None,
        source_message_id=(
            _identifier(source_message_id, "source_message_id") if is_reply else None
        ),
        source_thread_id=(
            _identifier(source_thread_id, "source_thread_id") if is_reply else None
        ),
        in_reply_to=_message_id(in_reply_to) if is_reply else None,
        references=_message_ids(references) if is_reply else (),
        google_subject=(
            _canonical_string(google_subject, "google_subject")
            if google_subject is not None
            else None
        ),
        connection_generation=connection_generation,
    )
    return FrozenActionProposal.create(
        action_id=action_id,
        request_id=request_id,
        kind=request.operation,
        preview=_preview(request),
        payload=_payload(request),
    )


def gmail_send_request_from_proposal(
    action: FrozenActionProposal, *, require_binding: bool = False
) -> GmailSendRequest:
    """Parse and re-validate a frozen proposal before its sole provider attempt."""

    if action.kind not in {"gmail_send", "gmail_reply"}:
        raise ValueError("proposal is not a Gmail send or reply")
    try:
        payload = json.loads(action.payload)
    except json.JSONDecodeError as exc:
        raise ValueError("Gmail proposal payload is malformed") from exc
    if not isinstance(payload, dict):
        raise TypeError("Gmail proposal payload must be an object")
    request = _request_from_payload(action.kind, payload)
    if require_binding and request.google_subject is None:
        raise ValueError("Gmail proposal is missing its Google connection binding")
    if action.preview != _preview(request):
        raise ValueError("Gmail proposal preview does not match its frozen payload")
    return request


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
    transport: GoogleReadHttpTransport | None = None,
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
        connection_state=lambda: oauth_lifecycle.connection,
        on_invalid_grant=lambda: oauth_lifecycle.handle_refresh_failure(
            "invalid_grant"
        ),
    )


def _request_from_payload(kind: str, payload: Mapping[str, object]) -> GmailSendRequest:
    expected = {
        "to",
        "cc",
        "bcc",
        "subject",
        "body",
        "mime_type",
        "threading",
    }
    if kind == "gmail_reply":
        expected |= {
            "thread_id",
            "source_message_id",
            "source_thread_id",
            "in_reply_to",
            "references",
        }
    binding_fields = {"google_subject", "connection_generation"}
    supplied_binding_fields = set(payload) & binding_fields
    if supplied_binding_fields and supplied_binding_fields != binding_fields:
        raise ValueError("Gmail proposal has an incomplete Google connection binding")
    if supplied_binding_fields:
        expected |= binding_fields
    if set(payload) != expected:
        raise ValueError(
            "Gmail proposal payload has missing or unknown delivery fields"
        )
    reply = kind == "gmail_reply"
    return GmailSendRequest(
        operation=kind,  # type: ignore[arg-type]
        to=_recipients(payload["to"], "to"),
        cc=_recipients(payload["cc"], "cc", allow_empty=True),
        bcc=_recipients(payload["bcc"], "bcc", allow_empty=True),
        subject=_subject(payload["subject"]),
        body=_body(payload["body"]),
        mime_type=_mime_type(payload["mime_type"]),
        threading=(_threading(payload["threading"], reply=reply)),
        thread_id=_identifier(payload["thread_id"], "thread_id") if reply else None,
        source_message_id=(
            _identifier(payload["source_message_id"], "source_message_id")
            if reply
            else None
        ),
        source_thread_id=(
            _identifier(payload["source_thread_id"], "source_thread_id")
            if reply
            else None
        ),
        in_reply_to=_message_id(payload["in_reply_to"]) if reply else None,
        references=_message_ids(payload["references"]) if reply else (),
        google_subject=(
            _canonical_string(payload["google_subject"], "google_subject")
            if supplied_binding_fields
            else None
        ),
        connection_generation=(
            _connection_generation(payload["connection_generation"])
            if supplied_binding_fields
            else None
        ),
    )


def _payload(request: GmailSendRequest) -> dict[str, object]:
    payload: dict[str, object] = {
        "to": request.to,
        "cc": request.cc,
        "bcc": request.bcc,
        "subject": request.subject,
        "body": request.body,
        "mime_type": request.mime_type,
        "threading": request.threading,
    }
    if request.operation == "gmail_reply":
        payload.update(
            {
                "thread_id": request.thread_id,
                "source_message_id": request.source_message_id,
                "source_thread_id": request.source_thread_id,
                "in_reply_to": request.in_reply_to,
                "references": request.references,
            }
        )
    if request.google_subject is not None:
        payload.update(
            {
                "google_subject": request.google_subject,
                "connection_generation": request.connection_generation,
            }
        )
    return payload


def _preview(request: GmailSendRequest) -> str:
    lines = [
        "Gmail typed reply" if request.operation == "gmail_reply" else "Gmail new send",
        f"To: {', '.join(request.to)}",
        f"Cc: {', '.join(request.cc) or '(none)'}",
        f"Bcc: {', '.join(request.bcc) or '(none)'}",
        f"Subject: {request.subject}",
        f"MIME: {request.mime_type}",
        f"Threading: {request.threading}",
    ]
    if request.google_subject is not None:
        lines.extend(
            (
                f"Google subject: {request.google_subject}",
                f"Google connection generation: {request.connection_generation}",
            )
        )
    if request.operation == "gmail_reply":
        lines.extend(
            (
                f"Source message: {request.source_message_id}",
                f"Source thread: {request.source_thread_id}",
                f"In-Reply-To: {request.in_reply_to}",
                f"References: {' '.join(request.references)}",
            )
        )
    return "\n".join((*lines, "", "Body:", request.body))


def _encode_rfc822(request: GmailSendRequest) -> str:
    message = EmailMessage(policy=SMTP)
    message["To"] = ", ".join(request.to)
    if request.cc:
        message["Cc"] = ", ".join(request.cc)
    if request.bcc:
        message["Bcc"] = ", ".join(request.bcc)
    message["Subject"] = request.subject
    if request.operation == "gmail_reply":
        message["In-Reply-To"] = request.in_reply_to
        message["References"] = " ".join(request.references)
    subtype = request.mime_type.split("/", 1)[1]
    message.set_content(request.body, subtype=subtype, charset="utf-8")
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")


def _json_object(
    response: GoogleReadHttpResponse,
    *,
    token_exchange: bool = False,
    may_have_sent: bool,
) -> Mapping[str, object]:
    if response.status_code != 200:
        code = (
            "invalid_grant"
            if token_exchange and b"invalid_grant" in response.body
            else "unavailable"
        )
        raise GmailWriteProviderError(code, may_have_sent=may_have_sent)
    try:
        value = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GmailWriteProviderError(
            "invalid_response", may_have_sent=may_have_sent
        ) from exc
    if not isinstance(value, dict):
        raise GmailWriteProviderError("invalid_response", may_have_sent=may_have_sent)
    return value


def _response_trace(response: GoogleReadHttpResponse) -> dict[str, object]:
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


def _connection_generation(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("connection_generation must be a non-negative integer")
    return value


def _recipients(
    value: object, name: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise TypeError(f"{name} must be a recipient sequence")
    recipients = tuple(value)
    if (not recipients and not allow_empty) or len(recipients) > 25:
        minimum = "zero" if allow_empty else "one"
        raise ValueError(f"{name} must contain between {minimum} and 25 recipients")
    if not all(
        isinstance(item, str) and _MAILBOX.fullmatch(item) for item in recipients
    ):
        raise ValueError(f"{name} contains an invalid mailbox")
    if len(set(recipients)) != len(recipients):
        raise ValueError(f"{name} contains a duplicate mailbox")
    return recipients


def _subject(value: object) -> str:
    value = _canonical_string(value, "subject")
    if len(value) > 998 or "\r" in value or "\n" in value:
        raise ValueError("subject is not a safe RFC822 header")
    return value


def _body(value: object) -> str:
    if not isinstance(value, str) or len(value) > 64 * 1024:
        raise ValueError("body must be text up to 65536 characters")
    return value


def _mime_type(value: object) -> Literal["text/plain", "text/html"]:
    if value not in _MIME_TYPES:
        raise ValueError("MIME type is outside the fixed Gmail text surface")
    return value  # type: ignore[return-value]


def _threading(
    value: object, *, reply: bool
) -> Literal["new_message", "gmail_threaded_reply"]:
    expected = "gmail_threaded_reply" if reply else "new_message"
    if value != expected:
        raise ValueError("Gmail threading behavior does not match the action type")
    return expected  # type: ignore[return-value]


def _message_id(value: object) -> str:
    value = _canonical_string(value, "message_id")
    if (
        len(value) > 998
        or "\r" in value
        or "\n" in value
        or not value.startswith("<")
        or not value.endswith(">")
    ):
        raise ValueError("reply message identifier is not a safe RFC822 header")
    return value


def _message_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise TypeError("references must be a message identifier sequence")
    result = tuple(_message_id(item) for item in value)
    if not result or len(result) > 20:
        raise ValueError(
            "references must contain between one and 20 message identifiers"
        )
    return result

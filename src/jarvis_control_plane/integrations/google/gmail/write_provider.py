"""Gmail provider transport and delivery evidence."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP
from typing import Literal, Protocol

from ....gmail_actions import GmailReplyRequest, GmailWriteRequest
from ....google_auth import (
    GoogleRefreshTokenExchanger,
    GoogleTokenExchangeError,
    GoogleTokenExchangeRequest,
)
from ....google_http import (
    GOOGLE_HTTP_TIMEOUT_SECONDS,
    GoogleHttpError,
    GoogleHttpResponse,
    GoogleHttpTransport,
    ensure_bounded_response_body,
)
from ....google_oauth import OAuthCredentialRecord

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

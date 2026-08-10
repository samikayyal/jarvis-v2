"""Pinned OpenWA v0.12.1/Baileys HTTP adapter boundary.

This module describes the future private handoff without attaching to the live
deployment. Production transports are injectable; automated verification uses
the controlled transport at the bottom of this file.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from .models import (
    InboundMessage,
    OutboundDelivery,
    OutboundReply,
    ReceiveResult,
    SignedInboundEvent,
)
from .ports import DurableStateStore, OutboundConnectorError

OPENWA_HTTP_TIMEOUT_SECONDS = 5.0
MAX_OPENWA_HTTP_RESPONSE_BYTES = 64 * 1024
OPENWA_MESSAGE_MAX_CHARACTERS = 4096

OpenWAHttpMethod = Literal["GET", "POST"]


def _canonical_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{name} must be a non-empty canonical string")
    return value


@dataclass(frozen=True, slots=True)
class OpenWAConfig:
    """Fixed routing plus the one injected OpenWA operator credential."""

    api_base_url: str
    api_key: str = field(repr=False)
    internal_session_id: str
    named_session: str
    operator_conversation_id: str
    max_text_characters: int = OPENWA_MESSAGE_MAX_CHARACTERS

    def __post_init__(self) -> None:
        for name in (
            "api_base_url",
            "api_key",
            "internal_session_id",
            "named_session",
            "operator_conversation_id",
        ):
            _canonical_text(getattr(self, name), name)
        parsed = urlsplit(self.api_base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("api_base_url must be a canonical HTTP(S) URL")
        object.__setattr__(self, "api_base_url", self.api_base_url.rstrip("/"))
        if self.max_text_characters != OPENWA_MESSAGE_MAX_CHARACTERS:
            raise ValueError("OpenWA message length is fixed at 4,096 characters")

    @property
    def authorization_headers(self) -> dict[str, str]:
        return {"X-API-Key": self.api_key}

    def api_url(self, path: str) -> str:
        if not path.startswith("/"):
            raise ValueError("OpenWA API paths must be absolute")
        return f"{self.api_base_url}{path}"


class OpenWAHttpError(RuntimeError):
    """Bounded transport failure with explicit external-side-effect uncertainty."""

    def __init__(self, code: str, *, may_have_sent: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.may_have_sent = may_have_sent


@dataclass(frozen=True, slots=True)
class OpenWAHttpResponse:
    status_code: int
    body: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.status_code, int) or isinstance(self.status_code, bool):
            raise TypeError("OpenWA HTTP status_code must be an integer")
        if not isinstance(self.body, bytes):
            raise TypeError("OpenWA HTTP response body must be bytes")
        if len(self.body) > MAX_OPENWA_HTTP_RESPONSE_BYTES:
            raise ValueError("OpenWA HTTP response exceeded the fixed limit")


class OpenWAHttpTransport(Protocol):
    def request(
        self,
        *,
        method: OpenWAHttpMethod,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> OpenWAHttpResponse: ...


class _ReadableResponse(Protocol):
    def read(self, n: int = -1) -> bytes: ...


class OpenWAIngressReceiver(Protocol):
    def receive(self, event: SignedInboundEvent) -> ReceiveResult: ...

    def admit(self, event: SignedInboundEvent) -> ReceiveResult: ...

    def dispatch_admitted_message(self, message: InboundMessage) -> ReceiveResult: ...

    def reconcile_ingress_restart(self) -> int: ...


class OpenWAWebhookAdapter:
    """Adapt one private OpenWA HTTP callback to the signed receiver seam."""

    def __init__(self, *, receiver: OpenWAIngressReceiver) -> None:
        self.receiver = receiver

    def receive(
        self,
        *,
        raw_body: bytes,
        headers: Mapping[str, str],
    ) -> ReceiveResult:
        signatures = tuple(
            value
            for name, value in headers.items()
            if name.lower() == "x-openwa-signature"
        )
        signature = signatures[0] if len(signatures) == 1 else None
        return self.receiver.admit(
            SignedInboundEvent(raw_body=raw_body, signature=signature)
        )


class OpenWAIngressWorker:
    """Drain the durable admitted-message handoff outside the webhook request."""

    def __init__(
        self,
        *,
        receiver: OpenWAIngressReceiver,
        state: DurableStateStore,
    ) -> None:
        self.receiver = receiver
        self.state = state
        self.startup_interrupted_count = receiver.reconcile_ingress_restart()

    def run_once(self) -> ReceiveResult | None:
        message = self.state.begin_next_ingress_dispatch()
        if message is None:
            return None
        try:
            result = self.receiver.dispatch_admitted_message(
                InboundMessage(
                    event_type="message.received",
                    session_id=message.transport_session_id,
                    event_id=message.event_id,
                    message_id=message.message_id,
                    sender_id=message.sender_id,
                    chat_id=message.chat_id,
                    chat_type="direct",
                    message_type="text",
                    from_me=False,
                    text=message.text,
                )
            )
        except Exception:
            self.state.finish_ingress_dispatch(
                transport_session_id=message.transport_session_id,
                message_id=message.message_id,
                disposition="interrupted",
            )
            raise
        self.state.finish_ingress_dispatch(
            transport_session_id=message.transport_session_id,
            message_id=message.message_id,
            disposition="dispatched",
        )
        return result


class UrllibOpenWAHttpTransport:
    """Production HTTP transport for the future private two-member network."""

    def request(
        self,
        *,
        method: OpenWAHttpMethod,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> OpenWAHttpResponse:
        request = Request(url, data=body, headers=dict(headers), method=method)
        try:
            response = urlopen(request, timeout=timeout_seconds)
        except HTTPError as error:
            try:
                return OpenWAHttpResponse(
                    status_code=error.code,
                    body=_read_bounded_body(error, may_have_sent=method == "POST"),
                )
            finally:
                error.close()
        except (TimeoutError, URLError, OSError) as error:
            raise OpenWAHttpError(
                "timeout" if isinstance(error, TimeoutError) else "unavailable",
                may_have_sent=method == "POST",
            ) from error
        try:
            return OpenWAHttpResponse(
                status_code=response.getcode(),
                body=_read_bounded_body(response, may_have_sent=method == "POST"),
            )
        finally:
            response.close()


def _read_bounded_body(
    response: _ReadableResponse, *, may_have_sent: bool
) -> bytes:
    body = response.read(MAX_OPENWA_HTTP_RESPONSE_BYTES + 1)
    if not isinstance(body, bytes):
        raise OpenWAHttpError("invalid_response", may_have_sent=may_have_sent)
    if len(body) > MAX_OPENWA_HTTP_RESPONSE_BYTES:
        raise OpenWAHttpError("oversized_response", may_have_sent=may_have_sent)
    return body


@dataclass(frozen=True, slots=True)
class OpenWAReadiness:
    """Independent container and configured-session readiness observations."""

    container_healthy: bool
    named_session_status: str

    @property
    def messaging_ready(self) -> bool:
        return self.container_healthy and self.named_session_status == "ready"


class OpenWAReadinessProbe:
    """Read container health and the exact named/internal session separately."""

    def __init__(
        self,
        *,
        config: OpenWAConfig,
        transport: OpenWAHttpTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = (
            transport if transport is not None else UrllibOpenWAHttpTransport()
        )

    def current(self) -> OpenWAReadiness:
        return OpenWAReadiness(
            container_healthy=self._container_healthy(),
            named_session_status=self._named_session_status(),
        )

    def _container_healthy(self) -> bool:
        try:
            response = self.transport.request(
                method="GET",
                url=self.config.api_url("/health/ready"),
                headers={},
                body=None,
                timeout_seconds=OPENWA_HTTP_TIMEOUT_SECONDS,
            )
        except OpenWAHttpError:
            return False
        return response.status_code == 200

    def _named_session_status(self) -> str:
        session_id = quote(self.config.internal_session_id, safe="")
        try:
            response = self.transport.request(
                method="GET",
                url=self.config.api_url(f"/sessions/{session_id}"),
                headers=self.config.authorization_headers,
                body=None,
                timeout_seconds=OPENWA_HTTP_TIMEOUT_SECONDS,
            )
        except OpenWAHttpError:
            return "unavailable"
        if response.status_code != 200:
            return "unavailable"
        try:
            payload = _json_object(response.body)
        except (TypeError, ValueError):
            return "invalid"
        if (
            payload.get("id") != self.config.internal_session_id
            or payload.get("name") != self.config.named_session
        ):
            return "identity_mismatch"
        status = payload.get("status")
        return status if isinstance(status, str) and status else "invalid"


class OpenWAOutboundConnector:
    """Fixed-session, fixed-conversation implementation of the outbound port."""

    def __init__(
        self,
        *,
        config: OpenWAConfig,
        readiness: OpenWAReadinessProbe | None = None,
        transport: OpenWAHttpTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = (
            transport if transport is not None else UrllibOpenWAHttpTransport()
        )
        if readiness is None:
            readiness = OpenWAReadinessProbe(
                config=config,
                transport=self.transport,
            )
        elif readiness.config != config or readiness.transport is not self.transport:
            raise ValueError(
                "OpenWA readiness and outbound delivery must share one configuration and transport"
            )
        self.readiness = readiness

    def preflight(self, reply: OutboundReply) -> None:
        self._validate_reply(reply)
        readiness = self.readiness.current()
        if not readiness.container_healthy:
            raise OutboundConnectorError("OpenWA container is not healthy")
        if readiness.named_session_status != "ready":
            raise OutboundConnectorError(
                "configured OpenWA named session is not ready"
            )

    def send(self, reply: OutboundReply) -> OutboundDelivery:
        self._validate_reply(reply)
        session_id = quote(self.config.internal_session_id, safe="")
        body = json.dumps(
            {
                "chatId": self.config.operator_conversation_id,
                "quotedMessageId": reply.quoted_message_id,
                "text": reply.body,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        try:
            response = self.transport.request(
                method="POST",
                url=self.config.api_url(f"/sessions/{session_id}/messages/reply"),
                headers={
                    "Content-Type": "application/json",
                    **self.config.authorization_headers,
                },
                body=body,
                timeout_seconds=OPENWA_HTTP_TIMEOUT_SECONDS,
            )
        except OpenWAHttpError as exc:
            raise OutboundConnectorError(
                "OpenWA reply outcome was unavailable",
                may_have_sent=exc.may_have_sent,
            ) from exc
        if response.status_code != 201:
            raise OutboundConnectorError(
                f"OpenWA reply failed with HTTP {response.status_code}",
                may_have_sent=response.status_code >= 500,
            )
        try:
            payload = _json_object(response.body)
            outbound_id = _canonical_text(payload.get("messageId"), "messageId")
        except (TypeError, ValueError) as exc:
            raise OutboundConnectorError(
                "OpenWA accepted the reply but returned an invalid identifier",
                may_have_sent=True,
            ) from exc
        return OutboundDelivery(outbound_id=outbound_id, accepted=True)

    def _validate_reply(self, reply: OutboundReply) -> None:
        if reply.session_id != self.config.internal_session_id:
            raise OutboundConnectorError("reply session is not configured")
        if reply.recipient_id != self.config.operator_conversation_id:
            raise OutboundConnectorError("reply recipient is not configured")
        if reply.quoted_message_id is None:
            raise OutboundConnectorError("reply is missing inbound message correlation")
        if len(reply.body) > self.config.max_text_characters:
            raise OutboundConnectorError(
                "OpenWA reply exceeds the fixed 4,096-character envelope"
            )
        if reply.request_id not in reply.body:
            raise OutboundConnectorError("reply is missing request correlation")


def _json_object(body: bytes) -> dict[str, object]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("OpenWA returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise TypeError("OpenWA returned a non-object response")
    return payload


@dataclass(frozen=True, slots=True)
class OpenWAHttpRequest:
    method: OpenWAHttpMethod
    url: str
    headers: dict[str, str]
    body: bytes | None
    timeout_seconds: float


class ControlledOpenWAHttpTransport:
    """Local contract double; it never opens a network connection."""

    def __init__(
        self,
        *,
        responses: Sequence[OpenWAHttpResponse] = (),
        failures: Sequence[OpenWAHttpError] = (),
    ) -> None:
        self._responses = deque(responses)
        self._failures = deque(failures)
        self.requests: list[OpenWAHttpRequest] = []

    def request(
        self,
        *,
        method: OpenWAHttpMethod,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> OpenWAHttpResponse:
        self.requests.append(
            OpenWAHttpRequest(
                method=method,
                url=url,
                headers=dict(headers),
                body=body,
                timeout_seconds=timeout_seconds,
            )
        )
        if self._responses:
            return self._responses.popleft()
        if self._failures:
            raise self._failures.popleft()
        raise OpenWAHttpError("controlled_outcome_missing", may_have_sent=False)

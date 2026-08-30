"""Minimal OpenWA handoff for the personal assistant runtime."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from http.client import HTTPException
from typing import Protocol, Self
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import LoadedRuntimeConfig
from .runtime import InboundText, PersonalRuntime, RuntimeResult


def _required_text(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value or value.strip() != value:
        raise ValueError(f"{name} must be non-empty and trimmed")
    return value


@dataclass(frozen=True, slots=True)
class OpenWASettings:
    api_base_url: str
    api_key: str
    internal_session_id: str
    authorized_sender: str
    operator_chat_id: str
    max_text_characters: int = 4096

    def __post_init__(self) -> None:
        for name in (
            "api_base_url",
            "api_key",
            "internal_session_id",
            "authorized_sender",
            "operator_chat_id",
        ):
            _required_text(getattr(self, name), name)
        if not isinstance(self.max_text_characters, int) or isinstance(
            self.max_text_characters, bool
        ):
            raise TypeError("max_text_characters must be an integer")
        if self.max_text_characters <= 0:
            raise ValueError("max_text_characters must be positive")

    @classmethod
    def from_loaded_config(cls, loaded: LoadedRuntimeConfig) -> OpenWASettings:
        config = loaded.config
        values = {
            "api_base_url": config.openwa_api_base_url,
            "internal_session_id": config.openwa_internal_session_id,
            "authorized_sender": config.openwa_authorized_sender,
            "operator_chat_id": config.openwa_operator_chat_id,
            "api_key": loaded.secrets.openwa_api_key,
        }
        missing = tuple(name for name, value in values.items() if not value)
        if missing:
            raise ValueError(
                "OpenWA message flow is missing configuration: " + ", ".join(missing)
            )
        return cls(**values)  # type: ignore[arg-type]


class Clock(Protocol):
    def now(self) -> datetime: ...


class OpenWASender(Protocol):
    def send_text(self, chat_id: str, text: str) -> str: ...


class OpenWASendError(RuntimeError):
    """One send attempt failed, possibly after OpenWA accepted its body."""

    def __init__(self, code: str, *, may_have_sent: bool) -> None:
        self.code = code
        self.may_have_sent = may_have_sent
        super().__init__(code)


class WebhookDisposition(str, Enum):
    ADMITTED = "admitted"
    UNAUTHENTICATED = "unauthenticated"
    IGNORED = "ignored"
    DUPLICATE = "duplicate"
    INVALID = "invalid"


class DeliveryDisposition(str, Enum):
    NOT_NEEDED = "not_needed"
    SENT = "sent"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class WebhookAcknowledgement:
    status_code: int
    disposition: WebhookDisposition


@dataclass(frozen=True, slots=True)
class MessageOutcome:
    message_id: str
    runtime_result: RuntimeResult
    outbound_ids: tuple[str, ...]
    delivery: DeliveryDisposition


@dataclass(frozen=True, slots=True)
class _AdmittedMessage:
    message_id: str
    body: str


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


MAX_HTTP_RESPONSE_BYTES = 64 * 1024
OPENWA_TIMEOUT_SECONDS = 5.0


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


class _ReadableResponse(Protocol):
    def __enter__(self) -> Self: ...

    def __exit__(self, *args: object) -> object: ...

    def getcode(self) -> int: ...

    def read(self, limit: int = -1) -> bytes: ...


class OpenWAHttpSender:
    """Send text through the one independently verified OpenWA route."""

    def __init__(
        self,
        settings: OpenWASettings,
        *,
        opener: Callable[..., _ReadableResponse] | None = None,
    ) -> None:
        self.settings = settings
        self._opener = opener or build_opener(_RejectRedirects()).open

    def send_text(self, chat_id: str, text: str) -> str:
        if chat_id != self.settings.operator_chat_id:
            raise OpenWASendError("recipient_not_configured", may_have_sent=False)
        if not isinstance(text, str) or not text:
            raise OpenWASendError("invalid_text", may_have_sent=False)
        if len(text) > self.settings.max_text_characters:
            raise OpenWASendError("text_too_long", may_have_sent=False)
        session_id = quote(self.settings.internal_session_id, safe="")
        body = json.dumps(
            {"chatId": chat_id, "text": text},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = Request(
            self.settings.api_base_url.rstrip("/")
            + f"/sessions/{session_id}/messages/send-text",
            data=body,
            method="POST",
            headers={
                "X-API-Key": self.settings.api_key,
                "Content-Type": "application/json",
            },
        )
        try:
            with self._opener(request, timeout=OPENWA_TIMEOUT_SECONDS) as response:
                status = response.getcode()
                response_body = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            exc.close()
            raise OpenWASendError(
                "redirect_rejected" if 300 <= exc.code < 400 else "http_error",
                may_have_sent=True,
            ) from exc
        except (HTTPException, TimeoutError, URLError, OSError) as exc:
            raise OpenWASendError("unavailable", may_have_sent=True) from exc
        if (
            not isinstance(response_body, bytes)
            or len(response_body) > MAX_HTTP_RESPONSE_BYTES
        ):
            raise OpenWASendError("invalid_response", may_have_sent=True)
        if status != 201:
            raise OpenWASendError("http_error", may_have_sent=True)
        try:
            payload = json.loads(response_body.decode("utf-8"))
            outbound_id = payload["messageId"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise OpenWASendError("invalid_response", may_have_sent=True) from exc
        if not isinstance(outbound_id, str) or not outbound_id.strip():
            raise OpenWASendError("invalid_response", may_have_sent=True)
        return outbound_id


class OpenWAMessageFlow:
    """Authenticate and acknowledge inbound text before processing it."""

    def __init__(
        self,
        *,
        settings: OpenWASettings,
        signing_secret: bytes,
        runtime: PersonalRuntime,
        sender: OpenWASender,
        clock: Clock | None = None,
    ) -> None:
        if not isinstance(signing_secret, bytes):
            raise TypeError("signing_secret must be bytes")
        if not signing_secret:
            raise ValueError("signing_secret must be non-empty")
        self.settings = settings
        self._signing_secret = signing_secret
        self._runtime = runtime
        self._sender = sender
        self._clock = clock or _SystemClock()
        self._tasks: set[asyncio.Task[MessageOutcome]] = set()

    def receive_webhook(
        self, raw_body: bytes, headers: Mapping[str, str]
    ) -> WebhookAcknowledgement:
        admitted = self._admit(raw_body, headers)
        if isinstance(admitted, WebhookAcknowledgement):
            return admitted
        task = asyncio.get_running_loop().create_task(self._process(admitted))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return WebhookAcknowledgement(202, WebhookDisposition.ADMITTED)

    async def drain(self) -> tuple[MessageOutcome, ...]:
        tasks = tuple(self._tasks)
        if not tasks:
            return ()
        return tuple(await asyncio.gather(*tasks))

    def _admit(
        self, raw_body: bytes, headers: Mapping[str, str]
    ) -> _AdmittedMessage | WebhookAcknowledgement:
        signature = next(
            (
                value
                for name, value in headers.items()
                if name.lower() == "x-openwa-signature"
            ),
            "",
        )
        expected = hmac.new(self._signing_secret, raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, f"sha256={expected}"):
            return WebhookAcknowledgement(401, WebhookDisposition.UNAUTHENTICATED)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return WebhookAcknowledgement(400, WebhookDisposition.INVALID)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            return WebhookAcknowledgement(400, WebhookDisposition.INVALID)
        data = payload["data"]
        required = (
            payload.get("idempotencyKey"),
            payload.get("deliveryId"),
            data.get("id"),
            data.get("body"),
        )
        if not all(isinstance(value, str) for value in required):
            return WebhookAcknowledgement(400, WebhookDisposition.INVALID)
        if (
            payload.get("event") != "message.received"
            or payload.get("sessionId") != self.settings.internal_session_id
            or data.get("from") != self.settings.authorized_sender
            or data.get("chatId") != self.settings.operator_chat_id
            or data.get("type") != "text"
            or data.get("fromMe") is not False
            or data.get("isGroup") is not False
            or not data["id"]
            or not data["body"]
        ):
            return WebhookAcknowledgement(202, WebhookDisposition.IGNORED)
        return _AdmittedMessage(data["id"], data["body"])

    async def _process(self, message: _AdmittedMessage) -> MessageOutcome:
        result = await self._runtime.receive(
            InboundText(message.message_id, message.body, self._clock.now())
        )
        chunks = tuple(
            chunk
            for reply in result.replies
            for chunk in split_text(reply, self.settings.max_text_characters)
        )
        outbound_ids: list[str] = []
        for chunk in chunks:
            try:
                outbound_ids.append(
                    self._sender.send_text(self.settings.operator_chat_id, chunk)
                )
            except OpenWASendError as exc:
                delivery = (
                    DeliveryDisposition.UNKNOWN
                    if exc.may_have_sent
                    else DeliveryDisposition.FAILED
                )
                return MessageOutcome(
                    message.message_id, result, tuple(outbound_ids), delivery
                )
        delivery = (
            DeliveryDisposition.SENT if chunks else DeliveryDisposition.NOT_NEEDED
        )
        return MessageOutcome(message.message_id, result, tuple(outbound_ids), delivery)


def build_openwa_message_flow(
    loaded: LoadedRuntimeConfig,
    runtime: PersonalRuntime,
    *,
    sender: OpenWASender | None = None,
    clock: Clock | None = None,
) -> OpenWAMessageFlow:
    """Compose the message flow from the replacement's three-file config."""

    settings = OpenWASettings.from_loaded_config(loaded)
    signing_secret = loaded.secrets.openwa_webhook_signing_secret
    if not signing_secret:
        raise ValueError("OpenWA message flow is missing webhook signing secret")
    return OpenWAMessageFlow(
        settings=settings,
        signing_secret=signing_secret.encode("utf-8"),
        runtime=runtime,
        sender=sender or OpenWAHttpSender(settings),
        clock=clock,
    )


def split_text(text: str, limit: int) -> tuple[str, ...]:
    """Split text deterministically at the last safe boundary within each limit."""

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    remaining = text
    chunks: list[str] = []
    while len(remaining) > limit:
        window = remaining[: limit + 1]
        newline = window.rfind("\n", 0, limit + 1)
        space = window.rfind(" ", 0, limit + 1)
        split_at = max(newline, space)
        if split_at <= 0:
            split_at = limit
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:]
        else:
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at + 1 :]
    if remaining:
        chunks.append(remaining)
    return tuple(chunks)


__all__ = [
    "DeliveryDisposition",
    "MessageOutcome",
    "OpenWAHttpSender",
    "OpenWAMessageFlow",
    "OpenWASendError",
    "OpenWASettings",
    "WebhookAcknowledgement",
    "WebhookDisposition",
    "build_openwa_message_flow",
    "split_text",
]

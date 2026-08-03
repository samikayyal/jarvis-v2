"""Typed envelopes used by the ticket01 control-plane tracer bullet.

The transport envelope deliberately keeps the exact signed bytes.  The
receiver verifies those bytes before decoding them into an :class:`InboundMessage`.
Everything after admission is represented by a small, immutable dataclass so
that adapters cannot exchange untyped dictionaries or hidden side effects.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any


def utc_now() -> datetime:
    """Return an aware UTC timestamp for callers that need a real clock."""

    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Normalize a timestamp while rejecting ambiguous naive datetimes."""

    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _non_empty_identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{name} must be a non-empty canonical string")
    return value


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sign_body(raw_body: bytes, secret: bytes) -> str:
    """Create the HMAC-SHA256 signature used by the controlled receiver."""

    if not isinstance(secret, bytes) or not secret:
        raise ValueError("signing secret must be non-empty bytes")
    return hmac.new(secret, raw_body, hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """Decoded fields from one signed ``message.received`` event."""

    event_type: str
    session_id: str
    event_id: str
    message_id: str
    sender_id: str
    chat_id: str
    chat_type: str
    message_type: str
    from_me: bool
    text: str

    def __post_init__(self) -> None:
        for name in (
            "event_type",
            "session_id",
            "event_id",
            "message_id",
            "sender_id",
            "chat_id",
            "chat_type",
            "message_type",
        ):
            _non_empty_identifier(getattr(self, name), name)
        if not isinstance(self.from_me, bool):
            raise TypeError("from_me must be a boolean")
        if not isinstance(self.text, str):
            raise TypeError("text must be a string")

    def as_mapping(self) -> dict[str, Any]:
        """Return the stable transport representation used for signing."""

        return {
            "chat_id": self.chat_id,
            "chat_type": self.chat_type,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "from_me": self.from_me,
            "message_id": self.message_id,
            "message_type": self.message_type,
            "sender_id": self.sender_id,
            "session_id": self.session_id,
            "text": self.text,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> InboundMessage:
        if not isinstance(payload, Mapping):
            raise TypeError("event payload must be an object")
        required = {
            "event_type",
            "session_id",
            "event_id",
            "message_id",
            "sender_id",
            "chat_id",
            "chat_type",
            "message_type",
            "from_me",
            "text",
        }
        if not required.issubset(payload):
            raise ValueError("event payload is missing required fields")
        try:
            return cls(
                event_type=payload["event_type"],
                session_id=payload["session_id"],
                event_id=payload["event_id"],
                message_id=payload["message_id"],
                sender_id=payload["sender_id"],
                chat_id=payload["chat_id"],
                chat_type=payload["chat_type"],
                message_type=payload["message_type"],
                from_me=payload["from_me"],
                text=payload["text"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("event payload has invalid field types") from exc


@dataclass(frozen=True, slots=True)
class SignedInboundEvent:
    """A signed transport envelope retaining the exact bytes for verification."""

    raw_body: bytes
    signature: str

    def __post_init__(self) -> None:
        if not isinstance(self.raw_body, bytes):
            raise TypeError("raw_body must be bytes")
        if not isinstance(self.signature, str) or not self.signature:
            raise TypeError("signature must be a non-empty string")

    @classmethod
    def from_message(
        cls, message: InboundMessage, secret: bytes
    ) -> SignedInboundEvent:
        raw_body = _canonical_json(message.as_mapping())
        return cls(raw_body=raw_body, signature=sign_body(raw_body, secret))

    @classmethod
    def from_mapping(
        cls, payload: Mapping[str, Any], secret: bytes
    ) -> SignedInboundEvent:
        raw_body = _canonical_json(payload)
        return cls(raw_body=raw_body, signature=sign_body(raw_body, secret))

    def verify(self, secret: bytes) -> bool:
        if not isinstance(secret, bytes) or not secret:
            return False
        expected = sign_body(self.raw_body, secret)
        return hmac.compare_digest(expected, self.signature)

    def decode(self) -> InboundMessage:
        try:
            payload = json.loads(self.raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise ValueError("signed body is not valid JSON") from exc
        return InboundMessage.from_mapping(payload)


@dataclass(frozen=True, slots=True)
class IngressClaim:
    """The durable identity used to reject a replayed message."""

    session_id: str
    message_id: str
    event_id: str
    claimed_at: datetime

    def __post_init__(self) -> None:
        for name in ("session_id", "message_id", "event_id"):
            _non_empty_identifier(getattr(self, name), name)
        object.__setattr__(self, "claimed_at", ensure_utc(self.claimed_at))


@dataclass(frozen=True, slots=True)
class RequestState:
    """Durable, bounded lifecycle state for one admitted request."""

    request_id: str
    event_id: str
    message_id: str
    operator_id: str
    session_id: str
    chat_id: str
    created_at: datetime
    updated_at: datetime
    status: str
    phase: str
    reply_id: str | None = None
    outcome: str | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "request_id",
            "event_id",
            "message_id",
            "operator_id",
            "session_id",
            "chat_id",
            "status",
            "phase",
        ):
            _non_empty_identifier(getattr(self, name), name)
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))
        object.__setattr__(self, "updated_at", ensure_utc(self.updated_at))

    @property
    def correlation_id(self) -> str:
        """Alias used when describing the request/reply correlation contract."""

        return self.request_id


@dataclass(frozen=True, slots=True)
class OrchestrationRequest:
    """Ephemeral input passed from the broker to an orchestration adapter."""

    state: RequestState
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("orchestration text must be non-blank")


@dataclass(frozen=True, slots=True)
class OrchestrationResult:
    """Typed, non-authoritative result returned by orchestration."""

    request_id: str
    outcome: str
    reply_text: str
    adapter: str = "controlled"

    def __post_init__(self) -> None:
        _non_empty_identifier(self.request_id, "request_id")
        _non_empty_identifier(self.outcome, "outcome")
        if not isinstance(self.reply_text, str) or not self.reply_text.strip():
            raise ValueError("reply_text must be non-blank")
        _non_empty_identifier(self.adapter, "adapter")


@dataclass(frozen=True, slots=True)
class OutboundReply:
    """Typed reply constrained to the admitted operator conversation."""

    reply_id: str
    request_id: str
    session_id: str
    recipient_id: str
    body: str

    def __post_init__(self) -> None:
        for name in ("reply_id", "request_id", "session_id", "recipient_id"):
            _non_empty_identifier(getattr(self, name), name)
        if not isinstance(self.body, str) or not self.body.strip():
            raise ValueError("reply body must be non-blank")

    @property
    def correlation_id(self) -> str:
        return self.request_id


_FORBIDDEN_AUDIT_KEYS = frozenset({
    "body",
    "credential",
    "message_text",
    "operator_id",
    "raw_body",
    "secret",
    "sender_id",
    "signature",
    "text",
})


@dataclass(frozen=True, slots=True)
class AuditEvidence:
    """Redacted append-only evidence; raw transport content never belongs here."""

    evidence_id: str
    kind: str
    occurred_at: datetime
    event_id: str | None = None
    request_id: str | None = None
    outcome: str = "recorded"
    actor: str = "control_plane"
    details: Mapping[str, str] = field(default_factory=dict)
    redacted: bool = True

    def __post_init__(self) -> None:
        _non_empty_identifier(self.evidence_id, "evidence_id")
        _non_empty_identifier(self.kind, "kind")
        _non_empty_identifier(self.outcome, "outcome")
        _non_empty_identifier(self.actor, "actor")
        if self.event_id is not None:
            _non_empty_identifier(self.event_id, "event_id")
        if self.request_id is not None:
            _non_empty_identifier(self.request_id, "request_id")
        if not self.redacted:
            raise ValueError("audit evidence must be redacted")
        normalized = {str(key): str(value) for key, value in self.details.items()}
        forbidden = _FORBIDDEN_AUDIT_KEYS.intersection(normalized)
        if forbidden:
            raise ValueError("audit evidence contains a forbidden raw-content field")
        object.__setattr__(self, "occurred_at", ensure_utc(self.occurred_at))
        object.__setattr__(self, "details", MappingProxyType(normalized))


@dataclass(frozen=True, slots=True)
class ReceiveResult:
    """Externally visible result of one receiver invocation."""

    status_code: int
    disposition: str
    request: RequestState | None = None
    reply: OutboundReply | None = None
    reason: str | None = None

    @property
    def request_id(self) -> str | None:
        return self.request.request_id if self.request else None

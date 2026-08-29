"""Ingress messaging envelopes, admission claims, and shared domain primitives."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
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
    sender_id: object | None
    chat_id: object | None
    chat_type: object | None
    message_type: object | None
    from_me: object | None
    text: object | None

    def __post_init__(self) -> None:
        for name in (
            "event_type",
            "session_id",
            "event_id",
            "message_id",
        ):
            _non_empty_identifier(getattr(self, name), name)

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
        }
        if not required.issubset(payload):
            raise ValueError("event payload is missing required fields")
        try:
            return cls(
                event_type=payload["event_type"],
                session_id=payload["session_id"],
                event_id=payload["event_id"],
                message_id=payload["message_id"],
                sender_id=payload.get("sender_id"),
                chat_id=payload.get("chat_id"),
                chat_type=payload.get("chat_type"),
                message_type=payload.get("message_type"),
                from_me=payload.get("from_me"),
                text=payload.get("text"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("event payload has invalid field types") from exc


@dataclass(frozen=True, slots=True)
class SignedInboundEvent:
    """A signed transport envelope retaining the exact bytes for verification."""

    raw_body: bytes
    signature: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.raw_body, bytes):
            raise TypeError("raw_body must be bytes")
        if self.signature is not None and not isinstance(self.signature, str):
            raise TypeError("signature must be a string or null")

    @classmethod
    def from_message(cls, message: InboundMessage, secret: bytes) -> SignedInboundEvent:
        raw_body = _canonical_json(message.as_mapping())
        return cls(raw_body=raw_body, signature=sign_body(raw_body, secret))

    @classmethod
    def from_mapping(
        cls, payload: Mapping[str, Any], secret: bytes
    ) -> SignedInboundEvent:
        raw_body = _canonical_json(payload)
        return cls(raw_body=raw_body, signature=sign_body(raw_body, secret))

    def verify(self, secret: bytes) -> bool:
        if (
            not isinstance(secret, bytes)
            or not secret
            or not isinstance(self.signature, str)
            or not self.signature
        ):
            return False
        expected = sign_body(self.raw_body, secret)
        supplied = self.signature
        if supplied.startswith("sha256="):
            supplied = supplied.removeprefix("sha256=")
        return hmac.compare_digest(expected, supplied)

    def decode(self) -> InboundMessage:
        try:
            payload = json.loads(self.raw_body.decode("utf-8"))
            if isinstance(payload, Mapping) and "sessionId" in payload:
                return _openwa_inbound_message(payload)
            return InboundMessage.from_mapping(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError("signed body is not valid JSON") from exc


def _openwa_inbound_message(payload: Mapping[str, Any]) -> InboundMessage:
    """Translate the pinned OpenWA webhook DTO after signature verification."""

    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise TypeError("OpenWA event data must be an object")
    is_group = data.get("isGroup")
    chat_type = "group" if is_group is True else "direct" if is_group is False else None
    sender_id = None if data.get("isLidSender") is True else data.get("from")
    return InboundMessage(
        event_type=payload.get("event"),  # type: ignore[arg-type]
        session_id=payload.get("sessionId"),  # type: ignore[arg-type]
        event_id=payload.get("idempotencyKey"),  # type: ignore[arg-type]
        message_id=data.get("id"),  # type: ignore[arg-type]
        sender_id=sender_id,
        chat_id=data.get("chatId"),
        chat_type=chat_type,
        message_type=data.get("type"),
        from_me=data.get("fromMe"),
        text=data.get("body"),
    )


@dataclass(frozen=True, slots=True)
class IngressClaim:
    """The durable identity used to reject a replayed message."""

    session_id: str
    message_id: str
    event_id: str
    claimed_at: datetime
    disposition: str = "admitted"

    def __post_init__(self) -> None:
        for name in ("session_id", "message_id", "event_id"):
            _non_empty_identifier(getattr(self, name), name)
        _non_empty_identifier(self.disposition, "disposition")
        if self.disposition == "pending_audit":
            raise ValueError("ingress claims must have a terminal disposition")
        object.__setattr__(self, "claimed_at", ensure_utc(self.claimed_at))


_CREDENTIAL_LIKE_PATTERNS = (
    re.compile(
        r"\b(?:api[_ -]?key|password|passwd|secret|token|access[_ -]?token|"
        r"refresh[_ -]?token|id[_ -]?token|client[_ -]?secret|webhook[_ -]?secret)"
        r"\s*[:=]",
        re.IGNORECASE,
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{8,}\b", re.IGNORECASE),
    # Authorization-shaped text fails closed.  The retained body is not
    # transformed, but it must never become automatic model context merely
    # because the credential issuer uses a form not listed above.
    re.compile(r"\bauthorization\s*:\s*\S+", re.IGNORECASE),
    re.compile(r"\bbearer\s+[A-Za-z0-9._~+/-]{8,}", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:whsec|xox[baprs]|EAAG)[-_A-Za-z0-9]{8,}\b", re.IGNORECASE),
)


def _contains_credential_like_text(text: str) -> bool:
    """Classify common credential-shaped text without transforming retention."""

    return any(
        pattern.search(text) is not None for pattern in _CREDENTIAL_LIKE_PATTERNS
    )


@dataclass(frozen=True, slots=True)
class IngressAdmissionResult:
    """Result of one atomic claim, audit, and terminal-disposition attempt."""

    claimed: bool
    disposition: str

    def __post_init__(self) -> None:
        _non_empty_identifier(self.disposition, "disposition")

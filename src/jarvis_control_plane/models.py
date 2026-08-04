"""Typed envelopes used by the ticket01 control-plane seam.

The transport envelope deliberately keeps the exact signed bytes.  The
receiver verifies those bytes before decoding them into an :class:`InboundMessage`.
Everything after admission is represented by a small, immutable dataclass so
that adapters cannot exchange untyped dictionaries or hidden side effects.
The audit models also define the ticket03 bounded redacted record and safe
administration filter without carrying raw transport or working content.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
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


_FORBIDDEN_AUDIT_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "body",
        "credential",
        "credentials",
        "chat_id",
        "content",
        "message_text",
        "password",
        "phone_number",
        "private_key",
        "prompt",
        "raw",
        "operator_id",
        "output",
        "payload",
        "recipient_id",
        "raw_body",
        "refresh_token",
        "secret",
        "secret_value",
        "sender_id",
        "signature",
        "text",
        "token",
    }
)

_REDACTED_AUDIT_VALUE = "[redacted]"
_MAX_AUDIT_IDENTIFIER_LENGTH = 128
_MAX_AUDIT_DETAIL_KEYS = 24
_MAX_AUDIT_DETAIL_KEY_LENGTH = 64
_MAX_AUDIT_DETAIL_VALUE_LENGTH = 256
_MAX_AUDIT_VIEW_LIMIT = 1_000
_SAFE_AUDIT_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_SAFE_AUDIT_DETAIL_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_ALLOWED_AUDIT_DETAIL_KEYS = frozenset(
    {
        "action",
        "adapter",
        "argument_count",
        "arguments",
        "approval",
        "args",
        "channel",
        "command",
        "command_line",
        "command_name",
        "cookie",
        "count",
        "dependency",
        "destination",
        "delivery_state",
        "dispatch_state",
        "environment",
        "error",
        "error_code",
        "execution_status",
        "exception",
        "headers",
        "host",
        "kind",
        "limit",
        "message",
        "mode",
        "operation",
        "path",
        "permission_scope",
        "phase",
        "policy",
        "query",
        "reason",
        "result",
        "scope",
        "service",
        "source",
        "stack_trace",
        "state",
        "status",
        "target",
        "target_category",
        "tool_input",
        "tool_result",
    }
)
_LEGACY_OPERATION_TYPES = {
    "inbound_admitted": "inbound_admission",
    "inbound_malformed": "inbound_admission",
    "inbound_rejected": "inbound_admission",
    "orchestration_result": "orchestration",
    "outbound_completion": "outbound_message",
    "outbound_result": "outbound_message",
    "request_accepted": "request_lifecycle",
}
_LEGACY_TARGET_CATEGORIES = {
    "configured_operator": "messaging_gateway",
    "controlled_orchestration": "control_plane",
    "controlled_outbound": "operator_conversation",
    "transport": "messaging_gateway",
}
_LEGACY_EXECUTION_STATUSES = {
    "accepted": "accepted",
    "failed": "failed",
    "rejected": "rejected",
    "recorded": "recorded",
    "outbound_unknown": "unknown",
}
_SENSITIVE_DETAIL_KEYS = frozenset(
    {
        "api_key",
        "args",
        "arguments",
        "authorization",
        "command",
        "command_line",
        "cookie",
        "error",
        "environment",
        "exception",
        "headers",
        "message",
        "path",
        "private_key",
        "query",
        "refresh_token",
        "result_payload",
        "secret_value",
        "stack_trace",
        "target",
        "tool_result",
        "tool_input",
    }
)
_SENSITIVE_AUDIT_VALUE = re.compile(
    r"(?i)(?:bearer\s+\S+|(?:access[_-]?token|api[_-]?key|authorization|cookie|"
    r"password|private[_-]?key|refresh[_-]?token|secret|token)\s*[:=]\s*\S+|"
    r"-----begin\s+[^-]+private key-----|\b(?:credential|password|secret|token)\b)"
)

_ADMISSION_REJECTION_REASONS = frozenset(
    {
        "blank_text",
        "not_direct_message",
        "self_message",
        "text_too_large",
        "unauthorized_chat",
        "unauthorized_operator",
        "unsupported_event_type",
        "unsupported_message_type",
        "wrong_session",
    }
)
_LIFECYCLE_PHASES = frozenset({"audit_gate", "completed", "orchestration", "outbound"})
_LIFECYCLE_STATUSES = frozenset(
    {"accepted", "blocked", "completed", "failed", "replying", "unknown"}
)
_OUTBOUND_RESULTS = frozenset(
    {
        "accepted",
        "failed",
        "outbound_failed",
        "outbound_unknown",
        "pending",
        "reply_sent",
        "unknown",
    }
)
_AUDIT_DETAIL_SCHEMAS: dict[str, dict[str, frozenset[str] | None]] = {
    "action_outcome": {
        "channel": frozenset({"controlled"}),
        "command": None,
        "reason": None,
        "result": frozenset(
            {"accepted", "completed", "failed", "not_started", "rejected", "unknown"}
        ),
    },
    "inbound_admitted": {
        "channel": frozenset({"direct_text"}),
        "phase": frozenset({"admission"}),
    },
    "inbound_malformed": {"reason": frozenset({"malformed_envelope"})},
    "inbound_rejected": {"reason": _ADMISSION_REJECTION_REASONS},
    "orchestration_result": {
        "adapter": frozenset({"controlled"}),
        "result": frozenset({"failed"}),
        "state": frozenset({"unavailable"}),
    },
    "outbound_attempt": {
        "channel": frozenset({"controlled_outbound"}),
        "destination": frozenset({"configured_operator"}),
    },
    "outbound_completion": {
        "result": _OUTBOUND_RESULTS | frozenset({"not_sent"}),
        "state": frozenset({"unavailable"}),
    },
    "outbound_result": {
        "channel": frozenset({"controlled_outbound"}),
        "result": _OUTBOUND_RESULTS,
    },
    "request_accepted": {"phase": frozenset({"orchestration"})},
    "request_lifecycle": {
        "phase": _LIFECYCLE_PHASES,
        "status": _LIFECYCLE_STATUSES,
    },
    "lifecycle": {
        "phase": _LIFECYCLE_PHASES,
        "status": _LIFECYCLE_STATUSES,
    },
}
_AUDIT_EVENT_RULES: dict[str, tuple[str, str, str, frozenset[str], frozenset[str]]] = {
    "inbound_admitted": (
        "inbound_admission",
        "messaging_gateway",
        "configured_operator",
        frozenset({"accepted"}),
        frozenset({"accepted"}),
    ),
    "inbound_malformed": (
        "inbound_admission",
        "messaging_gateway",
        "transport",
        frozenset({"rejected"}),
        frozenset({"rejected"}),
    ),
    "inbound_rejected": (
        "inbound_admission",
        "messaging_gateway",
        "transport",
        frozenset({"rejected"}),
        frozenset({"rejected"}),
    ),
    "orchestration_result": (
        "orchestration",
        "control_plane",
        "controlled_orchestration",
        frozenset({"completed", "failed"}),
        frozenset({"completed", "failed"}),
    ),
    "outbound_attempt": (
        "outbound_message",
        "operator_conversation",
        "controlled_outbound",
        frozenset({"attempted"}),
        frozenset({"attempted"}),
    ),
    "outbound_completion": (
        "outbound_message",
        "operator_conversation",
        "controlled_outbound",
        frozenset({"not_sent", "outbound_failed", "outbound_unknown", "reply_sent"}),
        frozenset({"failed", "unknown", "accepted"}),
    ),
    "outbound_result": (
        "outbound_message",
        "operator_conversation",
        "controlled_outbound",
        frozenset({"accepted", "failed", "pending", "unknown"}),
        frozenset({"accepted", "failed", "pending", "unknown"}),
    ),
    "request_accepted": (
        "request_lifecycle",
        "control_plane",
        "configured_operator",
        frozenset({"accepted"}),
        frozenset({"accepted"}),
    ),
    "request_lifecycle": (
        "request_lifecycle",
        "control_plane",
        "control_plane",
        frozenset(
            {
                "accepted",
                "audit_unavailable",
                "blocked",
                "completed",
                "failed",
                "orchestration_failed",
                "outbound_failed",
                "outbound_unknown",
                "reply_sent",
                "replying",
                "unknown",
            }
        ),
        _LIFECYCLE_STATUSES,
    ),
}


def _audit_identifier(value: str, name: str) -> str:
    _non_empty_identifier(value, name)
    if len(
        value
    ) > _MAX_AUDIT_IDENTIFIER_LENGTH or not _SAFE_AUDIT_IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} is too long for bounded audit evidence")
    if _SENSITIVE_AUDIT_VALUE.search(value):
        raise ValueError(f"{name} contains sensitive content")
    return value


def _normalize_audit_details(
    details: Mapping[str, str], *, kind: str
) -> Mapping[str, str]:
    if not isinstance(details, Mapping):
        raise TypeError("audit details must be a mapping")
    if len(details) > _MAX_AUDIT_DETAIL_KEYS:
        raise ValueError("audit evidence has too many detail fields")

    schema = _AUDIT_DETAIL_SCHEMAS.get(kind)

    normalized: dict[str, str] = {}
    for key, value in details.items():
        if not isinstance(key, str) or not key or key.strip() != key:
            raise ValueError("audit detail keys must be canonical strings")
        if len(key) > _MAX_AUDIT_DETAIL_KEY_LENGTH:
            raise ValueError("audit detail key is too long")
        canonical_key = key.casefold().replace("-", "_")
        if canonical_key in normalized:
            raise ValueError("audit evidence contains duplicate detail fields")
        if canonical_key in _FORBIDDEN_AUDIT_KEYS or any(
            part
            in {
                "body",
                "content",
                "credential",
                "operator",
                "password",
                "payload",
                "prompt",
                "raw",
                "secret",
                "sender",
                "signature",
                "token",
            }
            for part in canonical_key.split("_")
        ):
            raise ValueError("audit evidence contains a forbidden raw-content field")
        if canonical_key not in _ALLOWED_AUDIT_DETAIL_KEYS:
            raise ValueError("audit evidence contains an unsupported detail field")
        if isinstance(value, str) and len(value) > _MAX_AUDIT_DETAIL_VALUE_LENGTH:
            raise ValueError("audit detail value is too long")
        if schema is None or canonical_key not in schema:
            raise ValueError("audit evidence contains an unsupported detail field")

        if not isinstance(value, str):
            raise TypeError("audit detail values must be strings")
        if len(value) > _MAX_AUDIT_DETAIL_VALUE_LENGTH:
            raise ValueError("audit detail value is too long")
        safe_value = (
            _REDACTED_AUDIT_VALUE
            if canonical_key in _SENSITIVE_DETAIL_KEYS
            or (kind == "action_outcome" and canonical_key == "reason")
            or _SENSITIVE_AUDIT_VALUE.search(value)
            else value
        )
        if (
            safe_value != _REDACTED_AUDIT_VALUE
            and not _SAFE_AUDIT_DETAIL_VALUE.fullmatch(safe_value)
        ):
            raise ValueError("audit detail values must be bounded safe codes")
        allowed_values = schema.get(canonical_key) if schema is not None else None
        if (
            safe_value != _REDACTED_AUDIT_VALUE
            and allowed_values is not None
            and safe_value not in allowed_values
        ):
            raise ValueError("audit detail value is not a controlled audit code")
        normalized[canonical_key] = safe_value
    return MappingProxyType(normalized)


def _validate_audit_event_semantics(
    *,
    kind: str,
    operation_type: str | None,
    target_category: str | None,
    actor: str,
    outcome: str,
    execution_status: str | None,
    details: Mapping[str, str],
) -> None:
    rule = _AUDIT_EVENT_RULES.get(kind)
    if rule is not None:
        expected_operation, expected_target, expected_actor, outcomes, statuses = rule
        if operation_type is not None and operation_type != expected_operation:
            raise ValueError("audit operation type does not match its event kind")
        if target_category is not None and target_category != expected_target:
            raise ValueError("audit target category does not match its event kind")
        if actor != expected_actor:
            raise ValueError("audit actor does not match its event kind")
        if outcome not in outcomes:
            raise ValueError("audit outcome does not match its event kind")
        if execution_status is not None and execution_status not in statuses:
            raise ValueError("audit execution status does not match its event kind")

    detail_result = details.get("result")
    if kind in {"outbound_result", "outbound_completion"} and detail_result is not None:
        expected = {
            "pending": ("pending", "pending"),
            "accepted": ("accepted", "accepted"),
            "failed": ("failed", "failed"),
            "unknown": ("unknown", "unknown"),
            "not_sent": ("not_sent", "failed"),
            "outbound_failed": ("outbound_failed", "failed"),
            "outbound_unknown": ("outbound_unknown", "unknown"),
            "reply_sent": ("reply_sent", "accepted"),
        }.get(detail_result)
        if (
            expected is None
            or outcome != expected[0]
            or (kind == "outbound_result" and execution_status != expected[1])
            or (
                kind == "outbound_completion"
                and execution_status is not None
                and execution_status != expected[1]
            )
        ):
            raise ValueError("outbound result fields are contradictory")
    if kind == "request_lifecycle":
        status = details.get("status")
        if status is not None and execution_status != status:
            raise ValueError("request lifecycle fields are contradictory")
        expected_outcomes = {
            "accepted": "accepted",
            "blocked": "audit_unavailable",
            "completed": "reply_sent",
            "failed": None,
            "replying": "replying",
            "unknown": "outbound_unknown",
        }
        expected_outcome = expected_outcomes.get(status)
        if expected_outcome is not None and outcome != expected_outcome:
            raise ValueError("request lifecycle outcome does not match its status")
        if status == "failed" and outcome not in {
            "failed",
            "orchestration_failed",
            "outbound_failed",
        }:
            raise ValueError("request lifecycle outcome does not match its status")


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
    message_id: str | None = None
    operation_type: str | None = None
    target_category: str | None = None
    approval_decision: str | None = None
    policy_decision: str | None = None
    execution_status: str | None = None

    def __post_init__(self) -> None:
        _audit_identifier(self.evidence_id, "evidence_id")
        _audit_identifier(self.kind, "kind")
        _audit_identifier(self.outcome, "outcome")
        _audit_identifier(self.actor, "actor")
        if self.event_id is not None:
            _audit_identifier(self.event_id, "event_id")
        if self.request_id is not None:
            _audit_identifier(self.request_id, "request_id")
        for name in (
            "message_id",
            "operation_type",
            "target_category",
            "approval_decision",
            "policy_decision",
            "execution_status",
        ):
            value = getattr(self, name)
            if value is not None:
                _audit_identifier(value, name)
        if self.redacted is not True:
            raise ValueError("audit evidence must be redacted")
        object.__setattr__(self, "occurred_at", ensure_utc(self.occurred_at))
        object.__setattr__(
            self,
            "details",
            _normalize_audit_details(self.details, kind=self.kind),
        )
        _validate_audit_event_semantics(
            kind=self.kind,
            operation_type=self.operation_type,
            target_category=self.target_category,
            actor=self.actor,
            outcome=self.outcome,
            execution_status=self.execution_status,
            details=self.details,
        )

    @property
    def operation(self) -> str | None:
        """Compatibility alias for callers that call the operation a category."""

        return self.operation_type or _LEGACY_OPERATION_TYPES.get(self.kind, self.kind)

    @property
    def effective_target_category(self) -> str | None:
        return self.target_category or _LEGACY_TARGET_CATEGORIES.get(self.actor)

    @property
    def effective_execution_status(self) -> str | None:
        return self.execution_status or _LEGACY_EXECUTION_STATUSES.get(self.outcome)

    @property
    def approval(self) -> str | None:
        """Compatibility alias for the safe approval decision field."""

        return self.approval_decision

    @property
    def policy(self) -> str | None:
        """Compatibility alias for the safe policy decision field."""

        return self.policy_decision

    def as_safe_mapping(self) -> dict[str, Any]:
        """Return the only representation that may cross the admin export boundary."""

        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "occurred_at": self.occurred_at.isoformat(),
            "event_id": self.event_id,
            "request_id": self.request_id,
            "message_id": self.message_id,
            "operation_type": self.operation,
            "target_category": self.effective_target_category,
            "approval_decision": self.approval_decision,
            "policy_decision": self.policy_decision,
            "execution_status": self.effective_execution_status,
            "outcome": self.outcome,
            "actor": self.actor,
            "details": dict(self.details),
            "redacted": True,
        }


@dataclass(frozen=True, slots=True)
class AuditFilter:
    """Deterministic filters for the bounded redacted audit view.

    ``start_at`` is inclusive and ``end_at`` is exclusive.  ``on_date`` is a
    UTC calendar-day shortcut and cannot be combined with a time range.
    ``limit`` bounds the returned view rather than changing stored evidence.
    When omitted, the safe administration surface still applies the maximum
    bounded page size.
    """

    start_at: datetime | None = None
    end_at: datetime | None = None
    on_date: date | None = None
    request_id: str | None = None
    operation_type: str | None = None
    target_category: str | None = None
    approval_decision: str | None = None
    policy_decision: str | None = None
    execution_status: str | None = None
    outcome: str | None = None
    limit: int = _MAX_AUDIT_VIEW_LIMIT

    def __post_init__(self) -> None:
        normalized_start = ensure_utc(self.start_at) if self.start_at else None
        normalized_end = ensure_utc(self.end_at) if self.end_at else None
        if normalized_start and normalized_end and normalized_start > normalized_end:
            raise ValueError("audit filter start_at must not be after end_at")
        if self.on_date is not None:
            if not isinstance(self.on_date, date) or isinstance(self.on_date, datetime):
                raise TypeError("audit filter on_date must be a calendar date")
            if normalized_start is not None or normalized_end is not None:
                raise ValueError(
                    "audit filter on_date cannot be combined with a time range"
                )
        if (
            not isinstance(self.limit, int)
            or isinstance(self.limit, bool)
            or self.limit <= 0
            or self.limit > _MAX_AUDIT_VIEW_LIMIT
        ):
            raise ValueError(
                "audit filter limit must be a positive integer no greater than 1000"
            )
        for name in (
            "request_id",
            "operation_type",
            "target_category",
            "approval_decision",
            "policy_decision",
            "execution_status",
            "outcome",
        ):
            value = getattr(self, name)
            if value is not None:
                _audit_identifier(value, name)
        object.__setattr__(self, "start_at", normalized_start)
        object.__setattr__(self, "end_at", normalized_end)

    def matches(self, evidence: AuditEvidence) -> bool:
        """Return whether one redacted record belongs in this safe view."""

        occurred_at = ensure_utc(evidence.occurred_at)
        if self.on_date is not None and occurred_at.date() != self.on_date:
            return False
        if self.start_at is not None and occurred_at < self.start_at:
            return False
        if self.end_at is not None and occurred_at >= self.end_at:
            return False
        if self.request_id is not None and evidence.request_id != self.request_id:
            return False
        if (
            self.operation_type is not None
            and evidence.operation != self.operation_type
        ):
            return False
        if (
            self.target_category is not None
            and evidence.effective_target_category != self.target_category
        ):
            return False
        if (
            self.approval_decision is not None
            and evidence.approval_decision != self.approval_decision
        ):
            return False
        if (
            self.policy_decision is not None
            and evidence.policy_decision != self.policy_decision
        ):
            return False
        if (
            self.execution_status is not None
            and evidence.effective_execution_status != self.execution_status
        ):
            return False
        return not (self.outcome is not None and evidence.outcome != self.outcome)


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

"""Bounded audit identifiers, redaction values, and detail normalization."""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType

from .audit_rules import _AUDIT_DETAIL_SCHEMAS
from .ingress_messaging import _non_empty_identifier

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
        "disposition",
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
        "model",
        "mode",
        "operation",
        "outbound_not_started",
        "outbound_unknown",
        "path",
        "permission_scope",
        "permission_id",
        "phase",
        "policy",
        "query",
        "reason",
        "reasoning",
        "result",
        "scope",
        "service",
        "source",
        "stack_trace",
        "state",
        "status",
        "interrupted_requests",
        "interrupted_ingress",
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

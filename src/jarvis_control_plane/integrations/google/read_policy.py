"""Read-side bounds, serialization, and safe provider-failure policy."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Literal

from .read_models import (
    MAX_RESULT_ITEMS,
    GoogleReadOperation,
    GoogleReadResult,
)


def non_blank(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{name} must be a non-blank canonical string")
    return value


def text(value: object, name: str) -> str:
    if not isinstance(value, str) or value.strip() != value or len(value) > 1000:
        raise ValueError(f"{name} must be canonical text up to 1000 characters")
    return value


def limit(value: object, maximum: int, name: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value <= 0
        or value > maximum
    ):
        raise ValueError(f"{name} must be a positive integer no greater than {maximum}")
    return value


def requested_count(value: object) -> int:
    return limit(value, MAX_RESULT_ITEMS, "max_results")


def bounded_items(
    items: Sequence[str], *, max_items: int, max_item_bytes: int
) -> tuple[tuple[str, ...], bool]:
    bounded: list[str] = []
    truncated = False
    for item in items[:max_items]:
        encoded = item.encode("utf-8")
        if len(encoded) > max_item_bytes:
            item = encoded[:max_item_bytes].decode("utf-8", errors="ignore")
            truncated = True
        bounded.append(item)
    return tuple(bounded), truncated


def serialized_result(result: GoogleReadResult) -> bytes:
    return json.dumps(
        {
            "service": result.service,
            "operation": result.operation,
            "items": result.items,
            "truncated": result.truncated,
            "continuation_available": result.continuation_available,
            "content_available": result.content_available,
            "content_unavailable_reason": result.content_unavailable_reason,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def content_available(
    operation: GoogleReadOperation, items: Sequence[str]
) -> bool | None:
    if operation != "drive_files_get" or not items:
        return None
    try:
        payload = json.loads(items[0])
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    if payload.get("content_unavailable") == "unsupported_mime_type":
        return False
    if "content" in payload:
        return True
    return False if isinstance(payload.get("mimeType"), str) else None


def content_unavailable_reason(
    operation: GoogleReadOperation, items: Sequence[str]
) -> Literal["unsupported_mime_type"] | None:
    if operation != "drive_files_get" or not items:
        return None
    try:
        payload = json.loads(items[0])
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    reason = payload.get("content_unavailable")
    if reason == "unsupported_mime_type":
        return reason
    if "content" not in payload and isinstance(payload.get("mimeType"), str):
        return "unsupported_mime_type"
    return None


def safe_provider_failure(detail: str) -> str:
    if detail == "timeout":
        return "google_read_timeout"
    if detail == "rate_limited":
        return "google_read_rate_limited"
    if detail == "invalid_grant":
        return "google_read_disconnected"
    if detail == "oversized":
        return "google_read_oversized"
    return "google_read_unavailable"


_non_blank = non_blank
_text = text
_limit = limit
_requested_count = requested_count
_bounded_items = bounded_items
_serialized_result = serialized_result
_content_available = content_available
_content_unavailable_reason = content_unavailable_reason
_safe_provider_failure = safe_provider_failure

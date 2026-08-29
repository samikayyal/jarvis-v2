"""Strict JSON frame serialization for the archive IPC boundary."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import NoReturn

DEFAULT_MAX_ARCHIVE_FRAME_BYTES = 8 * 1024 * 1024


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON constant {value!r} is not supported")


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def validate_frame_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("deleted archive frame limit must be a positive integer")
    return value


_validate_frame_limit = validate_frame_limit


def encode_archive_frame(
    payload: Mapping[str, object],
    *,
    max_frame_bytes: int = DEFAULT_MAX_ARCHIVE_FRAME_BYTES,
) -> bytes:
    limit = validate_frame_limit(max_frame_bytes)
    try:
        frame = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("deleted archive payload is not strict JSON") from exc
    if len(frame) > limit:
        raise ValueError("deleted archive IPC frame exceeds the fixed size limit")
    return frame


def decode_archive_frame(
    frame: bytes,
    *,
    max_frame_bytes: int = DEFAULT_MAX_ARCHIVE_FRAME_BYTES,
) -> object:
    limit = validate_frame_limit(max_frame_bytes)
    if not isinstance(frame, bytes):
        raise TypeError("deleted archive IPC frame must be bytes")
    if len(frame) > limit:
        raise ValueError("deleted archive IPC frame exceeds the fixed size limit")
    try:
        return json.loads(
            frame.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("deleted archive IPC frame is not valid strict JSON") from exc


def require_exact_mapping(
    value: object,
    expected_fields: frozenset[str],
    description: str,
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise ValueError(f"{description} has an invalid schema")
    return value


_require_exact_mapping = require_exact_mapping


__all__ = [
    "DEFAULT_MAX_ARCHIVE_FRAME_BYTES",
    "decode_archive_frame",
    "encode_archive_frame",
    "require_exact_mapping",
    "validate_frame_limit",
]

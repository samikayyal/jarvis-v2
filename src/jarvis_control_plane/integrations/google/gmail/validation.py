"""Canonical Gmail action value validation."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal

_MIME_TYPES = frozenset({"text/plain", "text/html"})
_MAILBOX = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")


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

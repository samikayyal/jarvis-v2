"""Bounded and sanitized Gmail response parsing."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from html.parser import HTMLParser

from .read_models import GoogleReadOperation, GoogleReadProviderError

_GMAIL_TEXT_MIME_TYPES = frozenset({"text/plain", "text/html"})
_GMAIL_METADATA_HEADERS = frozenset(
    {"from", "to", "cc", "subject", "date", "message-id", "references"}
)


def gmail_message_fields() -> str:
    """Return the bounded Gmail fields requested by the live provider."""

    return (
        "id,threadId,internalDate,labelIds,sizeEstimate,snippet,"
        "payload(headers,mimeType,filename,body(size,data,attachmentId),"
        "parts(headers,mimeType,filename,body(size,data,attachmentId),"
        "parts(headers,mimeType,filename,body(size,data,attachmentId))))"
    )


def parse_gmail_response(
    operation: GoogleReadOperation, payload: Mapping[str, object]
) -> tuple[str, ...]:
    """Serialize only approved Gmail metadata and inline text."""

    if operation == "gmail_messages_get":
        messages: object = (payload,)
    else:
        messages = payload.get("messages", ())
    if not isinstance(messages, tuple | list) or not all(
        isinstance(message, Mapping) for message in messages
    ):
        raise GoogleReadProviderError(
            "unavailable", "Gmail response had invalid messages"
        )
    if operation == "gmail_threads_get" and not messages:
        return (
            json.dumps(
                {key: payload[key] for key in ("id", "historyId") if key in payload},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
    return tuple(
        json.dumps(
            gmail_message_with_text(message),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        for message in messages
    )


def gmail_message_with_text(message: Mapping[str, object]) -> dict[str, object]:
    result = {
        key: message[key]
        for key in (
            "id",
            "threadId",
            "internalDate",
            "labelIds",
            "sizeEstimate",
            "snippet",
        )
        if key in message
    }
    payload = message.get("payload")
    if not isinstance(payload, Mapping):
        return result
    headers = gmail_headers(payload)
    if headers:
        result["headers"] = headers
    text = gmail_payload_text(payload)
    if text:
        result["body"] = text
    return result


def gmail_headers(payload: Mapping[str, object]) -> dict[str, str]:
    raw_headers = payload.get("headers", ())
    if not isinstance(raw_headers, tuple | list):
        return {}
    headers: dict[str, str] = {}
    for raw_header in raw_headers:
        if not isinstance(raw_header, Mapping):
            continue
        name = raw_header.get("name")
        value = raw_header.get("value")
        if (
            isinstance(name, str)
            and isinstance(value, str)
            and name.lower() in _GMAIL_METADATA_HEADERS
        ):
            headers[name] = value
    return headers


def gmail_payload_text(payload: Mapping[str, object]) -> str:
    """Extract only inline approved text; attachment bytes stay at Google."""

    text_parts: list[str] = []
    pending: list[Mapping[str, object]] = [payload]
    while pending:
        part = pending.pop()
        nested = part.get("parts", ())
        if isinstance(nested, tuple | list):
            # pending is LIFO; reverse nested parts to preserve MIME order.
            pending.extend(
                reversed([child for child in nested if isinstance(child, Mapping)])
            )
        mime_type = part.get("mimeType")
        if mime_type not in _GMAIL_TEXT_MIME_TYPES or is_attachment_part(part):
            continue
        body = part.get("body")
        if not isinstance(body, Mapping) or not isinstance(body.get("data"), str):
            continue
        decoded = decode_gmail_part(body["data"])
        if decoded is None:
            continue
        if mime_type == "text/html":
            decoded = html_to_text(decoded)
        if decoded:
            text_parts.append(decoded)
    return "\n\n".join(text_parts)


def is_attachment_part(part: Mapping[str, object]) -> bool:
    if isinstance(part.get("filename"), str) and part["filename"]:
        return True
    body = part.get("body")
    if isinstance(body, Mapping) and body.get("attachmentId") is not None:
        return True
    for header in (
        part.get("headers", ()) if isinstance(part.get("headers"), tuple | list) else ()
    ):
        if not isinstance(header, Mapping):
            continue
        name = header.get("name")
        value = header.get("value")
        if (
            isinstance(name, str)
            and isinstance(value, str)
            and name.lower() == "content-disposition"
            and value.lower().lstrip().startswith("attachment")
        ):
            return True
    return False


def decode_gmail_part(data: str) -> str | None:
    try:
        padding = "=" * (-len(data) % 4)
        raw = base64.b64decode(data + padding, altchars=b"-_", validate=True)
        return raw.decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return None


class _HtmlTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._ignored_depth += 1
        if tag in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def html_to_text(value: str) -> str:
    parser = _HtmlTextExtractor()
    parser.feed(value)
    parser.close()
    return " ".join("".join(parser.parts).split())


# Private names retain the old parser vocabulary for direct compatibility
# imports from the former monolithic module.
def _gmail_full_fields() -> str:
    return _gmail_message_fields()


def _gmail_message_fields() -> str:
    return gmail_message_fields()


_gmail_response_items = parse_gmail_response
_gmail_message_with_text = gmail_message_with_text
_gmail_headers = gmail_headers
_gmail_payload_text = gmail_payload_text
_is_attachment_part = is_attachment_part
_decode_gmail_part = decode_gmail_part
_html_to_text = html_to_text

"""Small, fixed-surface adapter for the official Google REST APIs.

The adapter deliberately keeps the Google boundary boring: a single OAuth
token provider, one HTTP client, a closed tuple of prepared tools, and one
in-memory connection generation.  It does not expose a generic Google request
facility or pass provider payloads through without a result bound.
"""

from __future__ import annotations

import base64
import copy
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import getaddresses
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import quote
from uuid import uuid4

import httpx

from .mcp import (
    GoogleOAuthTokenProvider,
    _canonical,
    _is_strict_function_schema,
    _validate_arguments,
)
from .runtime import ApprovalRequired, PendingAction

GMAIL_ROOT = "https://gmail.googleapis.com/gmail/v1/users/me"
DRIVE_ROOT = "https://www.googleapis.com/drive/v3"
CALENDAR_ROOT = "https://www.googleapis.com/calendar/v3"

_GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."
_TEXT_MIME_TYPES = frozenset(
    {
        "text/plain",
        "text/csv",
        "text/markdown",
        "text/xml",
        "application/json",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
        "text/yaml",
    }
)
_GMAIL_HEADERS = frozenset(
    {
        "from",
        "to",
        "cc",
        "subject",
        "date",
        "message-id",
        "references",
        "in-reply-to",
    }
)


class _TokenProvider(Protocol):
    async def access_token(self) -> str: ...

    async def refresh(self) -> None: ...


class _Trace(Protocol):
    def record(self, event: str, payload: dict[str, object]) -> None: ...


class _NoTrace:
    def record(self, event: str, payload: dict[str, object]) -> None:
        return None


@dataclass(frozen=True, slots=True)
class _Connection:
    generation: str
    email: str


@dataclass(slots=True)
class _WriteContinuation:
    connection_id: str
    operation: str
    arguments: dict[str, object]
    resolved: bool = False


class _WriteFailure(RuntimeError):
    """A failure after a write request was attempted."""


_GMAIL_MESSAGE_FIELDS = (
    "id,threadId,internalDate,labelIds,sizeEstimate,snippet,"
    "payload(headers,mimeType,filename,body(size,data,attachmentId),"
    "parts(headers,mimeType,filename,body(size,data,attachmentId),"
    "parts(headers,mimeType,filename,body(size,data,attachmentId))))"
)


def _schema(properties: dict[str, object], required: list[str]) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _string(max_length: int, *, enum: list[str] | None = None) -> dict[str, object]:
    result: dict[str, object] = {"type": "string", "maxLength": max_length}
    if enum is not None:
        result["enum"] = enum
    return result


def _recipient_array() -> dict[str, object]:
    return {
        "type": "array",
        "items": _string(998),
        "minItems": 0,
        "maxItems": 25,
    }


def _definitions() -> tuple[dict[str, object], ...]:
    # Keep the ten existing MCP names and input schemas stable.  Their backing
    # implementation is now direct REST, but model-facing contracts remain the
    # same.
    schemas: list[tuple[str, str, dict[str, object]]] = [
        (
            "google_gmail_search",
            "Search at most 20 Gmail threads, excluding trash.",
            _schema(
                {
                    "query": _string(500),
                    "pageSize": {"type": "integer", "minimum": 1, "maximum": 20},
                    "includeTrash": {"type": "boolean", "enum": [False]},
                    "view": _string(32, enum=["THREAD_VIEW_MINIMAL"]),
                },
                ["query", "pageSize", "includeTrash", "view"],
            ),
        ),
        (
            "google_gmail_read_thread",
            "Read one Gmail thread as plain text.",
            _schema(
                {
                    "threadId": _string(256),
                    "messageFormat": _string(16, enum=["PLAIN_TEXT"]),
                },
                ["threadId", "messageFormat"],
            ),
        ),
        (
            "google_gmail_read_message",
            "Read one Gmail message as plain text.",
            _schema(
                {
                    "messageId": _string(256),
                    "messageFormat": _string(16, enum=["PLAIN_TEXT"]),
                },
                ["messageId", "messageFormat"],
            ),
        ),
        (
            "google_drive_search",
            "Search at most 20 Drive files without content snippets.",
            _schema(
                {
                    "query": _string(500),
                    "pageSize": {"type": "integer", "minimum": 1, "maximum": 20},
                    "excludeContentSnippets": {"type": "boolean", "enum": [True]},
                },
                ["query", "pageSize", "excludeContentSnippets"],
            ),
        ),
        (
            "google_drive_metadata",
            "Read metadata for one Drive file.",
            _schema(
                {
                    "fileId": _string(256),
                    "excludeContentSnippets": {"type": "boolean", "enum": [True]},
                },
                ["fileId", "excludeContentSnippets"],
            ),
        ),
        (
            "google_drive_read_text",
            "Read bounded text content from one Drive file without comments.",
            _schema(
                {
                    "fileId": _string(256),
                    "includeComments": {"type": "boolean", "enum": [False]},
                },
                ["fileId", "includeComments"],
            ),
        ),
        (
            "google_drive_export_text",
            "Export one Drive file in an approved text format.",
            _schema(
                {
                    "fileId": _string(256),
                    "exportMimeType": _string(32, enum=["text/plain", "text/csv"]),
                },
                ["fileId", "exportMimeType"],
            ),
        ),
        (
            "google_calendar_search",
            "Search at most 20 primary-calendar events.",
            _schema(
                {
                    "query": _string(500),
                    "pageSize": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                ["query", "pageSize"],
            ),
        ),
        (
            "google_calendar_list",
            "List at most 20 primary-calendar events in an explicit time window.",
            _schema(
                {
                    "calendarId": _string(7, enum=["primary"]),
                    "startTime": _string(64),
                    "endTime": _string(64),
                    "pageSize": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                ["calendarId", "startTime", "endTime", "pageSize"],
            ),
        ),
        (
            "google_calendar_read",
            "Read one event from the primary calendar.",
            _schema(
                {
                    "eventId": _string(256),
                    "calendarId": _string(7, enum=["primary"]),
                },
                ["eventId", "calendarId"],
            ),
        ),
        (
            "google_calendar_create",
            "Create one exact primary-calendar event after approval.",
            _schema(
                {
                    "calendarId": _string(7, enum=["primary"]),
                    "summary": _string(200),
                    "startTime": _string(64),
                    "endTime": _string(64),
                    "timeZone": _string(64),
                    "description": _string(2000),
                    "location": _string(500),
                },
                ["calendarId", "summary", "startTime", "endTime", "timeZone"],
            ),
        ),
        (
            "google_calendar_update",
            "Apply one exact primary-calendar event update after approval.",
            _schema(
                {
                    "calendarId": _string(7, enum=["primary"]),
                    "eventId": _string(256),
                    "summary": _string(200),
                    "startTime": _string(64),
                    "endTime": _string(64),
                    "timeZone": _string(64),
                    "description": _string(2000),
                    "location": _string(500),
                },
                ["calendarId", "eventId"],
            ),
        ),
        (
            "google_gmail_send",
            "Send one exact plain-text or HTML Gmail message after approval.",
            _schema(
                {
                    "to": _recipient_array(),
                    "cc": _recipient_array(),
                    "bcc": _recipient_array(),
                    "subject": _string(998),
                    "body": _string(65536),
                    "mimeType": _string(16, enum=["text/plain", "text/html"]),
                },
                ["to", "subject", "body"],
            ),
        ),
        (
            "google_gmail_reply",
            "Reply to one Gmail message with an exact frozen thread and headers.",
            _schema(
                {
                    "messageId": _string(256),
                    "body": _string(65536),
                    "mimeType": _string(16, enum=["text/plain", "text/html"]),
                },
                ["messageId", "body"],
            ),
        ),
    ]
    return tuple(
        {
            "type": "function",
            "name": name,
            "description": description,
            "strict": _is_strict_function_schema(schema),
            "parameters": schema,
        }
        for name, description, schema in schemas
    )


_SCHEMAS = {
    str(definition["name"]): definition["parameters"] for definition in _definitions()
}
_TOOL_NAMES = frozenset(_SCHEMAS)


class _HtmlTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
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


def _html_to_text(value: str) -> str:
    parser = _HtmlTextExtractor()
    parser.feed(value)
    parser.close()
    return " ".join("".join(parser.parts).split())


def _decode_gmail_data(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        return raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _is_attachment(part: Mapping[str, object]) -> bool:
    filename = part.get("filename")
    if isinstance(filename, str) and filename:
        return True
    body = part.get("body")
    if isinstance(body, Mapping) and body.get("attachmentId"):
        return True
    headers = part.get("headers")
    if isinstance(headers, list | tuple):
        for header in headers:
            if not isinstance(header, Mapping):
                continue
            if str(header.get("name", "")).lower() == "content-disposition" and str(
                header.get("value", "")
            ).lower().lstrip().startswith("attachment"):
                return True
    return False


def _gmail_text(payload: Mapping[str, object]) -> str:
    text_parts: list[str] = []
    pending: list[Mapping[str, object]] = [payload]
    while pending:
        part = pending.pop()
        nested = part.get("parts")
        if isinstance(nested, list | tuple):
            pending.extend(
                reversed([child for child in nested if isinstance(child, Mapping)])
            )
        if _is_attachment(part) or part.get("mimeType") not in {
            "text/plain",
            "text/html",
        }:
            continue
        body = part.get("body")
        if not isinstance(body, Mapping):
            continue
        decoded = _decode_gmail_data(body.get("data"))
        if decoded is None:
            continue
        if part.get("mimeType") == "text/html":
            decoded = _html_to_text(decoded)
        if decoded:
            text_parts.append(decoded)
    return "\n\n".join(text_parts)


def _gmail_headers(payload: Mapping[str, object]) -> dict[str, str]:
    raw_headers = payload.get("headers")
    if not isinstance(raw_headers, list | tuple):
        return {}
    result: dict[str, str] = {}
    for raw in raw_headers:
        if not isinstance(raw, Mapping):
            continue
        name, value = raw.get("name"), raw.get("value")
        if (
            isinstance(name, str)
            and isinstance(value, str)
            and name.lower() in _GMAIL_HEADERS
        ):
            # Canonicalize the handful of fields we expose while preserving
            # RFC822 spelling for the threading headers.
            canonical_name = {
                "message-id": "Message-ID",
                "in-reply-to": "In-Reply-To",
                "references": "References",
            }.get(name.lower(), name.title())
            result[canonical_name] = value
    return result


def _gmail_message(message: Mapping[str, object]) -> dict[str, object]:
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
    if isinstance(payload, Mapping):
        headers = _gmail_headers(payload)
        if headers:
            result["headers"] = headers
        body = _gmail_text(payload)
        if body:
            result["body"] = body
    return result


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def _header_addresses(value: str) -> list[str]:
    addresses = [address for _, address in getaddresses([value]) if address]
    return addresses or [value]


def _write_header(value: object, name: str) -> str:
    if not isinstance(value, str) or "\r" in value or "\n" in value:
        raise ValueError(f"{name} must be a safe text header")
    return value


def _canonical_email(value: object, name: str) -> str:
    result = _write_header(value, name)
    if not result.strip() or "@" not in result:
        raise ValueError(f"{name} must contain an email address")
    return result


def _recipient_values(value: object, name: str, *, required: bool = False) -> list[str]:
    if value is None and not required:
        return []
    if not isinstance(value, list | tuple) or isinstance(value, str):
        raise TypeError(f"{name} must be an array of email addresses")
    if required and not value:
        raise ValueError(f"{name} must contain at least one address")
    result = [_canonical_email(item, name) for item in value]
    if len({item.casefold() for item in result}) != len(result):
        raise ValueError(f"{name} contains duplicate addresses")
    return result


def _text_body(value: object) -> str:
    if not isinstance(value, str) or len(value) > 65_536:
        raise ValueError("body must be text up to 65536 characters")
    return value


def _mime_type(value: object) -> str:
    if value is None:
        return "text/plain"
    if value not in {"text/plain", "text/html"}:
        raise ValueError("mimeType must be text/plain or text/html")
    return str(value)


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError(  # noqa: TRY004 - all malformed windows share one public error
            "Google Calendar time window must be at most 31 days"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Google Calendar time window must be at most 31 days") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Google Calendar time window must be at most 31 days")
    return parsed


def _validate_calendar_window(arguments: Mapping[str, object]) -> None:
    start = _parse_time(arguments.get("startTime"))
    end = _parse_time(arguments.get("endTime"))
    if not start < end or end - start > timedelta(days=31):
        raise ValueError("Google Calendar time window must be at most 31 days")


def _validate_calendar_event_times(arguments: Mapping[str, object]) -> None:
    try:
        start = datetime.fromisoformat(str(arguments["startTime"]))
        end = datetime.fromisoformat(str(arguments["endTime"]))
    except (KeyError, ValueError) as exc:
        raise ValueError(
            "Google Calendar event times must be timezone-aware ISO timestamps"
        ) from exc
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError(
            "Google Calendar event times must be timezone-aware ISO timestamps"
        )
    if start >= end:
        raise ValueError("Google Calendar event must start before end")


def _response_json(response: httpx.Response) -> object:
    try:
        return response.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Google returned invalid JSON") from exc


def _content_type(response: httpx.Response) -> str:
    return response.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _message_raw(message: Mapping[str, object], *, body: str, mime_type: str) -> str:
    email = EmailMessage(policy=SMTP)
    recipients = _recipient_values(message.get("to"), "to", required=True)
    cc = _recipient_values(message.get("cc"), "cc")
    bcc = _recipient_values(message.get("bcc"), "bcc")
    email["To"] = ", ".join(recipients)
    if cc:
        email["Cc"] = ", ".join(cc)
    if bcc:
        email["Bcc"] = ", ".join(bcc)
    email["Subject"] = _write_header(message.get("subject"), "subject")
    if "inReplyTo" in message:
        email["In-Reply-To"] = _write_header(message["inReplyTo"], "inReplyTo")
    if "references" in message:
        email["References"] = _write_header(message["references"], "references")
    email.set_content(
        body, subtype="html" if mime_type == "text/html" else "plain", charset="utf-8"
    )
    return base64.urlsafe_b64encode(email.as_bytes()).decode("ascii").rstrip("=")


class GoogleApiTools:
    """Fixed Google Gmail, Drive, and Calendar prepared tools."""

    definitions = _definitions()

    def __init__(
        self,
        tokens: _TokenProvider,
        *,
        expected_email: str,
        max_output_chars: int,
        client: httpx.AsyncClient | None = None,
        trace: _Trace | None = None,
    ) -> None:
        if not isinstance(expected_email, str) or not expected_email.strip():
            raise ValueError("expected_email must be non-empty")
        if (
            isinstance(max_output_chars, bool)
            or not isinstance(max_output_chars, int)
            or max_output_chars <= 0
        ):
            raise ValueError("max_output_chars must be positive")
        if not hasattr(tokens, "access_token") or not hasattr(tokens, "refresh"):
            raise TypeError("tokens must provide access_token and refresh")
        self._tokens = tokens
        self._expected_email = expected_email
        self._max_output_chars = max_output_chars
        self._client = client or httpx.AsyncClient(timeout=30)
        self._trace = trace or _NoTrace()
        self._connection: _Connection | None = None

    def status(self) -> str:
        return (
            "Google: connected"
            if self._connection is not None
            else "Google: disconnected"
        )

    async def connect(self) -> str:
        # A reconnect starts a fresh identity check.  Clearing first makes a
        # failed reauthorization unable to leave old pending writes usable.
        self._connection = None
        try:
            await self._tokens.refresh()
        except Exception as exc:
            raise RuntimeError("Google connection failed") from exc
        try:
            response = await self._request(
                "GET", f"{GMAIL_ROOT}/profile", retry_401=True
            )
        except _KnownFailure as exc:
            self._trace_event("google_connect", {"status": "unavailable"})
            raise RuntimeError("Google connection failed") from exc
        self._trace_response("google_connect", response)
        if response.status_code in {401, 403}:
            raise RuntimeError("Google connection failed")
        if response.status_code != 200:
            raise RuntimeError("Google connection failed")
        try:
            payload = _response_json(response)
            email = payload["emailAddress"] if isinstance(payload, Mapping) else None
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Google connection failed") from exc
        if (
            not isinstance(email, str)
            or email.casefold() != self._expected_email.casefold()
        ):
            raise RuntimeError(
                "Google account does not match the configured Google account"
            )
        self._connection = _Connection(uuid4().hex, email)
        return "Connected Google account."

    def disconnect(self) -> str:
        was_connected = self._connection is not None
        self._connection = None
        return (
            "Disconnected Google account."
            if was_connected
            else "Google account is already disconnected."
        )

    async def execute(
        self, name: str, arguments: dict[str, object]
    ) -> str | ApprovalRequired:
        if name not in _TOOL_NAMES:
            raise ValueError(f"unknown prepared tool: {name}")
        schema = _SCHEMAS[name]
        if not isinstance(schema, dict):
            raise TypeError("prepared tool schema is invalid")
        _validate_arguments(arguments, schema)
        if name == "google_calendar_list":
            _validate_calendar_window(arguments)
        if name == "google_calendar_search":
            # Search has no model-facing window fields; impose a fixed 31-day
            # window so a search cannot become an unbounded calendar export.
            now = datetime.now(UTC)
            arguments = {
                **arguments,
                "_timeMin": now.isoformat().replace("+00:00", "Z"),
                "_timeMax": (now + timedelta(days=31))
                .isoformat()
                .replace("+00:00", "Z"),
            }
        if name == "google_calendar_update" and not any(
            key in arguments
            for key in (
                "summary",
                "startTime",
                "endTime",
                "timeZone",
                "description",
                "location",
            )
        ):
            raise ValueError(
                "Google Calendar update requires at least one changed field"
            )
        if name == "google_calendar_update":
            time_fields = {"startTime", "endTime", "timeZone"} & arguments.keys()
            if time_fields and time_fields != {"startTime", "endTime", "timeZone"}:
                raise ValueError(
                    "Google Calendar time updates require startTime, endTime, and timeZone"
                )
        if name == "google_calendar_create" or (
            name == "google_calendar_update" and "startTime" in arguments
        ):
            _validate_calendar_event_times(arguments)
        if self._connection is None:
            return _error("not_connected")
        frozen = copy.deepcopy(arguments)
        try:
            if name == "google_gmail_reply":
                frozen = await self._prepare_reply(frozen)
            if name in _WRITE_NAMES:
                return self._approval(name, frozen)
            result = await self._read(name, frozen)
            return self._bounded(result)
        except _KnownFailure as exc:
            return _error(exc.kind)
        except ValueError:
            raise
        except Exception:  # noqa: BLE001 - external API details stay private
            return _error("unavailable")

    async def resume(self, continuation: object, *, approved: bool) -> str:
        if not isinstance(continuation, _WriteContinuation):
            raise TypeError("invalid Google write continuation")
        if continuation.resolved:
            return _error("already_resolved")
        continuation.resolved = True
        if not approved:
            return _canonical({"rejected": True})
        connection = self._connection
        if connection is None or connection.generation != continuation.connection_id:
            return _error("connection_changed")
        try:
            result = await self._write(continuation.operation, continuation.arguments)
            return self._bounded(result)
        except _KnownFailure as exc:
            return _error(exc.kind)
        except _WriteFailure:
            return _error("outcome_ambiguous")
        except Exception:  # noqa: BLE001 - writes are one-attempt and safe
            return _error("outcome_ambiguous")

    def _approval(self, name: str, arguments: dict[str, object]) -> ApprovalRequired:
        connection = self._connection
        assert connection is not None
        display = (
            "Run Google write?\n"
            f"Connection: {connection.email}\n"
            f"Operation: {name}\n"
            f"Arguments: {_canonical(arguments)}"
        )
        return ApprovalRequired(
            PendingAction(
                host="google",
                prefix=name,
                display=display,
                allow_save_permission=False,
            ),
            _WriteContinuation(connection.generation, name, copy.deepcopy(arguments)),
        )

    async def _prepare_reply(self, arguments: dict[str, object]) -> dict[str, object]:
        message_id = str(arguments["messageId"])
        raw = await self._request_json(
            "GET",
            f"{GMAIL_ROOT}/messages/{quote(message_id, safe='')}",
            params={"format": "full", "fields": _GMAIL_MESSAGE_FIELDS},
            retry_401=True,
        )
        if not isinstance(raw, Mapping):
            raise _KnownFailure("invalid_response")
        payload = raw.get("payload")
        if not isinstance(payload, Mapping):
            raise _KnownFailure("invalid_response")
        headers = _gmail_headers(payload)
        source_thread = raw.get("threadId")
        source_id = raw.get("id")
        in_reply_to = _header(headers, "message-id")
        sender = _header(headers, "from")
        subject = _header(headers, "subject")
        if not all(
            isinstance(value, str) and value
            for value in (source_thread, source_id, in_reply_to, sender, subject)
        ):
            raise _KnownFailure("invalid_response")
        references = _header(headers, "references")
        refs = f"{references} {in_reply_to}".strip() if references else in_reply_to
        reply_subject = (
            subject if subject.casefold().startswith("re:") else f"Re: {subject}"
        )
        frozen: dict[str, object] = {
            "messageId": str(source_id),
            "body": _text_body(arguments.get("body")),
            "mimeType": _mime_type(arguments.get("mimeType")),
            "to": _header_addresses(sender),
            "cc": _header_addresses(_header(headers, "cc") or ""),
            "bcc": [],
            "subject": reply_subject,
            "threadId": str(source_thread),
            "inReplyTo": in_reply_to,
            "references": refs,
        }
        # Empty Cc from a missing header must remain an empty list; parsing an
        # empty address string would otherwise create a misleading recipient.
        if not _header(headers, "cc"):
            frozen["cc"] = []
        _recipient_values(frozen["to"], "to", required=True)
        return frozen

    async def _read(self, name: str, arguments: Mapping[str, object]) -> object:
        if name == "google_gmail_search":
            payload = await self._request_json(
                "GET",
                f"{GMAIL_ROOT}/threads",
                params={
                    "q": arguments["query"],
                    "maxResults": arguments["pageSize"],
                    "includeSpamTrash": "false",
                    "fields": "threads(id,snippet),nextPageToken",
                },
                retry_401=True,
                empty_collection_key="threads",
            )
            return _collection(payload, "threads")
        if name == "google_gmail_read_message":
            payload = await self._request_json(
                "GET",
                f"{GMAIL_ROOT}/messages/{quote(str(arguments['messageId']), safe='')}",
                params={"format": "full", "fields": _GMAIL_MESSAGE_FIELDS},
                retry_401=True,
            )
            if not isinstance(payload, Mapping):
                raise _KnownFailure("invalid_response")
            return _gmail_message(payload)
        if name == "google_gmail_read_thread":
            payload = await self._request_json(
                "GET",
                f"{GMAIL_ROOT}/threads/{quote(str(arguments['threadId']), safe='')}",
                params={
                    "format": "full",
                    "fields": f"id,historyId,messages({_GMAIL_MESSAGE_FIELDS})",
                },
                retry_401=True,
            )
            if not isinstance(payload, Mapping):
                raise _KnownFailure("invalid_response")
            messages = payload.get("messages", [])
            if not isinstance(messages, list | tuple) or not all(
                isinstance(item, Mapping) for item in messages
            ):
                raise _KnownFailure("invalid_response")
            return {
                key: payload[key] for key in ("id", "historyId") if key in payload
            } | {"messages": [_gmail_message(item) for item in messages]}
        if name == "google_drive_search":
            payload = await self._request_json(
                "GET",
                f"{DRIVE_ROOT}/files",
                params={
                    "q": arguments["query"],
                    "pageSize": arguments["pageSize"],
                    "fields": "files(id,name,mimeType,description,modifiedTime,size,webViewLink),nextPageToken",
                },
                retry_401=True,
                empty_collection_key="files",
            )
            return _collection(payload, "files")
        if name == "google_drive_metadata":
            return await self._request_json(
                "GET",
                f"{DRIVE_ROOT}/files/{quote(str(arguments['fileId']), safe='')}",
                params={
                    "fields": "id,name,mimeType,description,modifiedTime,size,webViewLink"
                },
                retry_401=True,
            )
        if name == "google_drive_read_text":
            return await self._drive_read_text(str(arguments["fileId"]))
        if name == "google_drive_export_text":
            file_id = str(arguments["fileId"])
            mime = str(arguments["exportMimeType"])
            response = await self._request(
                "GET",
                f"{DRIVE_ROOT}/files/{quote(file_id, safe='')}/export",
                params={"mimeType": mime},
                retry_401=True,
            )
            text = self._text_response(response, mime)
            return {"fileId": file_id, "exportMimeType": mime, "content": text}
        if name == "google_calendar_search":
            payload = await self._request_json(
                "GET",
                f"{CALENDAR_ROOT}/calendars/primary/events",
                params={
                    "q": arguments["query"],
                    "maxResults": arguments["pageSize"],
                    "timeMin": arguments["_timeMin"],
                    "timeMax": arguments["_timeMax"],
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "fields": "items(id,status,summary,description,location,start,end,attendees,organizer,recurrence,updated),nextPageToken",
                },
                retry_401=True,
                empty_collection_key="items",
            )
            return _collection(payload, "items")
        if name == "google_calendar_list":
            payload = await self._request_json(
                "GET",
                f"{CALENDAR_ROOT}/calendars/{quote(str(arguments['calendarId']), safe='')}/events",
                params={
                    "maxResults": arguments["pageSize"],
                    "timeMin": arguments["startTime"],
                    "timeMax": arguments["endTime"],
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "fields": "items(id,status,summary,description,location,start,end,attendees,organizer,recurrence,updated),nextPageToken",
                },
                retry_401=True,
                empty_collection_key="items",
            )
            return _collection(payload, "items")
        if name == "google_calendar_read":
            return await self._request_json(
                "GET",
                f"{CALENDAR_ROOT}/calendars/{quote(str(arguments['calendarId']), safe='')}/events/{quote(str(arguments['eventId']), safe='')}",
                params={
                    "fields": "id,status,summary,description,location,start,end,attendees,organizer,recurrence,updated"
                },
                retry_401=True,
            )
        raise ValueError(f"unsupported Google read tool: {name}")

    async def _drive_read_text(self, file_id: str) -> dict[str, object]:
        metadata = await self._request_json(
            "GET",
            f"{DRIVE_ROOT}/files/{quote(file_id, safe='')}",
            params={
                "fields": "id,name,mimeType,description,modifiedTime,size,webViewLink"
            },
            retry_401=True,
        )
        if not isinstance(metadata, Mapping) or not isinstance(
            metadata.get("mimeType"), str
        ):
            raise _KnownFailure("invalid_response")
        mime = str(metadata["mimeType"])
        if mime.startswith(_GOOGLE_NATIVE_PREFIX):
            response = await self._request(
                "GET",
                f"{DRIVE_ROOT}/files/{quote(file_id, safe='')}/export",
                params={"mimeType": "text/plain"},
                retry_401=True,
            )
        else:
            response = await self._request(
                "GET",
                f"{DRIVE_ROOT}/files/{quote(file_id, safe='')}",
                params={"alt": "media"},
                retry_401=True,
            )
        text = self._text_response(response, mime)
        return {"fileId": file_id, "content": text}

    async def _write(self, name: str, arguments: Mapping[str, object]) -> object:
        if name == "google_gmail_send":
            recipients = _recipient_values(arguments.get("to"), "to", required=True)
            cc = _recipient_values(arguments.get("cc"), "cc")
            bcc = _recipient_values(arguments.get("bcc"), "bcc")
            body = _text_body(arguments.get("body"))
            mime = _mime_type(arguments.get("mimeType"))
            message: dict[str, object] = {
                "to": recipients,
                "cc": cc,
                "bcc": bcc,
                "subject": _write_header(arguments.get("subject"), "subject"),
            }
            raw = _message_raw(message, body=body, mime_type=mime)
            payload: dict[str, object] = {"raw": raw}
            response = await self._request(
                "POST",
                f"{GMAIL_ROOT}/messages/send",
                json_body=payload,
                retry_401=False,
                write=True,
            )
            result = self._write_json(response, "gmail")
            return result
        if name == "google_gmail_reply":
            body = _text_body(arguments.get("body"))
            mime = _mime_type(arguments.get("mimeType"))
            message = {
                "to": _recipient_values(arguments.get("to"), "to", required=True),
                "cc": _recipient_values(arguments.get("cc"), "cc"),
                "bcc": _recipient_values(arguments.get("bcc"), "bcc"),
                "subject": _write_header(arguments.get("subject"), "subject"),
                "inReplyTo": _write_header(arguments.get("inReplyTo"), "inReplyTo"),
                "references": _write_header(arguments.get("references"), "references"),
            }
            payload = {
                "raw": _message_raw(message, body=body, mime_type=mime),
                "threadId": _write_header(arguments.get("threadId"), "threadId"),
            }
            response = await self._request(
                "POST",
                f"{GMAIL_ROOT}/messages/send",
                json_body=payload,
                retry_401=False,
                write=True,
            )
            return self._write_json(response, "gmail")
        if name in {"google_calendar_create", "google_calendar_update"}:
            calendar_id = quote(str(arguments["calendarId"]), safe="")
            if name == "google_calendar_create":
                body: dict[str, object] = {
                    "summary": _write_header(arguments["summary"], "summary"),
                    "start": {
                        "dateTime": _write_header(arguments["startTime"], "startTime"),
                        "timeZone": _write_header(arguments["timeZone"], "timeZone"),
                    },
                    "end": {
                        "dateTime": _write_header(arguments["endTime"], "endTime"),
                        "timeZone": _write_header(arguments["timeZone"], "timeZone"),
                    },
                }
                if "description" in arguments:
                    body["description"] = _write_header(
                        arguments["description"], "description"
                    )
                if "location" in arguments:
                    body["location"] = _write_header(arguments["location"], "location")
                url = f"{CALENDAR_ROOT}/calendars/{calendar_id}/events"
            else:
                body = {}
                for key in ("summary", "description", "location"):
                    if key in arguments:
                        body[key] = _write_header(arguments[key], key)
                if "startTime" in arguments or "timeZone" in arguments:
                    body["start"] = {
                        "dateTime": arguments.get("startTime"),
                        **(
                            {"timeZone": arguments["timeZone"]}
                            if "timeZone" in arguments
                            else {}
                        ),
                    }
                if "endTime" in arguments or "timeZone" in arguments:
                    body["end"] = {
                        "dateTime": arguments.get("endTime"),
                        **(
                            {"timeZone": arguments["timeZone"]}
                            if "timeZone" in arguments
                            else {}
                        ),
                    }
                url = f"{CALENDAR_ROOT}/calendars/{calendar_id}/events/{quote(str(arguments['eventId']), safe='')}"
            response = await self._request(
                "POST" if name == "google_calendar_create" else "PATCH",
                url,
                json_body=body,
                retry_401=False,
                write=True,
            )
            return self._write_json(response, "calendar")
        raise ValueError(f"unsupported Google write tool: {name}")

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, object] | None = None,
        retry_401: bool,
        empty_collection_key: str | None = None,
    ) -> object:
        response = await self._request(method, url, params=params, retry_401=retry_401)
        if response.status_code == 204 and empty_collection_key is not None:
            return {empty_collection_key: []}
        if response.status_code != 200:
            raise _KnownFailure(_status_kind(response.status_code))
        try:
            return _response_json(response)
        except ValueError as exc:
            raise _KnownFailure("invalid_response") from exc

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, object] | None = None,
        json_body: object | None = None,
        retry_401: bool,
        write: bool = False,
    ) -> httpx.Response:
        try:
            token = await self._tokens.access_token()
        except Exception as exc:
            raise _KnownFailure(_token_failure_kind(exc)) from exc
        try:
            response = await self._client.request(
                method,
                url,
                params=params,
                json=json_body,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.TimeoutException as exc:
            if write:
                raise _WriteFailure from exc
            raise _KnownFailure("unavailable") from exc
        except httpx.HTTPError as exc:
            if write:
                raise _WriteFailure from exc
            raise _KnownFailure("unavailable") from exc
        self._trace_response(f"google_{method.lower()}", response)
        if response.status_code == 401 and retry_401 and not write:
            try:
                await self._tokens.refresh()
                token = await self._tokens.access_token()
                response = await self._client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except Exception as exc:
                raise _KnownFailure(_token_failure_kind(exc)) from exc
            self._trace_response(f"google_{method.lower()}_retry", response)
        return response

    def _write_json(self, response: httpx.Response, service: str) -> object:
        if response.status_code not in {200, 201}:
            raise _WriteFailure
        try:
            payload = _response_json(response)
        except ValueError as exc:
            raise _WriteFailure from exc
        if not isinstance(payload, Mapping):
            raise _WriteFailure
        identifier = payload.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise _WriteFailure
        if service == "gmail":
            thread_id = payload.get("threadId")
            if not isinstance(thread_id, str) or not thread_id:
                raise _WriteFailure
            return {"id": identifier, "threadId": thread_id}
        acknowledgement: dict[str, object] = {"id": identifier}
        if isinstance(payload.get("status"), str):
            acknowledgement["status"] = payload["status"]
        return acknowledgement

    def _text_response(self, response: httpx.Response, fallback_mime: str) -> str:
        if response.status_code != 200:
            raise _KnownFailure(_status_kind(response.status_code))
        content_type = _content_type(response)
        if (
            content_type
            and content_type not in _TEXT_MIME_TYPES
            and not content_type.startswith("text/")
            and fallback_mime not in _TEXT_MIME_TYPES
            and not fallback_mime.startswith("text/")
        ):
            # A Google-native export can use a generic text content type; the
            # explicit binary type remains refused.
            raise _KnownFailure("invalid_response")
        try:
            return response.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _KnownFailure("invalid_response") from exc

    def _bounded(self, value: object) -> str:
        try:
            output = _canonical({"result": value})
        except (TypeError, ValueError, OverflowError) as exc:
            raise _KnownFailure("invalid_response") from exc
        if len(output) > self._max_output_chars:
            return _error("output_too_large")
        return output

    def _trace_event(self, event: str, payload: dict[str, object]) -> None:
        try:
            self._trace.record(event, payload)
        except Exception:  # noqa: BLE001 - tracing must not change API behavior
            return

    def _trace_response(self, operation: str, response: httpx.Response) -> None:
        try:
            request_url = str(response.request.url)
        except (RuntimeError, AttributeError):
            request_url = ""
        self._trace_event(
            "google_exchange",
            {
                "operation": operation,
                "url": request_url,
                "status_code": response.status_code,
            },
        )


class _KnownFailure(RuntimeError):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(kind)


_WRITE_NAMES = frozenset(
    {
        "google_gmail_send",
        "google_gmail_reply",
        "google_calendar_create",
        "google_calendar_update",
    }
)


def _collection(payload: object, key: str) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise _KnownFailure("invalid_response")
    values = payload.get(key, [])
    if not isinstance(values, list | tuple) or not all(
        isinstance(item, Mapping) for item in values
    ):
        raise _KnownFailure("invalid_response")
    return {key: [dict(item) for item in values]}


def _status_kind(status_code: int) -> str:
    if status_code in {401, 403}:
        return "unauthorized"
    if status_code == 429:
        return "rate_limited"
    if status_code >= 500:
        return "unavailable"
    return "operation_failed"


def _token_failure_kind(error: Exception) -> str:
    kind = getattr(error, "kind", None)
    return (
        kind
        if kind in {"unauthorized", "rate_limited", "unavailable"}
        else "unavailable"
    )


def _error(kind: str) -> str:
    return _canonical({"error": {"kind": kind}})


__all__ = ["GoogleApiTools", "GoogleOAuthTokenProvider"]

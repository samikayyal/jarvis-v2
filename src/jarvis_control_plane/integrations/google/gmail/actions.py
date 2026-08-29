"""Pure Gmail action contracts shared by orchestration and the connector.

This module intentionally has no OAuth, HTTP, tracing, or dispatch dependency.
It is the inward-facing capability contract for the two distinct Gmail actions.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from typing import ClassVar, Literal

from ....models import FrozenActionProposal
from .validation import (
    _IDENTIFIER,  # noqa: F401
    _MAILBOX,  # noqa: F401
    _MIME_TYPES,  # noqa: F401
    _body,
    _canonical_string,
    _connection_generation,
    _identifier,
    _message_id,
    _message_ids,
    _mime_type,
    _recipients,
    _subject,
    _threading,
)

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GmailOperation = Literal["gmail_send", "gmail_reply"]


@dataclass(frozen=True, slots=True)
class GmailMessage:
    """Shared immutable recipient/content value object for both Gmail actions."""

    to: tuple[str, ...]
    cc: tuple[str, ...]
    bcc: tuple[str, ...]
    subject: str
    body: str
    mime_type: Literal["text/plain", "text/html"]

    def __post_init__(self) -> None:
        object.__setattr__(self, "to", _recipients(self.to, "to"))
        object.__setattr__(self, "cc", _recipients(self.cc, "cc", allow_empty=True))
        object.__setattr__(self, "bcc", _recipients(self.bcc, "bcc", allow_empty=True))
        object.__setattr__(self, "subject", _subject(self.subject))
        object.__setattr__(self, "body", _body(self.body))
        object.__setattr__(self, "mime_type", _mime_type(self.mime_type))

    @classmethod
    def material_field_names(cls) -> tuple[str, ...]:
        """Return the dataclass fields that define the material message."""

        return tuple(field.name for field in dataclass_fields(cls))

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> GmailMessage:
        """Rebuild the canonical material message from a frozen payload."""

        try:
            values = {field: payload[field] for field in cls.material_field_names()}
        except KeyError as exc:
            raise ValueError(
                "Gmail message payload is missing a material field"
            ) from exc
        return cls(**values)  # type: ignore[arg-type]

    def to_payload(self) -> dict[str, object]:
        """Serialize every delivery-affecting message field exactly once."""

        return {field: getattr(self, field) for field in self.material_field_names()}

    def preview_lines(self) -> tuple[str, ...]:
        """Render the canonical material fields used by human approval."""

        return (
            f"To: {', '.join(self.to)}",
            f"Cc: {', '.join(self.cc) or '(none)'}",
            f"Bcc: {', '.join(self.bcc) or '(none)'}",
            f"Subject: {self.subject}",
            f"MIME: {self.mime_type}",
        )

    def mime_headers(self) -> tuple[tuple[str, str], ...]:
        """Return the canonical RFC822 headers for the material message."""

        headers: list[tuple[str, str]] = [("To", ", ".join(self.to))]
        if self.cc:
            headers.append(("Cc", ", ".join(self.cc)))
        if self.bcc:
            headers.append(("Bcc", ", ".join(self.bcc)))
        headers.append(("Subject", self.subject))
        return tuple(headers)

    @property
    def mime_subtype(self) -> Literal["plain", "html"]:
        """Return the validated RFC822 content subtype."""

        return "html" if self.mime_type == "text/html" else "plain"


@dataclass(frozen=True, slots=True)
class GmailNewSendRequest:
    """A Gmail new-send request with no reply or threading fields."""

    message: GmailMessage
    google_subject: str | None = None
    connection_generation: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.message, GmailMessage):
            raise TypeError("new-send message must be a GmailMessage")
        _validate_binding(self.google_subject, self.connection_generation)

    @property
    def operation(self) -> Literal["gmail_send"]:
        return "gmail_send"

    @property
    def threading(self) -> Literal["new_message"]:
        return "new_message"


@dataclass(frozen=True, slots=True)
class GmailReplyRequest:
    """A typed Gmail reply bound to one frozen source message and thread."""

    THREADING_FIELDS: ClassVar[tuple[str, ...]] = (
        "thread_id",
        "source_message_id",
        "source_thread_id",
        "in_reply_to",
        "references",
    )

    message: GmailMessage
    thread_id: str
    source_message_id: str
    source_thread_id: str
    in_reply_to: str
    references: tuple[str, ...]
    google_subject: str | None = None
    connection_generation: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.message, GmailMessage):
            raise TypeError("reply message must be a GmailMessage")
        object.__setattr__(self, "thread_id", _identifier(self.thread_id, "thread_id"))
        object.__setattr__(
            self,
            "source_message_id",
            _identifier(self.source_message_id, "source_message_id"),
        )
        object.__setattr__(
            self,
            "source_thread_id",
            _identifier(self.source_thread_id, "source_thread_id"),
        )
        object.__setattr__(self, "in_reply_to", _message_id(self.in_reply_to))
        object.__setattr__(self, "references", _message_ids(self.references))
        if self.thread_id != self.source_thread_id:
            raise ValueError("Gmail reply thread must match its frozen source thread")
        if self.references[-1] != self.in_reply_to:
            raise ValueError("Gmail reply references must end with In-Reply-To")
        _validate_binding(self.google_subject, self.connection_generation)

    @property
    def operation(self) -> Literal["gmail_reply"]:
        return "gmail_reply"

    @property
    def threading(self) -> Literal["gmail_threaded_reply"]:
        return "gmail_threaded_reply"

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, object],
        *,
        message: GmailMessage,
        binding: Mapping[str, object],
    ) -> GmailReplyRequest:
        """Rebuild the reply-only extension around the canonical message."""

        try:
            values = {field: payload[field] for field in cls.THREADING_FIELDS}
        except KeyError as exc:
            raise ValueError(
                "Gmail reply payload is missing a threading field"
            ) from exc
        return cls(message=message, **values, **binding)  # type: ignore[arg-type]

    def threading_payload(self) -> dict[str, object]:
        """Serialize only the reply-specific threading extension."""

        return {field: getattr(self, field) for field in self.THREADING_FIELDS}

    def threading_preview_lines(self) -> tuple[str, ...]:
        """Render only the reply-specific fields shown during approval."""

        return (
            f"Source message: {self.source_message_id}",
            f"Source thread: {self.source_thread_id}",
            f"In-Reply-To: {self.in_reply_to}",
            f"References: {' '.join(self.references)}",
        )

    def threading_mime_headers(self) -> tuple[tuple[str, str], ...]:
        """Return only the reply-specific RFC822 threading extension."""

        return (
            ("In-Reply-To", self.in_reply_to),
            ("References", " ".join(self.references)),
        )


type GmailWriteRequest = GmailNewSendRequest | GmailReplyRequest


def create_gmail_new_send_proposal(
    *,
    action_id: str,
    request_id: str,
    to: Sequence[str],
    cc: Sequence[str] = (),
    bcc: Sequence[str] = (),
    subject: str,
    body: str,
    mime_type: str,
    google_subject: str | None = None,
    connection_generation: int | None = None,
) -> FrozenActionProposal:
    """Create a canonical proposal for a new Gmail message only."""

    request = GmailNewSendRequest(
        message=GmailMessage(
            to=tuple(to),
            cc=tuple(cc),
            bcc=tuple(bcc),
            subject=subject,
            body=body,
            mime_type=mime_type,
        ),
        google_subject=(
            _canonical_string(google_subject, "google_subject")
            if google_subject is not None
            else None
        ),
        connection_generation=connection_generation,
    )
    return _proposal(action_id=action_id, request_id=request_id, request=request)


def create_gmail_reply_proposal(
    *,
    action_id: str,
    request_id: str,
    to: Sequence[str],
    subject: str,
    body: str,
    mime_type: str,
    source_message_id: str,
    source_thread_id: str,
    in_reply_to: str,
    references: Sequence[str],
    cc: Sequence[str] = (),
    bcc: Sequence[str] = (),
    google_subject: str | None = None,
    connection_generation: int | None = None,
) -> FrozenActionProposal:
    """Create a canonical proposal for one typed Gmail reply only."""

    request = GmailReplyRequest(
        message=GmailMessage(
            to=tuple(to),
            cc=tuple(cc),
            bcc=tuple(bcc),
            subject=subject,
            body=body,
            mime_type=mime_type,
        ),
        thread_id=source_thread_id,
        source_message_id=source_message_id,
        source_thread_id=source_thread_id,
        in_reply_to=in_reply_to,
        references=tuple(references),
        google_subject=(
            _canonical_string(google_subject, "google_subject")
            if google_subject is not None
            else None
        ),
        connection_generation=connection_generation,
    )
    return _proposal(action_id=action_id, request_id=request_id, request=request)


def gmail_write_request_from_proposal(
    action: FrozenActionProposal, *, require_binding: bool = False
) -> GmailWriteRequest:
    """Parse and revalidate one frozen new-send or typed-reply proposal."""

    try:
        payload = json.loads(action.payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Gmail proposal payload is malformed") from exc
    if not isinstance(payload, dict):
        raise TypeError("Gmail proposal payload must be an object")
    if action.kind == "gmail_send":
        request = _new_send_request_from_payload(payload)
    elif action.kind == "gmail_reply":
        request = _reply_request_from_payload(payload)
    else:
        raise ValueError("proposal is not a Gmail send or reply")
    if require_binding and request.google_subject is None:
        raise ValueError("Gmail proposal is missing its Google connection binding")
    if action.preview != gmail_proposal_preview(request):
        raise ValueError("Gmail proposal preview does not match its frozen payload")
    return request


def gmail_proposal_payload(request: GmailWriteRequest) -> dict[str, object]:
    """Serialize the complete typed request into its frozen proposal payload."""

    payload = request.message.to_payload()
    payload["threading"] = request.threading
    if isinstance(request, GmailReplyRequest):
        payload.update(request.threading_payload())
    if request.google_subject is not None:
        payload.update(
            {
                "google_subject": request.google_subject,
                "connection_generation": request.connection_generation,
            }
        )
    return payload


def gmail_proposal_preview(request: GmailWriteRequest) -> str:
    """Render the exact human approval preview for either Gmail action."""

    lines = [
        "Gmail typed reply"
        if isinstance(request, GmailReplyRequest)
        else "Gmail new send",
        *request.message.preview_lines(),
        f"Threading: {request.threading}",
    ]
    if request.google_subject is not None:
        lines.extend(
            (
                f"Google subject: {request.google_subject}",
                f"Google connection generation: {request.connection_generation}",
            )
        )
    if isinstance(request, GmailReplyRequest):
        lines.extend(request.threading_preview_lines())
    return "\n".join((*lines, "", "Body:", request.message.body))


def _proposal(
    *, action_id: str, request_id: str, request: GmailWriteRequest
) -> FrozenActionProposal:
    return FrozenActionProposal.create(
        action_id=action_id,
        request_id=request_id,
        kind=request.operation,
        preview=gmail_proposal_preview(request),
        payload=gmail_proposal_payload(request),
    )


def _new_send_request_from_payload(
    payload: Mapping[str, object],
) -> GmailNewSendRequest:
    expected = _common_fields(payload)
    if set(payload) != expected:
        raise ValueError(
            "Gmail new-send payload has missing or unknown delivery fields"
        )
    return GmailNewSendRequest(
        message=_message_from_payload(payload),
        **_binding_from_payload(payload),
    )


def _reply_request_from_payload(
    payload: Mapping[str, object],
) -> GmailReplyRequest:
    expected = _common_fields(payload) | {
        *GmailReplyRequest.THREADING_FIELDS,
    }
    if set(payload) != expected:
        raise ValueError("Gmail reply payload has missing or unknown delivery fields")
    return GmailReplyRequest.from_payload(
        payload,
        message=_message_from_payload(payload),
        binding=_binding_from_payload(payload),
    )


def _common_fields(payload: Mapping[str, object]) -> set[str]:
    fields = set(GmailMessage.material_field_names()) | {"threading"}
    supplied_binding = set(payload) & {"google_subject", "connection_generation"}
    if supplied_binding and supplied_binding != {
        "google_subject",
        "connection_generation",
    }:
        raise ValueError("Gmail proposal has an incomplete Google connection binding")
    return fields | supplied_binding


def _message_from_payload(payload: Mapping[str, object]) -> GmailMessage:
    _threading(payload["threading"], reply="thread_id" in payload)
    return GmailMessage.from_payload(payload)


def _binding_from_payload(
    payload: Mapping[str, object],
) -> dict[str, object]:
    if "google_subject" not in payload:
        return {"google_subject": None, "connection_generation": None}
    return {
        "google_subject": _canonical_string(
            payload["google_subject"], "google_subject"
        ),
        "connection_generation": _connection_generation(
            payload["connection_generation"]
        ),
    }


def _validate_binding(subject: str | None, generation: int | None) -> None:
    if (subject is None) != (generation is None):
        raise ValueError("Google action bindings require subject and generation")
    if subject is not None:
        _canonical_string(subject, "google_subject")
        _connection_generation(generation)

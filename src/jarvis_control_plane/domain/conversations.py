"""Conversation-history records, deletion boundaries, and outbound recovery state."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from enum import Enum
from types import MappingProxyType

from .ingress_messaging import (
    _contains_credential_like_text,
    _non_empty_identifier,
    ensure_utc,
)


@dataclass(frozen=True, slots=True)
class ConversationMessage:
    """One immutable text record in a working-session conversation."""

    working_session_id: str
    transport_session_id: str
    message_id: str
    event_id: str
    chat_id: str
    sender_id: str
    text: str
    occurred_at: datetime
    direction: str = "inbound"
    request_id: str | None = None
    credential_like: bool | None = None

    def __post_init__(self) -> None:
        for name in (
            "working_session_id",
            "transport_session_id",
            "message_id",
            "event_id",
            "chat_id",
            "sender_id",
            "direction",
        ):
            _non_empty_identifier(getattr(self, name), name)
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("conversation text must be non-blank")
        if self.direction not in {"inbound", "outbound"}:
            raise ValueError("conversation direction must be inbound or outbound")
        if self.request_id is not None:
            _non_empty_identifier(self.request_id, "request_id")
        if self.credential_like is not None and not isinstance(
            self.credential_like, bool
        ):
            raise TypeError("credential_like must be a boolean when provided")
        # Classification is deliberately conservative. Callers cannot mark a
        # detected secret safe, because automatic history selection must never
        # expose it to a model without an exact operator selection.
        object.__setattr__(
            self,
            "credential_like",
            bool(self.credential_like) or _contains_credential_like_text(self.text),
        )
        object.__setattr__(self, "occurred_at", ensure_utc(self.occurred_at))

    @property
    def session_id(self) -> str:
        """Compatibility alias for the OpenWA transport session identifier."""

        return self.transport_session_id

    @property
    def conversation_id(self) -> str:
        """Alias for the durable working-session conversation boundary."""

        return self.working_session_id

    @property
    def history_id(self) -> str:
        """Stable opaque selector for one record across every transport session."""

        return (
            f"history-{len(self.transport_session_id)}:"
            f"{self.transport_session_id}:{self.message_id}"
        )

    @staticmethod
    def history_id_parts(value: object) -> tuple[str, str]:
        """Parse the length-delimited, collision-free history-record selector."""

        if not isinstance(value, str) or not value.startswith("history-"):
            raise ValueError("history selector is invalid")
        length_text, separator, remainder = value[8:].partition(":")
        if not separator or not length_text.isdigit():
            raise ValueError("history selector is invalid")
        session_length = int(length_text)
        transport_session_id = remainder[:session_length]
        if (
            session_length < 1
            or len(transport_session_id) != session_length
            or remainder[session_length : session_length + 1] != ":"
        ):
            raise ValueError("history selector is invalid")
        message_id = remainder[session_length + 1 :]
        _non_empty_identifier(transport_session_id, "transport_session_id")
        _non_empty_identifier(message_id, "message_id")
        return transport_session_id, message_id


class OutboundAttemptStatus(str, Enum):
    """Durable outcome boundary for one exact outbound message."""

    UNATTEMPTED = "unattempted"
    ATTEMPTED = "attempted"
    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"
    NOT_STARTED = "not_started"


OUTBOUND_TERMINAL_TRANSITIONS: Mapping[
    OutboundAttemptStatus, frozenset[OutboundAttemptStatus]
] = MappingProxyType(
    {
        OutboundAttemptStatus.UNATTEMPTED: frozenset(
            {OutboundAttemptStatus.NOT_STARTED}
        ),
        OutboundAttemptStatus.ATTEMPTED: frozenset(
            {OutboundAttemptStatus.UNKNOWN, OutboundAttemptStatus.CONFIRMED}
        ),
        OutboundAttemptStatus.CONFIRMED: frozenset({OutboundAttemptStatus.CONFIRMED}),
        OutboundAttemptStatus.UNKNOWN: frozenset({OutboundAttemptStatus.UNKNOWN}),
        OutboundAttemptStatus.NOT_STARTED: frozenset(
            {OutboundAttemptStatus.NOT_STARTED}
        ),
    }
)


def is_outbound_terminal_transition_allowed(
    current: OutboundAttemptStatus | str,
    target: OutboundAttemptStatus | str,
) -> bool:
    """Return whether one terminal outbound transition preserves the state machine."""

    current_status = OutboundAttemptStatus(current)
    target_status = OutboundAttemptStatus(target)
    return target_status in OUTBOUND_TERMINAL_TRANSITIONS[current_status]


@dataclass(frozen=True, slots=True)
class OutboundAttemptRecord:
    """Private outbox state that prevents automatic duplicate delivery."""

    transport_session_id: str
    message_id: str
    request_id: str
    status: OutboundAttemptStatus | str
    reserved_at: datetime
    message: ConversationMessage | None
    attempted_at: datetime | None = None
    terminal_at: datetime | None = None
    outbound_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("transport_session_id", "message_id", "request_id"):
            _non_empty_identifier(getattr(self, name), name)
        status = OutboundAttemptStatus(self.status)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "reserved_at", ensure_utc(self.reserved_at))
        if self.attempted_at is not None:
            object.__setattr__(self, "attempted_at", ensure_utc(self.attempted_at))
        if self.terminal_at is not None:
            object.__setattr__(self, "terminal_at", ensure_utc(self.terminal_at))
        if self.outbound_id is not None:
            _non_empty_identifier(self.outbound_id, "outbound_id")
        if status in {
            OutboundAttemptStatus.UNATTEMPTED,
            OutboundAttemptStatus.ATTEMPTED,
        }:
            if self.message is None:
                raise ValueError("open outbound attempts require the exact message")
            if (
                self.message.transport_session_id,
                self.message.message_id,
                self.message.request_id,
            ) != (self.transport_session_id, self.message_id, self.request_id):
                raise ValueError("outbound attempt identity does not match its message")
            if self.terminal_at is not None:
                raise ValueError("open outbound attempts cannot have terminal_at")
            if self.outbound_id is not None:
                raise ValueError("open outbound attempts cannot have outbound_id")
        elif self.message is not None or self.terminal_at is None:
            raise ValueError(
                "terminal outbound attempts remove message content and require terminal_at"
            )
        if status is OutboundAttemptStatus.ATTEMPTED and self.attempted_at is None:
            raise ValueError("attempted outbound records require attempted_at")


@dataclass(frozen=True, slots=True)
class OutboundAttemptRecoveryProjection:
    """Body-free raw facts used before strict outbound-state validation.

    Restart recovery must be able to describe a damaged attempt/outbox pair
    without constructing :class:`OutboundAttemptRecord`, whose invariants are
    intentionally strict for healthy state.  The projection therefore keeps
    only bounded identity, status, timestamp, and row-presence facts; it never
    carries the private outbound body.
    """

    transport_session_id: str
    message_id: str
    attempt_present: bool
    outbox_present: bool
    attempt_request_id: str | None = None
    outbox_request_id: str | None = None
    status: str | None = None
    reserved_at: str | None = None
    attempted_at: str | None = None
    terminal_at: str | None = None
    outbound_id: str | None = None


@dataclass(frozen=True, slots=True)
class RecoveryDegradedMarker:
    """Durable evidence that restart recovery requires administrative repair."""

    reason: str
    marked_at: datetime

    def __post_init__(self) -> None:
        _non_empty_identifier(self.reason, "reason")
        object.__setattr__(self, "marked_at", ensure_utc(self.marked_at))


def _deletion_scope_datetime(
    value: date | datetime, name: str, *, end: bool
) -> datetime:
    """Normalize a date or aware timestamp for an inclusive deletion range."""

    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, date):
        return datetime.combine(
            value,
            time.max if end else time.min,
            tzinfo=UTC,
        )
    raise TypeError(f"{name} must be a date or timezone-aware datetime")


@dataclass(frozen=True, slots=True)
class ConversationDeletionScope:
    """One exact, deterministic selection boundary for conversation history."""

    scope_type: str
    history_ids: tuple[str, ...] = ()
    conversation_id: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.scope_type not in {"message", "conversation", "date_range"}:
            raise ValueError("conversation deletion scope type is invalid")
        history_ids = tuple(self.history_ids)
        if self.scope_type == "message":
            if not history_ids:
                raise ValueError("message deletion scope requires a history ID")
            if len(set(history_ids)) != len(history_ids):
                raise ValueError(
                    "message deletion scope contains duplicate history IDs"
                )
            for history_id in history_ids:
                ConversationMessage.history_id_parts(history_id)
            if self.conversation_id is not None:
                raise ValueError("message deletion scope cannot include a conversation")
            if self.start_at is not None or self.end_at is not None:
                raise ValueError("message deletion scope cannot include a date range")
        elif self.scope_type == "conversation":
            _non_empty_identifier(self.conversation_id, "conversation_id")
            if history_ids or self.start_at is not None or self.end_at is not None:
                raise ValueError("conversation deletion scope has unexpected selectors")
        else:
            if history_ids or self.conversation_id is not None:
                raise ValueError("date-range deletion scope has unexpected selectors")
            if self.start_at is None or self.end_at is None:
                raise ValueError("date-range deletion scope requires both boundaries")
            start_at = ensure_utc(self.start_at)
            end_at = ensure_utc(self.end_at)
            if end_at < start_at:
                raise ValueError("date-range deletion scope ends before it starts")
            object.__setattr__(self, "start_at", start_at)
            object.__setattr__(self, "end_at", end_at)
        object.__setattr__(self, "history_ids", history_ids)

    @classmethod
    def message(cls, history_id: str | tuple[str, ...]) -> ConversationDeletionScope:
        """Select one or more exact immutable history records."""

        selectors = (history_id,) if isinstance(history_id, str) else tuple(history_id)
        return cls(scope_type="message", history_ids=selectors)

    @classmethod
    def conversation(cls, conversation_id: str) -> ConversationDeletionScope:
        """Select every accessible record in one working-session conversation."""

        return cls(scope_type="conversation", conversation_id=conversation_id)

    @classmethod
    def date_range(
        cls,
        start: date | datetime,
        end: date | datetime,
    ) -> ConversationDeletionScope:
        """Select records whose timestamps fall within an inclusive range."""

        return cls(
            scope_type="date_range",
            start_at=_deletion_scope_datetime(start, "start", end=False),
            end_at=_deletion_scope_datetime(end, "end", end=True),
        )

    @property
    def kind(self) -> str:
        """Compatibility alias used by deterministic renderers."""

        return self.scope_type

    def describe(self) -> str:
        if self.scope_type == "message":
            return "message " + ", ".join(self.history_ids)
        if self.scope_type == "conversation":
            return f"conversation {self.conversation_id}"
        assert self.start_at is not None and self.end_at is not None
        return (
            f"date range {self.start_at.isoformat()} through {self.end_at.isoformat()}"
        )


def _conversation_message_digest(messages: tuple[ConversationMessage, ...]) -> str:
    material = [
        {
            "chat_id": message.chat_id,
            "credential_like": message.credential_like,
            "direction": message.direction,
            "event_id": message.event_id,
            "message_id": message.message_id,
            "occurred_at": message.occurred_at.isoformat(),
            "request_id": message.request_id,
            "sender_id": message.sender_id,
            "text": message.text,
            "transport_session_id": message.transport_session_id,
            "working_session_id": message.working_session_id,
        }
        for message in messages
    ]
    return hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class ConversationDeletionPreview:
    """Exact records selected before an operator can confirm deletion."""

    scope: ConversationDeletionScope
    messages: tuple[ConversationMessage, ...]
    content_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, ConversationDeletionScope):
            raise TypeError("scope must be a ConversationDeletionScope")
        messages = tuple(self.messages)
        if any(not isinstance(message, ConversationMessage) for message in messages):
            raise TypeError("messages must contain ConversationMessage values")
        if self.scope.scope_type == "message":
            allowed_history_ids = set(self.scope.history_ids)
            if any(
                message.history_id not in allowed_history_ids for message in messages
            ):
                raise ValueError("deletion preview contains a record outside its scope")
        elif self.scope.scope_type == "conversation":
            if any(
                message.working_session_id != self.scope.conversation_id
                for message in messages
            ):
                raise ValueError("deletion preview contains a record outside its scope")
        else:
            assert self.scope.start_at is not None and self.scope.end_at is not None
            if any(
                not self.scope.start_at <= message.occurred_at <= self.scope.end_at
                for message in messages
            ):
                raise ValueError("deletion preview contains a record outside its scope")
        expected_order = tuple(
            sorted(
                messages,
                key=lambda message: (
                    message.occurred_at,
                    message.transport_session_id,
                    message.message_id,
                ),
            )
        )
        if messages != expected_order:
            raise ValueError("deletion preview messages must be canonically ordered")
        expected = _conversation_message_digest(messages)
        if self.content_digest != expected:
            raise ValueError("deletion preview digest does not match selected content")
        object.__setattr__(self, "messages", messages)

    @property
    def history_ids(self) -> tuple[str, ...]:
        return tuple(message.history_id for message in self.messages)

    @property
    def digest(self) -> str:
        """Short compatibility alias for the content-bound preview digest."""

        return self.content_digest

    @property
    def count(self) -> int:
        return len(self.messages)


@dataclass(frozen=True, slots=True)
class ConversationTombstone:
    """Content-free reference retained after a history record leaves access."""

    tombstone_id: str
    deletion_id: str
    history_id: str
    transport_session_id: str
    message_id: str
    working_session_id: str
    occurred_at: datetime
    deleted_at: datetime
    scope_type: str

    def __post_init__(self) -> None:
        for name in (
            "tombstone_id",
            "deletion_id",
            "history_id",
            "transport_session_id",
            "message_id",
            "working_session_id",
            "scope_type",
        ):
            _non_empty_identifier(getattr(self, name), name)
        if self.scope_type not in {"message", "conversation", "date_range"}:
            raise ValueError("tombstone scope type is invalid")
        history_id_parts = ConversationMessage.history_id_parts(self.history_id)
        if history_id_parts != (self.transport_session_id, self.message_id):
            raise ValueError("tombstone identity does not match its history ID")
        object.__setattr__(self, "occurred_at", ensure_utc(self.occurred_at))
        object.__setattr__(self, "deleted_at", ensure_utc(self.deleted_at))

    @property
    def conversation_id(self) -> str:
        return self.working_session_id

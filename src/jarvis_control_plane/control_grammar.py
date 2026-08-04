"""Deterministic pure control grammar for the Ticket 05 foundation.

The parser is intentionally small: V1's `/status`, `/cancel`, and `/new`
controls are exact whole-message commands.  Normalization happens before
classification, slash commands take precedence over ordinary text and future
approval handling, and malformed slash commands never become requests.

This module delegates lifecycle changes to :mod:`jarvis_control_plane.sessions`
and does not call a receiver, broker, durable store, connector, or worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .sessions import (
    CancellationToken,
    Clock,
    RequestPhase,
    SessionTransition,
    StatusView,
    TransitionKind,
    WorkingSession,
    accept_request,
    cancel_active_request,
    new_working_session,
    status_view,
)

CONTROL_COMMANDS = ("/status", "/cancel", "/new")


class ControlCommand(str, Enum):
    STATUS = "status"
    CANCEL = "cancel"
    NEW = "new"


class MessageKind(str, Enum):
    EMPTY = "empty"
    ORDINARY = "ordinary"
    CONTROL_COMMAND = "control_command"
    MALFORMED_COMMAND = "malformed_command"
    UNKNOWN_COMMAND = "unknown_command"


class ControlTransitionKind(str, Enum):
    EMPTY = "empty"
    STATUS = "status"
    MALFORMED_COMMAND = "malformed_command"
    UNKNOWN_COMMAND = "unknown_command"
    REQUEST_ACCEPTED = "request_accepted"
    BUSY_REFUSED = "busy_refused"
    PENDING_BLOCKED = "pending_blocked"
    CANCELLED = "cancelled"
    NOTHING_TO_CANCEL = "nothing_to_cancel"
    NEW_SESSION = "new_session"


@dataclass(frozen=True, slots=True)
class ParsedControl:
    """Immutable classification after the exact V1 normalization step."""

    normalized: str
    kind: MessageKind
    command: ControlCommand | None = None
    args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "args", tuple(self.args))

    @property
    def is_command(self) -> bool:
        return self.kind is MessageKind.CONTROL_COMMAND

    @property
    def is_slash_message(self) -> bool:
        return self.normalized.startswith("/")

    @property
    def command_name(self) -> str | None:
        return self.command.value if self.command is not None else None


@dataclass(frozen=True, slots=True)
class ControlTransition:
    """Pure parser/reducer result with no external side effects."""

    state: WorkingSession
    parsed: ParsedControl
    kind: ControlTransitionKind
    reply: str | None = None
    status: StatusView | None = None
    effects: tuple[str, ...] = ()
    cancellation_token: CancellationToken | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "effects", tuple(self.effects))
        if self.reply is not None and not self.reply.strip():
            raise ValueError("reply must be non-blank when present")

    @property
    def changed(self) -> bool:
        return bool(self.effects) and self.kind not in {
            ControlTransitionKind.STATUS,
            ControlTransitionKind.EMPTY,
            ControlTransitionKind.MALFORMED_COMMAND,
            ControlTransitionKind.UNKNOWN_COMMAND,
            ControlTransitionKind.BUSY_REFUSED,
            ControlTransitionKind.PENDING_BLOCKED,
            ControlTransitionKind.NOTHING_TO_CANCEL,
        }

    @property
    def replies(self) -> tuple[str, ...]:
        """Prototype-compatible one-or-zero reply tuple."""

        return (self.reply,) if self.reply is not None else ()

    @property
    def request_token(self) -> CancellationToken | None:
        return self.cancellation_token

    @property
    def queued(self) -> bool:
        return False


def normalize_message(message: str) -> str:
    """Trim, case-fold, and collapse whitespace without fuzzy matching."""

    if not isinstance(message, str):
        raise TypeError("message must be a string")
    return " ".join(message.strip().casefold().split())


normalize = normalize_message


def _known_command(value: str) -> ControlCommand | None:
    return {
        "/status": ControlCommand.STATUS,
        "/cancel": ControlCommand.CANCEL,
        "/new": ControlCommand.NEW,
    }.get(value)


def parse_control(message: str) -> ParsedControl:
    """Classify one message using exact whole-message command matching."""

    normalized = normalize_message(message)
    if not normalized:
        return ParsedControl(normalized, MessageKind.EMPTY)
    if not normalized.startswith("/"):
        return ParsedControl(normalized, MessageKind.ORDINARY)

    parts = tuple(normalized.split(" "))
    command = _known_command(parts[0])
    if command is None:
        return ParsedControl(
            normalized,
            MessageKind.UNKNOWN_COMMAND,
            args=parts[1:],
        )
    if len(parts) != 1:
        return ParsedControl(
            normalized,
            MessageKind.MALFORMED_COMMAND,
            command=command,
            args=parts[1:],
        )
    return ParsedControl(normalized, MessageKind.CONTROL_COMMAND, command=command)


parse_command = parse_control


def _transition_from_session(
    parsed: ParsedControl,
    transition: SessionTransition,
    *,
    kind: ControlTransitionKind,
    reply: str | None,
    status: StatusView | None = None,
) -> ControlTransition:
    return ControlTransition(
        state=transition.state,
        parsed=parsed,
        kind=kind,
        reply=reply,
        status=status,
        effects=transition.effects,
        cancellation_token=transition.cancellation_token,
        reason=transition.reason,
    )


def render_status(view: StatusView) -> str:
    """Render only the fields deliberately exposed by :class:`StatusView`."""

    if view.active_request is None:
        request_text = "none"
    else:
        request = view.active_request
        host = request.execution_host or "not selected"
        request_text = f"{request.request_id} / {request.phase.value} / {host}"

    if view.pending_action is None:
        pending_text = "none"
    else:
        action = view.pending_action
        pending_text = (
            f"{action.action_id} / {action.kind} / {action.summary} / "
            f"expires {action.expires_at.isoformat()}"
        )

    services = (
        ", ".join(
            f"{service.service_id}={service.state}"
            for service in view.readiness.connected_services
        )
        or "none configured"
    )
    return (
        f"Session {view.session_id}: inactivity boundary {view.session_minutes}m; "
        f"model {view.model}; reasoning {view.reasoning}.\n"
        f"Active request: {request_text}.\n"
        f"Pending action: {pending_text}.\n"
        f"Command permissions: {view.permission_count} active.\n"
        f"Readiness: Ubuntu={view.readiness.ubuntu}, "
        f"Windows={view.readiness.windows}, OpenWA={view.readiness.openwa}, "
        f"connected services={services}."
    )


safe_status = render_status


def _usage(parsed: ParsedControl) -> str:
    if parsed.command is not None:
        return f"Usage: /{parsed.command.value}"
    return "Unknown or malformed control command. Valid: /new, /status, /cancel."


def _apply_control(
    session: WorkingSession,
    parsed: ParsedControl,
    *,
    now: datetime_or_clock,
) -> ControlTransition:
    if parsed.command is ControlCommand.STATUS:
        view = status_view(session)
        return ControlTransition(
            state=session,
            parsed=parsed,
            kind=ControlTransitionKind.STATUS,
            reply=render_status(view),
            status=view,
        )

    if parsed.command is ControlCommand.CANCEL:
        transition = cancel_active_request(session, now=now)
        if transition.kind is TransitionKind.NOOP:
            return _transition_from_session(
                parsed,
                transition,
                kind=ControlTransitionKind.NOTHING_TO_CANCEL,
                reply="Nothing is active or pending.",
            )
        return _transition_from_session(
            parsed,
            transition,
            kind=ControlTransitionKind.CANCELLED,
            reply=(
                "Cancelled the active request and invalidated its pending action. "
                "No side effect will start."
            ),
        )

    if parsed.command is ControlCommand.NEW:
        transition = new_working_session(session, now=now)
        return _transition_from_session(
            parsed,
            transition,
            kind=ControlTransitionKind.NEW_SESSION,
            reply=(
                f"Started {transition.state.session_id} with persistent defaults. "
                "Previous work will not resume."
            ),
        )

    raise AssertionError("_apply_control called without a complete control command")


def _apply_message(
    session: WorkingSession,
    message: str,
    *,
    now: datetime_or_clock,
    request_id: str | None,
    originating_message_id: str | None,
    phase: RequestPhase | str,
) -> ControlTransition:
    parsed = parse_control(message)

    # Slash commands are handled before pending-action or busy checks.  This
    # is the explicit precedence boundary that keeps /status, /cancel, and
    # /new available while work is active.
    if parsed.kind is MessageKind.CONTROL_COMMAND:
        return _apply_control(session, parsed, now=now)
    if parsed.kind in {MessageKind.MALFORMED_COMMAND, MessageKind.UNKNOWN_COMMAND}:
        return ControlTransition(
            state=session,
            parsed=parsed,
            kind=(
                ControlTransitionKind.MALFORMED_COMMAND
                if parsed.kind is MessageKind.MALFORMED_COMMAND
                else ControlTransitionKind.UNKNOWN_COMMAND
            ),
            reply=_usage(parsed),
            reason="slash commands are exact whole-message controls",
        )
    if parsed.kind is MessageKind.EMPTY:
        return ControlTransition(
            state=session,
            parsed=parsed,
            kind=ControlTransitionKind.EMPTY,
            reply="Empty messages have no effect.",
        )

    # Ordinary text is not an approval implementation in this foundation.  A
    # pending action blocks it; otherwise one active request refuses it rather
    # than creating a queue entry.
    if session.pending_action is not None:
        return ControlTransition(
            state=session,
            parsed=parsed,
            kind=ControlTransitionKind.PENDING_BLOCKED,
            reply=(
                "A pending action pauses this request and blocks unrelated work. "
                "Approval handling is owned by the later capability-broker phase."
            ),
            effects=("request_refused_pending",),
            reason="pending action owns the next operator control",
        )
    if session.active_request is not None:
        request_id_value = session.active_request.request_id
        return ControlTransition(
            state=session,
            parsed=parsed,
            kind=ControlTransitionKind.BUSY_REFUSED,
            reply=(
                f"Request {request_id_value} is still active. Use /status or /cancel; "
                "V1 does not queue another request."
            ),
            effects=("request_refused_busy",),
            reason="one active request is already present; no queue transition",
        )

    transition = accept_request(
        session,
        now=now,
        request_id=request_id,
        originating_message_id=originating_message_id,
        phase=phase,
    )
    return _transition_from_session(
        parsed,
        transition,
        kind=ControlTransitionKind.REQUEST_ACCEPTED,
        reply=(
            f"Accepted {transition.state.active_request.request_id}. "
            "The request is now active."
            if transition.state.active_request is not None
            else "Request accepted."
        ),
    )


# Keep the type alias local so public call signatures can accept either a
# controlled datetime or a Clock without importing a runtime implementation.
datetime_or_clock = datetime | Clock


def handle_message(
    session: WorkingSession,
    message: str,
    *,
    now: datetime_or_clock,
    request_id: str | None = None,
    originating_message_id: str | None = None,
    phase: RequestPhase | str = RequestPhase.INTERPRETING,
) -> ControlTransition:
    """Parse and reduce one authorized operator message purely."""

    return _apply_message(
        session,
        message,
        now=now,
        request_id=request_id,
        originating_message_id=originating_message_id,
        phase=phase,
    )


apply_message = handle_message
reduce_message = handle_message
handle_operator_message = handle_message

ControlState = WorkingSession
State = WorkingSession
Transition = ControlTransition

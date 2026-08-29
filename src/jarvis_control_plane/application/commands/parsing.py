"""Immutable control-command value types and deterministic parsing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ...sessions import CancellationToken, StatusView, WorkingSession

CONTROL_COMMANDS = (
    "/status",
    "/cancel",
    "/new",
    "/model",
    "/reasoning",
    "/config",
    "/permissions",
    "/revoke",
    "/history",
)

_THIS_TIME_APPROVALS = frozenset(
    {"yes", "okay", "ok", "allow", "approve", "confirm", "go ahead", "1"}
)
_SESSION_PERMISSION_APPROVALS = frozenset(
    {"allow for this session", "allow this session", "2"}
)
_PERSISTENT_PERMISSION_APPROVALS = frozenset({"allow every time", "always allow", "3"})
_REJECTIONS = frozenset(
    {
        "no",
        "reject",
        "deny",
        "cancel",
        "cancel action",
        "don't do it",
        "do not do it",
        "4",
    }
)


class ControlCommand(str, Enum):
    STATUS = "status"
    CANCEL = "cancel"
    NEW = "new"
    MODEL = "model"
    REASONING = "reasoning"
    CONFIG = "config"
    PERMISSIONS = "permissions"
    REVOKE = "revoke"
    HISTORY = "history"


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
    SESSION_MODEL_UPDATED = "session_model_updated"
    SESSION_REASONING_UPDATED = "session_reasoning_updated"
    CONFIG_UPDATED = "config_updated"
    CONFIGURATION_BLOCKED = "configuration_blocked"
    INVALID_CONFIGURATION = "invalid_configuration"
    MODEL_UNAVAILABLE = "model_unavailable"
    REASONING_UNAVAILABLE = "reasoning_unavailable"
    PERMISSIONS_LISTED = "permissions_listed"
    PERMISSION_REVOKED = "permission_revoked"
    PERMISSION_NOT_ACTIVE = "permission_not_active"
    HISTORY_REQUEST = "history_request"


class ApprovalChoice(str, Enum):
    APPROVE = "approve"
    SESSION_PERMISSION = "session_permission"
    PERSISTENT_PERMISSION = "persistent_permission"
    REJECT = "reject"


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


def parse_approval_choice(message: str) -> ApprovalChoice | None:
    """Recognize only complete normalized V1 approval or rejection replies."""

    normalized = normalize_message(message)
    if normalized in _THIS_TIME_APPROVALS:
        return ApprovalChoice.APPROVE
    if normalized in _SESSION_PERMISSION_APPROVALS:
        return ApprovalChoice.SESSION_PERMISSION
    if normalized in _PERSISTENT_PERMISSION_APPROVALS:
        return ApprovalChoice.PERSISTENT_PERMISSION
    if normalized in _REJECTIONS:
        return ApprovalChoice.REJECT
    return None


def _known_command(value: str) -> ControlCommand | None:
    return {
        "/status": ControlCommand.STATUS,
        "/cancel": ControlCommand.CANCEL,
        "/new": ControlCommand.NEW,
        "/model": ControlCommand.MODEL,
        "/reasoning": ControlCommand.REASONING,
        "/config": ControlCommand.CONFIG,
        "/permissions": ControlCommand.PERMISSIONS,
        "/revoke": ControlCommand.REVOKE,
        "/history": ControlCommand.HISTORY,
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
    allowed_argument_counts = {
        ControlCommand.STATUS: {0},
        ControlCommand.CANCEL: {0},
        ControlCommand.NEW: {0},
        ControlCommand.MODEL: {0, 1},
        ControlCommand.REASONING: {0, 1},
        ControlCommand.CONFIG: {0, 2},
        ControlCommand.PERMISSIONS: {0},
        ControlCommand.REVOKE: {1},
        ControlCommand.HISTORY: set(range(2, 1_000)),
    }
    history_is_valid = (
        command is ControlCommand.HISTORY
        and len(parts) >= 2
        and (
            (
                parts[1] in {"search", "inspect", "export", "conversation"}
                and (
                    (parts[1] == "search" and len(parts) >= 3)
                    or (parts[1] != "search" and len(parts) == 3)
                )
            )
            or (
                parts[1] == "delete"
                and len(parts) >= 3
                and (
                    (parts[2] in {"message", "conversation"} and len(parts) == 4)
                    or (parts[2] in {"date", "range"} and len(parts) == 5)
                )
            )
        )
    )
    if len(parts) - 1 not in allowed_argument_counts[command] or (
        command is ControlCommand.HISTORY and not history_is_valid
    ):
        return ParsedControl(
            normalized,
            MessageKind.MALFORMED_COMMAND,
            command=command,
            args=parts[1:],
        )
    return ParsedControl(
        normalized,
        MessageKind.CONTROL_COMMAND,
        command=command,
        args=parts[1:],
    )


parse_command = parse_control


__all__ = [
    "CONTROL_COMMANDS",
    "ApprovalChoice",
    "ControlCommand",
    "ControlTransition",
    "ControlTransitionKind",
    "MessageKind",
    "ParsedControl",
    "normalize",
    "normalize_message",
    "parse_approval_choice",
    "parse_command",
    "parse_control",
]

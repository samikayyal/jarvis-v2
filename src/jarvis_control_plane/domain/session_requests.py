"""Command permissions and active request values."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from .session_core import (
    PermissionLifetime,
    RequestPhase,
    SessionLifecycle,
    _identifier,
    ensure_utc,
)

if TYPE_CHECKING:
    from ..sessions import WorkingSession


@dataclass(frozen=True, slots=True)
class CommandPermissionComponent:
    """One ordered component in an exact permission identity."""

    executable: str
    arguments: tuple[str, ...]
    operator_before: str = ""
    redirections: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.executable, "executable")
        if self.operator_before not in {"", "|", "&&", "||", ";"}:
            raise ValueError("operator_before is not supported")
        arguments = tuple(self.arguments)
        redirections = tuple(self.redirections)
        if any(not isinstance(value, str) or not value for value in arguments):
            raise ValueError("arguments must be non-empty strings")
        if any(not isinstance(value, str) or not value for value in redirections):
            raise ValueError("redirections must be non-empty strings")
        object.__setattr__(self, "arguments", arguments)
        object.__setattr__(self, "redirections", redirections)


@dataclass(frozen=True, slots=True)
class CommandPermissionIdentity:
    """The complete immutable terminal identity an exact rule may match."""

    host: str
    cwd: str
    components: tuple[CommandPermissionComponent, ...]

    def __post_init__(self) -> None:
        _identifier(self.host, "host")
        _identifier(self.cwd, "cwd")
        components = tuple(self.components)
        if not components:
            raise ValueError("permission identity must contain a component")
        if components[0].operator_before:
            raise ValueError("first component cannot have a leading operator")
        object.__setattr__(self, "components", components)

    @property
    def command(self) -> str:
        parts: list[str] = []
        for component in self.components:
            if component.operator_before:
                parts.append(component.operator_before)
            parts.extend((component.executable, *component.arguments))
            parts.extend(f"redirect:{path}" for path in component.redirections)
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class CommandPermissionState:
    """Typed safe metadata for one exact command permission."""

    permission_id: str
    lifetime: PermissionLifetime | str
    identity: CommandPermissionIdentity
    created_at: datetime
    session_id: str | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    authorization_request_id: str | None = None
    authorization_action_id: str | None = None
    authorization_approval: str | None = None
    authorization_audit_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.permission_id, "permission_id")
        lifetime = PermissionLifetime(self.lifetime)
        if not isinstance(self.identity, CommandPermissionIdentity):
            raise TypeError("identity must be CommandPermissionIdentity")
        object.__setattr__(self, "lifetime", lifetime)
        if self.session_id is not None:
            _identifier(self.session_id, "session_id")
        if lifetime is PermissionLifetime.SESSION and self.session_id is None:
            raise ValueError("session permissions must identify their working session")
        if lifetime is PermissionLifetime.PERSISTENT and self.session_id is not None:
            raise ValueError("persistent permissions must not be session-bound")
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))
        if self.last_used_at is not None:
            object.__setattr__(self, "last_used_at", ensure_utc(self.last_used_at))
        if self.revoked_at is not None:
            object.__setattr__(self, "revoked_at", ensure_utc(self.revoked_at))
        for name in (
            "authorization_request_id",
            "authorization_action_id",
            "authorization_approval",
            "authorization_audit_id",
        ):
            value = getattr(self, name)
            if value is not None:
                _identifier(value, name)

    @property
    def scope(self) -> PermissionLifetime:
        return self.lifetime

    @property
    def normalized_command(self) -> str:
        return self.identity.command

    @property
    def host(self) -> str:
        return self.identity.host

    @property
    def command(self) -> str:
        return self.identity.command

    @property
    def canonical_working_directory(self) -> str:
        return self.identity.cwd

    @property
    def cwd(self) -> str:
        return self.identity.cwd

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


@dataclass(frozen=True, slots=True)
class ActiveRequestState:
    """Bounded live request metadata; request text is not operational state."""

    request_id: str
    session_id: str
    generation: int
    phase: RequestPhase | str
    created_at: datetime
    updated_at: datetime
    originating_message_id: str | None = None
    execution_host: str | None = None
    cancellation_reason: str | None = None
    terminal_outcome: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.request_id, "request_id")
        _identifier(self.session_id, "session_id")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
        ):
            raise ValueError("generation must be a non-negative integer")
        object.__setattr__(self, "phase", RequestPhase(self.phase))
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))
        object.__setattr__(self, "updated_at", ensure_utc(self.updated_at))
        for name in (
            "originating_message_id",
            "execution_host",
            "cancellation_reason",
            "terminal_outcome",
        ):
            value = getattr(self, name)
            if value is not None:
                _identifier(value, name)

    @property
    def is_processing(self) -> bool:
        """Whether session inactivity is suspended for genuine processing."""

        return self.phase in {
            RequestPhase.INTERPRETING,
            RequestPhase.PROCESSING,
            RequestPhase.DISPATCHING,
            RequestPhase.CANCELLING,
        }


@dataclass(frozen=True, slots=True)
class CancellationToken:
    """Capability to apply a result only to its exact live request generation."""

    session_id: str
    request_id: str
    generation: int

    def __post_init__(self) -> None:
        _identifier(self.session_id, "session_id")
        _identifier(self.request_id, "request_id")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
        ):
            raise ValueError("generation must be a non-negative integer")

    def matches(self, session: WorkingSession) -> bool:
        request = session.active_request
        return (
            session.lifecycle is SessionLifecycle.ACTIVE
            and request is not None
            and request.session_id == self.session_id
            and request.request_id == self.request_id
            and request.generation == self.generation
            and session.cancellation_generation == self.generation
        )


@dataclass(frozen=True, slots=True)
class RequestResult:
    """Small non-authoritative result envelope used by the pure result barrier."""

    request_id: str
    generation: int
    outcome: str

    def __post_init__(self) -> None:
        _identifier(self.request_id, "request_id")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
        ):
            raise ValueError("generation must be a non-negative integer")
        _identifier(self.outcome, "outcome")

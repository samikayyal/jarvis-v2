"""Core working-session configuration, readiness, and lifecycle values."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from ..sessions import CommandPermissionState, PendingActionState

ALLOWED_SESSION_MINUTES = (15, 30, 60, 120, 240)
SESSION_MINUTES = ALLOWED_SESSION_MINUTES
DEFAULT_SESSION_MINUTES = 60
PENDING_ACTION_MINUTES = 10
PENDING_ACTION_TTL = timedelta(minutes=PENDING_ACTION_MINUTES)
_WORKING_SESSION_SCHEMA_VERSION = 2
_MAX_LEGACY_PERMISSION_MIGRATION_COUNT = 1_000

CANONICAL_MODELS = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
MODELS = CANONICAL_MODELS
CANONICAL_REASONING_LEVELS = ("none", "low", "medium", "high", "xhigh", "max")
REASONING_LEVELS = CANONICAL_REASONING_LEVELS

ReadinessLevel = Literal["ready", "unavailable", "unknown"]


class SessionLifecycle(str, Enum):
    """Lifecycle of the in-memory current working-session record."""

    ACTIVE = "active"
    EXPIRED = "expired"
    ENDED = "ended"


class RequestPhase(str, Enum):
    """Non-terminal phases that an active request may occupy."""

    INTERPRETING = "interpreting"
    PROCESSING = "processing"
    AWAITING_APPROVAL = "awaiting_approval"
    DISPATCHING = "dispatching"
    CANCELLING = "cancelling"


class PermissionLifetime(str, Enum):
    """The two reusable command-permission lifetimes in V1."""

    SESSION = "session"
    PERSISTENT = "persistent"


class ProposalPresentationStatus(str, Enum):
    """Whether a frozen proposal may consume an approval choice."""

    PRESENTING = "presenting"
    PRESENTED = "presented"


class TransitionKind(str, Enum):
    """Bounded outcomes emitted by pure session transitions."""

    NOOP = "noop"
    REQUEST_ACCEPTED = "request_accepted"
    BUSY_REFUSED = "busy_refused"
    PENDING_BLOCKED = "pending_blocked"
    PENDING_INSTALLED = "pending_installed"
    CANCELLED = "cancelled"
    NEW_SESSION = "new_session"
    SESSION_EXPIRED = "session_expired"
    PENDING_EXPIRED = "pending_expired"
    PENDING_APPROVED = "pending_approved"
    PENDING_REJECTED = "pending_rejected"
    DISPATCH_ATTEMPTED = "dispatch_attempted"
    DISPATCH_CANCELLATION_RECONCILED = "dispatch_cancellation_reconciled"
    DISPATCH_COMPLETED = "dispatch_completed"
    RESTART_INTERRUPTED = "restart_interrupted"
    RESULT_APPLIED = "result_applied"
    LATE_RESULT_IGNORED = "late_result_ignored"
    PERMISSION_REVOKED = "permission_revoked"
    INVARIANT_REJECTED = "invariant_rejected"


class InvariantViolation(ValueError):
    """Raised when a caller attempts to construct an impossible state."""


class SessionStoreError(RuntimeError):
    """The local working-session store could not complete a transaction."""


class Clock(Protocol):
    """Controlled time source used by integration code and tests."""

    def now(self) -> datetime: ...


class PendingActionPort(Protocol):
    """Placeholder for the later pending-action store/broker integration."""

    def current(self, session_id: str) -> PendingActionState | None: ...


class PermissionPort(Protocol):
    """Placeholder for the later command-permission store integration."""

    def active_for_session(
        self, session_id: str
    ) -> tuple[CommandPermissionState, ...]: ...


class ReadinessPort(Protocol):
    """Placeholder for later connector, worker, and messaging readiness probes."""

    def snapshot(self) -> ReadinessState: ...


# Integration-facing vocabulary aliases.  These remain protocols only; this
# phase deliberately supplies no store, broker, connector, or readiness probe.
PendingActionStore = PendingActionPort
PermissionStore = PermissionPort
ReadinessProvider = ReadinessPort


def ensure_utc(value: datetime) -> datetime:
    """Require an aware timestamp and normalize it to UTC."""

    if not isinstance(value, datetime):
        raise TypeError("timestamps must be datetime values")
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _now(value: datetime | Clock) -> datetime:
    current = (
        value.now()
        if hasattr(value, "now") and not isinstance(value, datetime)
        else value
    )
    return ensure_utc(current)  # type: ignore[arg-type]


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{name} must be a non-empty canonical string")
    return value


def _canonical_choice(value: str, allowed: tuple[str, ...], name: str) -> str:
    value = _identifier(value, name)
    if value not in allowed:
        choices = ", ".join(allowed)
        raise ValueError(f"{name} must be one of: {choices}")
    return value


def _readiness_level(value: ReadinessLevel | bool, name: str) -> ReadinessLevel:
    if isinstance(value, bool):
        return "ready" if value else "unavailable"
    if value not in {"ready", "unavailable", "unknown"}:
        raise ValueError(f"{name} must be ready, unavailable, or unknown")
    return value


@dataclass(frozen=True, slots=True)
class SessionConfig:
    """Validated persistent defaults used when a clean session is created."""

    operator_id: str
    default_model: str = "gpt-5.6-luna"
    default_reasoning: str = "medium"
    inactivity_minutes: int = DEFAULT_SESSION_MINUTES

    def __post_init__(self) -> None:
        _identifier(self.operator_id, "operator_id")
        _canonical_choice(self.default_model, CANONICAL_MODELS, "default_model")
        _canonical_choice(
            self.default_reasoning, CANONICAL_REASONING_LEVELS, "default_reasoning"
        )
        if (
            isinstance(self.inactivity_minutes, bool)
            or not isinstance(self.inactivity_minutes, int)
            or self.inactivity_minutes not in ALLOWED_SESSION_MINUTES
        ):
            allowed = ", ".join(str(value) for value in ALLOWED_SESSION_MINUTES)
            raise ValueError(f"inactivity_minutes must be one of: {allowed}")

    @property
    def session_minutes(self) -> int:
        """Compatibility name used by the control grammar and status view."""

        return self.inactivity_minutes


@dataclass(frozen=True, slots=True)
class ModelAvailability:
    """The exact model/runtime choices currently admitted by the provider seam.

    Availability is runtime information, not an operator-selected default. A
    configured choice is retained verbatim, then checked again at request
    admission so an outage cannot silently substitute another choice.
    """

    available_models: tuple[str, ...] = CANONICAL_MODELS
    available_reasoning_levels: tuple[str, ...] = CANONICAL_REASONING_LEVELS

    def __post_init__(self) -> None:
        models = tuple(self.available_models)
        reasoning = tuple(self.available_reasoning_levels)
        if len(models) != len(set(models)):
            raise ValueError("available_models must not contain duplicates")
        if len(reasoning) != len(set(reasoning)):
            raise ValueError("available_reasoning_levels must not contain duplicates")
        for model in models:
            _canonical_choice(model, CANONICAL_MODELS, "available_models")
        for level in reasoning:
            _canonical_choice(
                level, CANONICAL_REASONING_LEVELS, "available_reasoning_levels"
            )
        object.__setattr__(self, "available_models", models)
        object.__setattr__(self, "available_reasoning_levels", reasoning)

    def model_is_available(self, model: str) -> bool:
        return model in self.available_models

    def reasoning_is_available(self, reasoning: str) -> bool:
        return reasoning in self.available_reasoning_levels

    def supports(self, *, model: str, reasoning: str) -> bool:
        return self.model_is_available(model) and self.reasoning_is_available(reasoning)


@dataclass(frozen=True, slots=True)
class DurableStateReferences:
    """Stable references that `/new` must preserve rather than delete."""

    operational_state_ref: str = "operational-state"
    conversation_store_ref: str = "conversation-history"
    durable_memory_ref: str = "durable-assistant-memory"
    audit_ref: str = "audit-record"

    def __post_init__(self) -> None:
        for name in (
            "operational_state_ref",
            "conversation_store_ref",
            "durable_memory_ref",
            "audit_ref",
        ):
            _identifier(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """One immutable authorized conversation-history entry.

    Session history is durable state, but it is deliberately separate from
    the safe status projection and is never rendered by this foundation.
    """

    session_id: str
    message_id: str
    direction: Literal["inbound", "outbound"]
    body: str
    occurred_at: datetime
    request_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.session_id, "session_id")
        _identifier(self.message_id, "message_id")
        if self.direction not in {"inbound", "outbound"}:
            raise ValueError("history direction must be inbound or outbound")
        if not isinstance(self.body, str) or not self.body.strip():
            raise ValueError("history body must be non-blank")
        if self.request_id is not None:
            _identifier(self.request_id, "request_id")
        object.__setattr__(self, "occurred_at", ensure_utc(self.occurred_at))


@dataclass(frozen=True, slots=True)
class ServiceReadiness:
    """Safe readiness metadata for one named connected service."""

    service_id: str
    state: ReadinessLevel = "unknown"

    def __post_init__(self) -> None:
        _identifier(self.service_id, "service_id")
        object.__setattr__(self, "state", _readiness_level(self.state, "state"))

    @property
    def ready(self) -> bool:
        return self.state == "ready"


@dataclass(frozen=True, slots=True)
class ReadinessState:
    """Typed safe placeholders; no credentials, handles, or diagnostic detail."""

    ubuntu: ReadinessLevel = "unknown"
    windows: ReadinessLevel = "unknown"
    openwa: ReadinessLevel = "unknown"
    connected_services: tuple[ServiceReadiness, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "ubuntu", _readiness_level(self.ubuntu, "ubuntu"))
        object.__setattr__(self, "windows", _readiness_level(self.windows, "windows"))
        object.__setattr__(self, "openwa", _readiness_level(self.openwa, "openwa"))
        services = tuple(self.connected_services)
        if any(not isinstance(service, ServiceReadiness) for service in services):
            raise TypeError("connected_services must contain ServiceReadiness values")
        identifiers = [service.service_id for service in services]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("connected_services must have unique service identifiers")
        object.__setattr__(self, "connected_services", services)

    @property
    def ubuntu_ready(self) -> bool:
        return self.ubuntu == "ready"

    @property
    def windows_ready(self) -> bool:
        return self.windows == "ready"

    @property
    def openwa_ready(self) -> bool:
        return self.openwa == "ready"

    @property
    def messaging_gateway(self) -> ReadinessLevel:
        """Glossary-facing alias; this is not a messaging gateway session."""

        return self.openwa

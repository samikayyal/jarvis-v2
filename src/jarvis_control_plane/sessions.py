"""Pure working-session state and lifecycle transitions for Jarvis V1.

This module deliberately models a Jarvis *working session*, not the named
logical connection owned by the WhatsApp messaging gateway.  It contains no
receiver, broker, persistence, connector, or model integration.  Callers pass
state in and receive a new immutable state plus a bounded transition record.

The later control-plane integration can persist the returned state and turn
the effects into audit, cancellation, and dispatch operations.  Until then,
the generation-bound :class:`CancellationToken` is the pure boundary that
prevents a result from an old request from being applied to a newer state.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Literal, Protocol

from .models import AuditEvidence

ALLOWED_SESSION_MINUTES = (15, 30, 60, 120, 240)
SESSION_MINUTES = ALLOWED_SESSION_MINUTES
DEFAULT_SESSION_MINUTES = 60
PENDING_ACTION_MINUTES = 10
PENDING_ACTION_TTL = timedelta(minutes=PENDING_ACTION_MINUTES)

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
    DISPATCH_COMPLETED = "dispatch_completed"
    RESTART_INTERRUPTED = "restart_interrupted"
    RESULT_APPLIED = "result_applied"
    LATE_RESULT_IGNORED = "late_result_ignored"
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
    default_model: str = "gpt-5.6-terra"
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


@dataclass(frozen=True, slots=True)
class PendingActionState:
    """One exact, immutable action awaiting its owning operator's decision."""

    action_id: str
    session_id: str
    request_id: str
    kind: str
    summary: str
    created_at: datetime
    expires_at: datetime
    digest: str = ""
    preview: str | None = None
    payload: str = ""

    def __post_init__(self) -> None:
        for name in ("action_id", "session_id", "request_id", "kind", "summary"):
            _identifier(getattr(self, name), name)
        created_at = ensure_utc(self.created_at)
        expires_at = ensure_utc(self.expires_at)
        if expires_at <= created_at:
            raise ValueError("pending action expiry must be after creation")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "expires_at", expires_at)
        preview = self.preview if self.preview is not None else self.summary
        _identifier(preview, "preview")
        if not isinstance(self.payload, str):
            raise TypeError("payload must be frozen text")
        expected = _pending_action_digest(
            action_id=self.action_id,
            request_id=self.request_id,
            kind=self.kind,
            preview=preview,
            payload=self.payload,
        )
        if self.digest and self.digest != expected:
            raise ValueError("pending action digest does not match frozen content")
        object.__setattr__(self, "preview", preview)
        object.__setattr__(self, "digest", expected)

    @classmethod
    def create(
        cls,
        *,
        action_id: str,
        session_id: str,
        request_id: str,
        kind: str,
        summary: str,
        created_at: datetime | Clock,
        preview: str | None = None,
        payload: str = "",
    ) -> PendingActionState:
        created = _now(created_at)
        return cls(
            action_id=action_id,
            session_id=session_id,
            request_id=request_id,
            kind=kind,
            summary=summary,
            created_at=created,
            expires_at=created + PENDING_ACTION_TTL,
            preview=preview,
            payload=payload,
        )

    @classmethod
    def from_proposal(
        cls,
        proposal: object,
        *,
        session_id: str,
        created_at: datetime | Clock,
    ) -> PendingActionState:
        """Freeze the typed orchestration proposal into durable session state."""

        try:
            action_id = proposal.action_id
            request_id = proposal.request_id
            kind = proposal.kind
            preview = proposal.preview
            payload = proposal.payload
            digest = proposal.digest
        except AttributeError as exc:
            raise TypeError("proposal must expose the frozen action contract") from exc
        created = _now(created_at)
        action = cls(
            action_id=action_id,
            session_id=session_id,
            request_id=request_id,
            kind=kind,
            summary=preview,
            preview=preview,
            payload=payload,
            created_at=created,
            expires_at=created + PENDING_ACTION_TTL,
        )
        if action.digest != digest:
            raise InvariantViolation("proposal digest does not match pending action")
        return action

    def is_expired(self, at: datetime | Clock) -> bool:
        return _now(at) >= self.expires_at


class DispatchStatus(str, Enum):
    """Durable lifecycle for one approval-gated dispatch attempt."""

    UNATTEMPTED = "unattempted"
    ATTEMPTED = "attempted"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"
    NOT_STARTED = "not_started"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ActionDispatchRecord:
    """One durable outbox record; live records retain the exact frozen payload."""

    action_id: str
    session_id: str
    request_id: str
    kind: str
    digest: str
    status: DispatchStatus | str
    approved_at: datetime
    payload: str | None
    preview: str | None
    attempted_at: datetime | None = None
    terminal_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("action_id", "session_id", "request_id", "kind", "digest"):
            _identifier(getattr(self, name), name)
        status = DispatchStatus(self.status)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "approved_at", ensure_utc(self.approved_at))
        if self.attempted_at is not None:
            object.__setattr__(self, "attempted_at", ensure_utc(self.attempted_at))
        if self.terminal_at is not None:
            object.__setattr__(self, "terminal_at", ensure_utc(self.terminal_at))
        live = status in {DispatchStatus.UNATTEMPTED, DispatchStatus.ATTEMPTED}
        if live:
            if not isinstance(self.payload, str) or not self.payload:
                raise ValueError("live dispatch records require a frozen payload")
            _identifier(self.preview, "preview")
        elif self.payload is not None or self.preview is not None:
            raise ValueError("terminal dispatch records must remove the frozen payload")
        if status is DispatchStatus.ATTEMPTED and self.attempted_at is None:
            raise ValueError("attempted dispatch records require attempted_at")
        if not live and self.terminal_at is None:
            raise ValueError("terminal dispatch records require terminal_at")

    @classmethod
    def unattempted(
        cls, action: PendingActionState, *, approved_at: datetime | Clock
    ) -> ActionDispatchRecord:
        return cls(
            action_id=action.action_id,
            session_id=action.session_id,
            request_id=action.request_id,
            kind=action.kind,
            digest=action.digest,
            status=DispatchStatus.UNATTEMPTED,
            approved_at=_now(approved_at),
            payload=action.payload,
            preview=action.preview,
        )

    @property
    def is_live(self) -> bool:
        return self.status in {DispatchStatus.UNATTEMPTED, DispatchStatus.ATTEMPTED}


def _pending_action_digest(
    *,
    action_id: str,
    request_id: str,
    kind: str,
    preview: str,
    payload: str,
) -> str:
    # Ownership is separately immutable state; this portable content digest is
    # deliberately identical to FrozenActionProposal's presentation digest.
    material = f"{action_id}\x1f{request_id}\x1f{kind}\x1f{preview}\x1f{payload}"
    return sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CommandPermissionState:
    """Typed safe metadata for one exact command permission."""

    permission_id: str
    lifetime: PermissionLifetime | str
    host: str
    command: str
    cwd: str
    created_at: datetime
    session_id: str | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        _identifier(self.permission_id, "permission_id")
        lifetime = PermissionLifetime(self.lifetime)
        _identifier(self.host, "host")
        if not isinstance(self.command, str) or not self.command.strip():
            raise ValueError("command must be non-blank")
        if not isinstance(self.cwd, str) or not self.cwd.strip():
            raise ValueError("cwd must be non-blank")
        object.__setattr__(self, "lifetime", lifetime)
        object.__setattr__(self, "command", " ".join(self.command.split()))
        object.__setattr__(self, "cwd", self.cwd.strip())
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

    @property
    def scope(self) -> PermissionLifetime:
        return self.lifetime

    @property
    def normalized_command(self) -> str:
        return self.command

    @property
    def canonical_working_directory(self) -> str:
        return self.cwd

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


@dataclass(frozen=True, slots=True)
class WorkingSession:
    """Immutable Jarvis working-session state.

    This type is deliberately distinct from the messaging gateway's session.
    ``conversation_ref`` identifies the current working conversation, while
    ``durable_refs`` identifies stores that remain authoritative across
    ``/new``.  Only one live request and one pending action can be represented.
    """

    session_id: str
    operator_id: str
    created_at: datetime
    last_activity_at: datetime
    inactivity_anchor_at: datetime
    session_minutes: int
    model: str
    reasoning: str
    default_model: str
    default_reasoning: str
    default_session_minutes: int
    conversation_ref: str
    durable_refs: DurableStateReferences
    active_request: ActiveRequestState | None = None
    pending_action: PendingActionState | None = None
    action_outbox: tuple[ActionDispatchRecord, ...] = ()
    permissions: tuple[CommandPermissionState, ...] = ()
    readiness: ReadinessState = field(default_factory=ReadinessState)
    cancellation_generation: int = 0
    session_number: int = 1
    next_request_number: int = 1
    lifecycle: SessionLifecycle | str = SessionLifecycle.ACTIVE
    last_request_id: str | None = None
    last_request_outcome: str | None = None
    last_terminal_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("session_id", "operator_id", "conversation_ref"):
            _identifier(getattr(self, name), name)
        if (
            isinstance(self.session_minutes, bool)
            or not isinstance(self.session_minutes, int)
            or self.session_minutes not in ALLOWED_SESSION_MINUTES
        ):
            raise ValueError(
                "session_minutes must be one of the configured allowed values"
            )
        _canonical_choice(self.model, CANONICAL_MODELS, "model")
        _canonical_choice(self.default_model, CANONICAL_MODELS, "default_model")
        _canonical_choice(self.reasoning, CANONICAL_REASONING_LEVELS, "reasoning")
        _canonical_choice(
            self.default_reasoning, CANONICAL_REASONING_LEVELS, "default_reasoning"
        )
        if (
            isinstance(self.default_session_minutes, bool)
            or not isinstance(self.default_session_minutes, int)
            or self.default_session_minutes not in ALLOWED_SESSION_MINUTES
        ):
            raise ValueError(
                "default_session_minutes must be one of the configured allowed values"
            )
        if (
            isinstance(self.cancellation_generation, bool)
            or not isinstance(self.cancellation_generation, int)
            or self.cancellation_generation < 0
        ):
            raise ValueError("cancellation_generation must be a non-negative integer")
        for name in ("session_number", "next_request_number"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        object.__setattr__(self, "lifecycle", SessionLifecycle(self.lifecycle))
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))
        object.__setattr__(self, "last_activity_at", ensure_utc(self.last_activity_at))
        object.__setattr__(
            self, "inactivity_anchor_at", ensure_utc(self.inactivity_anchor_at)
        )
        if self.last_terminal_at is not None:
            object.__setattr__(
                self, "last_terminal_at", ensure_utc(self.last_terminal_at)
            )
        for name in ("last_request_id", "last_request_outcome"):
            value = getattr(self, name)
            if value is not None:
                _identifier(value, name)
        permissions = tuple(self.permissions)
        if any(
            not isinstance(permission, CommandPermissionState)
            for permission in permissions
        ):
            raise TypeError("permissions must contain CommandPermissionState values")
        permission_ids = [permission.permission_id for permission in permissions]
        if len(permission_ids) != len(set(permission_ids)):
            raise InvariantViolation("permission identifiers must be unique")
        object.__setattr__(self, "permissions", permissions)
        outbox = tuple(self.action_outbox)
        if any(not isinstance(record, ActionDispatchRecord) for record in outbox):
            raise TypeError("action_outbox must contain ActionDispatchRecord values")
        action_ids = [record.action_id for record in outbox]
        if len(action_ids) != len(set(action_ids)):
            raise InvariantViolation("action outbox identifiers must be unique")
        live_outbox = tuple(record for record in outbox if record.is_live)
        if len(live_outbox) > 1:
            raise InvariantViolation("only one action dispatch may be live")
        object.__setattr__(self, "action_outbox", outbox)
        if self.active_request is not None:
            if self.lifecycle is not SessionLifecycle.ACTIVE:
                raise InvariantViolation(
                    "ended sessions cannot retain an active request"
                )
            if self.active_request.session_id != self.session_id:
                raise InvariantViolation(
                    "active request belongs to a different working session"
                )
            if self.active_request.generation != self.cancellation_generation:
                raise InvariantViolation("active request generation is not current")
        if self.pending_action is not None:
            if self.active_request is None:
                raise InvariantViolation("a pending action requires an active request")
            if self.pending_action.session_id != self.session_id:
                raise InvariantViolation(
                    "pending action belongs to a different working session"
                )
            if self.pending_action.request_id != self.active_request.request_id:
                raise InvariantViolation(
                    "pending action belongs to a different request"
                )
        if live_outbox:
            record = live_outbox[0]
            if self.pending_action is not None or self.active_request is None:
                raise InvariantViolation(
                    "live action dispatch requires no pending action"
                )
            if self.active_request.request_id != record.request_id:
                raise InvariantViolation(
                    "live action dispatch belongs to another request"
                )
            if self.active_request.phase is not RequestPhase.DISPATCHING:
                raise InvariantViolation(
                    "live action dispatch requires dispatching phase"
                )
        if self.lifecycle is not SessionLifecycle.ACTIVE and (
            self.active_request is not None or self.pending_action is not None
        ):
            raise InvariantViolation("non-active sessions cannot retain live work")

    @classmethod
    def initial(
        cls,
        operator_id: str,
        now: datetime | Clock,
        *,
        session_id: str = "S-001",
        conversation_ref: str | None = None,
        config: SessionConfig | None = None,
        durable_refs: DurableStateReferences | None = None,
        readiness: ReadinessState | None = None,
        permissions: Iterable[CommandPermissionState] = (),
        session_number: int = 1,
    ) -> WorkingSession:
        return create_working_session(
            operator_id,
            now,
            session_id=session_id,
            conversation_ref=conversation_ref,
            config=config,
            durable_refs=durable_refs,
            readiness=readiness,
            permissions=permissions,
            session_number=session_number,
        )

    @property
    def inactivity_deadline(self) -> datetime:
        return self.inactivity_anchor_at + timedelta(minutes=self.session_minutes)

    @property
    def request(self) -> ActiveRequestState | None:
        """Alias matching the domain phrase used by the status contract."""

        return self.active_request

    @property
    def pending(self) -> PendingActionState | None:
        return self.pending_action

    @property
    def durable_state_references(self) -> DurableStateReferences:
        return self.durable_refs

    @property
    def session_permission_ids(self) -> tuple[str, ...]:
        return tuple(
            permission.permission_id
            for permission in self.permissions
            if permission.lifetime is PermissionLifetime.SESSION
            and permission.is_active
        )


@dataclass(frozen=True, slots=True)
class SessionTransition:
    """Pure transition result; effects are integration hooks, not execution."""

    state: WorkingSession
    kind: TransitionKind | str
    changed: bool
    effects: tuple[str, ...] = ()
    reason: str | None = None
    cancellation_token: CancellationToken | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", TransitionKind(self.kind))
        object.__setattr__(self, "effects", tuple(self.effects))
        if self.reason is not None:
            _identifier(self.reason, "reason")

    @property
    def outcome(self) -> TransitionKind:
        return self.kind

    @property
    def request_token(self) -> CancellationToken | None:
        return self.cancellation_token

    @property
    def queued(self) -> bool:
        """The foundation never queues an ordinary request."""

        return False


def create_working_session(
    operator_id: str,
    now: datetime | Clock,
    *,
    session_id: str = "S-001",
    conversation_ref: str | None = None,
    config: SessionConfig | None = None,
    durable_refs: DurableStateReferences | None = None,
    readiness: ReadinessState | None = None,
    permissions: Iterable[CommandPermissionState] = (),
    session_number: int = 1,
) -> WorkingSession:
    """Create a deterministic idle working session from persistent defaults."""

    _identifier(operator_id, "operator_id")
    if config is not None and config.operator_id != operator_id:
        raise ValueError("session operator does not match SessionConfig")
    effective_config = config or SessionConfig(operator_id=operator_id)
    created = _now(now)
    conversation = conversation_ref or f"conversation:{session_id}"
    return WorkingSession(
        session_id=session_id,
        operator_id=operator_id,
        created_at=created,
        last_activity_at=created,
        inactivity_anchor_at=created,
        session_minutes=effective_config.inactivity_minutes,
        model=effective_config.default_model,
        reasoning=effective_config.default_reasoning,
        default_model=effective_config.default_model,
        default_reasoning=effective_config.default_reasoning,
        default_session_minutes=effective_config.inactivity_minutes,
        conversation_ref=conversation,
        durable_refs=durable_refs or DurableStateReferences(),
        permissions=tuple(permissions),
        readiness=readiness or ReadinessState(),
        session_number=session_number,
    )


initial_session = create_working_session


def _transition(
    before: WorkingSession,
    after: WorkingSession,
    kind: TransitionKind,
    *,
    effects: tuple[str, ...] = (),
    reason: str | None = None,
    cancellation_token: CancellationToken | None = None,
) -> SessionTransition:
    return SessionTransition(
        state=after,
        kind=kind,
        changed=after != before,
        effects=effects,
        reason=reason,
        cancellation_token=cancellation_token,
    )


def _busy_or_pending(session: WorkingSession) -> SessionTransition | None:
    if session.pending_action is not None:
        return _transition(
            session,
            session,
            TransitionKind.PENDING_BLOCKED,
            effects=("request_refused_pending",),
            reason="a pending action blocks unrelated work",
        )
    if session.active_request is not None:
        return _transition(
            session,
            session,
            TransitionKind.BUSY_REFUSED,
            effects=("request_refused_busy",),
            reason="one active request is already present; no queue transition",
        )
    return None


def accept_request(
    session: WorkingSession,
    *,
    now: datetime | Clock,
    request_id: str | None = None,
    originating_message_id: str | None = None,
    phase: RequestPhase | str = RequestPhase.INTERPRETING,
    execution_host: str | None = None,
) -> SessionTransition:
    """Accept one request or return a busy/pending refusal without queueing."""

    refusal = _busy_or_pending(session)
    if refusal is not None:
        return refusal
    if session.lifecycle is not SessionLifecycle.ACTIVE:
        return _transition(
            session,
            session,
            TransitionKind.INVARIANT_REJECTED,
            reason="a request cannot be accepted into an ended working session",
        )
    current = _now(now)
    identifier = request_id or f"R-{session.next_request_number:03d}"
    request = ActiveRequestState(
        request_id=identifier,
        session_id=session.session_id,
        generation=session.cancellation_generation,
        phase=phase,
        created_at=current,
        updated_at=current,
        originating_message_id=originating_message_id,
        execution_host=execution_host,
    )
    after = replace(
        session,
        active_request=request,
        last_activity_at=current,
        inactivity_anchor_at=current,
        next_request_number=session.next_request_number + 1,
    )
    return _transition(
        session,
        after,
        TransitionKind.REQUEST_ACCEPTED,
        effects=("start_request",),
        cancellation_token=CancellationToken(
            session_id=session.session_id,
            request_id=identifier,
            generation=session.cancellation_generation,
        ),
    )


start_request = accept_request
begin_request = accept_request


def install_pending_action(
    session: WorkingSession,
    action: PendingActionState,
    *,
    now: datetime | Clock | None = None,
) -> SessionTransition:
    """Attach one typed pending-action placeholder to its owning request."""

    request = session.active_request
    if request is None:
        raise InvariantViolation("a pending action requires an active request")
    if session.pending_action is not None:
        raise InvariantViolation("only one pending action may be live")
    if (
        action.session_id != session.session_id
        or action.request_id != request.request_id
    ):
        raise InvariantViolation(
            "pending action ownership does not match the live request"
        )
    at = _now(now or action.created_at)
    if action.is_expired(at):
        raise InvariantViolation("an expired pending action cannot be installed")
    updated_request = replace(
        request,
        phase=RequestPhase.AWAITING_APPROVAL,
        updated_at=at,
    )
    after = replace(
        session,
        active_request=updated_request,
        pending_action=action,
        last_activity_at=at,
        inactivity_anchor_at=at,
    )
    return _transition(
        session,
        after,
        TransitionKind.PENDING_INSTALLED,
        effects=("install_pending_action",),
    )


set_pending_action = install_pending_action
attach_pending_action = install_pending_action


def cancel_active_request(
    session: WorkingSession,
    *,
    now: datetime | Clock,
    reason: str = "operator_cancelled",
) -> SessionTransition:
    """Atomically cancel live work, invalidate pending state, and advance the barrier."""

    if (
        session.active_request is None
        and session.pending_action is None
        and not any(record.is_live for record in session.action_outbox)
    ):
        return _transition(
            session,
            session,
            TransitionKind.NOOP,
            reason="nothing is active or pending",
        )
    at = _now(now)
    request_id = session.active_request.request_id if session.active_request else None
    pending_id = session.pending_action.action_id if session.pending_action else None
    effects: list[str] = []
    if request_id is not None:
        effects.append("cancel_active_request")
    if pending_id is not None:
        effects.append("invalidate_pending_action")
    outbox = tuple(
        replace(
            record,
            status=DispatchStatus.CANCELLED,
            payload=None,
            preview=None,
            terminal_at=at,
        )
        if record.is_live
        else record
        for record in session.action_outbox
    )
    if any(record.is_live for record in session.action_outbox):
        effects.append("close_action_dispatch")
    effects.append("advance_cancellation_generation")
    after = replace(
        session,
        active_request=None,
        pending_action=None,
        action_outbox=outbox,
        cancellation_generation=session.cancellation_generation + 1,
        last_activity_at=at,
        inactivity_anchor_at=at,
        last_request_id=request_id or session.last_request_id,
        last_request_outcome="cancelled"
        if request_id is not None
        else session.last_request_outcome,
        last_terminal_at=at,
    )
    return _transition(
        session,
        after,
        TransitionKind.CANCELLED,
        effects=tuple(effects),
        reason=reason,
    )


cancel_request = cancel_active_request
cancel = cancel_active_request


def new_working_session(
    session: WorkingSession,
    *,
    now: datetime | Clock,
    session_id: str | None = None,
    conversation_ref: str | None = None,
) -> SessionTransition:
    """Atomically end the current session and create a clean one.

    Only session-scoped permissions are removed.  The durable reference object
    is reused by identity, and persistent permissions are carried forward.
    """

    at = _now(now)
    next_number = session.session_number + 1
    new_id = session_id or f"S-{next_number:03d}"
    new_conversation = conversation_ref or f"conversation:{new_id}"
    old_request = session.active_request
    old_pending = session.pending_action
    live_outbox = tuple(record for record in session.action_outbox if record.is_live)
    persistent_permissions = tuple(
        permission
        for permission in session.permissions
        if permission.lifetime is PermissionLifetime.PERSISTENT and permission.is_active
    )
    effects: list[str] = []
    if old_request is not None:
        effects.append("cancel_active_request")
    if old_pending is not None:
        effects.append("invalidate_pending_action")
    if live_outbox:
        effects.append("close_action_dispatch")
    effects.extend(
        (
            "revoke_session_permissions",
            "end_working_session",
            "start_clean_session",
            "advance_cancellation_generation",
        )
    )
    after = WorkingSession(
        session_id=new_id,
        operator_id=session.operator_id,
        created_at=at,
        last_activity_at=at,
        inactivity_anchor_at=at,
        session_minutes=session.default_session_minutes,
        model=session.default_model,
        reasoning=session.default_reasoning,
        default_model=session.default_model,
        default_reasoning=session.default_reasoning,
        default_session_minutes=session.default_session_minutes,
        conversation_ref=new_conversation,
        durable_refs=session.durable_refs,
        action_outbox=tuple(
            replace(
                record,
                status=(
                    DispatchStatus.NOT_STARTED
                    if record.status is DispatchStatus.UNATTEMPTED
                    else DispatchStatus.UNKNOWN
                ),
                payload=None,
                preview=None,
                terminal_at=at,
            )
            if record.is_live
            else record
            for record in session.action_outbox
        ),
        permissions=persistent_permissions,
        readiness=session.readiness,
        cancellation_generation=session.cancellation_generation + 1,
        session_number=next_number,
        next_request_number=session.next_request_number,
        lifecycle=SessionLifecycle.ACTIVE,
        last_request_id=old_request.request_id
        if old_request is not None
        else session.last_request_id,
        last_request_outcome="cancelled"
        if old_request is not None
        else session.last_request_outcome,
        last_terminal_at=at
        if old_request is not None or old_pending is not None
        else session.last_terminal_at,
    )
    return _transition(
        session,
        after,
        TransitionKind.NEW_SESSION,
        effects=tuple(effects),
        reason="previous work cancelled; pending action invalidated; session permissions revoked",
    )


start_new_session = new_working_session
new_session = new_working_session


def session_inactivity_suspended(session: WorkingSession) -> bool:
    """Return whether genuine processing suspends the inactivity countdown."""

    return (
        session.active_request is not None
        and session.pending_action is None
        and session.active_request.is_processing
    )


def is_session_inactive(session: WorkingSession, at: datetime | Clock) -> bool:
    """Use an inclusive deterministic boundary: expiry begins at the deadline."""

    if session.lifecycle is not SessionLifecycle.ACTIVE or session_inactivity_suspended(
        session
    ):
        return False
    return _now(at) >= session.inactivity_deadline


session_is_inactive = is_session_inactive


def expire_inactive_session(
    session: WorkingSession,
    *,
    now: datetime | Clock,
) -> SessionTransition:
    """End an idle session at its controlled boundary and start a clean one."""

    if not is_session_inactive(session, now):
        return _transition(
            session,
            session,
            TransitionKind.NOOP,
            reason="inactivity boundary not reached",
        )
    transition = new_working_session(session, now=now)
    return SessionTransition(
        state=transition.state,
        kind=TransitionKind.SESSION_EXPIRED,
        changed=transition.changed,
        effects=("session_inactivity_expired",) + transition.effects,
        reason="working-session inactivity boundary reached",
    )


expire_session = expire_inactive_session


def pending_action_is_expired(session: WorkingSession, at: datetime | Clock) -> bool:
    return session.pending_action is not None and session.pending_action.is_expired(at)


def expire_pending_action(
    session: WorkingSession,
    *,
    now: datetime | Clock,
) -> SessionTransition:
    """Invalidate an expired pending action without executing it."""

    action = session.pending_action
    if action is None or not action.is_expired(now):
        return _transition(
            session,
            session,
            TransitionKind.NOOP,
            reason="pending action has not expired",
        )
    at = _now(now)
    request_id = session.active_request.request_id if session.active_request else None
    after = replace(
        session,
        active_request=None,
        pending_action=None,
        cancellation_generation=session.cancellation_generation + 1,
        last_activity_at=at,
        inactivity_anchor_at=at,
        last_request_id=request_id or session.last_request_id,
        last_request_outcome="expired"
        if request_id is not None
        else session.last_request_outcome,
        last_terminal_at=at,
    )
    return _transition(
        session,
        after,
        TransitionKind.PENDING_EXPIRED,
        effects=(
            "invalidate_pending_action",
            "expire_pending_action",
            "advance_cancellation_generation",
        ),
        reason="pending action expired without execution",
    )


def approve_pending_action(
    session: WorkingSession,
    *,
    now: datetime | Clock,
) -> SessionTransition:
    """Consume the frozen action before any external dispatcher is called."""

    action = session.pending_action
    request = session.active_request
    if action is None or request is None:
        raise InvariantViolation("approval requires one live pending action")
    if action.is_expired(now):
        raise InvariantViolation("expired pending action cannot be approved")
    at = _now(now)
    after = replace(
        session,
        active_request=replace(request, phase=RequestPhase.DISPATCHING, updated_at=at),
        pending_action=None,
        action_outbox=session.action_outbox
        + (ActionDispatchRecord.unattempted(action, approved_at=at),),
        last_activity_at=at,
        inactivity_anchor_at=at,
    )
    return _transition(
        session,
        after,
        TransitionKind.PENDING_APPROVED,
        effects=("record_pending_approval", "consume_pending_action"),
    )


def mark_action_dispatch_attempted(
    session: WorkingSession,
    *,
    action_id: str,
    now: datetime | Clock,
) -> SessionTransition:
    """Persist the ambiguous side-effect boundary before invoking a dispatcher."""

    at = _now(now)
    record = next(
        (item for item in session.action_outbox if item.action_id == action_id), None
    )
    if record is None or record.status is not DispatchStatus.UNATTEMPTED:
        raise InvariantViolation("dispatch attempt is not unattempted")
    updated = replace(record, status=DispatchStatus.ATTEMPTED, attempted_at=at)
    after = replace(
        session,
        action_outbox=tuple(
            updated if item.action_id == action_id else item
            for item in session.action_outbox
        ),
        last_activity_at=at,
        inactivity_anchor_at=at,
    )
    return _transition(
        session,
        after,
        TransitionKind.DISPATCH_ATTEMPTED,
        effects=("record_dispatch_attempt",),
    )


def complete_action_dispatch(
    session: WorkingSession,
    *,
    action_id: str,
    status: DispatchStatus,
    now: datetime | Clock,
) -> SessionTransition:
    """Close one attempted record and immediately remove its exact payload."""

    if status not in {
        DispatchStatus.COMPLETED,
        DispatchStatus.FAILED,
        DispatchStatus.UNKNOWN,
        DispatchStatus.NOT_STARTED,
        DispatchStatus.CANCELLED,
    }:
        raise ValueError("action dispatch completion requires a terminal status")
    at = _now(now)
    record = next(
        (item for item in session.action_outbox if item.action_id == action_id), None
    )
    if record is None or not record.is_live:
        raise InvariantViolation("action dispatch record is not live")
    request = session.active_request
    if request is None or request.request_id != record.request_id:
        raise InvariantViolation("action dispatch record does not own the live request")
    terminal = replace(
        record,
        status=status,
        payload=None,
        preview=None,
        terminal_at=at,
    )
    outcome = {
        DispatchStatus.COMPLETED: "action_dispatched",
        DispatchStatus.FAILED: "action_dispatch_failed",
        DispatchStatus.UNKNOWN: "action_dispatch_unknown",
        DispatchStatus.NOT_STARTED: "action_not_started",
        DispatchStatus.CANCELLED: "action_cancelled",
    }[status]
    after = replace(
        session,
        active_request=None,
        action_outbox=tuple(
            terminal if item.action_id == action_id else item
            for item in session.action_outbox
        ),
        cancellation_generation=session.cancellation_generation + 1,
        last_activity_at=at,
        inactivity_anchor_at=at,
        last_request_id=request.request_id,
        last_request_outcome=outcome,
        last_terminal_at=at,
    )
    return _transition(
        session,
        after,
        TransitionKind.DISPATCH_COMPLETED,
        effects=("close_action_dispatch", "advance_cancellation_generation"),
    )


def reject_pending_action(
    session: WorkingSession,
    *,
    now: datetime | Clock,
) -> SessionTransition:
    """End a paused request without making its stored action dispatchable."""

    action = session.pending_action
    request = session.active_request
    if action is None or request is None:
        raise InvariantViolation("rejection requires one live pending action")
    at = _now(now)
    after = replace(
        session,
        active_request=None,
        pending_action=None,
        cancellation_generation=session.cancellation_generation + 1,
        last_activity_at=at,
        inactivity_anchor_at=at,
        last_request_id=request.request_id,
        last_request_outcome="rejected",
        last_terminal_at=at,
    )
    return _transition(
        session,
        after,
        TransitionKind.PENDING_REJECTED,
        effects=(
            "record_pending_rejection",
            "invalidate_pending_action",
            "advance_cancellation_generation",
        ),
    )


def interrupt_for_restart(
    session: WorkingSession,
    *,
    now: datetime | Clock,
) -> SessionTransition:
    """Invalidate non-resumable work and session permissions at restart."""

    live_dispatches = tuple(
        record for record in session.action_outbox if record.is_live
    )
    if (
        session.active_request is None
        and session.pending_action is None
        and not live_dispatches
    ):
        return _transition(
            session, session, TransitionKind.NOOP, reason="no work to interrupt"
        )
    at = _now(now)
    request = session.active_request
    reconciled_outbox = tuple(
        replace(
            record,
            status=(
                DispatchStatus.NOT_STARTED
                if record.status is DispatchStatus.UNATTEMPTED
                else DispatchStatus.UNKNOWN
            ),
            payload=None,
            preview=None,
            terminal_at=at,
        )
        if record.is_live
        else record
        for record in session.action_outbox
    )
    after = replace(
        session,
        active_request=None,
        pending_action=None,
        action_outbox=reconciled_outbox,
        permissions=tuple(
            permission
            for permission in session.permissions
            if permission.lifetime is PermissionLifetime.PERSISTENT
            and permission.is_active
        ),
        cancellation_generation=session.cancellation_generation + 1,
        last_activity_at=at,
        inactivity_anchor_at=at,
        last_request_id=(
            request.request_id if request is not None else session.last_request_id
        ),
        last_request_outcome=(
            "action_dispatch_unknown"
            if any(
                record.status is DispatchStatus.ATTEMPTED for record in live_dispatches
            )
            else "interrupted"
            if request is not None
            else session.last_request_outcome
        ),
        last_terminal_at=(at if request is not None else session.last_terminal_at),
    )
    return _transition(
        session,
        after,
        TransitionKind.RESTART_INTERRUPTED,
        effects=(
            "interrupt_active_request",
            "invalidate_pending_action",
            "revoke_session_permissions",
            "advance_cancellation_generation",
        ),
        reason="service restart invalidated non-resumable work",
    )


def cancellation_token_is_current(
    session: WorkingSession, token: CancellationToken
) -> bool:
    """Check the pure late-result barrier without changing state."""

    return token.matches(session)


is_current_cancellation = cancellation_token_is_current


def apply_request_result(
    session: WorkingSession,
    token: CancellationToken,
    result: RequestResult,
    *,
    now: datetime | Clock,
) -> SessionTransition:
    """Apply one result only when its request and cancellation generation match."""

    if (
        result.request_id != token.request_id
        or result.generation != token.generation
        or not token.matches(session)
        or session.pending_action is not None
    ):
        return _transition(
            session,
            session,
            TransitionKind.LATE_RESULT_IGNORED,
            effects=("late_result_ignored",),
            reason="result token no longer owns the live request",
        )
    at = _now(now)
    request = session.active_request
    assert request is not None
    after = replace(
        session,
        active_request=None,
        pending_action=None,
        cancellation_generation=session.cancellation_generation + 1,
        last_activity_at=at,
        inactivity_anchor_at=at,
        last_request_id=request.request_id,
        last_request_outcome=result.outcome,
        last_terminal_at=at,
    )
    return _transition(
        session,
        after,
        TransitionKind.RESULT_APPLIED,
        effects=("complete_request", "advance_cancellation_generation"),
    )


apply_result = apply_request_result


@dataclass(frozen=True, slots=True)
class StatusRequestView:
    """Only the request fields allowed in the operator `/status` view."""

    request_id: str
    phase: RequestPhase
    execution_host: str | None


@dataclass(frozen=True, slots=True)
class StatusPendingActionView:
    """Only safe pending-action summary and expiry metadata."""

    action_id: str
    request_id: str
    kind: str
    summary: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class StatusPermissionView:
    """Safe permission metadata for a future `/permissions` adapter."""

    permission_id: str
    lifetime: PermissionLifetime
    host: str
    command: str
    cwd: str
    created_at: datetime
    last_used_at: datetime | None


@dataclass(frozen=True, slots=True)
class StatusReadinessView:
    """Readiness without identities, credentials, errors, or connector detail."""

    ubuntu: ReadinessLevel
    windows: ReadinessLevel
    openwa: ReadinessLevel
    connected_services: tuple[ServiceReadiness, ...]


@dataclass(frozen=True, slots=True)
class StatusView:
    """The complete safe status surface; no raw request/action data is present."""

    session_id: str
    session_minutes: int
    model: str
    reasoning: str
    active_request: StatusRequestView | None
    pending_action: StatusPendingActionView | None
    permission_count: int
    readiness: StatusReadinessView

    @property
    def inactivity_minutes(self) -> int:
        return self.session_minutes


def status_view(session: WorkingSession) -> StatusView:
    """Build a safe, immutable view containing only configured status fields."""

    request = session.active_request
    action = session.pending_action
    return StatusView(
        session_id=session.session_id,
        session_minutes=session.session_minutes,
        model=session.model,
        reasoning=session.reasoning,
        active_request=(
            StatusRequestView(
                request_id=request.request_id,
                phase=request.phase,
                execution_host=request.execution_host,
            )
            if request is not None
            else None
        ),
        pending_action=(
            StatusPendingActionView(
                action_id=action.action_id,
                request_id=action.request_id,
                kind=action.kind,
                summary=action.summary,
                expires_at=action.expires_at,
            )
            if action is not None
            else None
        ),
        permission_count=sum(
            permission.is_active for permission in session.permissions
        ),
        readiness=StatusReadinessView(
            ubuntu=session.readiness.ubuntu,
            windows=session.readiness.windows,
            openwa=session.readiness.openwa,
            connected_services=session.readiness.connected_services,
        ),
    )


safe_status_view = status_view
session_status = status_view


class WorkingSessionStore(Protocol):
    """Authoritative current-session state with atomic history writes."""

    def load(self) -> WorkingSession | None: ...

    def create(self, session: WorkingSession) -> None: ...

    def compare_and_set(
        self,
        expected: WorkingSession,
        updated: WorkingSession,
        *,
        history: Iterable[HistoryEntry] = (),
    ) -> None: ...

    def compare_and_set_with_audit(
        self,
        expected: WorkingSession,
        updated: WorkingSession,
        *,
        audit: object,
        evidence: AuditEvidence,
        history: Iterable[HistoryEntry] = (),
    ) -> None: ...

    def append_history(self, entry: HistoryEntry) -> None: ...

    def list_history(
        self, session_id: str | None = None
    ) -> tuple[HistoryEntry, ...]: ...


class InMemoryWorkingSessionStore:
    """Thread-safe working-session store used by the composed control plane.

    The compare-and-set boundary is deliberately identical to the SQLite
    adapter so cancellation can race an in-flight orchestration result without
    allowing both transitions to win.
    """

    def __init__(self) -> None:
        self._session: WorkingSession | None = None
        self._history: list[HistoryEntry] = []
        self._lock = threading.RLock()

    def load(self) -> WorkingSession | None:
        with self._lock:
            return self._session

    def create(self, session: WorkingSession) -> None:
        with self._lock:
            if self._session is not None:
                raise SessionStoreError("working session already exists")
            self._session = session

    def compare_and_set(
        self,
        expected: WorkingSession,
        updated: WorkingSession,
        *,
        history: Iterable[HistoryEntry] = (),
    ) -> None:
        with self._lock:
            if self._session != expected:
                raise SessionStoreError("stale working-session transition")
            entries = tuple(history)
            self._session = updated
            self._history.extend(entries)

    def compare_and_set_with_audit(
        self,
        expected: WorkingSession,
        updated: WorkingSession,
        *,
        audit: object,
        evidence: AuditEvidence,
        history: Iterable[HistoryEntry] = (),
    ) -> None:
        """Commit state and required evidence as one lock-held in-memory operation."""

        append = getattr(audit, "append", None)
        if not callable(append):
            raise SessionStoreError("audit boundary does not support append")
        with self._lock:
            if self._session != expected:
                raise SessionStoreError("stale working-session transition")
            append(evidence)
            self._session = updated
            self._history.extend(tuple(history))

    def append_history(self, entry: HistoryEntry) -> None:
        with self._lock:
            self._history.append(entry)

    def list_history(self, session_id: str | None = None) -> tuple[HistoryEntry, ...]:
        with self._lock:
            if session_id is None:
                return tuple(self._history)
            return tuple(
                entry for entry in self._history if entry.session_id == session_id
            )


def _session_json(session: WorkingSession) -> str:
    """Serialize every durable field in a stable representation for CAS."""

    def default(value: object) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, Enum):
            return value.value
        raise TypeError(f"cannot serialize working-session field {type(value)!r}")

    return json.dumps(
        {
            "session_id": session.session_id,
            "operator_id": session.operator_id,
            "created_at": session.created_at,
            "last_activity_at": session.last_activity_at,
            "inactivity_anchor_at": session.inactivity_anchor_at,
            "session_minutes": session.session_minutes,
            "model": session.model,
            "reasoning": session.reasoning,
            "default_model": session.default_model,
            "default_reasoning": session.default_reasoning,
            "default_session_minutes": session.default_session_minutes,
            "conversation_ref": session.conversation_ref,
            "durable_refs": {
                "operational_state_ref": session.durable_refs.operational_state_ref,
                "conversation_store_ref": session.durable_refs.conversation_store_ref,
                "durable_memory_ref": session.durable_refs.durable_memory_ref,
                "audit_ref": session.durable_refs.audit_ref,
            },
            "active_request": (
                {
                    "request_id": session.active_request.request_id,
                    "session_id": session.active_request.session_id,
                    "generation": session.active_request.generation,
                    "phase": session.active_request.phase,
                    "created_at": session.active_request.created_at,
                    "updated_at": session.active_request.updated_at,
                    "originating_message_id": session.active_request.originating_message_id,
                    "execution_host": session.active_request.execution_host,
                    "cancellation_reason": session.active_request.cancellation_reason,
                    "terminal_outcome": session.active_request.terminal_outcome,
                }
                if session.active_request is not None
                else None
            ),
            "pending_action": (
                {
                    "action_id": session.pending_action.action_id,
                    "session_id": session.pending_action.session_id,
                    "request_id": session.pending_action.request_id,
                    "kind": session.pending_action.kind,
                    "summary": session.pending_action.summary,
                    "digest": session.pending_action.digest,
                    "preview": session.pending_action.preview,
                    "payload": session.pending_action.payload,
                    "created_at": session.pending_action.created_at,
                    "expires_at": session.pending_action.expires_at,
                }
                if session.pending_action is not None
                else None
            ),
            "action_outbox": [
                {
                    "action_id": record.action_id,
                    "session_id": record.session_id,
                    "request_id": record.request_id,
                    "kind": record.kind,
                    "digest": record.digest,
                    "status": record.status,
                    "approved_at": record.approved_at,
                    "payload": record.payload,
                    "preview": record.preview,
                    "attempted_at": record.attempted_at,
                    "terminal_at": record.terminal_at,
                }
                for record in session.action_outbox
            ],
            "permissions": [
                {
                    "permission_id": item.permission_id,
                    "lifetime": item.lifetime,
                    "host": item.host,
                    "command": item.command,
                    "cwd": item.cwd,
                    "created_at": item.created_at,
                    "session_id": item.session_id,
                    "last_used_at": item.last_used_at,
                    "revoked_at": item.revoked_at,
                }
                for item in session.permissions
            ],
            "readiness": {
                "ubuntu": session.readiness.ubuntu,
                "windows": session.readiness.windows,
                "openwa": session.readiness.openwa,
                "connected_services": [
                    {"service_id": item.service_id, "state": item.state}
                    for item in session.readiness.connected_services
                ],
            },
            "cancellation_generation": session.cancellation_generation,
            "session_number": session.session_number,
            "next_request_number": session.next_request_number,
            "lifecycle": session.lifecycle,
            "last_request_id": session.last_request_id,
            "last_request_outcome": session.last_request_outcome,
            "last_terminal_at": session.last_terminal_at,
        },
        default=default,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _parse_timestamp(value: object) -> datetime | None:
    return datetime.fromisoformat(str(value)) if value is not None else None


def _session_from_json(value: str) -> WorkingSession:
    """Deserialize a complete persisted session, failing closed on bad state."""

    try:
        payload = json.loads(value)
        refs = payload["durable_refs"]
        readiness = payload["readiness"]
        request = payload["active_request"]
        action = payload["pending_action"]
        active_request = (
            ActiveRequestState(
                request_id=request["request_id"],
                session_id=request["session_id"],
                generation=request["generation"],
                phase=request["phase"],
                created_at=_parse_timestamp(request["created_at"]),
                updated_at=_parse_timestamp(request["updated_at"]),
                originating_message_id=request["originating_message_id"],
                execution_host=request["execution_host"],
                cancellation_reason=request["cancellation_reason"],
                terminal_outcome=request["terminal_outcome"],
            )
            if request is not None
            else None
        )
        pending_action = (
            PendingActionState(
                action_id=action["action_id"],
                session_id=action["session_id"],
                request_id=action["request_id"],
                kind=action["kind"],
                summary=action["summary"],
                digest=action.get("digest", ""),
                preview=action.get("preview"),
                payload=action.get("payload", ""),
                created_at=_parse_timestamp(action["created_at"]),
                expires_at=_parse_timestamp(action["expires_at"]),
            )
            if action is not None
            else None
        )
        action_outbox = tuple(
            ActionDispatchRecord(
                action_id=item["action_id"],
                session_id=item["session_id"],
                request_id=item["request_id"],
                kind=item["kind"],
                digest=item["digest"],
                status=item["status"],
                approved_at=_parse_timestamp(item["approved_at"]),
                payload=item["payload"],
                preview=item["preview"],
                attempted_at=_parse_timestamp(item["attempted_at"]),
                terminal_at=_parse_timestamp(item["terminal_at"]),
            )
            for item in payload.get("action_outbox", ())
        )
        permissions = tuple(
            CommandPermissionState(
                permission_id=item["permission_id"],
                lifetime=item["lifetime"],
                host=item["host"],
                command=item["command"],
                cwd=item["cwd"],
                created_at=_parse_timestamp(item["created_at"]),
                session_id=item["session_id"],
                last_used_at=_parse_timestamp(item["last_used_at"]),
                revoked_at=_parse_timestamp(item["revoked_at"]),
            )
            for item in payload["permissions"]
        )
        return WorkingSession(
            session_id=payload["session_id"],
            operator_id=payload["operator_id"],
            created_at=_parse_timestamp(payload["created_at"]),
            last_activity_at=_parse_timestamp(payload["last_activity_at"]),
            inactivity_anchor_at=_parse_timestamp(payload["inactivity_anchor_at"]),
            session_minutes=payload["session_minutes"],
            model=payload["model"],
            reasoning=payload["reasoning"],
            default_model=payload["default_model"],
            default_reasoning=payload["default_reasoning"],
            # Ticket 05 persisted only the current duration.  Before Ticket 06
            # it was also the duration carried into `/new`, so retain that
            # behavior while adding the distinct future-session default.
            default_session_minutes=payload.get(
                "default_session_minutes", payload["session_minutes"]
            ),
            conversation_ref=payload["conversation_ref"],
            durable_refs=DurableStateReferences(**refs),
            active_request=active_request,
            pending_action=pending_action,
            action_outbox=action_outbox,
            permissions=permissions,
            readiness=ReadinessState(
                ubuntu=readiness["ubuntu"],
                windows=readiness["windows"],
                openwa=readiness["openwa"],
                connected_services=tuple(
                    ServiceReadiness(**item) for item in readiness["connected_services"]
                ),
            ),
            cancellation_generation=payload["cancellation_generation"],
            session_number=payload["session_number"],
            next_request_number=payload["next_request_number"],
            lifecycle=payload["lifecycle"],
            last_request_id=payload["last_request_id"],
            last_request_outcome=payload["last_request_outcome"],
            last_terminal_at=_parse_timestamp(payload["last_terminal_at"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SessionStoreError("persisted working session is invalid") from exc


class SQLiteWorkingSessionStore:
    """SQLite current-session store with complete-state compare-and-set.

    Every transition starts an immediate transaction, compares the canonical
    serialization of the whole previous state, then commits the new state and
    any history entries together. This prevents a stale transition from
    overwriting a newer cancellation or clean-session boundary.
    """

    def __init__(self, database: str | Path | sqlite3.Connection = ":memory:") -> None:
        self._owns_connection = not isinstance(database, sqlite3.Connection)
        self.connection = (
            database
            if isinstance(database, sqlite3.Connection)
            else sqlite3.connect(str(database))
        )
        try:
            self.connection.execute("PRAGMA journal_mode = WAL")
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS working_session_current (
                    slot INTEGER PRIMARY KEY CHECK (slot = 1),
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS working_session_history (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
                    body TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    request_id TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS working_session_history_message
                ON working_session_history(session_id, message_id);
                """
            )
            self.connection.commit()
        except sqlite3.Error as exc:
            raise SessionStoreError(
                "could not initialize working-session store"
            ) from exc

    def load(self) -> WorkingSession | None:
        try:
            row = self.connection.execute(
                "SELECT payload FROM working_session_current WHERE slot = 1"
            ).fetchone()
        except sqlite3.Error as exc:
            raise SessionStoreError("could not read working session") from exc
        return _session_from_json(row[0]) if row is not None else None

    def create(self, session: WorkingSession) -> None:
        try:
            self.connection.execute(
                "INSERT INTO working_session_current(slot, payload) VALUES (1, ?)",
                (_session_json(session),),
            )
            self.connection.commit()
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise SessionStoreError("could not create working session") from exc

    def compare_and_set(
        self,
        expected: WorkingSession,
        updated: WorkingSession,
        *,
        history: Iterable[HistoryEntry] = (),
    ) -> None:
        expected_json = _session_json(expected)
        updated_json = _session_json(updated)
        entries = tuple(history)
        if any(not isinstance(entry, HistoryEntry) for entry in entries):
            raise TypeError("history must contain HistoryEntry values")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT payload FROM working_session_current WHERE slot = 1"
            ).fetchone()
            if row is None or row[0] != expected_json:
                self.connection.rollback()
                raise SessionStoreError("stale working-session transition")
            cursor = self.connection.execute(
                """
                UPDATE working_session_current SET payload = ?
                WHERE slot = 1 AND payload = ?
                """,
                (updated_json, expected_json),
            )
            if cursor.rowcount != 1:
                self.connection.rollback()
                raise SessionStoreError("stale working-session transition")
            self._append_history_in_transaction(entries)
            self.connection.commit()
        except SessionStoreError:
            raise
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise SessionStoreError(
                "could not compare and set working session"
            ) from exc

    def compare_and_set_with_audit(
        self,
        expected: WorkingSession,
        updated: WorkingSession,
        *,
        audit: object,
        evidence: AuditEvidence,
        history: Iterable[HistoryEntry] = (),
    ) -> None:
        """Commit session state, outbox, and audit admission in one transaction.

        When the append-only audit shares this SQLite connection, its record is
        written inside the same transaction. Independent audit adapters are
        invoked only while the session transaction is still rollbackable.
        """

        expected_json = _session_json(expected)
        updated_json = _session_json(updated)
        entries = tuple(history)
        append = getattr(audit, "append", None)
        shared_append = getattr(audit, "_append_batch_in_transaction", None)
        if not callable(append):
            raise SessionStoreError("audit boundary does not support append")
        if any(not isinstance(entry, HistoryEntry) for entry in entries):
            raise TypeError("history must contain HistoryEntry values")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT payload FROM working_session_current WHERE slot = 1"
            ).fetchone()
            if row is None or row[0] != expected_json:
                self.connection.rollback()
                raise SessionStoreError("stale working-session transition")
            cursor = self.connection.execute(
                "UPDATE working_session_current SET payload = ? WHERE slot = 1 AND payload = ?",
                (updated_json, expected_json),
            )
            if cursor.rowcount != 1:
                self.connection.rollback()
                raise SessionStoreError("stale working-session transition")
            self._append_history_in_transaction(entries)
            if getattr(audit, "_connection", None) is self.connection and callable(
                shared_append
            ):
                shared_append((evidence,))
            else:
                append(evidence)
            self.connection.commit()
        except SessionStoreError:
            raise
        except Exception as exc:
            if self.connection.in_transaction:
                self.connection.rollback()
            raise SessionStoreError(
                "could not atomically commit working-session audit admission"
            ) from exc

    def append_history(self, entry: HistoryEntry) -> None:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self._append_history_in_transaction((entry,))
            self.connection.commit()
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise SessionStoreError("could not append session history") from exc

    def _append_history_in_transaction(self, entries: Iterable[HistoryEntry]) -> None:
        self.connection.executemany(
            """
            INSERT INTO working_session_history(
                session_id, message_id, direction, body, occurred_at, request_id
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    entry.session_id,
                    entry.message_id,
                    entry.direction,
                    entry.body,
                    entry.occurred_at.isoformat(),
                    entry.request_id,
                )
                for entry in entries
            ),
        )

    def list_history(self, session_id: str | None = None) -> tuple[HistoryEntry, ...]:
        try:
            if session_id is None:
                rows = self.connection.execute(
                    """
                    SELECT session_id, message_id, direction, body, occurred_at, request_id
                    FROM working_session_history ORDER BY sequence
                    """
                ).fetchall()
            else:
                _identifier(session_id, "session_id")
                rows = self.connection.execute(
                    """
                    SELECT session_id, message_id, direction, body, occurred_at, request_id
                    FROM working_session_history WHERE session_id = ? ORDER BY sequence
                    """,
                    (session_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise SessionStoreError("could not read session history") from exc
        return tuple(
            HistoryEntry(
                session_id=row[0],
                message_id=row[1],
                direction=row[2],
                body=row[3],
                occurred_at=datetime.fromisoformat(row[4]),
                request_id=row[5],
            )
            for row in rows
        )

    def close(self) -> None:
        if self._owns_connection:
            self.connection.close()


SQLiteSessionStore = SQLiteWorkingSessionStore
SessionState = WorkingSession
WorkingSessionState = WorkingSession
WorkingSessionConfig = SessionConfig
DurableReferences = DurableStateReferences
PendingAction = PendingActionState
CommandPermission = CommandPermissionState
Permission = CommandPermissionState
Readiness = ReadinessState
ReadinessSnapshot = ReadinessState
RequestState = ActiveRequestState
Transition = SessionTransition

"""Aggregate working-session state and transition results."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .session_actions import ActionDispatchRecord, PendingActionState
from .session_core import (
    _MAX_LEGACY_PERMISSION_MIGRATION_COUNT,
    ALLOWED_SESSION_MINUTES,
    CANONICAL_MODELS,
    CANONICAL_REASONING_LEVELS,
    Clock,
    DurableStateReferences,
    InvariantViolation,
    PermissionLifetime,
    ReadinessState,
    RequestPhase,
    SessionConfig,
    SessionLifecycle,
    TransitionKind,
    _canonical_choice,
    _identifier,
    ensure_utc,
)
from .session_requests import (
    ActiveRequestState,
    CancellationToken,
    CommandPermissionState,
)


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
    legacy_permissions_invalidated: int = 0

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
        if (
            isinstance(self.legacy_permissions_invalidated, bool)
            or not isinstance(self.legacy_permissions_invalidated, int)
            or not 0
            <= self.legacy_permissions_invalidated
            <= _MAX_LEGACY_PERMISSION_MIGRATION_COUNT
        ):
            raise ValueError(
                "legacy_permissions_invalidated must be a bounded non-negative integer"
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
        open_outbox = tuple(record for record in outbox if record.is_open)
        if len(open_outbox) > 1:
            raise InvariantViolation("only one action dispatch may be open")
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
        cancelling_outbox = tuple(record for record in outbox if record.is_cancelling)
        if cancelling_outbox and (
            self.active_request is not None or self.pending_action is not None
        ):
            raise InvariantViolation(
                "cancelling action dispatch cannot retain live request state"
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
        from ..sessions import create_working_session

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

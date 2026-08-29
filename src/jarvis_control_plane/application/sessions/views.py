"""Safe working-session status views and permission revocation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from ...domain.session_core import (
    Clock,
    PermissionLifetime,
    ReadinessLevel,
    RequestPhase,
    ServiceReadiness,
    TransitionKind,
    _identifier,
    _now,
)
from ...domain.session_requests import CommandPermissionState
from ...domain.session_state import SessionTransition, WorkingSession
from .request import _transition


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


def active_command_permissions(
    session: WorkingSession,
) -> tuple[CommandPermissionState, ...]:
    """Return the inspectable permission projection in one stable order."""

    return tuple(
        sorted(
            (permission for permission in session.permissions if permission.is_active),
            key=lambda permission: (permission.created_at, permission.permission_id),
        )
    )


def revoke_command_permissions(
    session: WorkingSession,
    *,
    selector: str,
    now: datetime | Clock,
) -> SessionTransition:
    """Remove matching rules from usable authority before any acknowledgement.

    The revocation timestamp stays in operational state for provenance, while
    policy matching considers only active rules. ``selector`` is an exact
    permission identifier or one of the canonical ``session``, ``persistent``,
    and ``all`` scopes. A selector with no active matches is an idempotent no-op.
    """

    _identifier(selector, "permission_selector")
    at = _now(now)
    if selector == "all":
        matches = lambda permission: permission.is_active
    elif selector in {item.value for item in PermissionLifetime}:
        lifetime = PermissionLifetime(selector)
        matches = lambda permission: (
            permission.is_active and permission.lifetime is lifetime
        )
    else:
        matches = lambda permission: (
            permission.is_active and permission.permission_id == selector
        )
    revoked_ids = tuple(
        permission.permission_id
        for permission in session.permissions
        if matches(permission)
    )
    if not revoked_ids:
        return _transition(
            session,
            session,
            TransitionKind.NOOP,
            reason="no matching command permissions were active",
        )
    after = replace(
        session,
        permissions=tuple(
            replace(permission, revoked_at=at)
            if permission.permission_id in revoked_ids
            else permission
            for permission in session.permissions
        ),
    )
    return _transition(
        session,
        after,
        TransitionKind.PERMISSION_REVOKED,
        effects=("revoke_command_permissions",),
        reason=f"revoked {len(revoked_ids)} command permission(s) before acknowledgement",
    )


def revoke_command_permission(
    session: WorkingSession,
    *,
    permission_id: str,
    now: datetime | Clock,
) -> SessionTransition:
    """Compatibility wrapper for revoking one exact permission identifier."""

    return revoke_command_permissions(session, selector=permission_id, now=now)

"""Working-session lifecycle, approval, dispatch, and rejection transitions."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from ...domain.session_actions import ActionDispatchRecord, DispatchStatus
from ...domain.session_core import (
    Clock,
    InvariantViolation,
    PermissionLifetime,
    RequestPhase,
    SessionLifecycle,
    TransitionKind,
    _now,
)
from ...domain.session_state import SessionTransition, WorkingSession
from .request import _transition


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
    open_outbox = tuple(record for record in session.action_outbox if record.is_open)
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
    if open_outbox:
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
                status=DispatchStatus.CANCELLING,
                payload=None,
                preview=None,
                terminal_at=at,
            )
            if record.is_open
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
        legacy_permissions_invalidated=session.legacy_permissions_invalidated,
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


def reconcile_action_cancellation(
    session: WorkingSession,
    *,
    action_id: str,
    status: DispatchStatus,
    now: datetime | Clock,
) -> SessionTransition:
    """Persist the result of stopping an action after admission was closed."""

    if status not in {DispatchStatus.CANCELLED, DispatchStatus.UNKNOWN}:
        raise ValueError(
            "action cancellation reconciliation requires cancelled or unknown"
        )
    at = _now(now)
    record = next(
        (item for item in session.action_outbox if item.action_id == action_id), None
    )
    if record is None or not record.is_cancelling:
        raise InvariantViolation("action cancellation record is not reconciling")
    outcome = (
        "action_cancelled"
        if status is DispatchStatus.CANCELLED
        else "action_dispatch_unknown"
    )
    terminal = replace(record, status=status, terminal_at=at)
    after = replace(
        session,
        action_outbox=tuple(
            terminal if item.action_id == action_id else item
            for item in session.action_outbox
        ),
        last_activity_at=at,
        inactivity_anchor_at=at,
        last_request_id=record.request_id,
        last_request_outcome=outcome,
        last_terminal_at=at,
    )
    return _transition(
        session,
        after,
        TransitionKind.DISPATCH_CANCELLATION_RECONCILED,
        effects=("reconcile_action_cancellation",),
        reason=outcome,
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

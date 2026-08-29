"""Restart recovery and request-result transitions."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from ...domain.session_actions import DispatchStatus
from ...domain.session_core import Clock, PermissionLifetime, TransitionKind, _now
from ...domain.session_requests import CancellationToken, RequestResult
from ...domain.session_state import SessionTransition, WorkingSession
from .request import _transition


def interrupt_for_restart(
    session: WorkingSession,
    *,
    now: datetime | Clock,
) -> SessionTransition:
    """Invalidate non-resumable work and session permissions at restart."""

    open_dispatches = tuple(
        record for record in session.action_outbox if record.is_open
    )
    active_session_permissions = any(
        permission.lifetime is PermissionLifetime.SESSION and permission.is_active
        for permission in session.permissions
    )
    if (
        session.active_request is None
        and session.pending_action is None
        and not open_dispatches
        and not active_session_permissions
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
        if record.is_open
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
                record.status in {DispatchStatus.ATTEMPTED, DispatchStatus.CANCELLING}
                for record in open_dispatches
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

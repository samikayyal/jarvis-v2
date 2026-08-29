"""Working-session creation, admission, pending action, and cancellation transitions."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime

from ...domain.session_actions import (
    DispatchStatus,
    PendingActionState,
    ProposalPresentationFragment,
)
from ...domain.session_core import (
    Clock,
    DurableStateReferences,
    InvariantViolation,
    ProposalPresentationStatus,
    ReadinessState,
    RequestPhase,
    SessionConfig,
    SessionLifecycle,
    TransitionKind,
    _identifier,
    _now,
)
from ...domain.session_requests import (
    ActiveRequestState,
    CancellationToken,
    CommandPermissionState,
)
from ...domain.session_state import SessionTransition, WorkingSession


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
    if any(record.is_open for record in session.action_outbox):
        return _transition(
            session,
            session,
            TransitionKind.BUSY_REFUSED,
            effects=("request_refused_busy",),
            reason="an action cancellation is still being reconciled; no queue transition",
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


def record_proposal_fragment(
    session: WorkingSession,
    *,
    action_id: str,
    number: int,
    total: int,
    outbound_id: str,
    now: datetime | Clock,
) -> SessionTransition:
    """Record one accepted envelope fragment in the only valid send order."""

    action = session.pending_action
    if action is None or action.action_id != action_id:
        raise InvariantViolation(
            "presentation fragment does not own the pending action"
        )
    if action.presentation_status is not ProposalPresentationStatus.PRESENTING:
        raise InvariantViolation("only a presenting action can receive fragments")
    if number != len(action.presentation_fragments) + 1:
        raise InvariantViolation("presentation fragment is out of order")
    if (
        action.presentation_fragments
        and action.presentation_fragments[0].total != total
    ):
        raise InvariantViolation("presentation fragment total changed")
    fragment = ProposalPresentationFragment(number, total, outbound_id)
    return _transition(
        session,
        replace(
            session,
            pending_action=replace(
                action,
                presentation_fragments=(*action.presentation_fragments, fragment),
            ),
            last_activity_at=_now(now),
        ),
        TransitionKind.PENDING_INSTALLED,
        effects=("record_proposal_fragment",),
    )


def mark_proposal_presented(
    session: WorkingSession, *, action_id: str, now: datetime | Clock
) -> SessionTransition:
    """Enable approval only after every envelope fragment is confirmed."""

    action = session.pending_action
    if action is None or action.action_id != action_id:
        raise InvariantViolation(
            "presentation completion does not own the pending action"
        )
    if action.presentation_status is not ProposalPresentationStatus.PRESENTING:
        raise InvariantViolation("proposal is not presenting")
    if (
        not action.presentation_fragments
        or len(action.presentation_fragments) != action.presentation_fragments[0].total
    ):
        raise InvariantViolation("proposal presentation is incomplete")
    return _transition(
        session,
        replace(
            session,
            pending_action=replace(
                action, presentation_status=ProposalPresentationStatus.PRESENTED
            ),
            last_activity_at=_now(now),
        ),
        TransitionKind.PENDING_INSTALLED,
        effects=("mark_proposal_presented",),
    )


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
        and not any(record.is_open for record in session.action_outbox)
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
            status=DispatchStatus.CANCELLING,
            payload=None,
            preview=None,
            terminal_at=at,
        )
        if record.is_live
        else record
        for record in session.action_outbox
    )
    if any(record.is_open for record in session.action_outbox):
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

"""Admission policy for model-backed control-plane requests."""

from __future__ import annotations

from datetime import datetime

from ...sessions import (
    Clock,
    ModelAvailability,
    RequestPhase,
    SessionTransition,
    WorkingSession,
    accept_request,
)
from .parsing import (
    ControlTransition,
    ControlTransitionKind,
    MessageKind,
    ParsedControl,
    normalize_message,
)

datetime_or_clock = datetime | Clock


def _from_session(
    parsed: ParsedControl,
    transition: SessionTransition,
    *,
    kind: ControlTransitionKind,
    reply: str | None,
) -> ControlTransition:
    return ControlTransition(
        state=transition.state,
        parsed=parsed,
        kind=kind,
        reply=reply,
        effects=transition.effects,
        cancellation_token=transition.cancellation_token,
        reason=transition.reason,
    )


def _admit_orchestration_request(
    session: WorkingSession,
    parsed: ParsedControl,
    *,
    now: datetime_or_clock,
    request_id: str | None,
    originating_message_id: str | None,
    phase: RequestPhase | str,
    model_availability: ModelAvailability,
) -> ControlTransition:
    """Apply the one shared admission policy for model-backed requests."""

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
    if any(record.is_open for record in session.action_outbox):
        return ControlTransition(
            state=session,
            parsed=parsed,
            kind=ControlTransitionKind.BUSY_REFUSED,
            reply=(
                "A prior action cancellation is still being reconciled. "
                "Use /status or /cancel; V1 does not queue another request."
            ),
            effects=("request_refused_busy",),
            reason="an action cancellation is still being reconciled; no queue transition",
        )

    if not model_availability.model_is_available(session.model):
        return ControlTransition(
            state=session,
            parsed=parsed,
            kind=ControlTransitionKind.MODEL_UNAVAILABLE,
            reply=(
                f"Model {session.model} is unavailable. No substitute was selected; "
                "choose an available model and try again."
            ),
        )
    if not model_availability.reasoning_is_available(session.reasoning):
        return ControlTransition(
            state=session,
            parsed=parsed,
            kind=ControlTransitionKind.REASONING_UNAVAILABLE,
            reply=(
                f"Reasoning level {session.reasoning} is unavailable. No substitute was "
                "selected; choose an available level and try again."
            ),
        )

    transition = accept_request(
        session,
        now=now,
        request_id=request_id,
        originating_message_id=originating_message_id,
        phase=phase,
    )
    return _from_session(
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


def admit_orchestration_request(
    session: WorkingSession,
    request_text: str,
    *,
    now: datetime_or_clock,
    request_id: str | None = None,
    originating_message_id: str | None = None,
    phase: RequestPhase | str = RequestPhase.INTERPRETING,
    model_availability: ModelAvailability,
) -> ControlTransition:
    """Apply model-backed admission to text embedded in an explicit command.

    The embedded text is always treated as the request body, even when it
    happens to begin with a slash command.  The surrounding command parser
    remains responsible for the outer control surface.
    """

    normalized = normalize_message(request_text)
    if not normalized:
        raise ValueError("orchestration request text must be non-blank")
    parsed = ParsedControl(normalized=normalized, kind=MessageKind.ORDINARY)
    return _admit_orchestration_request(
        session,
        parsed,
        now=now,
        request_id=request_id,
        originating_message_id=originating_message_id,
        phase=phase,
        model_availability=model_availability,
    )

"""Deterministic pure control grammar for the Ticket 05 foundation.

The parser is intentionally small: V1's `/status`, `/cancel`, and `/new`
controls are exact whole-message commands.  Normalization happens before
classification, slash commands take precedence over ordinary text and future
approval handling, and malformed slash commands never become requests.

This module delegates lifecycle changes to :mod:`jarvis_control_plane.sessions`
and does not call a receiver, broker, durable store, connector, or worker.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from ...sessions import (
    ALLOWED_SESSION_MINUTES,
    CANONICAL_MODELS,
    CANONICAL_REASONING_LEVELS,
    Clock,
    ModelAvailability,
    RequestPhase,
    SessionTransition,
    StatusView,
    TransitionKind,
    WorkingSession,
    cancel_active_request,
    ensure_utc,
    new_working_session,
    revoke_command_permissions,
    status_view,
)
from .admission import (  # noqa: F401
    _admit_orchestration_request,
    admit_orchestration_request,
)
from .parsing import (  # noqa: F401
    _PERSISTENT_PERMISSION_APPROVALS,
    _REJECTIONS,
    _SESSION_PERMISSION_APPROVALS,
    _THIS_TIME_APPROVALS,
    CONTROL_COMMANDS,
    ApprovalChoice,
    ControlCommand,
    ControlTransition,
    ControlTransitionKind,
    MessageKind,
    ParsedControl,
    _known_command,
    normalize,
    normalize_message,
    parse_approval_choice,
    parse_command,
    parse_control,
)
from .rendering import (  # noqa: F401
    _usage,
    render_permissions,
    render_status,
    safe_status,
)

_SESSION_MINUTE_TOKENS = frozenset(str(minutes) for minutes in ALLOWED_SESSION_MINUTES)
_MODEL_OPTIONS = ", ".join(CANONICAL_MODELS)
_REASONING_OPTIONS = ", ".join(CANONICAL_REASONING_LEVELS)


def _transition_from_session(
    parsed: ParsedControl,
    transition: SessionTransition,
    *,
    kind: ControlTransitionKind,
    reply: str | None,
    status: StatusView | None = None,
) -> ControlTransition:
    return ControlTransition(
        state=transition.state,
        parsed=parsed,
        kind=kind,
        reply=reply,
        status=status,
        effects=transition.effects,
        cancellation_token=transition.cancellation_token,
        reason=transition.reason,
    )


def _configuration_is_mutable(session: WorkingSession) -> bool:
    return session.active_request is None and session.pending_action is None


def _configuration_blocked(
    session: WorkingSession,
    parsed: ParsedControl,
) -> ControlTransition:
    return ControlTransition(
        state=session,
        parsed=parsed,
        kind=ControlTransitionKind.CONFIGURATION_BLOCKED,
        reply=(
            "Configuration cannot change while a request or approval is active. "
            "Cancel or finish it first."
        ),
        reason="model and configuration mutations require an idle working session",
    )


def _now(value: datetime_or_clock) -> datetime:
    current = (
        value.now()
        if hasattr(value, "now") and not isinstance(value, datetime)
        else value
    )
    return ensure_utc(current)


def _apply_control(
    session: WorkingSession,
    parsed: ParsedControl,
    *,
    now: datetime_or_clock,
    model_availability: ModelAvailability,
) -> ControlTransition:
    if parsed.command is ControlCommand.STATUS:
        view = status_view(session)
        return ControlTransition(
            state=session,
            parsed=parsed,
            kind=ControlTransitionKind.STATUS,
            reply=render_status(view),
            status=view,
        )

    if parsed.command is ControlCommand.CANCEL:
        transition = cancel_active_request(session, now=now)
        if transition.kind is TransitionKind.NOOP:
            return _transition_from_session(
                parsed,
                transition,
                kind=ControlTransitionKind.NOTHING_TO_CANCEL,
                reply="Nothing is active or pending.",
            )
        return _transition_from_session(
            parsed,
            transition,
            kind=ControlTransitionKind.CANCELLED,
            reply=(
                "Cancelled the active request and invalidated its pending action. "
                "No side effect will start."
            ),
        )

    if parsed.command is ControlCommand.NEW:
        transition = new_working_session(session, now=now)
        return _transition_from_session(
            parsed,
            transition,
            kind=ControlTransitionKind.NEW_SESSION,
            reply=(
                f"Started {transition.state.session_id} with persistent defaults. "
                "Previous work will not resume."
            ),
        )

    if parsed.command is ControlCommand.MODEL:
        if not parsed.args:
            return ControlTransition(
                state=session,
                parsed=parsed,
                kind=ControlTransitionKind.STATUS,
                reply=(f"Session model: {session.model}. Valid: {_MODEL_OPTIONS}."),
            )
        model = parsed.args[0]
        if model not in CANONICAL_MODELS:
            return ControlTransition(
                state=session,
                parsed=parsed,
                kind=ControlTransitionKind.INVALID_CONFIGURATION,
                reply="Invalid model. Use a canonical model value.",
            )
        if not _configuration_is_mutable(session):
            return _configuration_blocked(session, parsed)
        if model not in model_availability.available_models:
            return ControlTransition(
                state=session,
                parsed=parsed,
                kind=ControlTransitionKind.MODEL_UNAVAILABLE,
                reply=f"Model {model} is unavailable. Current session model remains {session.model}.",
            )
        return ControlTransition(
            state=replace(session, model=model),
            parsed=parsed,
            kind=ControlTransitionKind.SESSION_MODEL_UPDATED,
            reply=f"Session model set to {model}. Persistent default unchanged.",
            effects=("set_session_model",),
        )

    if parsed.command is ControlCommand.REASONING:
        if not parsed.args:
            return ControlTransition(
                state=session,
                parsed=parsed,
                kind=ControlTransitionKind.STATUS,
                reply=(
                    f"Session reasoning: {session.reasoning}. Valid: "
                    f"{_REASONING_OPTIONS}."
                ),
            )
        reasoning = parsed.args[0]
        if reasoning not in CANONICAL_REASONING_LEVELS:
            return ControlTransition(
                state=session,
                parsed=parsed,
                kind=ControlTransitionKind.INVALID_CONFIGURATION,
                reply="Invalid reasoning level. Use a canonical reasoning value.",
            )
        if not _configuration_is_mutable(session):
            return _configuration_blocked(session, parsed)
        if reasoning not in model_availability.available_reasoning_levels:
            return ControlTransition(
                state=session,
                parsed=parsed,
                kind=ControlTransitionKind.REASONING_UNAVAILABLE,
                reply=(
                    f"Reasoning level {reasoning} is unavailable. Current session reasoning "
                    f"remains {session.reasoning}."
                ),
            )
        return ControlTransition(
            state=replace(session, reasoning=reasoning),
            parsed=parsed,
            kind=ControlTransitionKind.SESSION_REASONING_UPDATED,
            reply=f"Session reasoning set to {reasoning}. Persistent default unchanged.",
            effects=("set_session_reasoning",),
        )

    if parsed.command is ControlCommand.CONFIG:
        if not parsed.args:
            return ControlTransition(
                state=session,
                parsed=parsed,
                kind=ControlTransitionKind.STATUS,
                reply=(
                    f"Persistent defaults: model {session.default_model}; reasoning "
                    f"{session.default_reasoning}; session-minutes "
                    f"{session.default_session_minutes}."
                ),
            )
        if not _configuration_is_mutable(session):
            return _configuration_blocked(session, parsed)
        key, value = parsed.args
        if key == "model":
            if value not in CANONICAL_MODELS:
                return ControlTransition(
                    state=session,
                    parsed=parsed,
                    kind=ControlTransitionKind.INVALID_CONFIGURATION,
                    reply="Invalid model. Use a canonical model value.",
                )
            if value not in model_availability.available_models:
                return ControlTransition(
                    state=session,
                    parsed=parsed,
                    kind=ControlTransitionKind.MODEL_UNAVAILABLE,
                    reply=(
                        f"Model {value} is unavailable. Persistent default remains "
                        f"{session.default_model}."
                    ),
                )
            return ControlTransition(
                state=replace(session, default_model=value),
                parsed=parsed,
                kind=ControlTransitionKind.CONFIG_UPDATED,
                reply=(
                    f"Persistent model default set to {value}; current session remains "
                    f"{session.model}."
                ),
                effects=("set_default_model",),
            )
        if key == "reasoning":
            if value not in CANONICAL_REASONING_LEVELS:
                return ControlTransition(
                    state=session,
                    parsed=parsed,
                    kind=ControlTransitionKind.INVALID_CONFIGURATION,
                    reply="Invalid reasoning level. Use a canonical reasoning value.",
                )
            if value not in model_availability.available_reasoning_levels:
                return ControlTransition(
                    state=session,
                    parsed=parsed,
                    kind=ControlTransitionKind.REASONING_UNAVAILABLE,
                    reply=(
                        f"Reasoning level {value} is unavailable. Persistent default remains "
                        f"{session.default_reasoning}."
                    ),
                )
            return ControlTransition(
                state=replace(session, default_reasoning=value),
                parsed=parsed,
                kind=ControlTransitionKind.CONFIG_UPDATED,
                reply=(
                    f"Persistent reasoning default set to {value}; current session remains "
                    f"{session.reasoning}."
                ),
                effects=("set_default_reasoning",),
            )
        if key == "session-minutes" and value in _SESSION_MINUTE_TOKENS:
            minutes = int(value)
            current = _now(now)
            return ControlTransition(
                state=replace(
                    session,
                    session_minutes=minutes,
                    default_session_minutes=minutes,
                    last_activity_at=current,
                    inactivity_anchor_at=current,
                ),
                parsed=parsed,
                kind=ControlTransitionKind.CONFIG_UPDATED,
                reply=(
                    f"Working-session inactivity boundary set to {minutes} minutes, "
                    "effective now and for future sessions."
                ),
                effects=("set_session_minutes",),
            )
        return ControlTransition(
            state=session,
            parsed=parsed,
            kind=ControlTransitionKind.INVALID_CONFIGURATION,
            reply=(
                "Invalid config value. Use canonical models/reasoning or session-minutes "
                "15, 30, 60, 120, or 240."
            ),
        )

    if parsed.command is ControlCommand.PERMISSIONS:
        return ControlTransition(
            state=session,
            parsed=parsed,
            kind=ControlTransitionKind.PERMISSIONS_LISTED,
            reply=render_permissions(session),
        )

    if parsed.command is ControlCommand.REVOKE:
        selector = parsed.args[0]
        transition = revoke_command_permissions(session, selector=selector, now=now)
        if transition.kind is TransitionKind.NOOP:
            return _transition_from_session(
                parsed,
                transition,
                kind=ControlTransitionKind.PERMISSION_NOT_ACTIVE,
                reply="No matching command permissions are active.",
            )
        revoked_count = sum(
            before.is_active and not after.is_active
            for before, after in zip(session.permissions, transition.state.permissions)
        )
        return _transition_from_session(
            parsed,
            transition,
            kind=ControlTransitionKind.PERMISSION_REVOKED,
            reply=f"Revoked {revoked_count} command permission(s) matching {selector}.",
        )

    if parsed.command is ControlCommand.HISTORY:
        # The broker owns the authorized store read and outbound delivery;
        # this pure grammar only reserves the exact command shape.
        return ControlTransition(
            state=session,
            parsed=parsed,
            kind=ControlTransitionKind.HISTORY_REQUEST,
        )

    raise AssertionError("_apply_control called without a complete control command")


def _apply_message(
    session: WorkingSession,
    message: str,
    *,
    now: datetime_or_clock,
    request_id: str | None,
    originating_message_id: str | None,
    phase: RequestPhase | str,
    model_availability: ModelAvailability,
) -> ControlTransition:
    parsed = parse_control(message)

    # Slash commands are handled before pending-action or busy checks.  This
    # is the explicit precedence boundary that keeps /status, /cancel, and
    # /new available while work is active.
    if parsed.kind is MessageKind.CONTROL_COMMAND:
        return _apply_control(
            session,
            parsed,
            now=now,
            model_availability=model_availability,
        )
    if parsed.kind in {MessageKind.MALFORMED_COMMAND, MessageKind.UNKNOWN_COMMAND}:
        return ControlTransition(
            state=session,
            parsed=parsed,
            kind=(
                ControlTransitionKind.MALFORMED_COMMAND
                if parsed.kind is MessageKind.MALFORMED_COMMAND
                else ControlTransitionKind.UNKNOWN_COMMAND
            ),
            reply=_usage(parsed),
            reason="slash commands are exact whole-message controls",
        )
    if parsed.kind is MessageKind.EMPTY:
        return ControlTransition(
            state=session,
            parsed=parsed,
            kind=ControlTransitionKind.EMPTY,
            reply="Empty messages have no effect.",
        )

    return _admit_orchestration_request(
        session,
        parsed,
        now=now,
        request_id=request_id,
        originating_message_id=originating_message_id,
        phase=phase,
        model_availability=model_availability,
    )


# Keep the type alias local so public call signatures can accept either a
# controlled datetime or a Clock without importing a runtime implementation.
datetime_or_clock = datetime | Clock


def handle_message(
    session: WorkingSession,
    message: str,
    *,
    now: datetime_or_clock,
    request_id: str | None = None,
    originating_message_id: str | None = None,
    phase: RequestPhase | str = RequestPhase.INTERPRETING,
    model_availability: ModelAvailability | None = None,
) -> ControlTransition:
    """Parse and reduce one authorized operator message purely."""

    return _apply_message(
        session,
        message,
        now=now,
        request_id=request_id,
        originating_message_id=originating_message_id,
        phase=phase,
        model_availability=model_availability or ModelAvailability(),
    )


apply_message = handle_message
reduce_message = handle_message
handle_operator_message = handle_message

ControlState = WorkingSession
State = WorkingSession
Transition = ControlTransition

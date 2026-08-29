from __future__ import annotations

# ruff: noqa: F401, I001, RUF100 -- split modules retain shared imports.

from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from threading import Event, Thread

import pytest
from test_support import build_receiver_components

from jarvis_control_plane import (
    ControlledOrchestrationAdapter,
    InboundMessage,
    SignedInboundEvent,
)
from jarvis_control_plane.control_grammar import (
    ControlCommand,
    ControlTransitionKind,
    MessageKind,
    handle_message,
    normalize_message,
    parse_control,
    render_status,
)
from jarvis_control_plane.sessions import (
    ALLOWED_SESSION_MINUTES,
    ActiveRequestState,
    CommandPermissionComponent,
    CommandPermissionIdentity,
    CommandPermissionState,
    DurableStateReferences,
    HistoryEntry,
    PendingActionState,
    PermissionLifetime,
    ReadinessState,
    RequestPhase,
    RequestResult,
    ServiceReadiness,
    SessionConfig,
    SessionLifecycle,
    SessionStoreError,
    SQLiteWorkingSessionStore,
    TransitionKind,
    WorkingSession,
    accept_request,
    apply_request_result,
    cancel_active_request,
    cancellation_token_is_current,
    create_working_session,
    expire_inactive_session,
    expire_pending_action,
    install_pending_action,
    interrupt_for_restart,
    is_session_inactive,
    new_working_session,
    session_inactivity_suspended,
    status_view,
)

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
SECRET = b"ticket05-test-secret"
OPERATOR = "operator.test"
TRANSPORT_SESSION = "session.test"


def make_receiver_components(
    *, orchestration: ControlledOrchestrationAdapter | None = None
):
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=SECRET,
        now=NOW,
        id_prefix="ticket05-receiver",
        orchestration=orchestration,
    )
    assert components.trace_store is not None
    return (
        components.state,
        components.audit,
        components.orchestration,
        components.outbound,
        components.broker,
        components.receiver,
        components.trace_store,
    )


def make_signed_event(
    text: str, *, event_id: str, message_id: str
) -> SignedInboundEvent:
    return SignedInboundEvent.from_message(
        InboundMessage(
            event_type="message.received",
            session_id=TRANSPORT_SESSION,
            event_id=event_id,
            message_id=message_id,
            sender_id=OPERATOR,
            chat_id=OPERATOR,
            chat_type="direct",
            message_type="text",
            from_me=False,
            text=text,
        ),
        SECRET,
    )


def make_session(*, config: SessionConfig | None = None) -> WorkingSession:
    return create_working_session(
        "operator.test",
        NOW,
        config=config,
        durable_refs=DurableStateReferences(
            operational_state_ref="state.sqlite",
            conversation_store_ref="conversation.sqlite",
            durable_memory_ref="memory.sqlite",
            audit_ref="audit.sqlite",
        ),
        readiness=ReadinessState(
            ubuntu="ready",
            windows="unavailable",
            openwa="ready",
            connected_services=(
                # The values are deliberately safe readiness labels only.
                ServiceReadiness("google", "unknown"),
            ),
        ),
    )


def make_session_permission(session: WorkingSession) -> CommandPermissionState:
    return CommandPermissionState(
        permission_id="P-session",
        lifetime=PermissionLifetime.SESSION,
        identity=CommandPermissionIdentity(
            host="ubuntu",
            cwd="/tmp",
            components=(
                CommandPermissionComponent(
                    executable="/usr/bin/cat", arguments=("/tmp/example",)
                ),
            ),
        ),
        created_at=NOW,
        session_id=session.session_id,
    )


def make_persistent_permission() -> CommandPermissionState:
    return CommandPermissionState(
        permission_id="P-persistent",
        lifetime=PermissionLifetime.PERSISTENT,
        identity=CommandPermissionIdentity(
            host="ubuntu",
            cwd="/workspace",
            components=(
                CommandPermissionComponent(
                    executable="/usr/bin/git", arguments=("status",)
                ),
            ),
        ),
        created_at=NOW,
    )


def make_pending(session: WorkingSession, request_id: str) -> PendingActionState:
    return PendingActionState.create(
        action_id="A-001",
        session_id=session.session_id,
        request_id=request_id,
        kind="placeholder",
        summary="A safe bounded placeholder summary",
        created_at=NOW,
    )


def test_restart_revokes_idle_session_permissions() -> None:
    session = make_session()
    session = replace(
        session,
        permissions=(make_session_permission(session), make_persistent_permission()),
    )

    transition = interrupt_for_restart(session, now=NOW + timedelta(minutes=1))

    assert transition.kind is TransitionKind.RESTART_INTERRUPTED
    assert transition.state.permissions == (make_persistent_permission(),)
    assert "revoke_session_permissions" in transition.effects


def test_normalization_is_exact_and_slash_commands_have_precedence() -> None:
    assert normalize_message("  /STATUS\t  ") == "/status"

    status = parse_control("  /STATUS\t  ")
    assert status.kind is MessageKind.CONTROL_COMMAND
    assert status.command is ControlCommand.STATUS
    assert status.args == ()

    for value in ("/status now", "/cancel please", "/new extra"):
        parsed = parse_control(value)
        assert parsed.kind is MessageKind.MALFORMED_COMMAND
        assert parsed.command is not None

    assert parse_control("yes").kind is MessageKind.ORDINARY
    assert parse_control("some text /status").kind is MessageKind.ORDINARY
    assert parse_control("/unknown").kind is MessageKind.UNKNOWN_COMMAND


def test_working_session_is_distinct_from_gateway_session_and_has_typed_placeholders() -> (
    None
):
    session = make_session()

    assert isinstance(session, WorkingSession)
    assert session.session_id == "S-001"
    assert session.conversation_ref == "conversation:S-001"
    assert session.readiness.ubuntu == "ready"
    assert session.readiness.openwa == "ready"
    assert session.readiness.connected_services[0].service_id == "google"
    assert session.active_request is None
    assert session.pending_action is None
    assert session.permissions == ()


def test_one_active_request_and_busy_refusal_never_create_a_queue() -> None:
    session = make_session()
    first = accept_request(
        session,
        now=NOW,
        request_id="R-001",
        originating_message_id="message-001",
    )
    assert first.kind is TransitionKind.REQUEST_ACCEPTED
    assert first.cancellation_token is not None

    busy = handle_message(
        first.state, "A second ordinary request", now=NOW + timedelta(minutes=1)
    )
    assert busy.kind is ControlTransitionKind.BUSY_REFUSED
    assert busy.state == first.state
    assert busy.queued is False
    assert busy.effects == ("request_refused_busy",)
    assert busy.reply == (
        "Request R-001 is still active. Use /status or /cancel; V1 does not queue another request."
    )


def test_pending_action_is_single_owner_and_blocks_ordinary_text() -> None:
    session = make_session()
    accepted = accept_request(session, now=NOW, request_id="R-001")
    pending = install_pending_action(
        accepted.state,
        make_pending(accepted.state, "R-001"),
        now=NOW,
    )
    assert pending.kind is TransitionKind.PENDING_INSTALLED
    assert pending.state.active_request is not None
    assert pending.state.active_request.phase is RequestPhase.AWAITING_APPROVAL

    blocked = handle_message(pending.state, "1", now=NOW + timedelta(minutes=1))
    assert blocked.kind is ControlTransitionKind.PENDING_BLOCKED
    assert blocked.state == pending.state
    assert blocked.queued is False

    with pytest.raises(ValueError, match="only one pending action"):
        install_pending_action(
            pending.state,
            make_pending(pending.state, "R-001"),
            now=NOW + timedelta(minutes=1),
        )


def test_status_is_safe_and_contains_only_configured_fields() -> None:
    session = make_session()
    accepted = accept_request(
        session,
        now=NOW,
        request_id="R-001",
        execution_host="ubuntu",
        phase=RequestPhase.PROCESSING,
    )
    pending = install_pending_action(
        accepted.state,
        make_pending(accepted.state, "R-001"),
        now=NOW,
    )
    with_permission = replace(
        pending.state,
        permissions=(make_session_permission(pending.state),),
    )

    view = status_view(with_permission)
    assert view.session_id == "S-001"
    assert view.session_minutes == 60
    assert view.active_request is not None
    assert view.active_request.request_id == "R-001"
    assert view.pending_action is not None
    assert view.pending_action.summary == "A safe bounded placeholder summary"
    assert view.permission_count == 1
    assert view.readiness.openwa == "ready"

    safe_payload = asdict(view)
    rendered = render_status(view)
    for forbidden in ("credential", "raw_payload", "terminal_output", "request_text"):
        assert forbidden not in str(safe_payload).lower()
        assert forbidden not in rendered.lower()
    assert "cat /tmp/example" not in rendered


def test_cancel_is_atomic_invalidates_pending_and_advances_generation() -> None:
    session = make_session()
    accepted = accept_request(session, now=NOW, request_id="R-001")
    token = accepted.cancellation_token
    assert token is not None
    pending = install_pending_action(
        accepted.state,
        make_pending(accepted.state, "R-001"),
        now=NOW,
    )
    cancelled = cancel_active_request(pending.state, now=NOW + timedelta(minutes=1))
    assert cancelled.kind is TransitionKind.CANCELLED
    assert cancelled.state.active_request is None
    assert cancelled.state.pending_action is None
    assert cancelled.state.permissions == ()
    assert cancelled.state.cancellation_generation == 1
    assert "invalidate_pending_action" in cancelled.effects
    assert cancellation_token_is_current(cancelled.state, token) is False

    late = apply_request_result(
        cancelled.state,
        token,
        RequestResult("R-001", 0, "completed"),
        now=NOW + timedelta(minutes=2),
    )
    assert late.kind is TransitionKind.LATE_RESULT_IGNORED
    assert late.state == cancelled.state
    assert late.effects == ("late_result_ignored",)


def test_new_creates_clean_session_reusing_durable_refs_and_persistent_permissions() -> (
    None
):
    original = make_session()
    with_permissions = replace(
        original,
        permissions=(make_session_permission(original), make_persistent_permission()),
        model="gpt-5.6-sol",
        reasoning="high",
    )
    accepted = accept_request(with_permissions, now=NOW, request_id="R-001")
    pending = install_pending_action(
        accepted.state,
        make_pending(accepted.state, "R-001"),
        now=NOW,
    )

    fresh = new_working_session(pending.state, now=NOW + timedelta(minutes=1))
    assert fresh.kind is TransitionKind.NEW_SESSION
    assert fresh.state.session_id == "S-002"
    assert fresh.state.conversation_ref == "conversation:S-002"
    assert fresh.state.durable_refs is pending.state.durable_refs
    assert fresh.state.model == "gpt-5.6-luna"
    assert fresh.state.reasoning == "medium"
    assert fresh.state.active_request is None
    assert fresh.state.pending_action is None
    assert [p.permission_id for p in fresh.state.permissions] == ["P-persistent"]
    assert (
        fresh.state.cancellation_generation == pending.state.cancellation_generation + 1
    )
    assert "revoke_session_permissions" in fresh.effects


def test_cancel_preserves_working_session_and_permissions_while_new_revokes_session_scope() -> (
    None
):
    session = make_session()
    with_permissions = replace(
        session,
        permissions=(make_session_permission(session), make_persistent_permission()),
    )
    accepted = accept_request(with_permissions, now=NOW, request_id="R-001")
    cancelled = cancel_active_request(accepted.state, now=NOW + timedelta(minutes=1))
    assert cancelled.state.session_id == with_permissions.session_id
    assert cancelled.state.conversation_ref == with_permissions.conversation_ref
    assert len(cancelled.state.permissions) == 2

    fresh = new_working_session(cancelled.state, now=NOW + timedelta(minutes=2))
    assert [p.lifetime for p in fresh.state.permissions] == [
        PermissionLifetime.PERSISTENT
    ]


@pytest.mark.parametrize("minutes", ALLOWED_SESSION_MINUTES)
def test_allowed_session_minute_values_are_accepted(minutes: int) -> None:
    config = SessionConfig(operator_id="operator.test", inactivity_minutes=minutes)
    session = make_session(config=config)
    assert session.session_minutes == minutes


@pytest.mark.parametrize("minutes", (0, 1, 14, 16, 59, 61, 241, 60.0, True))
def test_disallowed_session_minute_values_are_rejected(minutes: object) -> None:
    with pytest.raises(ValueError, match="inactivity_minutes"):
        SessionConfig(operator_id="operator.test", inactivity_minutes=minutes)  # type: ignore[arg-type]


def test_inactivity_boundary_is_inclusive_and_processing_suspends_it() -> None:
    session = make_session()
    assert (
        is_session_inactive(session, NOW + timedelta(minutes=59, seconds=59)) is False
    )
    assert is_session_inactive(session, NOW + timedelta(minutes=60)) is True

    accepted = accept_request(
        session,
        now=NOW,
        request_id="R-001",
        phase=RequestPhase.PROCESSING,
    )
    assert session_inactivity_suspended(accepted.state) is True
    assert is_session_inactive(accepted.state, NOW + timedelta(hours=24)) is False

    pending = install_pending_action(
        accepted.state,
        make_pending(accepted.state, "R-001"),
        now=NOW,
    )
    assert session_inactivity_suspended(pending.state) is False
    assert is_session_inactive(pending.state, NOW + timedelta(minutes=60)) is True


def test_inactivity_expiry_cancels_live_work_and_starts_a_clean_session() -> None:
    session = make_session()
    accepted = accept_request(
        session, now=NOW, request_id="R-001", phase=RequestPhase.AWAITING_APPROVAL
    )
    pending = install_pending_action(
        accepted.state,
        make_pending(accepted.state, "R-001"),
        now=NOW,
    )
    expired = expire_inactive_session(pending.state, now=NOW + timedelta(minutes=60))
    assert expired.kind is TransitionKind.SESSION_EXPIRED
    assert expired.state.session_id == "S-002"
    assert expired.state.active_request is None
    assert expired.state.pending_action is None
    assert "session_inactivity_expired" in expired.effects


def test_pending_action_has_independent_ten_minute_expiry() -> None:
    session = make_session()
    accepted = accept_request(session, now=NOW, request_id="R-001")
    pending = install_pending_action(
        accepted.state,
        make_pending(accepted.state, "R-001"),
        now=NOW,
    )
    assert pending.state.pending_action is not None
    assert (
        pending.state.pending_action.is_expired(NOW + timedelta(minutes=9, seconds=59))
        is False
    )
    assert pending.state.pending_action.is_expired(NOW + timedelta(minutes=10)) is True

    expired = expire_pending_action(pending.state, now=NOW + timedelta(minutes=10))
    assert expired.kind is TransitionKind.PENDING_EXPIRED
    assert expired.state.active_request is None
    assert expired.state.pending_action is None

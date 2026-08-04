from __future__ import annotations

from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import pytest

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
    is_session_inactive,
    new_working_session,
    session_inactivity_suspended,
    status_view,
)

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


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
        host="ubuntu",
        command="cat /tmp/example",
        cwd="/tmp",
        created_at=NOW,
        session_id=session.session_id,
    )


def make_persistent_permission() -> CommandPermissionState:
    return CommandPermissionState(
        permission_id="P-persistent",
        lifetime=PermissionLifetime.PERSISTENT,
        host="ubuntu",
        command="git status",
        cwd="/workspace",
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
    assert fresh.state.model == "gpt-5.6-terra"
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


def test_valid_result_applies_once_and_invalidates_its_own_token() -> None:
    session = make_session()
    accepted = accept_request(session, now=NOW, request_id="R-001")
    token = accepted.cancellation_token
    assert token is not None
    result = apply_request_result(
        accepted.state,
        token,
        RequestResult("R-001", 0, "completed"),
        now=NOW + timedelta(minutes=1),
    )
    assert result.kind is TransitionKind.RESULT_APPLIED
    assert result.state.active_request is None
    assert result.state.last_request_outcome == "completed"
    assert result.state.cancellation_generation == 1

    replay = apply_request_result(
        result.state,
        token,
        RequestResult("R-001", 0, "completed"),
        now=NOW + timedelta(minutes=2),
    )
    assert replay.kind is TransitionKind.LATE_RESULT_IGNORED
    assert replay.state == result.state


def test_command_precedence_keeps_status_available_and_commands_are_exact() -> None:
    session = make_session()
    accepted = handle_message(session, "ordinary request", now=NOW, request_id="R-001")
    assert accepted.kind is ControlTransitionKind.REQUEST_ACCEPTED

    status = handle_message(
        accepted.state, "  /STATUS  ", now=NOW + timedelta(minutes=1)
    )
    assert status.kind is ControlTransitionKind.STATUS
    assert status.status is not None
    assert status.state == accepted.state

    malformed = handle_message(
        accepted.state, "/status now", now=NOW + timedelta(minutes=1)
    )
    assert malformed.kind is ControlTransitionKind.MALFORMED_COMMAND
    assert malformed.state == accepted.state

    cancelled = handle_message(
        accepted.state, "/cancel", now=NOW + timedelta(minutes=1)
    )
    assert cancelled.kind is ControlTransitionKind.CANCELLED
    assert cancelled.state.active_request is None

    fresh = handle_message(accepted.state, "/new", now=NOW + timedelta(minutes=1))
    assert fresh.kind is ControlTransitionKind.NEW_SESSION
    assert fresh.state.session_id == "S-002"


def test_late_result_from_previous_session_cannot_escape_new_boundary() -> None:
    session = make_session()
    accepted = accept_request(session, now=NOW, request_id="R-001")
    token = accepted.cancellation_token
    assert token is not None
    fresh = new_working_session(accepted.state, now=NOW + timedelta(minutes=1))
    assert token.matches(fresh.state) is False
    late = apply_request_result(
        fresh.state,
        token,
        RequestResult("R-001", 0, "completed"),
        now=NOW + timedelta(minutes=2),
    )
    assert late.kind is TransitionKind.LATE_RESULT_IGNORED
    assert late.state == fresh.state


def test_session_config_and_state_reject_inconsistent_operator_or_phase() -> None:
    with pytest.raises(ValueError, match="operator"):
        create_working_session(
            "operator.test",
            NOW,
            config=SessionConfig(operator_id="different.operator"),
        )

    request = ActiveRequestState(
        request_id="R-001",
        session_id="S-001",
        generation=0,
        phase=RequestPhase.PROCESSING,
        created_at=NOW,
        updated_at=NOW,
    )
    with pytest.raises(ValueError):
        # The constructor validates the enum rather than accepting an
        # unbounded later-ticket lifecycle value.
        ActiveRequestState(
            request_id=request.request_id,
            session_id=request.session_id,
            generation=request.generation,
            phase="not-a-phase",
            created_at=NOW,
            updated_at=NOW,
        )


def test_session_lifecycle_cannot_retain_live_work() -> None:
    with pytest.raises(ValueError):
        replace(
            make_session(),
            lifecycle=SessionLifecycle.EXPIRED,
            active_request=ActiveRequestState(
                request_id="R-001",
                session_id="S-001",
                generation=0,
                phase=RequestPhase.PROCESSING,
                created_at=NOW,
                updated_at=NOW,
            ),
        )


def test_sqlite_store_round_trips_full_state_and_history(tmp_path) -> None:
    database = tmp_path / "sessions.sqlite3"
    original = make_session()
    accepted = accept_request(
        original,
        now=NOW,
        request_id="R-001",
        originating_message_id="message-001",
    )
    entry = HistoryEntry(
        session_id=original.session_id,
        message_id="message-001",
        direction="inbound",
        body="An authorized message is retained in history.",
        occurred_at=NOW,
        request_id="R-001",
    )

    store = SQLiteWorkingSessionStore(database)
    try:
        store.create(original)
        store.compare_and_set(original, accepted.state, history=(entry,))
        assert store.load() == accepted.state
        assert store.list_history() == (entry,)
    finally:
        store.close()

    reconstructed = SQLiteWorkingSessionStore(database)
    try:
        assert reconstructed.load() == accepted.state
        assert reconstructed.list_history(original.session_id) == (entry,)
    finally:
        reconstructed.close()


def test_sqlite_store_rejects_stale_complete_state_transition(tmp_path) -> None:
    store = SQLiteWorkingSessionStore(tmp_path / "sessions.sqlite3")
    original = make_session()
    accepted = accept_request(original, now=NOW, request_id="R-001")
    cancelled = cancel_active_request(accepted.state, now=NOW + timedelta(minutes=1))
    try:
        store.create(original)
        store.compare_and_set(original, accepted.state)

        with pytest.raises(SessionStoreError, match="stale working-session transition"):
            store.compare_and_set(original, cancelled.state)

        assert store.load() == accepted.state
    finally:
        store.close()

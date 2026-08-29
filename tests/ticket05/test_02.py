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


def test_signed_receiver_routes_status_and_new_through_working_session() -> None:
    state, _, orchestration, outbound, broker, receiver, trace_store = (
        make_receiver_components()
    )
    try:
        original_session_id = broker.current_working_session_id
        status = receiver.receive(
            make_signed_event(
                " /STATUS ", event_id="event-status", message_id="m-status"
            )
        )
        assert status.disposition == "status", status.reason
        assert status.reply is not None
        assert f"Session {original_session_id}" in status.reply.body
        assert orchestration.calls == []

        replaced = receiver.receive(
            make_signed_event("/new", event_id="event-new", message_id="m-new")
        )
        assert replaced.disposition == "new_session", replaced.reason
        assert broker.current_working_session_id != original_session_id
        assert orchestration.calls == []
        assert len(outbound.sent) == 2

        completed = receiver.receive(
            make_signed_event(
                "Start clean work", event_id="event-work", message_id="m-work"
            )
        )
        assert completed.disposition == "completed"
        history = state.list_conversation_messages()
        assert len(history) == 6
        assert (
            sum(entry.working_session_id == original_session_id for entry in history)
            == 3
        )
        assert (
            sum(
                entry.working_session_id == broker.current_working_session_id
                for entry in history
            )
            == 3
        )
        assert [entry.direction for entry in history].count("inbound") == 3
        assert [entry.direction for entry in history].count("outbound") == 3
        assert {entry.text for entry in history if entry.direction == "outbound"} == {
            reply.body for reply in outbound.sent
        }
    finally:
        trace_store._close_writer_service()


def test_cancel_wins_race_and_late_result_never_dispatches() -> None:
    orchestration_started = Event()
    release_orchestration = Event()

    def blocked_response(_: object) -> str:
        orchestration_started.set()
        assert release_orchestration.wait(timeout=5)
        return "This late result must not be sent."

    class _CancellableOrchestration(ControlledOrchestrationAdapter):
        def __init__(self) -> None:
            super().__init__(response_factory=blocked_response)
            self.cancelled_requests: list[str] = []

        def cancel(self, *, request_id: str) -> bool:
            self.cancelled_requests.append(request_id)
            release_orchestration.set()
            return True

    orchestration = _CancellableOrchestration()
    _, audit, _, outbound, broker, receiver, trace_store = make_receiver_components(
        orchestration=orchestration
    )
    result_holder: list[object] = []

    def run_request() -> None:
        result_holder.append(
            receiver.receive(
                make_signed_event(
                    "Long running work",
                    event_id="event-work",
                    message_id="m-work",
                )
            )
        )

    worker = Thread(target=run_request)
    worker.start()
    try:
        assert orchestration_started.wait(timeout=5)
        active = broker.working_sessions.load()
        assert active is not None and active.active_request is not None

        busy = receiver.receive(
            make_signed_event(
                "Second request", event_id="event-busy", message_id="m-busy"
            )
        )
        assert busy.disposition == "busy_refused", busy.reason
        assert len(orchestration.calls) == 1

        cancelled = receiver.receive(
            make_signed_event("/cancel", event_id="event-cancel", message_id="m-cancel")
        )
        assert cancelled.disposition == "cancelled"
        assert orchestration.cancelled_requests == [active.active_request.request_id]
        current = broker.working_sessions.load()
        assert current is not None and current.active_request is None

        release_orchestration.set()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert result_holder[0].disposition == "late_result_ignored"  # type: ignore[attr-defined]
        assert all("late result" not in reply.body.lower() for reply in outbound.sent)
        assert "late_result_ignored" in [record.kind for record in audit.records]
    finally:
        release_orchestration.set()
        worker.join(timeout=5)
        trace_store._close_writer_service()

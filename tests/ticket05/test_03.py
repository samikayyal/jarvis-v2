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


def test_cancel_during_outbound_preflight_prevents_dispatch() -> None:
    preflight_started = Event()
    release_preflight = Event()
    state, _, _, outbound, broker, receiver, trace_store = make_receiver_components()
    original_preflight = outbound.preflight

    def blocking_preflight(reply: object) -> None:
        original_preflight(reply)  # type: ignore[arg-type]
        if "Controlled orchestration completed" in reply.body:  # type: ignore[attr-defined]
            preflight_started.set()
            assert release_preflight.wait(timeout=5)

    outbound.preflight = blocking_preflight  # type: ignore[method-assign]
    result_holder: list[object] = []

    worker = Thread(
        target=lambda: result_holder.append(
            receiver.receive(
                make_signed_event(
                    "Work that reaches outbound preflight",
                    event_id="event-preflight-work",
                    message_id="m-preflight-work",
                )
            )
        )
    )
    worker.start()
    try:
        assert preflight_started.wait(timeout=5)
        active = broker.working_sessions.load()
        assert active is not None and active.active_request is not None

        cancelled = receiver.receive(
            make_signed_event(
                "/cancel",
                event_id="event-preflight-cancel",
                message_id="m-preflight-cancel",
            )
        )
        assert cancelled.disposition == "cancelled"

        release_preflight.set()
        worker.join(timeout=5)
        assert not worker.is_alive()
        result = result_holder[0]
        assert result.disposition == "late_result_ignored"  # type: ignore[attr-defined]
        assert all(
            "Controlled orchestration completed" not in reply.body
            for reply in outbound.sent
        )
        assert state.list_requests()[0].outcome == "late_result_ignored"
    finally:
        release_preflight.set()
        worker.join(timeout=5)
        trace_store._close_writer_service()

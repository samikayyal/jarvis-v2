"""Ticket 06 configuration and runtime-availability contract tests."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from test_support import build_receiver_components

from jarvis_control_plane import (
    ControlledOrchestrationAdapter,
    InboundMessage,
    ModelAvailability,
    SignedInboundEvent,
)
from jarvis_control_plane.control_grammar import (
    ControlTransitionKind,
    handle_message,
    parse_control,
)
from jarvis_control_plane.sessions import (
    SessionConfig,
    SessionStoreError,
    SQLiteWorkingSessionStore,
    create_working_session,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
SECRET = b"ticket06-test-secret"
OPERATOR = "operator.test"
TRANSPORT_SESSION = "session.test"


def make_receiver_components(
    *,
    availability: ModelAvailability | None = None,
    orchestration: ControlledOrchestrationAdapter | None = None,
):
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=SECRET,
        now=NOW,
        id_prefix="ticket06-receiver",
        availability=availability,
        orchestration=orchestration,
    )
    assert components.trace_store is not None
    return (
        components.state,
        components.audit,
        components.clock,
        components.provider,
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


def make_session():
    return create_working_session("operator.test", NOW)


def apply(session, message: str, *, availability: ModelAvailability | None = None):
    return handle_message(
        session,
        message,
        now=NOW,
        model_availability=availability,
    )


def test_session_and_persistent_configuration_have_distinct_lifetimes() -> None:
    session = make_session()

    assert parse_control(" /MODEL gpt-5.6-sol ").is_command
    model = apply(session, "/model gpt-5.6-sol")
    assert model.kind is ControlTransitionKind.SESSION_MODEL_UPDATED
    assert model.state.model == "gpt-5.6-sol"
    assert model.state.default_model == "gpt-5.6-terra"

    reasoning = apply(model.state, "/reasoning high")
    assert reasoning.kind is ControlTransitionKind.SESSION_REASONING_UPDATED
    assert reasoning.state.reasoning == "high"
    assert reasoning.state.default_reasoning == "medium"

    default_model = apply(reasoning.state, "/config model gpt-5.6-luna")
    assert default_model.kind is ControlTransitionKind.CONFIG_UPDATED
    assert default_model.state.model == "gpt-5.6-sol"
    assert default_model.state.default_model == "gpt-5.6-luna"

    default_reasoning = apply(default_model.state, "/config reasoning max")
    duration = apply(
        default_reasoning.state,
        "/config session-minutes 120",
    )
    assert duration.kind is ControlTransitionKind.CONFIG_UPDATED
    assert duration.state.session_minutes == 120
    assert duration.state.default_session_minutes == 120
    assert duration.state.inactivity_anchor_at == NOW

    fresh = apply(duration.state, "/new")
    assert fresh.state.model == "gpt-5.6-luna"
    assert fresh.state.reasoning == "max"
    assert fresh.state.session_minutes == 120


@pytest.mark.parametrize(
    "command",
    (
        "/model gpt-4.1",
        "/reasoning ultra",
        "/config model gpt-4.1",
        "/config reasoning ultra",
        "/config session-minutes 90",
        "/config session-minutes 060",
        "/config other gpt-5.6-sol",
    ),
)
def test_only_canonical_configuration_values_are_accepted(command: str) -> None:
    session = make_session()

    transition = apply(session, command)

    assert transition.kind is ControlTransitionKind.INVALID_CONFIGURATION
    assert transition.state == session


@pytest.mark.parametrize(
    "command",
    (
        "/model gpt-5.6-sol extra",
        "/reasoning high extra",
        "/config model",
    ),
)
def test_configuration_commands_require_their_exact_form(command: str) -> None:
    session = make_session()

    transition = apply(session, command)

    assert transition.kind is ControlTransitionKind.MALFORMED_COMMAND
    assert transition.state == session


@pytest.mark.parametrize(
    "command",
    (
        "/model gpt-5.6-sol",
        "/reasoning high",
        "/config model gpt-5.6-sol",
        "/config reasoning high",
        "/config session-minutes 30",
    ),
)
def test_mutating_configuration_is_refused_during_active_work(command: str) -> None:
    session = make_session()
    active = apply(session, "ordinary request").state

    transition = apply(active, command)

    assert transition.kind is ControlTransitionKind.CONFIGURATION_BLOCKED
    assert transition.state == active


def test_unavailable_runtime_choices_fail_closed_without_substitution() -> None:
    session = make_session()
    availability = ModelAvailability(
        available_models=("gpt-5.6-terra",),
        available_reasoning_levels=("medium",),
    )

    unavailable_model = apply(session, "/model gpt-5.6-sol", availability=availability)
    unavailable_reasoning = apply(session, "/reasoning high", availability=availability)
    unavailable_default = apply(
        session,
        "/config model gpt-5.6-luna",
        availability=availability,
    )
    assert unavailable_model.kind is ControlTransitionKind.MODEL_UNAVAILABLE
    assert unavailable_reasoning.kind is ControlTransitionKind.REASONING_UNAVAILABLE
    assert unavailable_default.kind is ControlTransitionKind.MODEL_UNAVAILABLE
    assert unavailable_model.state == session
    assert unavailable_reasoning.state == session
    assert unavailable_default.state == session

    selected_then_unavailable = replace(session, model="gpt-5.6-sol")
    request = apply(
        selected_then_unavailable,
        "read my calendar",
        availability=availability,
    )
    assert request.kind is ControlTransitionKind.MODEL_UNAVAILABLE
    assert request.state == selected_then_unavailable
    assert request.state.active_request is None


def test_configuration_is_persisted_and_used_after_sqlite_restart(tmp_path) -> None:
    store = SQLiteWorkingSessionStore(tmp_path / "working-session.sqlite")
    original = create_working_session(
        "operator.test",
        NOW,
        config=SessionConfig(operator_id="operator.test"),
    )
    store.create(original)

    changed_model = apply(original, "/config model gpt-5.6-sol")
    changed_duration = apply(
        changed_model.state,
        "/config session-minutes 30",
    )
    store.compare_and_set(original, changed_duration.state)
    store.close()

    reopened = SQLiteWorkingSessionStore(tmp_path / "working-session.sqlite")
    restored = reopened.load()
    assert restored is not None
    assert restored.default_model == "gpt-5.6-sol"
    assert restored.default_session_minutes == 30

    fresh = apply(restored, "/new")
    assert fresh.state.model == "gpt-5.6-sol"
    assert fresh.state.session_minutes == 30
    reopened.close()


def test_invalid_persisted_configuration_fails_closed(tmp_path) -> None:
    store = SQLiteWorkingSessionStore(tmp_path / "working-session.sqlite")
    session = make_session()
    store.create(session)
    store.connection.execute(
        'UPDATE working_session_current SET payload = replace(payload, \'"model":"gpt-5.6-terra"\', \'"model":"gpt-4.1"\')'
    )
    store.connection.commit()

    with pytest.raises(SessionStoreError, match="persisted working session is invalid"):
        store.load()
    store.close()


def test_ticket05_persisted_session_uses_its_duration_as_legacy_default(
    tmp_path,
) -> None:
    store = SQLiteWorkingSessionStore(tmp_path / "working-session.sqlite")
    session = make_session()
    store.create(session)
    payload = json.loads(
        store.connection.execute(
            "SELECT payload FROM working_session_current WHERE slot = 1"
        ).fetchone()[0]
    )
    payload.pop("default_session_minutes")
    store.connection.execute(
        "UPDATE working_session_current SET payload = ? WHERE slot = 1",
        (json.dumps(payload, separators=(",", ":"), sort_keys=True),),
    )
    store.connection.commit()

    restored = store.load()
    assert restored is not None
    assert restored.default_session_minutes == session.session_minutes
    assert apply(restored, "/new").state.session_minutes == session.session_minutes
    store.close()


def test_broker_freezes_selected_configuration_for_orchestration_and_audit() -> None:
    state, audit, _, _, orchestration, _, _, receiver, trace_store = (
        make_receiver_components()
    )
    try:
        assert (
            receiver.receive(
                make_signed_event(
                    "/model gpt-5.6-sol", event_id="event-model", message_id="m-model"
                )
            ).disposition
            == "session_model_updated"
        )
        assert (
            receiver.receive(
                make_signed_event(
                    "/reasoning high",
                    event_id="event-reasoning",
                    message_id="m-reasoning",
                )
            ).disposition
            == "session_reasoning_updated"
        )

        result = receiver.receive(
            make_signed_event(
                "inspect state", event_id="event-request", message_id="m-request"
            )
        )

        assert result.disposition == "completed"
        request = orchestration.calls[0]
        assert request.model == "gpt-5.6-sol"
        assert request.reasoning == "high"
        stored = state.get_request(request.state.request_id)
        assert stored is not None
        assert (stored.model, stored.reasoning) == ("gpt-5.6-sol", "high")
        accepted = next(
            record for record in audit.records if record.kind == "request_accepted"
        )
        assert accepted.details == {
            "phase": "orchestration",
            "model": "gpt-5.6-sol",
            "reasoning": "high",
        }
    finally:
        trace_store._close_writer_service()


def test_broker_rechecks_exact_configuration_before_dispatch_without_fallback() -> None:
    def revoke_selected_model(_: object) -> str:
        provider.availability = ModelAvailability(
            available_models=("gpt-5.6-terra",),
            available_reasoning_levels=("medium",),
        )
        return "This reply must not be sent."

    orchestration = ControlledOrchestrationAdapter(
        response_factory=revoke_selected_model
    )
    _, _, _, provider, orchestration, outbound, _, receiver, trace_store = (
        make_receiver_components(orchestration=orchestration)
    )
    try:
        assert (
            receiver.receive(
                make_signed_event(
                    "/model gpt-5.6-sol", event_id="event-model", message_id="m-model"
                )
            ).disposition
            == "session_model_updated"
        )

        result = receiver.receive(
            make_signed_event(
                "inspect state", event_id="event-request", message_id="m-request"
            )
        )

        assert result.disposition == "model_availability_unavailable"
        assert orchestration.calls[0].model == "gpt-5.6-sol"
        assert (
            len(outbound.sent) == 1
        )  # Only the /model acknowledgement was dispatched.
    finally:
        trace_store._close_writer_service()


def test_broker_expires_idle_session_before_assigning_next_conversation() -> None:
    state, audit, clock, _, orchestration, _, broker, receiver, trace_store = (
        make_receiver_components()
    )
    try:
        original_session_id = broker.current_working_session_id
        assert (
            receiver.receive(
                make_signed_event(
                    "/model gpt-5.6-sol", event_id="event-model", message_id="m-model"
                )
            ).disposition
            == "session_model_updated"
        )
        clock.advance(minutes=60)

        result = receiver.receive(
            make_signed_event(
                "fresh request", event_id="event-fresh", message_id="m-fresh"
            )
        )

        assert result.disposition == "completed"
        assert broker.current_working_session_id != original_session_id
        assert orchestration.calls[0].model == "gpt-5.6-terra"
        assert state.list_conversation_messages()[-1].working_session_id == (
            broker.current_working_session_id
        )
        assert "working_session_expired" in [record.kind for record in audit.records]
    finally:
        trace_store._close_writer_service()

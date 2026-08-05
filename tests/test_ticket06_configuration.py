"""Ticket 06 configuration and runtime-availability contract tests."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from jarvis_control_plane.control_grammar import (
    ControlTransitionKind,
    handle_message,
    parse_control,
)
from jarvis_control_plane.sessions import (
    ModelAvailability,
    SessionConfig,
    SessionStoreError,
    SQLiteWorkingSessionStore,
    create_working_session,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


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

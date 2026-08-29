"""Ticket 12 outbound-attempt store contract tests."""

from __future__ import annotations

from datetime import timedelta

import pytest

from jarvis_control_plane import OutboundAttemptStatus, StateStoreError

from .helpers import (
    NOW,
    _close_contract_store,
    _contract_store,
    _outbound_message,
)


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_outbound_attempt_identity_cannot_be_reserved_after_terminalization(
    store_kind: str,
    tmp_path,
) -> None:
    state = _contract_store(store_kind, tmp_path)
    message = _outbound_message(message_id=f"reuse-{store_kind}")
    try:
        state.reserve_outbound_conversation_message(message)
        state.terminalize_outbound_conversation_attempt(
            transport_session_id=message.transport_session_id,
            message_id=message.message_id,
            status=OutboundAttemptStatus.NOT_STARTED,
            terminal_at=NOW + timedelta(seconds=1),
        )

        with pytest.raises(StateStoreError, match="identifier already exists"):
            state.reserve_outbound_conversation_message(message)
        assert len(state.list_outbound_conversation_attempts()) == 1
    finally:
        _close_contract_store(state)


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
@pytest.mark.parametrize(
    "illegal_transition",
    [
        "mark_before_reserve",
        "terminalize_before_reserve",
        "confirm_unattempted",
        "mark_after_terminalization",
        "terminalize_after_terminalization",
    ],
)
def test_outbound_attempt_store_contract_rejects_illegal_transitions(
    store_kind: str,
    illegal_transition: str,
    tmp_path,
) -> None:
    state = _contract_store(store_kind, tmp_path)
    message = _outbound_message(message_id=f"illegal-{store_kind}-{illegal_transition}")
    try:
        with pytest.raises(StateStoreError):
            if illegal_transition == "mark_before_reserve":
                state.mark_outbound_conversation_attempted(
                    transport_session_id=message.transport_session_id,
                    message_id=message.message_id,
                    attempted_at=NOW,
                )
            elif illegal_transition == "terminalize_before_reserve":
                state.terminalize_outbound_conversation_attempt(
                    transport_session_id=message.transport_session_id,
                    message_id=message.message_id,
                    status=OutboundAttemptStatus.NOT_STARTED,
                    terminal_at=NOW,
                )
            else:
                state.reserve_outbound_conversation_message(message)
                state.terminalize_outbound_conversation_attempt(
                    transport_session_id=message.transport_session_id,
                    message_id=message.message_id,
                    status=(
                        OutboundAttemptStatus.CONFIRMED
                        if illegal_transition == "confirm_unattempted"
                        else OutboundAttemptStatus.NOT_STARTED
                    ),
                    terminal_at=NOW + timedelta(seconds=1),
                )
                if illegal_transition == "mark_after_terminalization":
                    state.mark_outbound_conversation_attempted(
                        transport_session_id=message.transport_session_id,
                        message_id=message.message_id,
                        attempted_at=NOW + timedelta(seconds=2),
                    )
                else:
                    state.terminalize_outbound_conversation_attempt(
                        transport_session_id=message.transport_session_id,
                        message_id=message.message_id,
                        status=OutboundAttemptStatus.CONFIRMED,
                        terminal_at=NOW + timedelta(seconds=2),
                    )
    finally:
        _close_contract_store(state)


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
@pytest.mark.parametrize(
    ("source_status", "target_status"),
    [
        (OutboundAttemptStatus.UNATTEMPTED, OutboundAttemptStatus.UNKNOWN),
        (OutboundAttemptStatus.ATTEMPTED, OutboundAttemptStatus.NOT_STARTED),
        (OutboundAttemptStatus.CONFIRMED, OutboundAttemptStatus.UNKNOWN),
        (OutboundAttemptStatus.UNKNOWN, OutboundAttemptStatus.NOT_STARTED),
        (OutboundAttemptStatus.NOT_STARTED, OutboundAttemptStatus.UNKNOWN),
    ],
)
def test_outbound_attempt_store_rejects_forbidden_terminal_transitions(
    store_kind: str,
    source_status: OutboundAttemptStatus,
    target_status: OutboundAttemptStatus,
    tmp_path,
) -> None:
    state = _contract_store(store_kind, tmp_path)
    message = _outbound_message(
        message_id=f"matrix-{store_kind}-{source_status.value}-{target_status.value}"
    )
    try:
        state.reserve_outbound_conversation_message(message)
        if source_status in {
            OutboundAttemptStatus.ATTEMPTED,
            OutboundAttemptStatus.CONFIRMED,
            OutboundAttemptStatus.UNKNOWN,
        }:
            state.mark_outbound_conversation_attempted(
                transport_session_id=message.transport_session_id,
                message_id=message.message_id,
                attempted_at=NOW + timedelta(seconds=1),
            )
        if (
            source_status
            in {
                OutboundAttemptStatus.CONFIRMED,
                OutboundAttemptStatus.UNKNOWN,
            }
            or source_status is OutboundAttemptStatus.NOT_STARTED
        ):
            state.terminalize_outbound_conversation_attempt(
                transport_session_id=message.transport_session_id,
                message_id=message.message_id,
                status=source_status,
                terminal_at=NOW + timedelta(seconds=2),
            )

        with pytest.raises(StateStoreError, match="terminal transition"):
            state.terminalize_outbound_conversation_attempt(
                transport_session_id=message.transport_session_id,
                message_id=message.message_id,
                status=target_status,
                terminal_at=NOW + timedelta(seconds=3),
            )

        assert state.list_outbound_conversation_attempts()[0].status is source_status
    finally:
        _close_contract_store(state)

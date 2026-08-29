"""Ticket 12 restart recovery tests."""

from __future__ import annotations

from datetime import timedelta

import pytest

from jarvis_control_plane import (
    AuditWriteError,
    InMemoryAuditBoundary,
    InMemoryDurableStateStore,
    OutboundAttemptStatus,
)
from jarvis_control_plane.sessions import InMemoryWorkingSessionStore

from .helpers import NOW, _components, _outbound_message, _request


@pytest.mark.parametrize(
    ("before_restart", "after_restart"),
    [
        (OutboundAttemptStatus.UNATTEMPTED, OutboundAttemptStatus.NOT_STARTED),
        (OutboundAttemptStatus.ATTEMPTED, OutboundAttemptStatus.UNKNOWN),
        (OutboundAttemptStatus.CONFIRMED, OutboundAttemptStatus.CONFIRMED),
        (OutboundAttemptStatus.UNKNOWN, OutboundAttemptStatus.UNKNOWN),
    ],
)
def test_restart_reconciles_outbound_attempts_without_resending(
    before_restart: OutboundAttemptStatus,
    after_restart: OutboundAttemptStatus,
) -> None:
    state = InMemoryDurableStateStore()
    components = _components(state=state)
    message = _outbound_message(message_id=f"reply-{before_restart.value}")
    state.reserve_outbound_conversation_message(message)
    if before_restart is not OutboundAttemptStatus.UNATTEMPTED:
        state.mark_outbound_conversation_attempted(
            transport_session_id=message.transport_session_id,
            message_id=message.message_id,
            attempted_at=NOW + timedelta(seconds=1),
        )
    if before_restart is OutboundAttemptStatus.CONFIRMED:
        state.accept_reserved_outbound_conversation_message(
            transport_session_id=message.transport_session_id,
            message_id=message.message_id,
            terminal_at=NOW + timedelta(seconds=2),
        )
    elif before_restart is OutboundAttemptStatus.UNKNOWN:
        state.reconcile_outbound_conversation_attempts(
            interrupted_at=NOW + timedelta(seconds=2)
        )

    restarted = _components(
        state=state,
        working_sessions=components.broker.working_sessions,
    )

    record = state.list_outbound_conversation_attempts()[-1]
    assert record.status is after_restart
    assert record.message is None
    assert restarted.outbound.sent == []


def test_restart_interrupts_only_nonterminal_requests_and_records_safe_evidence() -> (
    None
):
    state = InMemoryDurableStateStore()
    components = _components(state=state)
    active = _request("request-active", status="accepted", phase="orchestration")
    completed = _request(
        "request-completed",
        status="completed",
        phase="completed",
        outcome="reply_sent",
    )
    state.save_request(active)
    state.save_request(completed)

    restarted = _components(
        state=state,
        working_sessions=components.broker.working_sessions,
    )

    interrupted = state.get_request(active.request_id)
    assert interrupted is not None
    assert interrupted.status == "interrupted"
    assert interrupted.phase == "interrupted"
    assert interrupted.outcome == "interrupted"
    assert interrupted.error_code == "service_restart"
    assert state.get_request(completed.request_id) == completed
    assert restarted.outbound.sent == []
    restart_records = [
        record for record in restarted.audit.records if record.kind == "service_restart"
    ]
    assert restart_records
    assert restart_records[-1].details == {
        "interrupted_requests": "1",
        "outbound_not_started": "0",
        "outbound_unknown": "0",
    }


def test_restart_audit_failure_does_not_mutate_recovery_state() -> None:
    state = InMemoryDurableStateStore()
    audit = InMemoryAuditBoundary()
    initial = _components(state=state, audit=audit)
    active = _request("request-audit-gated", status="accepted", phase="orchestration")
    message = _outbound_message(message_id="reply-audit-gated")
    state.save_request(active)
    state.reserve_outbound_conversation_message(message)
    state.mark_outbound_conversation_attempted(
        transport_session_id=message.transport_session_id,
        message_id=message.message_id,
        attempted_at=NOW + timedelta(seconds=1),
    )
    before_session = initial.broker.working_sessions.load()
    before_requests = state.list_requests()
    before_attempts = state.list_outbound_conversation_attempts()
    audit.fail = True

    with pytest.raises(AuditWriteError, match="controlled audit append failure"):
        _components(
            state=state,
            audit=audit,
            working_sessions=initial.broker.working_sessions,
        )

    assert initial.broker.working_sessions.load() == before_session
    assert state.list_requests() == before_requests
    assert state.list_outbound_conversation_attempts() == before_attempts
    assert audit.records == []

    audit.fail = False
    restarted = _components(
        state=state,
        audit=audit,
        working_sessions=initial.broker.working_sessions,
    )
    interrupted = state.get_request(active.request_id)
    assert interrupted is not None
    assert interrupted.status == "interrupted"
    attempt = state.list_outbound_conversation_attempts()[0]
    assert attempt.status is OutboundAttemptStatus.UNKNOWN
    assert attempt.message is None
    restart_records = [
        record for record in restarted.audit.records if record.kind == "service_restart"
    ]
    assert restart_records[-1].details == {
        "interrupted_requests": "1",
        "outbound_not_started": "0",
        "outbound_unknown": "1",
    }


def test_restart_audit_failure_does_not_create_missing_working_session() -> None:
    state = InMemoryDurableStateStore()
    active = _request("request-no-session", status="accepted", phase="orchestration")
    state.save_request(active)
    audit = InMemoryAuditBoundary(fail=True)
    working_sessions = InMemoryWorkingSessionStore()
    before_requests = state.list_requests()

    with pytest.raises(AuditWriteError, match="controlled audit append failure"):
        _components(
            state=state,
            audit=audit,
            working_sessions=working_sessions,
        )

    assert working_sessions.load() is None
    assert state.list_requests() == before_requests
    assert audit.records == []

    audit.fail = False
    recovered = _components(
        state=state,
        audit=audit,
        working_sessions=working_sessions,
    )
    assert working_sessions.load() is not None
    assert state.get_request(active.request_id).status == "interrupted"
    assert any(record.kind == "service_restart" for record in recovered.audit.records)

from __future__ import annotations

# ruff: noqa: F401, I001, RUF100 -- split modules retain shared imports.

import json
from dataclasses import replace
from datetime import UTC, datetime

from test_support import build_receiver_components

from jarvis_control_plane import (
    ControlledActionDispatcher,
    ControlledOrchestrationAdapter,
    FrozenActionProposal,
    InboundMessage,
    InMemoryAuditBoundary,
    SignedInboundEvent,
)
from jarvis_control_plane.sessions import (
    CommandPermissionComponent,
    CommandPermissionIdentity,
    CommandPermissionState,
    DispatchStatus,
    InMemoryWorkingSessionStore,
    PermissionLifetime,
    ReadinessState,
    SQLiteWorkingSessionStore,
    WorkingSession,
    _session_json,
    new_working_session,
)
from jarvis_control_plane.terminal_policy import (
    TerminalAction,
    TerminalDisposition,
    authorize_terminal_action,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
OPERATOR = "operator.test"
TRANSPORT_SESSION = "session.test"
SECRET = b"ticket10-test-secret"


def _event(text: str, suffix: str) -> SignedInboundEvent:
    return SignedInboundEvent.from_message(
        InboundMessage(
            event_type="message.received",
            session_id=TRANSPORT_SESSION,
            event_id=f"event-permission-{suffix}",
            message_id=f"message-permission-{suffix}",
            sender_id=OPERATOR,
            chat_id=OPERATOR,
            chat_type="direct",
            message_type="text",
            from_me=False,
            text=text,
        ),
        SECRET,
    )


def _permission(permission_id: str) -> CommandPermissionState:
    return CommandPermissionState(
        permission_id=permission_id,
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


def _components_with_permissions(*permissions: CommandPermissionState):
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=SECRET,
        now=NOW,
        id_prefix="ticket10",
    )
    session = components.broker.working_sessions.load()
    assert session is not None
    components.broker.working_sessions.compare_and_set(
        session, replace(session, permissions=permissions)
    )
    return components


def _components_for_terminal_payload(payload: object):
    dispatcher = ControlledActionDispatcher()
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=SECRET,
        now=NOW,
        id_prefix="ticket10-terminal",
        action_dispatcher=dispatcher,
        orchestration=ControlledOrchestrationAdapter(
            proposal_factory=lambda request: FrozenActionProposal.create(
                action_id="action-ticket10-001",
                request_id=request.state.request_id,
                kind="terminal",
                preview="Run the exact terminal action.",
                payload=payload,
            )
        ),
    )
    session = components.broker.working_sessions.load()
    assert session is not None
    components.broker.working_sessions.compare_and_set(
        session,
        replace(
            session,
            readiness=ReadinessState(ubuntu="ready", windows="unavailable"),
        ),
    )
    return components, dispatcher


class _RevokeBeforeDispatchStore(InMemoryWorkingSessionStore):
    """Simulate an external durable revocation after approval admission."""

    def load(self) -> WorkingSession | None:
        session = super().load()
        if session is None or not any(
            record.status is DispatchStatus.UNATTEMPTED
            for record in session.action_outbox
        ):
            return session
        active = next(
            (permission for permission in session.permissions if permission.is_active),
            None,
        )
        if active is None:
            return session
        revoked = replace(
            session,
            permissions=tuple(
                replace(permission, revoked_at=NOW)
                if permission.permission_id == active.permission_id
                else permission
                for permission in session.permissions
            ),
        )
        super().compare_and_set(session, revoked)
        return revoked


def test_bulk_revoke_remains_available_while_work_is_active() -> None:
    components, _dispatcher = _components_for_terminal_payload(
        {
            "host": "ubuntu",
            "executable": "/usr/bin/touch",
            "arguments": ["/workspace/pending.txt"],
            "cwd": "/workspace",
        }
    )
    session = components.broker.working_sessions.load()
    assert session is not None
    session_permission = replace(
        _permission("permission-session"),
        lifetime=PermissionLifetime.SESSION,
        session_id=session.session_id,
    )
    persistent_permission = _permission("permission-persistent")
    components.broker.working_sessions.compare_and_set(
        session,
        replace(
            session,
            permissions=(session_permission, persistent_permission),
        ),
    )

    pending = components.receiver.receive(_event("prepare a change", "active"))
    revoked_session = components.receiver.receive(
        _event("/revoke session", "active-revoke-session")
    )

    assert pending.disposition == "pending_action"
    assert revoked_session.disposition == "permission_revoked"
    current = components.broker.working_sessions.load()
    assert current is not None
    assert current.active_request is not None
    assert current.pending_action is not None
    assert not current.permissions[0].is_active
    assert current.permissions[1].is_active

    revoked_persistent = components.receiver.receive(
        _event("/revoke persistent", "active-revoke-persistent")
    )

    assert revoked_persistent.disposition == "permission_revoked"
    current = components.broker.working_sessions.load()
    assert current is not None
    assert current.active_request is not None
    assert current.pending_action is not None
    assert all(not permission.is_active for permission in current.permissions)


def test_revoked_approved_but_not_started_action_never_dispatches() -> None:
    action = {
        "host": "ubuntu",
        "executable": "/usr/bin/touch",
        "arguments": ["/workspace/exact.txt"],
        "cwd": "/workspace",
    }
    store = _RevokeBeforeDispatchStore()
    dispatcher = ControlledActionDispatcher()
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=SECRET,
        now=NOW,
        id_prefix="ticket10-revoked-dispatch",
        working_sessions=store,
        action_dispatcher=dispatcher,
        orchestration=ControlledOrchestrationAdapter(
            proposal_factory=lambda request: FrozenActionProposal.create(
                action_id="action-ticket10-revoked",
                request_id=request.state.request_id,
                kind="terminal",
                preview="Run the exact terminal action.",
                payload=action,
            )
        ),
    )
    session = store.load()
    assert session is not None
    permission = CommandPermissionState(
        permission_id="permission-approved-not-started",
        lifetime=PermissionLifetime.PERSISTENT,
        identity=TerminalAction.from_mapping(action).permission_identity,
        created_at=NOW,
    )
    store.compare_and_set(
        session,
        replace(
            session,
            readiness=ReadinessState(ubuntu="ready", windows="unavailable"),
            permissions=(permission,),
        ),
    )

    result = components.receiver.receive(
        _event("run the exact permitted action", "revoked-before-dispatch")
    )

    assert result.disposition == "permission_revoked"
    assert dispatcher.dispatched == []
    current = store.load()
    assert current is not None
    assert current.action_outbox[-1].status is DispatchStatus.NOT_STARTED


def test_persistent_permission_records_provenance_and_audit_permission_id() -> None:
    components, dispatcher = _components_for_terminal_payload(
        {
            "host": "ubuntu",
            "executable": "/usr/bin/touch",
            "arguments": ["/workspace/example.txt"],
            "cwd": "/workspace",
        }
    )

    pending = components.receiver.receive(_event("create a file", "persistent"))
    approved = components.receiver.receive(_event("3", "persistent-approval"))

    assert pending.request is not None
    assert approved.disposition == "action_dispatched"
    assert len(dispatcher.dispatched) == 1
    session = components.broker.working_sessions.load()
    assert session is not None
    assert len(session.permissions) == 1
    permission = session.permissions[0]
    assert permission.authorization_request_id == pending.request.request_id
    assert permission.authorization_action_id == "action-ticket10-001"
    assert permission.authorization_approval == "persistent_permission"
    assert permission.authorization_audit_id is not None
    creation_audit = next(
        evidence
        for evidence in components.audit.records
        if evidence.kind == "pending_action"
        and evidence.approval_decision == "persistent_permission"
    )
    assert creation_audit.details["permission_id"] == permission.permission_id
    assert permission.authorization_audit_id == creation_audit.evidence_id


def test_exact_permission_reuse_updates_last_use_before_dispatch() -> None:
    action = {
        "host": "ubuntu",
        "executable": "/usr/bin/touch",
        "arguments": ["/workspace/example.txt"],
        "cwd": "/workspace",
    }
    components, dispatcher = _components_for_terminal_payload(action)
    permission = CommandPermissionState(
        permission_id="permission-reuse",
        lifetime=PermissionLifetime.PERSISTENT,
        identity=TerminalAction.from_mapping(action).permission_identity,
        created_at=NOW,
    )
    session = components.broker.working_sessions.load()
    assert session is not None
    components.broker.working_sessions.compare_and_set(
        session, replace(session, permissions=(permission,))
    )

    result = components.receiver.receive(_event("reuse the exact permission", "reuse"))

    assert result.disposition == "action_dispatched"
    assert len(dispatcher.dispatched) == 1
    updated = components.broker.working_sessions.load()
    assert updated is not None
    assert updated.permissions[0].last_used_at == NOW
    reuse_audit = next(
        evidence
        for evidence in components.audit.records
        if evidence.kind == "pending_action"
        and evidence.details.get("permission_id") == "permission-reuse"
    )
    assert reuse_audit.details["state"] == "approved"


def test_permission_provenance_and_last_use_round_trip_through_sqlite(tmp_path) -> None:
    permission = replace(
        _permission("permission-round-trip"),
        authorization_request_id="request-round-trip",
        authorization_action_id="action-round-trip",
        authorization_approval="persistent_permission",
        authorization_audit_id="audit-round-trip",
        last_used_at=NOW,
    )
    session = WorkingSession.initial(
        OPERATOR,
        NOW,
        session_id="working-session-round-trip",
        permissions=(permission,),
    )
    store = SQLiteWorkingSessionStore(tmp_path / "permission-round-trip.sqlite3")
    try:
        store.create(session)
    finally:
        store.close()
    reopened = SQLiteWorkingSessionStore(tmp_path / "permission-round-trip.sqlite3")
    try:
        restored = reopened.load()
    finally:
        reopened.close()

    assert restored is not None
    assert restored.permissions == (permission,)

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from test_support import build_receiver_components

from jarvis_control_plane import InboundMessage, SignedInboundEvent
from jarvis_control_plane.sessions import CommandPermissionState, PermissionLifetime
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
        host="ubuntu",
        command="/usr/bin/git status",
        cwd="/workspace",
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


def test_permissions_are_listed_in_a_deterministic_safe_projection() -> None:
    components = _components_with_permissions(
        _permission("permission-b"), _permission("permission-a")
    )

    result = components.receiver.receive(_event("/permissions", "list"))

    assert result.disposition == "permissions_listed"
    body = components.outbound.sent[-1].body
    assert (
        "permission-a | persistent | ubuntu | /workspace | /usr/bin/git status" in body
    )
    assert body.index("permission-a") < body.index("permission-b")


def test_revoke_removes_permission_before_an_acknowledgement_can_fail() -> None:
    components = _components_with_permissions(_permission("permission-001"))
    components.outbound.failure = "gateway unavailable"

    result = components.receiver.receive(
        _event("/permissions revoke permission-001", "revoke")
    )

    assert result.disposition == "failed"
    session = components.broker.working_sessions.load()
    assert session is not None
    permission = session.permissions[0]
    assert permission.revoked_at == NOW
    action = TerminalAction(
        host="ubuntu",
        executable="/usr/bin/git",
        arguments=("status",),
        cwd="/workspace",
    )
    assert (
        authorize_terminal_action(action, permissions=session.permissions).disposition
        is TerminalDisposition.SAFE_READ
    )

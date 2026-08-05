from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from test_support import build_receiver_components

from jarvis_control_plane import InboundMessage, SignedInboundEvent
from jarvis_control_plane.sessions import (
    CommandPermissionComponent,
    CommandPermissionIdentity,
    CommandPermissionState,
    PermissionLifetime,
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


def test_exact_permission_binds_redirection_and_argument_boundaries() -> None:
    approved_action = TerminalAction(
        host="ubuntu",
        executable="/usr/bin/printf",
        arguments=("ok",),
        cwd="/workspace",
        components=(
            {
                "executable": "/usr/bin/printf",
                "arguments": ["ok"],
                "redirections": ["/workspace/out.txt"],
            },
        ),
    )
    permission = CommandPermissionState(
        permission_id="permission-redirect",
        lifetime=PermissionLifetime.PERSISTENT,
        identity=approved_action.permission_identity,
        created_at=NOW,
    )

    assert (
        authorize_terminal_action(
            approved_action, permissions=(permission,)
        ).disposition
        is TerminalDisposition.EXACT_PERMISSION
    )

    redirected_elsewhere = TerminalAction(
        host="ubuntu",
        executable="/usr/bin/printf",
        arguments=("ok",),
        cwd="/workspace",
        components=(
            {
                "executable": "/usr/bin/printf",
                "arguments": ["ok"],
                "redirections": ["/etc/passwd"],
            },
        ),
    )
    split_arguments = TerminalAction(
        host="ubuntu",
        executable="/usr/bin/printf",
        arguments=("o", "k"),
        cwd="/workspace",
    )

    assert (
        authorize_terminal_action(
            redirected_elsewhere, permissions=(permission,)
        ).disposition
        is TerminalDisposition.ORDINARY_APPROVAL
    )
    assert (
        authorize_terminal_action(
            split_arguments, permissions=(permission,)
        ).disposition
        is TerminalDisposition.ORDINARY_APPROVAL
    )


def test_exact_permission_binds_compound_component_order_and_operator() -> None:
    approved_action = TerminalAction(
        host="ubuntu",
        executable="/usr/bin/printf",
        arguments=("ok",),
        cwd="/workspace",
        components=(
            {"executable": "/usr/bin/printf", "arguments": ["ok"]},
            {
                "executable": "/usr/bin/printf",
                "arguments": ["done"],
                "operator_before": "&&",
            },
        ),
    )
    permission = CommandPermissionState(
        permission_id="permission-compound",
        lifetime=PermissionLifetime.PERSISTENT,
        identity=approved_action.permission_identity,
        created_at=NOW,
    )
    different_operator = TerminalAction(
        host="ubuntu",
        executable="/usr/bin/printf",
        arguments=("ok",),
        cwd="/workspace",
        components=(
            {"executable": "/usr/bin/printf", "arguments": ["ok"]},
            {
                "executable": "/usr/bin/printf",
                "arguments": ["done"],
                "operator_before": "||",
            },
        ),
    )

    assert (
        authorize_terminal_action(
            approved_action, permissions=(permission,)
        ).disposition
        is TerminalDisposition.EXACT_PERMISSION
    )
    assert (
        authorize_terminal_action(
            different_operator, permissions=(permission,)
        ).disposition
        is TerminalDisposition.ORDINARY_APPROVAL
    )

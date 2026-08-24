from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from test_support import build_receiver_components

from jarvis_control_plane import (
    ControlledActionDispatcher,
    ControlledOrchestrationAdapter,
    FrozenActionProposal,
    InboundMessage,
    SignedInboundEvent,
)
from jarvis_control_plane.sessions import (
    CommandPermissionComponent,
    CommandPermissionIdentity,
    CommandPermissionState,
    DispatchStatus,
    PermissionLifetime,
    ReadinessState,
)
from jarvis_control_plane.terminal_policy import (
    TerminalAction,
    TerminalDisposition,
    authorize_terminal_action,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
OPERATOR = "operator.test"
TRANSPORT_SESSION = "session.test"
SECRET = b"ticket09-test-secret"


def action(
    *,
    executable: str,
    arguments: tuple[str, ...] = (),
    components: tuple[object, ...] = (),
) -> TerminalAction:
    return TerminalAction(
        host="ubuntu",
        executable=executable,
        arguments=arguments,
        cwd="/workspace",
        components=components,
    )


def test_terminal_policy_applies_fixed_precedence_without_a_model_classifier() -> None:
    permission = CommandPermissionState(
        permission_id="permission-001",
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

    assert (
        authorize_terminal_action(
            action(executable="/usr/bin/git", arguments=("status",))
        ).disposition
        is TerminalDisposition.SAFE_READ
    )
    assert (
        authorize_terminal_action(
            action(executable="/usr/bin/git", arguments=("status",)),
            permissions=(permission,),
        ).disposition
        is TerminalDisposition.EXACT_PERMISSION
    )
    assert (
        authorize_terminal_action(
            action(
                executable="/usr/bin/cat", arguments=("/home/operator/.ssh/id_ed25519",)
            ),
            permissions=(permission,),
        ).disposition
        is TerminalDisposition.PROTECTED_APPROVAL
    )
    assert (
        authorize_terminal_action(
            action(executable="/usr/bin/git", arguments=("reset", "--hard")),
            permissions=(permission,),
        ).disposition
        is TerminalDisposition.HARD_PROHIBITED
    )
    assert (
        authorize_terminal_action(
            action(executable="/usr/bin/apt", arguments=("install", "curl"))
        ).disposition
        is TerminalDisposition.MANDATORY_FRESH
    )


def test_each_compound_component_is_authorized_before_execution() -> None:
    compound = action(
        executable="/usr/bin/git",
        arguments=("status",),
        components=(
            {"executable": "/usr/bin/git", "arguments": ["status"]},
            {"executable": "/usr/bin/rm", "arguments": ["-rf", "/"]},
        ),
    )

    result = authorize_terminal_action(compound)

    assert result.disposition is TerminalDisposition.HARD_PROHIBITED
    assert result.component_dispositions == (
        TerminalDisposition.SAFE_READ,
        TerminalDisposition.HARD_PROHIBITED,
    )


def test_dynamic_or_unstructured_shell_behavior_never_becomes_implicit_authorization() -> (
    None
):
    dynamic = action(executable="/bin/sh", arguments=("-c", "echo $HOME"))

    assert (
        authorize_terminal_action(dynamic).disposition
        is TerminalDisposition.MANDATORY_FRESH
    )


def test_safe_read_requires_no_redirection_and_no_protected_or_relative_paths() -> None:
    redirected = action(
        executable="/usr/bin/cat",
        arguments=("/workspace/input.txt",),
        components=(
            {
                "executable": "/usr/bin/cat",
                "arguments": ["/workspace/input.txt"],
                "redirections": ["/workspace/output.txt"],
            },
        ),
    )

    assert (
        authorize_terminal_action(redirected).disposition
        is TerminalDisposition.ORDINARY_APPROVAL
    )
    assert (
        authorize_terminal_action(
            action(executable="/usr/bin/git", arguments=("status",)),
        ).disposition
        is TerminalDisposition.SAFE_READ
    )
    protected_cwd = TerminalAction(
        host="ubuntu",
        executable="/usr/bin/git",
        arguments=("status",),
        cwd="/home/operator/.ssh",
    )
    assert (
        authorize_terminal_action(protected_cwd).disposition
        is TerminalDisposition.PROTECTED_APPROVAL
    )


def _event(text: str, suffix: str) -> SignedInboundEvent:
    return SignedInboundEvent.from_message(
        InboundMessage(
            event_type="message.received",
            session_id=TRANSPORT_SESSION,
            event_id=f"event-terminal-{suffix}",
            message_id=f"message-terminal-{suffix}",
            sender_id=OPERATOR,
            chat_id=OPERATOR,
            chat_type="direct",
            message_type="text",
            from_me=False,
            text=text,
        ),
        SECRET,
    )


def _broker_for(payload: object) -> tuple[object, ControlledActionDispatcher]:
    dispatcher = ControlledActionDispatcher()
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=SECRET,
        now=NOW,
        id_prefix="ticket09",
        action_dispatcher=dispatcher,
        orchestration=ControlledOrchestrationAdapter(
            proposal_factory=lambda request: FrozenActionProposal.create(
                action_id="action-terminal-001",
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


def test_broker_dispatches_safe_reads_without_an_operator_approval() -> None:
    components, dispatcher = _broker_for(
        {
            "host": "ubuntu",
            "executable": "/usr/bin/git",
            "arguments": ["status"],
            "cwd": "/workspace",
        }
    )

    result = components.receiver.receive(_event("show repo status", "safe"))

    assert result.disposition == "action_dispatched"
    assert len(dispatcher.dispatched) == 1


def test_broker_auto_authorizes_operating_system_name_safe_read() -> None:
    assert (
        authorize_terminal_action(
            action(executable="/usr/bin/uname", arguments=("-a",))
        ).disposition
        is TerminalDisposition.ORDINARY_APPROVAL
    )
    components, dispatcher = _broker_for(
        {
            "host": "ubuntu",
            "executable": "/usr/bin/uname",
            "arguments": ["-s"],
            "cwd": "/workspace",
        }
    )

    result = components.receiver.receive(_event("show operating system name", "uname"))

    assert result.disposition == "action_dispatched"
    assert len(dispatcher.dispatched) == 1


def test_broker_creates_a_session_permission_for_choice_two_then_dispatches() -> None:
    components, dispatcher = _broker_for(
        {
            "host": "ubuntu",
            "executable": "/usr/bin/touch",
            "arguments": ["/workspace/example.txt"],
            "cwd": "/workspace",
        }
    )

    pending = components.receiver.receive(_event("create a file", "permission"))
    approved = components.receiver.receive(_event("2", "permission-approval"))

    assert pending.disposition == "pending_action"
    assert "Allow for this session" in components.outbound.sent[-1].body
    assert approved.disposition == "action_dispatched"
    assert len(dispatcher.dispatched) == 1
    permissions = components.broker.working_sessions.load().permissions
    assert len(permissions) == 1
    assert permissions[0].lifetime is PermissionLifetime.SESSION


def test_broker_does_not_dispatch_to_an_unavailable_selected_host() -> None:
    components, dispatcher = _broker_for(
        {
            "host": "windows",
            "executable": "C:/Program Files/Git/bin/git.exe",
            "arguments": ["status"],
            "cwd": "C:/workspace",
        }
    )

    pending = components.receiver.receive(_event("show repo status", "unavailable"))
    approved = components.receiver.receive(_event("1", "unavailable-approval"))

    assert pending.disposition == "pending_action"
    assert approved.disposition == "action_dispatch_unavailable"
    assert "windows is not ready" in (approved.reason or "")
    assert dispatcher.dispatched == []
    session = components.broker.working_sessions.load()
    assert session is not None
    assert session.action_outbox[-1].status is DispatchStatus.NOT_STARTED

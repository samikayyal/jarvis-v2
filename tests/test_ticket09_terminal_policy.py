from __future__ import annotations

from datetime import UTC, datetime

from jarvis_control_plane.sessions import CommandPermissionState, PermissionLifetime
from jarvis_control_plane.terminal_policy import (
    TerminalAction,
    TerminalDisposition,
    authorize_terminal_action,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


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
        host="ubuntu",
        command="git status",
        cwd="/workspace",
        created_at=NOW,
    )

    assert (
        authorize_terminal_action(
            action(executable="git", arguments=("status",))
        ).disposition
        is TerminalDisposition.SAFE_READ
    )
    assert (
        authorize_terminal_action(
            action(executable="git", arguments=("status",)), permissions=(permission,)
        ).disposition
        is TerminalDisposition.EXACT_PERMISSION
    )
    assert (
        authorize_terminal_action(
            action(executable="cat", arguments=("/home/operator/.ssh/id_ed25519",)),
            permissions=(permission,),
        ).disposition
        is TerminalDisposition.PROTECTED_APPROVAL
    )
    assert (
        authorize_terminal_action(
            action(executable="git", arguments=("reset", "--hard")),
            permissions=(permission,),
        ).disposition
        is TerminalDisposition.HARD_PROHIBITED
    )
    assert (
        authorize_terminal_action(
            action(executable="apt", arguments=("install", "curl"))
        ).disposition
        is TerminalDisposition.MANDATORY_FRESH
    )


def test_each_compound_component_is_authorized_before_execution() -> None:
    compound = action(
        executable="git",
        arguments=("status",),
        components=(
            {"executable": "git", "arguments": ["status"]},
            {"executable": "rm", "arguments": ["-rf", "/"]},
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
    dynamic = action(executable="sh", arguments=("-c", "echo $HOME"))

    assert (
        authorize_terminal_action(dynamic).disposition
        is TerminalDisposition.MANDATORY_FRESH
    )

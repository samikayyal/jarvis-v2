"""Safe rendering for control commands."""

from __future__ import annotations

from ...sessions import (
    CANONICAL_MODELS,
    CANONICAL_REASONING_LEVELS,
    StatusView,
    WorkingSession,
    active_command_permissions,
)
from .parsing import ControlCommand, ParsedControl

_MODEL_USAGE_TOKENS = "|".join(CANONICAL_MODELS)
_REASONING_USAGE_TOKENS = "|".join(CANONICAL_REASONING_LEVELS)


def render_status(view: StatusView) -> str:
    """Render only the fields deliberately exposed by :class:`StatusView`."""

    if view.active_request is None:
        request_text = "none"
    else:
        request = view.active_request
        host = request.execution_host or "not selected"
        request_text = f"{request.request_id} / {request.phase.value} / {host}"
    if view.pending_action is None:
        pending_text = "none"
    else:
        action = view.pending_action
        pending_text = (
            f"{action.action_id} / {action.kind} / {action.summary} / "
            f"expires {action.expires_at.isoformat()}"
        )
    services = (
        ", ".join(
            f"{service.service_id}={service.state}"
            for service in view.readiness.connected_services
        )
        or "none configured"
    )
    return (
        f"Session {view.session_id}: inactivity boundary {view.session_minutes}m; "
        f"model {view.model}; reasoning {view.reasoning}.\n"
        f"Active request: {request_text}.\n"
        f"Pending action: {pending_text}.\n"
        f"Command permissions: {view.permission_count} active.\n"
        f"Readiness: Ubuntu={view.readiness.ubuntu}, "
        f"Windows={view.readiness.windows}, OpenWA={view.readiness.openwa}, "
        f"connected services={services}."
    )


safe_status = render_status


def render_permissions(session: WorkingSession) -> str:
    """Render active exact rules without command output or hidden payloads."""

    permissions = active_command_permissions(session)
    if not permissions:
        return "No active command permissions."
    return "\n".join(
        (
            "Active command permissions:",
            *(
                f"{permission.permission_id} | {permission.lifetime.value} | "
                f"{permission.host} | {permission.command} | {permission.cwd} | "
                f"created {permission.created_at.isoformat()} | last used "
                f"{permission.last_used_at.isoformat() if permission.last_used_at else 'never'}"
                for permission in permissions
            ),
        )
    )


def _usage(parsed: ParsedControl) -> str:
    usage = {
        ControlCommand.MODEL: f"Usage: /model [{_MODEL_USAGE_TOKENS}]",
        ControlCommand.REASONING: f"Usage: /reasoning [{_REASONING_USAGE_TOKENS}]",
        ControlCommand.CONFIG: (
            "Usage: /config [model <model>|reasoning <level>|session-minutes <minutes>]"
        ),
        ControlCommand.PERMISSIONS: "Usage: /permissions",
        ControlCommand.REVOKE: "Usage: /revoke <permission-id|session|persistent|all>",
        ControlCommand.HISTORY: (
            "Usage: /history search <text> | /history conversation <conversation-id> | "
            "/history inspect <message-id> | /history export <message-id> | "
            "/history delete message <history-id> | "
            "/history delete conversation <conversation-id> | "
            "/history delete date <YYYY-MM-DD> <YYYY-MM-DD>"
        ),
    }
    if parsed.command is not None:
        return usage.get(parsed.command, f"Usage: /{parsed.command.value}")
    return (
        "Unknown or malformed control command. Valid: /new, /status, /cancel, "
        "/model, /reasoning, /config, /permissions, /revoke, /history, /memory."
    )

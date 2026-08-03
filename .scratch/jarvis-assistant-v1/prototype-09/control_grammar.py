"""PROTOTYPE: pure control grammar and state reducer for ticket 09."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Literal


MODELS = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
REASONING_LEVELS = ("none", "low", "medium", "high", "xhigh", "max")
HOSTS = ("ubuntu", "windows")
SESSION_MINUTES = (15, 30, 60, 120, 240)

ApprovalChoice = Literal["this_time", "session", "persistent", "reject"]

THIS_TIME_PHRASES = frozenset(
    {"1", "yes", "okay", "ok", "allow", "approve", "confirm", "go ahead"}
)
SESSION_PHRASES = frozenset({"2", "allow for this session", "allow this session"})
PERSISTENT_PHRASES = frozenset({"3", "allow every time", "always allow"})
REJECTION_PHRASES = frozenset(
    {
        "4",
        "no",
        "reject",
        "deny",
        "cancel",
        "cancel action",
        "don't do it",
        "do not do it",
    }
)


@dataclass(frozen=True)
class Request:
    id: str
    text: str
    host: str | None
    host_basis: str
    phase: str = "routing"


@dataclass(frozen=True)
class PendingAction:
    id: str
    kind: str
    summary: str
    command: str | None
    host: str | None
    cwd: str | None
    permission_eligible: bool
    expires_in_minutes: int = 10


@dataclass(frozen=True)
class Permission:
    id: str
    lifetime: str
    host: str
    command: str
    cwd: str


@dataclass(frozen=True)
class ControlState:
    session_id: str = "S-001"
    session_number: int = 1
    session_minutes: int = 60
    model: str = "gpt-5.6-terra"
    reasoning: str = "medium"
    default_model: str = "gpt-5.6-terra"
    default_reasoning: str = "medium"
    active_request: Request | None = None
    pending_action: PendingAction | None = None
    permissions: tuple[Permission, ...] = ()
    ubuntu_ready: bool = True
    windows_ready: bool = True
    last_notice: str = "New working session started."
    next_request: int = 1
    next_action: int = 1
    next_permission: int = 1


@dataclass(frozen=True)
class Transition:
    state: ControlState
    replies: tuple[str, ...]
    effects: tuple[str, ...] = ()


def normalize(message: str) -> str:
    return " ".join(message.strip().lower().split())


def approval_choice(message: str) -> ApprovalChoice | None:
    value = normalize(message)
    if value in THIS_TIME_PHRASES:
        return "this_time"
    if value in SESSION_PHRASES:
        return "session"
    if value in PERSISTENT_PHRASES:
        return "persistent"
    if value in REJECTION_PHRASES:
        return "reject"
    return None


def _status(state: ControlState) -> str:
    request = "none"
    if state.active_request:
        request = f"{state.active_request.id} / {state.active_request.phase} / {state.active_request.host}"
    pending = "none"
    if state.pending_action:
        pending = f"{state.pending_action.id} / {state.pending_action.summary} / expires in {state.pending_action.expires_in_minutes}m"
    return (
        f"Session {state.session_id}: idle expiry {state.session_minutes}m; "
        f"model {state.model}; reasoning {state.reasoning}.\n"
        f"Active request: {request}.\nPending action: {pending}.\n"
        f"Permissions: {len(state.permissions)} active. "
        f"Readiness: Ubuntu={'ready' if state.ubuntu_ready else 'unavailable'}, "
        f"Windows={'ready' if state.windows_ready else 'unavailable'}."
    )


def _new_session(state: ControlState) -> ControlState:
    number = state.session_number + 1
    persistent = tuple(p for p in state.permissions if p.lifetime == "persistent")
    return replace(
        state,
        session_id=f"S-{number:03d}",
        session_number=number,
        model=state.default_model,
        reasoning=state.default_reasoning,
        active_request=None,
        pending_action=None,
        permissions=persistent,
        last_notice="Previous work cancelled; pending action invalidated; session permissions revoked.",
    )


def handle_operator_message(state: ControlState, message: str) -> Transition:
    raw = message.strip()
    if not raw:
        return Transition(state, ("Empty messages have no effect.",))

    if raw.startswith("/"):
        return _handle_command(state, raw)

    if state.pending_action:
        choice = approval_choice(raw)
        if choice is None:
            return Transition(
                state,
                (
                    "A pending action pauses this request and blocks unrelated work. "
                    "Reply with 1, 2, 3, or 4 (or an exact displayed phrase). "
                    "Qualified or ambiguous replies do not execute it.",
                ),
            )
        return _resolve_approval(state, choice)

    if state.active_request:
        return Transition(
            state,
            (
                f"Request {state.active_request.id} is still active. "
                "Use /status or /cancel; V1 does not queue another request.",
            ),
        )

    request = Request(
        f"R-{state.next_request:03d}",
        raw,
        None,
        "awaiting orchestration-agent decision from natural language",
    )
    updated = replace(
        state,
        active_request=request,
        next_request=state.next_request + 1,
        last_notice=f"Accepted {request.id}; selecting execution host.",
    )
    return Transition(
        updated,
        (
            f"Accepted {request.id}. I’m choosing between the default Ubuntu host and your personal "
            "Windows laptop from the request and host availability. I’ll state the selection and reason before execution.",
        ),
        ("start_request",),
    )


def _handle_command(state: ControlState, message: str) -> Transition:
    parts = message.split()
    command = parts[0].lower()
    args = parts[1:]

    if command == "/new" and not args:
        updated = _new_session(state)
        return Transition(
            updated,
            (
                f"Started {updated.session_id} with persistent defaults. Previous work will not resume.",
            ),
            ("cancel_and_new",),
        )
    if command == "/status" and not args:
        return Transition(state, (_status(state),))
    if command == "/cancel" and not args:
        if not state.active_request and not state.pending_action:
            return Transition(state, ("Nothing is active or pending.",))
        updated = replace(
            state,
            active_request=None,
            pending_action=None,
            last_notice="Active work cancelled; pending action invalidated.",
        )
        return Transition(
            updated,
            (
                "Cancelled the active request and invalidated its pending action. No side effect will start.",
            ),
            ("cancel_request",),
        )
    if command == "/model":
        if not args:
            return Transition(
                state, (f"Session model: {state.model}. Valid: {', '.join(MODELS)}.",)
            )
        if len(args) != 1 or args[0] not in MODELS:
            return Transition(
                state, (f"Invalid model. Use exactly one of: {', '.join(MODELS)}.",)
            )
        if state.active_request:
            return Transition(
                state,
                (
                    "Model cannot change while a request or approval is active. Cancel or finish it first.",
                ),
            )
        updated = replace(
            state, model=args[0], last_notice=f"Session model set to {args[0]}."
        )
        return Transition(
            updated, (f"Session model set to {args[0]}. Persistent default unchanged.",)
        )
    if command == "/reasoning":
        if not args:
            return Transition(
                state,
                (
                    f"Session reasoning: {state.reasoning}. Valid: {', '.join(REASONING_LEVELS)}.",
                ),
            )
        if len(args) != 1 or args[0] not in REASONING_LEVELS:
            return Transition(
                state,
                (
                    f"Invalid reasoning. Use exactly one of: {', '.join(REASONING_LEVELS)}.",
                ),
            )
        if state.active_request:
            return Transition(
                state,
                (
                    "Reasoning cannot change while a request or approval is active. Cancel or finish it first.",
                ),
            )
        updated = replace(
            state, reasoning=args[0], last_notice=f"Session reasoning set to {args[0]}."
        )
        return Transition(
            updated,
            (f"Session reasoning set to {args[0]}. Persistent default unchanged.",),
        )
    if command == "/config":
        return _handle_config(state, args)
    if command == "/permissions" and not args:
        if not state.permissions:
            return Transition(state, ("No active command permissions.",))
        lines = ["Active command permissions:"]
        lines.extend(
            f"{p.id}: {p.lifetime}; {p.host}; `{p.command}`; cwd `{p.cwd}`"
            for p in state.permissions
        )
        return Transition(state, ("\n".join(lines),))
    if command == "/revoke":
        return _handle_revoke(state, args)
    return Transition(
        state,
        (
            "Unknown or malformed control command. Valid: /new, /status, /cancel, /model [value], "
            "/reasoning [value], /config [model|reasoning|session-minutes] [value], /permissions, /revoke <ID|session|persistent|all>.",
        ),
    )


def _handle_config(state: ControlState, args: list[str]) -> Transition:
    if not args:
        return Transition(
            state,
            (
                f"Persistent defaults: model {state.default_model}; reasoning {state.default_reasoning}; "
                f"session-minutes {state.session_minutes}. Changes apply to future sessions; session duration applies now too.",
            ),
        )
    if state.active_request:
        return Transition(
            state,
            (
                "Persistent configuration cannot change while a request or approval is active. Cancel or finish it first.",
            ),
        )
    if len(args) != 2:
        return Transition(
            state,
            (
                "Usage: /config model VALUE, /config reasoning VALUE, or /config session-minutes VALUE.",
            ),
        )
    key, value = args
    if key == "model" and value in MODELS:
        return Transition(
            replace(state, default_model=value),
            (
                f"Persistent model default set to {value}; current session remains {state.model}.",
            ),
        )
    if key == "reasoning" and value in REASONING_LEVELS:
        return Transition(
            replace(state, default_reasoning=value),
            (
                f"Persistent reasoning default set to {value}; current session remains {state.reasoning}.",
            ),
        )
    if key == "session-minutes" and value.isdigit() and int(value) in SESSION_MINUTES:
        minutes = int(value)
        return Transition(
            replace(state, session_minutes=minutes),
            (
                f"Working-session inactivity boundary set to {minutes} minutes, effective now and for future sessions.",
            ),
        )
    return Transition(
        state,
        (
            "Invalid config value. Use canonical models/reasoning or session-minutes 15, 30, 60, 120, or 240.",
        ),
    )


def _handle_revoke(state: ControlState, args: list[str]) -> Transition:
    if len(args) != 1:
        return Transition(
            state, ("Usage: /revoke <permission ID|session|persistent|all>.",)
        )
    selector = args[0]
    before = state.permissions
    if selector == "all":
        after = ()
    elif selector in {"session", "persistent"}:
        after = tuple(p for p in before if p.lifetime != selector)
    else:
        after = tuple(p for p in before if p.id.lower() != selector.lower())
    removed = len(before) - len(after)
    if removed == 0:
        return Transition(state, ("No matching active permission; nothing changed.",))
    updated = replace(
        state, permissions=after, last_notice=f"Revoked {removed} permission(s)."
    )
    return Transition(
        updated,
        (f"Revoked {removed} permission(s) immediately.",),
        ("revoke_permission",),
    )


def _resolve_approval(state: ControlState, choice: ApprovalChoice) -> Transition:
    action = state.pending_action
    assert action is not None
    if choice == "reject":
        updated = replace(
            state,
            pending_action=None,
            active_request=None,
            last_notice=f"Rejected {action.id}.",
        )
        return Transition(
            updated,
            (
                f"Rejected {action.id}. The frozen payload was removed and nothing executed.",
            ),
            ("reject_action",),
        )

    if choice in {"session", "persistent"} and not action.permission_eligible:
        return Transition(
            state,
            (
                "That action permits only ‘Allow this time’. Reply 1 to allow once or 4 to reject.",
            ),
        )

    permission: Permission | None = None
    updated_permissions = state.permissions
    next_permission = state.next_permission
    if choice in {"session", "persistent"}:
        assert action.command and action.host and action.cwd
        permission = Permission(
            f"P-{next_permission:03d}", choice, action.host, action.command, action.cwd
        )
        updated_permissions += (permission,)
        next_permission += 1
    updated = replace(
        state,
        pending_action=None,
        permissions=updated_permissions,
        next_permission=next_permission,
        active_request=replace(state.active_request, phase="executing")
        if state.active_request
        else None,
        last_notice=f"Approved {action.id} ({choice}).",
    )
    permission_text = (
        f" Created {permission.id} ({permission.lifetime})." if permission else ""
    )
    return Transition(
        updated,
        (
            f"Approved exact action {action.id} {choice.replace('_', ' ')}.{permission_text} Executing now.",
        ),
        ("execute_action",),
    )


def handle_system_event(state: ControlState, event: str, *args: str) -> Transition:
    """Drive orchestration events in the prototype without pretending they are WhatsApp grammar."""
    if event == "route":
        if (
            not state.active_request
            or state.pending_action
            or state.active_request.phase != "routing"
        ):
            return Transition(
                state,
                (
                    "Prototype route refused: first enter one ordinary request that is still awaiting the agent's host decision.",
                ),
            )
        if len(args) < 1 or args[0] not in HOSTS:
            return Transition(state, ("Prototype usage: :route ubuntu|windows REASON",))
        host = args[0]
        reason = " ".join(args[1:]).strip()
        if not reason:
            reason = (
                "Ubuntu default; no personal-laptop dependency detected"
                if host == "ubuntu"
                else "request requires the personal Windows laptop"
            )
        ready = state.ubuntu_ready if host == "ubuntu" else state.windows_ready
        if not ready:
            updated = replace(
                state,
                active_request=None,
                last_notice=f"Agent selected unavailable {host}: {reason}.",
            )
            label = "your personal Windows laptop" if host == "windows" else "Ubuntu"
            return Transition(
                updated,
                (
                    f"I selected {label} because {reason}, but it is unavailable. Nothing was queued or failed over; I’ll wait for further instruction.",
                ),
                ("route_unavailable",),
            )
        request = replace(
            state.active_request,
            host=host,
            host_basis=f"agent decision: {reason}",
            phase="accepted",
        )
        label = "your personal Windows laptop" if host == "windows" else "Ubuntu"
        updated = replace(
            state,
            active_request=request,
            last_notice=f"Agent routed {request.id} to {host}: {reason}.",
        )
        return Transition(
            updated,
            (
                f"Running {request.id} on {label}. Reason: {reason}. I’ll send milestone updates here.",
            ),
            ("route_request",),
        )
    if event == "milestone":
        if (
            not state.active_request
            or state.pending_action
            or not state.active_request.host
        ):
            return Transition(
                state,
                (
                    "Prototype milestone refused: enter a request and simulate its agent routing decision first.",
                ),
            )
        text = " ".join(args) or "Working"
        request = replace(state.active_request, phase=text.lower().replace(" ", "_"))
        return Transition(
            replace(state, active_request=request), (f"{request.id} update: {text}.",)
        )
    if event == "propose":
        if (
            not state.active_request
            or state.pending_action
            or not state.active_request.host
        ):
            return Transition(
                state,
                (
                    "Prototype proposal refused: `:propose` simulates a proposal produced by an already active, "
                    "routed agent request. First enter ordinary request text, then use `:route ubuntu|windows REASON`.",
                ),
            )
        mandatory = bool(args and args[0] == "mandatory")
        is_windows = state.active_request.host == "windows"
        command = (
            "Restart-Service -Name ExampleService"
            if is_windows
            else "systemctl restart example.service"
        )
        cwd = "C:\\Services\\Example" if is_windows else "/opt/example"
        action = PendingAction(
            id=f"A-{state.next_action:03d}",
            kind="terminal",
            summary="Restart the example service",
            command=command,
            host=state.active_request.host,
            cwd=cwd,
            permission_eligible=not mandatory,
        )
        choices = (
            "1 Allow this time | 4 Reject"
            if mandatory
            else "1 Allow this time | 2 Allow for this session | 3 Allow every time | 4 Reject"
        )
        updated = replace(
            state,
            pending_action=action,
            active_request=replace(state.active_request, phase="awaiting_approval"),
            next_action=state.next_action + 1,
        )
        return Transition(
            updated,
            (
                f"Approval required for {action.id} (expires in 10 minutes).\n"
                f"Host: {action.host}\nCommand: `{action.command}`\nWorking directory: `{action.cwd}`\n{choices}",
            ),
        )
    if event == "complete":
        if not state.active_request:
            return Transition(state, ("Prototype event refused: no active request.",))
        request_id = state.active_request.id
        return Transition(
            replace(
                state,
                active_request=None,
                pending_action=None,
                last_notice=f"Completed {request_id}.",
            ),
            (
                f"{request_id} completed successfully. Final result: simulated work finished.",
            ),
            ("finish_request",),
        )
    if event == "expire":
        if not state.pending_action:
            return Transition(state, ("Prototype event refused: no pending action.",))
        action_id = state.pending_action.id
        return Transition(
            replace(
                state,
                pending_action=None,
                active_request=None,
                last_notice=f"Expired {action_id}.",
            ),
            (
                f"{action_id} expired after 10 minutes. Nothing executed; ask again for a fresh proposal.",
            ),
            ("expire_action",),
        )
    if event == "restart":
        persistent = tuple(p for p in state.permissions if p.lifetime == "persistent")
        updated = replace(
            state,
            active_request=None,
            pending_action=None,
            permissions=persistent,
            last_notice="Service restarted; active work interrupted and session permissions revoked.",
        )
        return Transition(
            updated,
            (
                "Jarvis restarted. Active work will not resume; pending approval is invalid; persistent permissions remain.",
            ),
            ("restart",),
        )
    if (
        event == "availability"
        and len(args) == 2
        and args[0] in HOSTS
        and args[1] in {"ready", "down"}
    ):
        field = f"{args[0]}_ready"
        updated = replace(state, **{field: args[1] == "ready"})
        return Transition(
            updated,
            (
                f"Prototype: {args[0].title()} is now {'ready' if args[1] == 'ready' else 'unavailable'}.",
            ),
        )
    return Transition(state, ("Unknown prototype system event.",))


def state_dict(state: ControlState) -> dict[str, object]:
    return asdict(state)

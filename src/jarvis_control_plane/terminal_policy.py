"""Deterministic V1 authorization for typed terminal actions.

This module is deliberately a pure policy boundary.  It neither parses shell
source nor starts a process: workers receive only actions that have already
been represented as complete, typed command components and classified here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from .models import FrozenActionProposal
from .sessions import CommandPermissionState


class TerminalDisposition(str, Enum):
    """The only V1 terminal-policy outcomes, in precedence order."""

    HARD_PROHIBITED = "hard_prohibited"
    MANDATORY_FRESH = "mandatory_fresh"
    PROTECTED_APPROVAL = "protected_approval"
    EXACT_PERMISSION = "exact_permission"
    SAFE_READ = "safe_read"
    ORDINARY_APPROVAL = "ordinary_approval"


@dataclass(frozen=True, slots=True)
class TerminalComponent:
    """One normalized process in a proposed compound command."""

    executable: str
    arguments: tuple[str, ...] = ()
    operator_before: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.executable, str) or not self.executable.strip():
            raise ValueError("terminal executable must be non-blank")
        if self.executable != self.executable.strip():
            raise ValueError("terminal executable must be canonical")
        if self.operator_before not in {"", "|", "&&", "||", ";"}:
            raise ValueError("compound operator is not supported")
        arguments = tuple(self.arguments)
        if any(not isinstance(argument, str) or not argument for argument in arguments):
            raise ValueError("terminal arguments must be non-empty strings")
        object.__setattr__(self, "arguments", arguments)

    @property
    def normalized_command(self) -> str:
        return " ".join((self.executable, *self.arguments))


@dataclass(frozen=True, slots=True)
class TerminalAction:
    """Complete terminal identity used for policy and permission matching."""

    host: str
    executable: str
    arguments: tuple[str, ...]
    cwd: str
    components: tuple[TerminalComponent | Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not self.host.strip():
            raise ValueError("terminal host must be non-blank")
        if not isinstance(self.cwd, str) or not self.cwd.strip():
            raise ValueError("terminal cwd must be non-blank")
        root = TerminalComponent(self.executable, tuple(self.arguments))
        raw_components = tuple(self.components)
        components = (
            tuple(_component_from_value(value) for value in raw_components)
            if raw_components
            else (root,)
        )
        if (
            components[0].executable != root.executable
            or components[0].arguments != root.arguments
        ):
            raise ValueError("first compound component must match terminal action")
        if components[0].operator_before:
            raise ValueError("first compound component cannot have a leading operator")
        object.__setattr__(self, "host", self.host.strip())
        object.__setattr__(self, "cwd", self.cwd.strip())
        object.__setattr__(self, "arguments", root.arguments)
        object.__setattr__(self, "components", components)

    @property
    def normalized_command(self) -> str:
        parts: list[str] = []
        for component in self.components:
            if component.operator_before:
                parts.append(component.operator_before)
            parts.append(component.normalized_command)
        return " ".join(parts)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> TerminalAction:
        """Construct an action only from a fully structured proposal payload."""

        required = {"host", "executable", "arguments", "cwd"}
        if set(value) - (required | {"components"}) or not required <= set(value):
            raise ValueError("terminal action has unknown or missing fields")
        arguments = value["arguments"]
        if not isinstance(arguments, (list, tuple)):
            raise TypeError("terminal arguments must be an ordered sequence")
        components = value.get("components", ())
        if not isinstance(components, (list, tuple)):
            raise TypeError("terminal components must be an ordered sequence")
        return cls(
            host=_string(value["host"], "host"),
            executable=_string(value["executable"], "executable"),
            arguments=tuple(_string(item, "argument") for item in arguments),
            cwd=_string(value["cwd"], "cwd"),
            components=tuple(components),
        )


@dataclass(frozen=True, slots=True)
class TerminalPolicyResult:
    """Deterministic classification plus every pre-execution component result."""

    disposition: TerminalDisposition
    component_dispositions: tuple[TerminalDisposition, ...]
    reason: str


def authorize_terminal_action(
    action: TerminalAction,
    *,
    permissions: tuple[CommandPermissionState, ...] = (),
) -> TerminalPolicyResult:
    """Apply the V1 precedence to every component before any execution starts."""

    if not isinstance(action, TerminalAction):
        raise TypeError("terminal policy requires a typed TerminalAction")
    component_dispositions = tuple(
        _classify_component(component) for component in action.components
    )
    if TerminalDisposition.HARD_PROHIBITED in component_dispositions:
        return _result(TerminalDisposition.HARD_PROHIBITED, component_dispositions)
    if TerminalDisposition.MANDATORY_FRESH in component_dispositions:
        return _result(TerminalDisposition.MANDATORY_FRESH, component_dispositions)

    permission_matches = any(
        permission.is_active
        and permission.host == action.host
        and permission.cwd == action.cwd
        and permission.normalized_command == action.normalized_command
        for permission in permissions
    )
    if TerminalDisposition.PROTECTED_APPROVAL in component_dispositions:
        return _result(
            TerminalDisposition.EXACT_PERMISSION
            if permission_matches
            else TerminalDisposition.PROTECTED_APPROVAL,
            component_dispositions,
        )
    if permission_matches:
        return _result(TerminalDisposition.EXACT_PERMISSION, component_dispositions)
    if all(item is TerminalDisposition.SAFE_READ for item in component_dispositions):
        return _result(TerminalDisposition.SAFE_READ, component_dispositions)
    return _result(TerminalDisposition.ORDINARY_APPROVAL, component_dispositions)


def authorize_terminal_proposal(
    proposal: FrozenActionProposal,
    *,
    permissions: tuple[CommandPermissionState, ...] = (),
) -> TerminalPolicyResult:
    """Classify one frozen terminal proposal without consulting a model.

    An invalid or unstructured payload is deliberately mandatory-fresh rather
    than being guessed into a reusable or safe authorization class.
    """

    if proposal.kind != "terminal":
        raise ValueError("terminal policy accepts only terminal proposals")
    try:
        payload = json.loads(proposal.payload)
        if not isinstance(payload, Mapping):
            raise TypeError("terminal payload must be an object")
        action = TerminalAction.from_mapping(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return TerminalPolicyResult(
            disposition=TerminalDisposition.MANDATORY_FRESH,
            component_dispositions=(TerminalDisposition.MANDATORY_FRESH,),
            reason="terminal proposal is not a fully structured action",
        )
    return authorize_terminal_action(action, permissions=permissions)


def _component_from_value(
    value: TerminalComponent | Mapping[str, object],
) -> TerminalComponent:
    if isinstance(value, TerminalComponent):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("terminal components must be TerminalComponent mappings")
    if set(value) - {"executable", "arguments", "operator_before"} or not {
        "executable",
        "arguments",
    } <= set(value):
        raise ValueError("terminal component has unknown or missing fields")
    arguments = value["arguments"]
    if not isinstance(arguments, (list, tuple)):
        raise TypeError("terminal component arguments must be an ordered sequence")
    return TerminalComponent(
        executable=_string(value["executable"], "executable"),
        arguments=tuple(_string(argument, "argument") for argument in arguments),
        operator_before=_string(value.get("operator_before", ""), "operator_before"),
    )


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"terminal {name} must be a string")
    return value


def _classify_component(component: TerminalComponent) -> TerminalDisposition:
    command = (
        component.executable.rsplit("/", maxsplit=1)[-1]
        .rsplit("\\", maxsplit=1)[-1]
        .casefold()
    )
    arguments = tuple(argument.casefold() for argument in component.arguments)
    joined = " ".join((command, *arguments))

    if _is_hard_prohibited(command, arguments, joined):
        return TerminalDisposition.HARD_PROHIBITED
    if _is_mandatory_fresh(command, arguments, joined):
        return TerminalDisposition.MANDATORY_FRESH
    if _uses_protected_resource(arguments):
        return TerminalDisposition.PROTECTED_APPROVAL
    if _is_safe_read(command, arguments):
        return TerminalDisposition.SAFE_READ
    return TerminalDisposition.ORDINARY_APPROVAL


def _is_hard_prohibited(command: str, arguments: tuple[str, ...], joined: str) -> bool:
    if command in {"shred", "wipefs", "mkfs", "format", "ngrok"}:
        return True
    if command in {"rm", "rmdir", "del", "remove-item"} and any(
        target in {"/", "*", ".", "c:\\", "c:/"} or "*" in target
        for target in arguments
    ):
        return True
    if command == "git" and (
        ("reset" in arguments and "--hard" in arguments)
        or "clean" in arguments
        or ("push" in arguments and any("force" in argument for argument in arguments))
    ):
        return True
    return any(
        marker in joined
        for marker in (
            "audit",
            "setenforce 0",
            "ufw disable",
            "history -c",
            "wevtutil cl",
            "systemctl restart jarvis",
        )
    )


def _is_mandatory_fresh(command: str, arguments: tuple[str, ...], joined: str) -> bool:
    if command in {
        "apt",
        "apt-get",
        "dnf",
        "yum",
        "brew",
        "winget",
        "choco",
        "pip",
        "pip3",
        "npm",
    } and any(
        argument in {"install", "remove", "upgrade", "uninstall", "update"}
        for argument in arguments
    ):
        return True
    if command in {"sh", "bash", "zsh", "cmd", "powershell", "pwsh"}:
        return True
    if any(token in joined for token in ("$(", "`", "${", "*")):
        return True
    return command in {"curl", "wget", "invoke-webrequest"} and any(
        token in {"|", "-command", "iex"} for token in arguments
    )


def _uses_protected_resource(arguments: tuple[str, ...]) -> bool:
    markers = (
        ".ssh",
        ".aws",
        ".gnupg",
        "credential",
        "secret",
        "token",
        "id_ed25519",
        "id_rsa",
        "audit",
        "backup",
        "deleted-conversation",
    )
    return any(marker in argument for argument in arguments for marker in markers)


def _is_safe_read(command: str, arguments: tuple[str, ...]) -> bool:
    if command in {"pwd", "ls", "dir", "get-childitem"}:
        return not any(
            argument.startswith("-") and argument not in {"-a", "-l", "-la", "/a"}
            for argument in arguments
        )
    if command == "git":
        return arguments[:1] in {
            ("status",),
            ("diff",),
            ("log",),
            ("show",),
        } and not any(argument.startswith("--output") for argument in arguments)
    return command in {"cat", "head", "tail", "type"} and bool(arguments)


def _result(
    disposition: TerminalDisposition,
    components: tuple[TerminalDisposition, ...],
) -> TerminalPolicyResult:
    return TerminalPolicyResult(
        disposition=disposition,
        component_dispositions=components,
        reason=disposition.value,
    )

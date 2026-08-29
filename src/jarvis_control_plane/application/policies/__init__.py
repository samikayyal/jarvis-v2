"""Application authorization policies."""

from .terminal import (
    TerminalAction,
    TerminalComponent,
    TerminalDisposition,
    TerminalPolicyResult,
    authorize_terminal_action,
    authorize_terminal_proposal,
    terminal_action_from_proposal,
)

__all__ = [
    "TerminalAction",
    "TerminalComponent",
    "TerminalDisposition",
    "TerminalPolicyResult",
    "authorize_terminal_action",
    "authorize_terminal_proposal",
    "terminal_action_from_proposal",
]

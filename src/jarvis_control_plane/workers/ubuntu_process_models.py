"""Internal models and systemd call adapter for Ubuntu process scopes."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from threading import Event, RLock
from typing import Protocol


@dataclass(slots=True)
class _StartingSystemdScope:
    cancel_requested: Event
    resolved: Event


@dataclass(slots=True)
class _RunningSystemdScope:
    unit_name: str
    process: subprocess.Popen[bytes]
    cancel_requested: Event
    termination_lock: RLock
    unit_observed: Event


class _UbuntuProcessScopeAdapter(Protocol):  # noqa: PYI046
    """Local seam for systemd unit observation and whole-unit signalling.

    The process-scope policy remains in :class:`SystemdUbuntuProcessScope`:
    it owns process launch, bounded stream capture, deadline handling, the
    TERM-then-KILL sequence, and cleanup state.  Implementations here only
    perform the systemd calls needed by that policy, which keeps those calls
    replaceable by a deterministic adapter in contract tests.
    """

    def check_unit(
        self, unit_name: str, *, timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]: ...

    def signal_unit(
        self, unit_name: str, signal: str, *, timeout_seconds: float
    ) -> None: ...


class _SystemdUbuntuProcessScopeAdapter:
    """Production systemd implementation of the local process-scope seam."""

    def __init__(self, *, systemctl_path: str) -> None:
        self._systemctl_path = systemctl_path

    def check_unit(
        self, unit_name: str, *, timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            (
                self._systemctl_path,
                "--user",
                "is-active",
                unit_name,
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout_seconds,
            text=True,
        )

    def signal_unit(
        self, unit_name: str, signal: str, *, timeout_seconds: float
    ) -> None:
        subprocess.run(
            (
                self._systemctl_path,
                "--user",
                "kill",
                "--kill-whom=all",
                f"--signal={signal}",
                unit_name,
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout_seconds,
        )

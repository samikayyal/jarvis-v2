"""Lifecycle and systemd-control operations for Ubuntu process scopes."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath
from threading import RLock
from time import monotonic, sleep
from typing import cast

from ..ports import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ActionDispatcherError,
)
from ..terminal_policy import TerminalComponent
from .contracts import WorkerInvocation
from .ubuntu_process_models import (
    _RunningSystemdScope,
    _StartingSystemdScope,
    _SystemdUbuntuProcessScopeAdapter,
    _UbuntuProcessScopeAdapter,
)


class _UbuntuProcessLifecycleMixin:
    def __init__(
        self,
        *,
        systemd_run_path: str = "/usr/bin/systemd-run",
        systemctl_path: str = "/usr/bin/systemctl",
        process_limit: int = 32,
        systemd_adapter: _UbuntuProcessScopeAdapter | None = None,
    ) -> None:
        if isinstance(process_limit, bool) or not isinstance(process_limit, int):
            raise TypeError("Ubuntu process limit must be an integer")
        if not 1 <= process_limit <= 64:
            raise ValueError("Ubuntu process limit must be between one and 64")
        for name, value in (
            ("systemd-run", systemd_run_path),
            ("systemctl", systemctl_path),
        ):
            if not isinstance(value, str) or not PurePosixPath(value).is_absolute():
                raise ValueError(f"{name} path must be absolute")
        self._systemd_run_path = systemd_run_path
        self._systemd_adapter = (
            systemd_adapter
            if systemd_adapter is not None
            else _SystemdUbuntuProcessScopeAdapter(systemctl_path=systemctl_path)
        )
        self._process_limit = process_limit
        runtime_uid = os.getuid() if hasattr(os, "getuid") else 0
        self._user_runtime_directory = f"/run/user/{runtime_uid}"
        self._lock = RLock()
        self._running: dict[str, _RunningSystemdScope] = {}
        self._starting: dict[str, _StartingSystemdScope] = {}
        self._active_action_ids: set[str] = set()
        self._reserved_action_ids: set[str] = set()
        self._cancelled_action_ids: set[str] = set()
        self._stopped_action_ids: set[str] = set()

    def reserve(self, *, action_id: str) -> None:
        with self._lock:
            if self._active_action_ids:
                raise ActionDispatcherError("native Ubuntu process scope is busy")
            known = (
                self._reserved_action_ids
                | self._active_action_ids
                | self._cancelled_action_ids
                | self._stopped_action_ids
            )
            if action_id in known:
                raise ActionDispatcherError(
                    f"Ubuntu process scope {action_id} is already reserved"
                )
            self._reserved_action_ids.add(action_id)

    def retire(self, *, action_id: str) -> None:
        with self._lock:
            self._reserved_action_ids.discard(action_id)
            self._cancelled_action_ids.discard(action_id)
            self._stopped_action_ids.discard(action_id)

    def command_for(self, invocation: WorkerInvocation) -> tuple[str, ...]:
        """Return the exact argv used to create the bounded native scope."""

        if invocation.interactive:
            raise ActionDispatcherError("Ubuntu process scopes are non-interactive")
        if invocation.action.host != "ubuntu":
            raise ActionDispatcherError("Ubuntu process scope rejected another host")
        components = tuple(
            cast(TerminalComponent, component)
            for component in invocation.action.components
        )
        if any(component.redirections for component in components):
            raise ActionDispatcherError(
                "Ubuntu process scope cannot execute directionless redirections"
            )
        unit_name = self._unit_name(invocation.action_id)
        action_command = self._action_command(components)
        return (
            self._systemd_run_path,
            "--user",
            "--quiet",
            "--wait",
            "--collect",
            "--pipe",
            "--service-type=exec",
            f"--unit={unit_name}",
            f"--property=TasksMax={self._process_limit}",
            "--property=NoNewPrivileges=yes",
            "--property=RestrictNamespaces=yes",
            (
                "--property=InaccessiblePaths=/run/systemd/private "
                "/run/dbus/system_bus_socket "
                f"{self._user_runtime_directory}/systemd/private "
                f"{self._user_runtime_directory}/bus"
            ),
            f"--property=RuntimeMaxSec={invocation.deadline_seconds}s",
            f"--property=TimeoutStopSec={invocation.cancellation_grace_seconds}s",
            f"--working-directory={invocation.action.cwd}",
            "--",
            *action_command,
        )

    @staticmethod
    def _action_command(
        components: tuple[TerminalComponent, ...],
    ) -> tuple[str, ...]:
        if len(components) == 1:
            component = components[0]
            return (component.executable, *component.arguments)
        plan = [
            {
                "executable": component.executable,
                "arguments": list(component.arguments),
                "operator_before": component.operator_before,
            }
            for component in components
        ]
        encoded = base64.urlsafe_b64encode(
            json.dumps(plan, separators=(",", ":")).encode()
        ).decode()
        return (
            sys.executable,
            str(Path(__file__).with_name("ubuntu_worker_runner.py").resolve()),
            encoded,
        )

    def cancel(
        self, *, action_id: str, timeout_seconds: int
    ) -> ActionCancellationResult:
        deadline = monotonic() + timeout_seconds
        with self._lock:
            if action_id in self._reserved_action_ids:
                self._reserved_action_ids.remove(action_id)
                self._cancelled_action_ids.add(action_id)
                return ActionCancellationResult(ActionCancellationStatus.NOT_STARTED)
            starting = self._starting.get(action_id)
            running = self._running.get(action_id)
        if starting is not None:
            starting.cancel_requested.set()
            if not starting.resolved.wait(timeout=max(deadline - monotonic(), 0)):
                return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
            with self._lock:
                if action_id in self._stopped_action_ids:
                    return ActionCancellationResult(ActionCancellationStatus.STOPPED)
                if action_id in self._cancelled_action_ids:
                    return ActionCancellationResult(
                        ActionCancellationStatus.NOT_STARTED
                    )
                running = self._running.get(action_id)
        if running is None:
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
        running.cancel_requested.set()
        remaining = deadline - monotonic()
        if remaining <= 0:
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
        stopped = self._stop_scope(running, remaining)
        if stopped:
            with self._lock:
                if self._running.get(action_id) is running:
                    del self._running[action_id]
                self._active_action_ids.discard(action_id)
                self._stopped_action_ids.add(action_id)
        return ActionCancellationResult(
            ActionCancellationStatus.STOPPED
            if stopped
            else ActionCancellationStatus.UNKNOWN
        )

    def _stop_scope(
        self, running: _RunningSystemdScope, timeout_seconds: float
    ) -> bool:
        deadline = monotonic() + timeout_seconds
        with running.termination_lock:
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            if self._unit_is_stopped(
                running,
                timeout_seconds=min(remaining, 1),
                wrapper_completed=running.process.poll() is not None,
            ):
                return True
            self._signal_unit(running.unit_name, "TERM", deadline)
            if running.process.poll() is None:
                remaining = max(deadline - monotonic(), 0.001)
                try:
                    running.process.wait(timeout=max(remaining / 2, 0.001))
                except subprocess.TimeoutExpired:
                    pass
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            if self._unit_is_stopped(
                running,
                timeout_seconds=min(remaining, 1),
                wrapper_completed=running.process.poll() is not None,
            ):
                return True
            self._signal_unit(running.unit_name, "KILL", deadline)
            if running.process.poll() is None:
                remaining = max(deadline - monotonic(), 0.001)
                try:
                    running.process.wait(timeout=max(remaining / 2, 0.001))
                except subprocess.TimeoutExpired:
                    return False
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            return self._unit_is_stopped(
                running,
                timeout_seconds=remaining,
                wrapper_completed=running.process.poll() is not None,
            )

    def _signal_unit(self, unit_name: str, signal: str, deadline: float) -> None:
        remaining = max(deadline - monotonic(), 0.001)
        try:
            self._systemd_adapter.signal_unit(
                unit_name,
                signal,
                timeout_seconds=remaining,
            )
        except (OSError, subprocess.TimeoutExpired):
            return

    def _unit_is_stopped(
        self,
        running: _RunningSystemdScope,
        *,
        timeout_seconds: float,
        wrapper_completed: bool = False,
    ) -> bool:
        if timeout_seconds <= 0:
            return False
        deadline = monotonic() + timeout_seconds
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            try:
                check = self._systemd_adapter.check_unit(
                    running.unit_name,
                    timeout_seconds=min(remaining, 0.25),
                )
            except subprocess.TimeoutExpired:
                continue
            except OSError:
                return False
            state = check.stdout.strip()
            if state in {"active", "activating", "deactivating", "inactive", "failed"}:
                running.unit_observed.set()
            if check.returncode == 3 and state in {"inactive", "failed"}:
                return True
            if (
                wrapper_completed
                and check.returncode == 4
                and state in {"inactive", "unknown"}
            ):
                return True
            if state not in {"active", "activating", "deactivating"}:
                return False
            sleep(min(0.05, max(deadline - monotonic(), 0)))

    @staticmethod
    def _unit_name(action_id: str) -> str:
        digest = hashlib.sha256(action_id.encode()).hexdigest()[:24]
        return f"jarvis-action-{digest}.service"

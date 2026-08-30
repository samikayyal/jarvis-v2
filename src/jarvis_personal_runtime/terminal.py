"""Approval-gated terminal tool for the native Ubuntu host."""

from __future__ import annotations

import asyncio
import base64
import codecs
import json
import os
import shlex
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .permissions import PermissionRule
from .runtime import ApprovalRequired, PendingAction


@dataclass(frozen=True, slots=True)
class CommandResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    output_truncated: bool = False
    remote_effect_uncertain: bool = False


class CommandExecutor(Protocol):
    async def run(
        self,
        command: str,
        *,
        cwd: str | Path,
        timeout: float,
        max_output_chars: int,
    ) -> CommandResult: ...


class CommandPermissions(Protocol):
    def matches(self, host: str, command: str) -> PermissionRule | None: ...


class TerminalTrace(Protocol):
    def record(self, event: str, payload: dict[str, object]) -> None: ...


class _NoTrace:
    def record(self, event: str, payload: dict[str, object]) -> None:
        return None


@dataclass(frozen=True, slots=True)
class TerminalContinuation:
    host: str
    command: str


_COMPOUND = frozenset(";|&<>\n\r`#")
_SCRIPT_RUNNERS = frozenset(
    {
        "bash",
        "dash",
        "fish",
        "node",
        "perl",
        "python",
        "python3",
        "ruby",
        "sh",
        "source",
        "zsh",
        ".",
    }
)


def _is_simple_command(command: str) -> bool:
    quote: str | None = None
    escaped = False
    for index, character in enumerate(command):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote != "'":
            escaped = True
            continue
        if quote:
            if character == quote:
                quote = None
            elif quote == '"' and character in {"$", "`"}:
                return False
            continue
        if character in {"'", '"'}:
            quote = character
        elif character in _COMPOUND or character == "$":
            return False
    if quote is not None or escaped:
        return False
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        return False
    if not words:
        return False
    executable = Path(words[0]).name
    return executable not in _SCRIPT_RUNNERS and not executable.endswith(".sh")


def _matches_prefix(command: str, prefix: str) -> bool:
    return command == prefix or (
        command.startswith(prefix) and command[len(prefix) : len(prefix) + 1].isspace()
    )


def _is_simple_windows_command(command: str) -> bool:
    if any(
        character in _COMPOUND or character in {"$", "(", ")", "{", "}"}
        for character in command
    ):
        return False
    quote: str | None = None
    for character in command:
        if character in {"'", '"'}:
            quote = (
                None if quote == character else character if quote is None else quote
            )
    if quote is not None:
        return False
    executable = command.split(maxsplit=1)[0].strip("'\"").casefold()
    return executable not in {
        "cmd",
        "cmd.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
    } and not executable.endswith((".bat", ".cmd", ".ps1"))


def _matches_windows_prefix(command: str, prefix: str) -> bool:
    return _matches_prefix(command.casefold(), prefix.casefold())


class RunTerminalTool:
    definitions: tuple[dict[str, object], ...] = (
        {
            "type": "function",
            "name": "run_terminal",
            "description": "Run one terminal command on the named execution host.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "enum": ["ubuntu"]},
                    "command": {"type": "string", "minLength": 1},
                },
                "required": ["host", "command"],
                "additionalProperties": False,
            },
        },
    )

    def __init__(
        self,
        *,
        working_directory: Path,
        read_only_prefixes: tuple[str, ...],
        permission_store: CommandPermissions,
        executor: CommandExecutor,
        windows_working_directory: str | None = None,
        windows_read_only_prefixes: tuple[str, ...] = (),
        windows_executor: CommandExecutor | None = None,
        timeout_seconds: float,
        max_output_chars: int,
        trace: TerminalTrace | None = None,
    ) -> None:
        self._working_directory = working_directory.resolve()
        if not self._working_directory.is_dir():
            raise ValueError("Ubuntu working directory must be an existing directory")
        self._read_only_prefixes = tuple(
            prefix.strip() for prefix in read_only_prefixes
        )
        if (windows_working_directory is None) != (windows_executor is None):
            raise ValueError(
                "Windows working directory and executor must be configured together"
            )
        self._windows_working_directory = windows_working_directory
        self._windows_read_only_prefixes = tuple(
            prefix.strip() for prefix in windows_read_only_prefixes
        )
        self._windows_executor = windows_executor
        self._permission_store = permission_store
        self._executor = executor
        self._timeout_seconds = timeout_seconds
        if max_output_chars < 2:
            raise ValueError("terminal output limit must be at least 2 characters")
        self._max_output_chars = max_output_chars
        self._trace = trace or _NoTrace()
        hosts = ["ubuntu"]
        if windows_executor is not None:
            hosts.append("windows")
        definition = dict(self.definitions[0])
        parameters = dict(definition["parameters"])
        properties = dict(parameters["properties"])
        properties["host"] = {"type": "string", "enum": hosts}
        parameters["properties"] = properties
        definition["parameters"] = parameters
        self.definitions = (definition,)

    async def execute(
        self, name: str, arguments: dict[str, object]
    ) -> str | ApprovalRequired:
        if name != "run_terminal":
            raise ValueError(f"unknown prepared tool: {name}")
        if set(arguments) != {"host", "command"}:
            raise ValueError("run_terminal arguments must be exactly host and command")
        host = arguments["host"]
        command = arguments["command"]
        if host not in {"ubuntu", "windows"}:
            raise ValueError("run_terminal host must be ubuntu or windows")
        if host == "windows" and self._windows_executor is None:
            raise ValueError("run_terminal Windows host is not configured")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("run_terminal command must be a non-empty string")
        command = command.strip()
        saved = self._permission_store.matches(host, command)
        if host == "windows":
            read_only = _is_simple_windows_command(command) and any(
                _matches_windows_prefix(command, prefix)
                for prefix in self._windows_read_only_prefixes
            )
            working_directory = self._windows_working_directory
        else:
            read_only = _is_simple_command(command) and any(
                _matches_prefix(command, prefix) for prefix in self._read_only_prefixes
            )
            working_directory = str(self._working_directory)
        assert working_directory is not None
        if saved is None and not read_only:
            command_literal = json.dumps(command, ensure_ascii=False)
            cwd_literal = json.dumps(working_directory, ensure_ascii=False)
            display = (
                "Run terminal command?\n"
                f"Host: {host}\n"
                f"Command: {command_literal}\n"
                f"Working directory: {cwd_literal}\n"
                f"Timeout: {self._timeout_seconds:g} seconds\n"
                f"Permission host: {host}\n"
                f"Permission literal prefix: {command_literal}"
            )
            self._trace.record(
                "terminal_proposal",
                {
                    "host": host,
                    "command": command,
                    "working_directory": working_directory,
                    "timeout_seconds": self._timeout_seconds,
                    "permission_prefix": command,
                    "display": display,
                },
            )
            return ApprovalRequired(
                PendingAction(host=host, prefix=command, display=display),
                TerminalContinuation(host, command),
            )
        return await self.run_approved(host, command)

    async def run_approved(self, host: str, command: str) -> str:
        if host == "windows":
            executor = self._windows_executor
            working_directory: str | Path | None = self._windows_working_directory
        else:
            executor = self._executor
            working_directory = self._working_directory
        if executor is None or working_directory is None:
            raise ValueError(f"run_terminal {host} host is not configured")
        event = {
            "host": host,
            "command": command,
            "working_directory": str(working_directory),
            "timeout_seconds": self._timeout_seconds,
        }
        self._trace.record("terminal_execution_started", event)
        try:
            result = await executor.run(
                command,
                cwd=working_directory,
                timeout=self._timeout_seconds,
                max_output_chars=self._max_output_chars,
            )
        except asyncio.CancelledError:
            cancellation = {**event, "scope": "local_best_effort"}
            if host == "windows":
                cancellation["remote_effect_uncertain"] = True
            self._trace.record(
                "terminal_execution_cancelled",
                cancellation,
            )
            raise
        except Exception as exc:
            self._trace.record(
                "terminal_execution_error",
                {**event, "error": f"{type(exc).__name__}: {exc}"},
            )
            raise
        self._trace.record(
            "terminal_execution_finished",
            {
                **event,
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "timed_out": result.timed_out,
                "output_truncated": result.output_truncated,
                "remote_effect_uncertain": result.remote_effect_uncertain,
            },
        )
        return self._bounded_result(result)

    async def resume(self, continuation: object, *, approved: bool) -> str:
        if not isinstance(continuation, TerminalContinuation):
            raise TypeError("invalid terminal approval continuation")
        if not approved:
            return json.dumps({"rejected": True}, separators=(",", ":"))
        return await self.run_approved(continuation.host, continuation.command)

    def _bounded_result(self, result: CommandResult) -> str:
        payload: dict[str, object] = {
            "exit_code": result.exit_code,
            "output_truncated": result.output_truncated,
            "remote_effect_uncertain": result.remote_effect_uncertain,
            "stderr": result.stderr,
            "stdout": result.stdout,
            "timed_out": result.timed_out,
        }
        while True:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if len(encoded) <= self._max_output_chars:
                return encoded
            payload["output_truncated"] = True
            key = "stderr" if payload["stderr"] else "stdout"
            value = str(payload[key])
            if not value:
                minimal = '{"output_truncated":true}'
                return minimal if len(minimal) <= self._max_output_chars else "{}"
            remove = min(len(value), max(1, len(encoded) - self._max_output_chars))
            payload[key] = value[:-remove]


@dataclass(slots=True)
class _OutputBudget:
    remaining: int
    truncated: bool = False

    def take(self, text: str) -> str:
        if len(text) <= self.remaining:
            self.remaining -= len(text)
            return text
        accepted = text[: self.remaining]
        self.remaining = 0
        self.truncated = True
        return accepted


async def _read_stream(
    stream: asyncio.StreamReader, budget: _OutputBudget, lock: asyncio.Lock
) -> str:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    parts: list[str] = []
    while chunk := await stream.read(4096):
        text = decoder.decode(chunk)
        async with lock:
            accepted = budget.take(text)
            if accepted:
                parts.append(accepted)
    tail = decoder.decode(b"", final=True)
    async with lock:
        accepted = budget.take(tail)
        if accepted:
            parts.append(accepted)
    return "".join(parts)


async def _settle(tasks: set[asyncio.Task[object]]) -> None:
    _, pending = await asyncio.wait(tasks, timeout=1)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _text_result(task: asyncio.Task[str]) -> str:
    if task.cancelled() or not task.done() or task.exception() is not None:
        return ""
    return task.result()


async def _capture_process(
    process: Any,
    *,
    timeout: float,
    max_output_chars: int,
    stop: Callable[[Any], Awaitable[None]],
    remote: bool,
) -> CommandResult:
    assert process.stdout is not None and process.stderr is not None
    budget = _OutputBudget(max_output_chars)
    lock = asyncio.Lock()
    stdout_task = asyncio.create_task(_read_stream(process.stdout, budget, lock))
    stderr_task = asyncio.create_task(_read_stream(process.stderr, budget, lock))
    wait_task = asyncio.create_task(process.wait())
    tasks = {wait_task, stdout_task, stderr_task}
    timed_out = False
    try:
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            timed_out = True
            await stop(process)
            await _settle(tasks)
    except asyncio.CancelledError:
        await stop(process)
        await _settle(tasks)
        raise
    return CommandResult(
        exit_code=process.returncode,
        stdout=_text_result(stdout_task),
        stderr=_text_result(stderr_task),
        timed_out=timed_out,
        output_truncated=budget.truncated,
        remote_effect_uncertain=remote and (timed_out or process.returncode == 255),
    )


class NativeUbuntuExecutor:
    """Run one shell command locally with bounded capture and process-group cleanup."""

    async def run(
        self,
        command: str,
        *,
        cwd: Path,
        timeout: float,
        max_output_chars: int,
    ) -> CommandResult:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        return await _capture_process(
            process,
            timeout=timeout,
            max_output_chars=max_output_chars,
            stop=self._stop,
            remote=False,
        )

    @staticmethod
    async def _stop(process: asyncio.subprocess.Process) -> None:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            await asyncio.sleep(0.1)
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return
        if process.returncode is not None:
            return
        process.terminate()
        try:
            async with asyncio.timeout(1):
                await process.wait()
                return
        except TimeoutError:
            pass
        try:
            process.kill()
        except ProcessLookupError:
            return
        await process.wait()


class OpenSshWindowsExecutor:
    """Run one encoded PowerShell command through ordinary OpenSSH."""

    def __init__(
        self,
        *,
        host: str,
        user: str,
        identity_file: Path,
        process_factory: Callable[..., Awaitable[Any]] = asyncio.create_subprocess_exec,
    ) -> None:
        self._target = f"{user}@{host}"
        self._identity_file = identity_file
        self._process_factory = process_factory

    async def run(
        self,
        command: str,
        *,
        cwd: str | Path,
        timeout: float,
        max_output_chars: int,
    ) -> CommandResult:
        directory = str(cwd).replace("'", "''")
        script = f"Set-Location -LiteralPath '{directory}'; {command}"
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        process = await self._process_factory(
            "ssh",
            "-i",
            str(self._identity_file),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "--",
            self._target,
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            encoded,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return await _capture_process(
            process,
            timeout=timeout,
            max_output_chars=max_output_chars,
            stop=self._stop,
            remote=True,
        )

    @staticmethod
    async def _stop(process: Any) -> None:
        if process.returncode is not None:
            return
        process.terminate()
        try:
            async with asyncio.timeout(1):
                await process.wait()
                return
        except TimeoutError:
            pass
        process.kill()
        await process.wait()


__all__ = [
    "CommandResult",
    "NativeUbuntuExecutor",
    "OpenSshWindowsExecutor",
    "RunTerminalTool",
    "TerminalContinuation",
]

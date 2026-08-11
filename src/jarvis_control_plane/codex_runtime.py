"""Production adapters for the bounded Codex specialist composition root."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
from pathlib import Path
from threading import Lock
from time import monotonic

from .codex_specialist import (
    CodexAdapterResult,
    CodexExecutionEnvelope,
    CodexInterruption,
    CodexPolicyError,
    CodexVerificationError,
    CodexWorkspace,
    CodexWorkspaceSnapshot,
)


class CodexCliAdapter:
    """Invoke the pinned Codex CLI with Jarvis-owned read-only settings."""

    def __init__(self, *, executable: Path, api_key: str) -> None:
        if not executable.is_file():
            raise CodexPolicyError("the pinned Codex executable is unavailable")
        if not api_key or api_key.strip() != api_key:
            raise CodexPolicyError("the Codex API credential is unavailable")
        self._executable = executable
        self._api_key = api_key
        self._lock = Lock()
        self._processes: dict[str, subprocess.Popen[str]] = {}

    def invoke(
        self, envelope: CodexExecutionEnvelope, *, deadline: float
    ) -> CodexAdapterResult:
        if envelope.operation not in {"inspect", "review"}:
            raise CodexPolicyError(
                "the deployed orchestration seam permits read-only Codex work only"
            )
        command = [
            str(self._executable),
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "--model",
            envelope.model,
            "--config",
            f'model_reasoning_effort="{envelope.reasoning}"',
            "--cd",
            envelope.cwd,
            "-",
        ]
        environment = {
            "HOME": "/tmp/codex-home",
            "OPENAI_API_KEY": self._api_key,
            "PATH": os.environ.get("PATH", ""),
        }
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="strict",
            env=environment,
            start_new_session=True,
        )
        with self._lock:
            self._processes[envelope.request_id] = process
        try:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError
            stdout, stderr = process.communicate(
                self._prompt(envelope), timeout=remaining
            )
        except subprocess.TimeoutExpired as exc:
            self._stop(process)
            raise TimeoutError("Codex CLI exceeded its frozen deadline") from exc
        finally:
            with self._lock:
                self._processes.pop(envelope.request_id, None)
        if process.returncode != 0:
            message = stderr.strip()[:500]
            raise CodexVerificationError(
                f"Codex CLI failed without verified output: {message}"
            )
        return self._parse_events(stdout)

    def interrupt(self, request_id: str, *, deadline: float) -> CodexInterruption:
        with self._lock:
            process = self._processes.get(request_id)
        if process is not None:
            self._stop(process)
            remaining = max(0.0, deadline - monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                raise CodexVerificationError(
                    "Codex process scope did not become quiescent"
                ) from exc
        return CodexInterruption(
            result=CodexAdapterResult(
                status="failed",
                summary="Codex invocation was interrupted before verification.",
                changed_paths=(),
                test_evidence=(),
                unresolved_questions=("Retry with a fresh authorized request.",),
                thread_id=f"interrupted-{request_id}",
            )
        )

    @staticmethod
    def _stop(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=2)

    @staticmethod
    def _prompt(envelope: CodexExecutionEnvelope) -> str:
        return (
            "Perform only the requested read-only workspace operation. Do not modify "
            "files, Git refs, configuration, or external state. Return exactly one "
            "JSON object with keys status, summary, changed_paths, test_evidence, and "
            "unresolved_questions. status must be completed, incomplete, or failed; "
            "the three list fields must contain strings.\n\n"
            f"Operation: {envelope.operation}\nTask: {envelope.task}\n"
        )

    @staticmethod
    def _parse_events(stdout: str) -> CodexAdapterResult:
        thread_id: str | None = None
        message: str | None = None
        try:
            for line in stdout.splitlines():
                event = json.loads(line)
                if event.get("type") == "thread.started":
                    thread_id = event.get("thread_id")
                item = event.get("item")
                if (
                    event.get("type") == "item.completed"
                    and isinstance(item, dict)
                    and item.get("type") == "agent_message"
                ):
                    message = item.get("text")
            payload = json.loads(message or "")
            if not isinstance(thread_id, str) or not isinstance(payload, dict):
                raise TypeError
            return CodexAdapterResult(
                status=payload["status"],
                summary=payload["summary"],
                changed_paths=tuple(payload["changed_paths"]),
                test_evidence=tuple(payload["test_evidence"]),
                unresolved_questions=tuple(payload["unresolved_questions"]),
                thread_id=thread_id,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CodexVerificationError(
                "Codex CLI returned malformed structured output"
            ) from exc


class GitCodexWorkspaceInspector:
    """Capture independent Git and content evidence without repository hooks."""

    _GIT_PREFIX = (
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "diff.external=",
        "-c",
        "core.attributesFile=/dev/null",
    )

    def snapshot(self, workspace: CodexWorkspace) -> CodexWorkspaceSnapshot:
        root = Path(workspace.cwd)
        head = self._git(root, "rev-parse", "HEAD").strip()
        changed_paths = tuple(
            sorted(
                set(
                    self._nul_paths(
                        self._git(root, "diff", "--no-ext-diff", "--name-only", "-z")
                    )
                    + self._nul_paths(
                        self._git(
                            root,
                            "diff",
                            "--cached",
                            "--no-ext-diff",
                            "--name-only",
                            "-z",
                        )
                    )
                    + self._nul_paths(
                        self._git(
                            root,
                            "ls-files",
                            "--others",
                            "--exclude-standard",
                            "-z",
                        )
                    )
                )
            )
        )
        refs = self._git(
            root,
            "for-each-ref",
            "--format=%(refname:short) %(objectname)",
            "refs/remotes",
        )
        remote_refs = tuple(
            sorted(tuple(line.rsplit(" ", 1)) for line in refs.splitlines() if line)
        )
        digests: list[tuple[str, str | None]] = []
        for relative in changed_paths:
            candidate = (root / relative).resolve()
            try:
                candidate.relative_to(root.resolve())
            except ValueError as exc:
                raise CodexVerificationError(
                    "workspace status escaped the configured root"
                ) from exc
            digests.append(
                (
                    relative,
                    hashlib.sha256(candidate.read_bytes()).hexdigest()
                    if candidate.is_file()
                    else None,
                )
            )
        return CodexWorkspaceSnapshot(
            head=head,
            remote_refs=remote_refs,
            changed_paths=changed_paths,
            file_digests=tuple(digests),
        )

    @staticmethod
    def _nul_paths(output: str) -> tuple[str, ...]:
        return tuple(path.replace("\\", "/") for path in output.split("\0") if path)

    @classmethod
    def _git(cls, root: Path, *arguments: str) -> str:
        completed = subprocess.run(
            (*cls._GIT_PREFIX, *arguments),
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=20,
        )
        if completed.returncode != 0:
            raise CodexVerificationError("independent Git inspection failed")
        return completed.stdout


__all__ = ["CodexCliAdapter", "GitCodexWorkspaceInspector"]

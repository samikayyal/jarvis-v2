from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path

from jarvis_personal_runtime.config import RuntimeConfig
from jarvis_personal_runtime.runtime import (
    ApprovalDecision,
    ApprovalRequired,
    Completed,
    ContextLimitReached,
    InboundText,
    PendingAction,
    PersonalRuntime,
    RuntimeDisposition,
    build_runtime,
)
from jarvis_personal_runtime.trace import TRACE_FAILURE_WARNING, JsonlRuntimeTrace

NOW = datetime(2026, 8, 30, 12, tzinfo=UTC)


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


@dataclass
class FakeClock:
    current: datetime = NOW

    def now(self) -> datetime:
        return self.current


class FakeRunner:
    def __init__(self, step: object = Completed("done")) -> None:
        self.step = step
        self.calls: list[tuple[str, str, str]] = []
        self.resumes: list[tuple[ApprovalDecision, object]] = []
        self.system_prompts: list[str] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.wait = False
        self.sessions_started = 0
        self.cancelled_pending: list[object] = []

    def start_session(self) -> None:
        self.sessions_started += 1

    async def run(
        self, text: str, *, model: str, reasoning: str, system_prompt: str
    ) -> object:
        self.calls.append((text, model, reasoning))
        self.system_prompts.append(system_prompt)
        self.started.set()
        if self.wait:
            await self.release.wait()
        return self.step

    async def resume(self, decision: ApprovalDecision, continuation: object) -> object:
        self.resumes.append((decision, continuation))
        return Completed("resumed")

    def cancel_pending(self, continuation: object) -> None:
        self.cancelled_pending.append(continuation)


class FakePermissionStore:
    def __init__(self) -> None:
        self.saved: list[tuple[str, str]] = []
        self.removed: list[object] = []
        self.fail = False

    def list_rules(self) -> tuple[object, ...]:
        return ()

    def add(self, host: str, prefix: str) -> object:
        if self.fail:
            raise OSError("cannot write permissions")
        self.saved.append((host, prefix))
        return object()

    def remove(self, selector: object) -> bool:
        self.removed.append(selector)
        return True


class MemoryCache:
    def __init__(self) -> None:
        self.entries: dict[str, datetime] = {}

    def claim(self, message_id: str, now: datetime) -> bool:
        self.entries = {
            key: seen
            for key, seen in self.entries.items()
            if now - seen < timedelta(days=7)
        }
        if message_id in self.entries:
            return False
        self.entries[message_id] = now
        return True


def inbound(message_id: str, text: str, at: datetime = NOW) -> InboundText:
    return InboundText(message_id=message_id, text=text, received_at=at)


@async_test
async def test_ordinary_text_starts_one_request_and_returns_completed_reply() -> None:
    runner = FakeRunner()
    runtime = PersonalRuntime(
        RuntimeConfig(model="gpt-5.6-luna", reasoning_effort="medium"),
        request_runner=runner,
    )

    result = await runtime.receive(inbound("m1", "hello"))

    assert result.disposition == "completed"
    assert result.replies == ("done",)
    assert runner.calls == [("hello", "gpt-5.6-luna", "medium")]
    assert runtime.status().active_request is None


@async_test
async def test_trace_storage_failure_warns_operator_without_blocking_reply(
    tmp_path: Path,
) -> None:
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("occupied", encoding="utf-8")
    trace = JsonlRuntimeTrace(parent_file / "runtime.jsonl", max_bytes=1_000)
    runner = FakeRunner(Completed("done"))
    runtime = PersonalRuntime(request_runner=runner, clock=FakeClock(), trace=trace)

    result = await runtime.receive(inbound("trace-failure", "hello"))

    assert result.disposition is RuntimeDisposition.COMPLETED
    assert result.replies == ("done", TRACE_FAILURE_WARNING)


@async_test
async def test_second_ordinary_message_is_refused_while_first_request_is_active() -> (
    None
):
    runner = FakeRunner()
    runner.wait = True
    runtime = PersonalRuntime(request_runner=runner)

    first = asyncio.create_task(runtime.receive(inbound("m1", "first")))
    await runner.started.wait()
    second = await runtime.receive(inbound("m2", "second"))

    assert second.disposition in {"busy", "busy_refused"}
    assert runner.calls == [("first", "gpt-5.6-luna", "medium")]

    runner.release.set()
    assert (await first).replies == ("done",)


@async_test
async def test_pending_modal_accepts_only_exact_choices_and_persists_before_resume() -> (
    None
):
    action = PendingAction(
        host="ubuntu", prefix="git status", display="Run git status?"
    )
    runner = FakeRunner(ApprovalRequired(action, "continuation"))
    permissions = FakePermissionStore()
    runtime = PersonalRuntime(request_runner=runner, permission_store=permissions)

    pending = await runtime.receive(inbound("m1", "run it"))
    assert pending.disposition == "approval_required"
    assert pending.replies == (
        "Run git status?\nReply 1 to approve once, 2 to save permission, or 9 to reject.",
    )

    ignored = await runtime.receive(inbound("m2", "yes"))
    assert ignored.replies == ()
    assert runner.resumes == []

    resumed = await runtime.receive(inbound("m3", "  2  "))
    assert resumed.replies == ("resumed",)
    assert permissions.saved == [("ubuntu", "git status")]
    assert runner.resumes == [(ApprovalDecision.SAVE_PERMISSION, "continuation")]


@async_test
async def test_pending_action_without_saved_permission_ignores_choice_two() -> None:
    action = PendingAction(
        host="google",
        prefix="google_calendar_create_event",
        display="Create the exact event?",
        allow_save_permission=False,
    )
    runner = FakeRunner(ApprovalRequired(action, "continuation"))
    permissions = FakePermissionStore()
    runtime = PersonalRuntime(request_runner=runner, permission_store=permissions)

    pending = await runtime.receive(inbound("m1", "create it"))
    ignored = await runtime.receive(inbound("m2", "2"))

    assert pending.replies == (
        "Create the exact event?\nReply 1 to approve once or 9 to reject.",
    )
    assert ignored.disposition == "ignored"
    assert runtime.status().pending_action == action
    assert permissions.saved == []
    assert runner.resumes == []


@async_test
async def test_google_connection_commands_are_exact_and_deterministic() -> None:
    class Connections:
        def __init__(self) -> None:
            self.connected = False

        def status(self) -> str:
            return f"Google: {'connected' if self.connected else 'disconnected'}"

        async def connect(self) -> str:
            self.connected = True
            return "Connected Google account."

        def disconnect(self) -> str:
            self.connected = False
            return "Disconnected Google account."

    connections = Connections()
    runtime = PersonalRuntime(
        request_runner=FakeRunner(Completed("unused")),
        connections=connections,
    )

    listed = await runtime.receive(inbound("m1", "/connections"))
    connected = await runtime.receive(inbound("m2", "/connect google"))
    malformed = await runtime.receive(inbound("m3", "/connect drive"))
    disconnected = await runtime.receive(inbound("m4", "/disconnect google"))

    assert listed.replies == ("Google: disconnected",)
    assert connected.replies == ("Connected Google account.",)
    assert malformed.disposition == "malformed_command"
    assert disconnected.replies == ("Disconnected Google account.",)


@async_test
async def test_failed_permission_save_keeps_pending_action_and_does_not_resume() -> (
    None
):
    action = PendingAction(host="ubuntu", prefix="rm file", display="Delete file?")
    runner = FakeRunner(ApprovalRequired(action, object()))
    permissions = FakePermissionStore()
    permissions.fail = True
    runtime = PersonalRuntime(request_runner=runner, permission_store=permissions)

    await runtime.receive(inbound("m1", "delete"))
    failed = await runtime.receive(inbound("m2", "2"))

    assert failed.disposition == "permission_save_failed"
    assert runtime.status().pending_action == action
    assert runner.resumes == []


@async_test
async def test_cancel_cancels_active_runner_and_suppresses_late_reply() -> None:
    runner = FakeRunner()
    runner.wait = True
    runtime = PersonalRuntime(request_runner=runner)

    first = asyncio.create_task(runtime.receive(inbound("m1", "long")))
    await runner.started.wait()
    session_id = runtime.status().session_id
    cancelled = await runtime.receive(inbound("m2", "/cancel"))

    assert cancelled.disposition == "cancelled"
    assert cancelled.replies
    assert runtime.status().active_request is None
    assert runtime.status().session_id == session_id

    runner.release.set()
    late = await first
    assert late.replies == ()


@async_test
async def test_model_and_reasoning_commands_are_deterministic_and_slash_unknown_never_runs() -> (
    None
):
    runner = FakeRunner()
    runtime = PersonalRuntime(request_runner=runner)

    model = await runtime.receive(inbound("m1", "/model sol"))
    reasoning = await runtime.receive(inbound("m2", "/reasoning max"))
    malformed = await runtime.receive(inbound("m3", "/model gpt-5.6-sol extra"))
    unknown = await runtime.receive(inbound("m4", "/not-a-command"))

    assert model.disposition == "command"
    assert reasoning.disposition == "command"
    assert malformed.disposition == "malformed_command"
    assert unknown.disposition == "unknown_command"
    assert runner.calls == []
    assert runtime.status().model == "gpt-5.6-sol"
    assert runtime.status().reasoning == "max"


@async_test
async def test_duplicate_ids_and_seven_day_expiry_are_deterministic() -> None:
    clock = FakeClock()
    cache = MemoryCache()
    runner = FakeRunner()
    runtime = PersonalRuntime(clock=clock, cache=cache, request_runner=runner)

    first = await runtime.receive(inbound("same", "hello"))
    duplicate = await runtime.receive(inbound("same", "hello again"))
    assert first.disposition == "completed"
    assert duplicate.disposition == "duplicate"
    assert len(runner.calls) == 1

    clock.current += timedelta(days=7, seconds=1)
    replay = await runtime.receive(inbound("same", "after retention"))
    assert replay.disposition == "completed"
    assert len(runner.calls) == 2


@async_test
async def test_pending_modal_suspends_inactivity_and_new_starts_fresh_session() -> None:
    clock = FakeClock()
    action = PendingAction(host="ubuntu", prefix="touch x", display="Touch x?")
    runner = FakeRunner(ApprovalRequired(action, "continuation"))
    runtime = PersonalRuntime(
        RuntimeConfig(inactivity_minutes=15),
        clock=clock,
        request_runner=runner,
    )

    await runtime.receive(inbound("m1", "touch"))
    first_session = runtime.status().session_id
    clock.current += timedelta(hours=2)
    ignored = await runtime.receive(inbound("m2", "hello"))
    assert ignored.replies == ()
    assert runtime.status().session_id == first_session

    ignored_command = await runtime.receive(inbound("m3", "/new"))
    assert ignored_command.replies == ()
    cancelled = await runtime.receive(inbound("m4", "/cancel"))
    assert cancelled.disposition == "cancelled"
    assert runner.cancelled_pending == ["continuation"]
    assert runtime.status().session_id == first_session
    reset = await runtime.receive(inbound("m5", "/new"))
    assert reset.disposition == "new_session"
    assert runtime.status().session_id != first_session
    assert runtime.status().pending_action is None


@async_test
async def test_approval_once_rejection_and_context_limit_are_explicit() -> None:
    action = PendingAction(host="ubuntu", prefix="pwd", display="Run pwd?")
    runner = FakeRunner(ApprovalRequired(action, "first"))
    runtime = PersonalRuntime(request_runner=runner)

    await runtime.receive(inbound("m1", "run"))
    approved = await runtime.receive(inbound("m2", "1"))
    assert approved.disposition == "completed"
    assert runner.resumes[-1] == (ApprovalDecision.APPROVE_ONCE, "first")

    runner.step = ApprovalRequired(action, "second")
    await runtime.receive(inbound("m3", "run again"))
    rejected = await runtime.receive(inbound("m4", "9"))
    assert rejected.disposition == "rejected"
    assert runner.resumes[-1] == (ApprovalDecision.REJECT, "second")

    runner.step = ContextLimitReached()
    limited = await runtime.receive(inbound("m5", "too much context"))
    assert limited.disposition == "context_limit"
    assert runtime.status().session_id is None


@async_test
async def test_restart_discards_session_work_and_persists_message_ids(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=sk-test\n"
        "OPENWA_API_KEY=openwa-test\n"
        "OPENWA_WEBHOOK_SIGNING_SECRET=signing-test\n",
        encoding="utf-8",
    )
    (tmp_path / "jarvis.toml").write_text("", encoding="utf-8")
    (tmp_path / "SYSTEM.md").write_text("System instructions.\n", encoding="utf-8")
    clock = FakeClock()
    action = PendingAction(host="ubuntu", prefix="pwd", display="Run pwd?")
    first_runner = FakeRunner(ApprovalRequired(action, "continuation"))
    first = build_runtime(tmp_path, request_runner=first_runner, clock=clock)

    await first.receive(inbound("selection", "/model gpt-5.6-sol"))
    assert (await first.receive(inbound("durable-id", "hello"))).disposition == (
        "approval_required"
    )
    assert first_runner.system_prompts == ["System instructions.\n"]
    before_restart = first.status()
    assert before_restart.session_id is not None
    assert before_restart.model == "gpt-5.6-sol"
    assert before_restart.active_request is not None
    assert before_restart.pending_action == action

    restarted_runner = FakeRunner()
    restarted = build_runtime(tmp_path, request_runner=restarted_runner, clock=clock)
    after_restart = restarted.status()
    assert after_restart.session_id is None
    assert after_restart.model == "gpt-5.6-luna"
    assert after_restart.active_request is None
    assert after_restart.pending_action is None

    duplicate = await restarted.receive(inbound("durable-id", "hello again"))
    assert duplicate.disposition == "duplicate"
    assert restarted_runner.calls == []

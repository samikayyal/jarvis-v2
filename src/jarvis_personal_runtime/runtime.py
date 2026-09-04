"""In-memory working-session state for the personal assistant runtime."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from .config import LoadedRuntimeConfig, RuntimeConfig, load_runtime_config
from .dedup import CacheError, MessageIdCache
from .permissions import PermissionRule, TomlPermissionStore
from .trace import build_runtime_trace

BUSY_NOTICE = "Jarvis is busy with another request. Use /cancel to stop it."
CANCELLED_NOTICE = (
    "Cancelled locally. A provider or remote command may already have started."
)
CONTEXT_LIMIT_NOTICE = (
    "This working session reached its context limit. Send your request again "
    "to start a new session."
)
APPROVAL_SUFFIX = "Reply 1 to approve once, 2 to save permission, or 9 to reject."
HELP_TEXT = (
    "Commands: /help, /new, /status, /cancel, /model, /reasoning, "
    "/permissions, /forget-permission, /connections, /connect google, "
    "/disconnect google"
)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("timestamp must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _required_text(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} must be non-empty")
    return value


class RuntimeDisposition(str, Enum):
    COMPLETED = "completed"
    APPROVAL_REQUIRED = "approval_required"
    BUSY = "busy"
    IGNORED = "ignored"
    DUPLICATE = "duplicate"
    COMMAND = "command"
    NEW_SESSION = "new_session"
    CANCELLED = "cancelled"
    NOTHING_TO_CANCEL = "nothing_to_cancel"
    UNKNOWN_COMMAND = "unknown_command"
    MALFORMED_COMMAND = "malformed_command"
    INVALID_CONFIGURATION = "invalid_configuration"
    CONFIGURATION_BLOCKED = "configuration_blocked"
    PERMISSION_SAVE_FAILED = "permission_save_failed"
    REJECTED = "rejected"
    FAILED = "failed"
    CONTEXT_LIMIT = "context_limit"


@dataclass(frozen=True, slots=True)
class InboundText:
    """Text already admitted by the messaging-gateway adapter."""

    message_id: str
    text: str
    received_at: datetime

    def __post_init__(self) -> None:
        _required_text(self.message_id, "message_id")
        if not isinstance(self.text, str):
            raise TypeError("text must be a string")
        object.__setattr__(self, "received_at", _utc(self.received_at))


@dataclass(frozen=True, slots=True)
class PendingAction:
    host: str
    prefix: str
    display: str
    allow_save_permission: bool = True

    def __post_init__(self) -> None:
        _required_text(self.host, "host")
        _required_text(self.prefix, "prefix")
        _required_text(self.display, "display")
        if not isinstance(self.allow_save_permission, bool):
            raise TypeError("allow_save_permission must be a boolean")


class ApprovalDecision(str, Enum):
    APPROVE_ONCE = "approve_once"
    SAVE_PERMISSION = "save_permission"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class ApprovalRequired:
    action: PendingAction
    continuation: object


@dataclass(frozen=True, slots=True)
class Completed:
    reply: str | tuple[str, ...] | None = None

    @property
    def replies(self) -> tuple[str, ...]:
        if self.reply is None:
            return ()
        if isinstance(self.reply, str):
            return (self.reply,) if self.reply else ()
        return tuple(item for item in self.reply if item)


@dataclass(frozen=True, slots=True)
class ContextLimitReached:
    """The candidate request exceeded the configured local context gate."""


RequestStep = Completed | ApprovalRequired | ContextLimitReached


class RequestRunner(Protocol):
    def cancel_pending(self, continuation: object) -> None: ...

    async def run(
        self,
        text: str,
        *,
        model: str,
        reasoning: str,
        system_prompt: str,
    ) -> RequestStep: ...

    async def resume(
        self, decision: ApprovalDecision, continuation: object
    ) -> RequestStep: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class ClaimCache(Protocol):
    def claim(self, message_id: str, now: datetime) -> bool: ...


class PermissionStore(Protocol):
    def list_rules(self) -> tuple[PermissionRule, ...]: ...

    def add(self, host: str, prefix: str) -> PermissionRule: ...

    def remove(self, selector: str) -> bool: ...


class RuntimeTrace(Protocol):
    def record(self, event: str, payload: dict[str, object]) -> None: ...


class ConnectionControl(Protocol):
    def status(self) -> str: ...

    async def connect(self) -> str: ...

    def disconnect(self) -> str: ...


class _NoConnections:
    def status(self) -> str:
        return "Google: unavailable"

    async def connect(self) -> str:
        return "Google is not configured."

    def disconnect(self) -> str:
        return "Google is not configured."


class _NoTrace:
    def record(self, event: str, payload: dict[str, object]) -> None:
        return None

    def take_warning(self) -> str | None:
        return None


@dataclass(frozen=True, slots=True)
class ActiveRequestStatus:
    request_id: str
    started_at: datetime
    phase: str


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    session_id: str | None
    model: str
    reasoning: str
    created_at: datetime | None
    last_activity_at: datetime | None
    expires_at: datetime | None
    active_request: ActiveRequestStatus | None
    pending_action: PendingAction | None
    permission_count: int


@dataclass(frozen=True, slots=True)
class RuntimeResult:
    disposition: RuntimeDisposition
    replies: tuple[str, ...] = ()
    status: RuntimeStatus | None = None


@dataclass(slots=True)
class _Pending:
    action: PendingAction
    continuation: object
    claimed: bool = False


@dataclass(slots=True)
class _Active:
    request_id: str
    started_at: datetime
    task: asyncio.Task[object] | None
    phase: str = "processing"


@dataclass(slots=True)
class _Session:
    session_id: str
    created_at: datetime
    last_activity_at: datetime
    model: str
    reasoning: str
    active: _Active | None = None
    pending: _Pending | None = None


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class _MemoryCache:
    """Test fallback; :func:`build_runtime` always uses the disk cache."""

    def __init__(self, retention: timedelta = timedelta(days=7)) -> None:
        self._retention = retention
        self._entries: dict[str, datetime] = {}

    def claim(self, message_id: str, now: datetime) -> bool:
        self._entries = {
            key: seen
            for key, seen in self._entries.items()
            if now - seen < self._retention
        }
        if message_id in self._entries:
            return False
        self._entries[message_id] = now
        return True


class _MemoryPermissions:
    def __init__(self) -> None:
        self._rules: dict[str, PermissionRule] = {}

    def list_rules(self) -> tuple[PermissionRule, ...]:
        return tuple(self._rules.values())

    def add(self, host: str, prefix: str) -> PermissionRule:
        rule = PermissionRule(host, prefix)
        self._rules.setdefault(rule.id, rule)
        return self._rules[rule.id]

    def remove(self, selector: str) -> bool:
        return self._rules.pop(selector, None) is not None


class PersonalRuntime:
    """Route admitted text through one deterministic in-memory state machine."""

    def __init__(
        self,
        config: RuntimeConfig | None = None,
        *,
        request_runner: RequestRunner,
        system_prompt: str = "",
        cache: ClaimCache | None = None,
        permission_store: PermissionStore | None = None,
        clock: Clock | None = None,
        trace: RuntimeTrace | None = None,
        connections: ConnectionControl | None = None,
    ) -> None:
        self.config = config or RuntimeConfig()
        self.request_runner = request_runner
        self.system_prompt = system_prompt
        self.cache = cache or _MemoryCache()
        self.permission_store = permission_store or _MemoryPermissions()
        self.clock = clock or _SystemClock()
        self.trace = trace or _NoTrace()
        self.connections = connections or _NoConnections()
        self._lock = asyncio.Lock()
        self._session: _Session | None = None
        self._model = self.config.model
        self._reasoning = self.config.reasoning_effort

    def status(self) -> RuntimeStatus:
        session = self._session
        active = session.active if session else None
        pending = session.pending if session else None
        expires = None
        if session is not None and active is None and pending is None:
            expires = session.last_activity_at + timedelta(
                minutes=self.config.inactivity_minutes
            )
        return RuntimeStatus(
            session_id=session.session_id if session else None,
            model=session.model if session else self._model,
            reasoning=session.reasoning if session else self._reasoning,
            created_at=session.created_at if session else None,
            last_activity_at=session.last_activity_at if session else None,
            expires_at=expires,
            active_request=(
                ActiveRequestStatus(active.request_id, active.started_at, active.phase)
                if active
                else None
            ),
            pending_action=pending.action if pending else None,
            permission_count=len(self.permission_store.list_rules()),
        )

    async def receive(self, message: InboundText) -> RuntimeResult:
        now = _utc(self.clock.now())
        self.trace.record(
            "authorized_message",
            {
                "message_id": message.message_id,
                "text": message.text,
                "received_at": message.received_at.isoformat(),
            },
        )
        try:
            if not self.cache.claim(message.message_id, now):
                return self._result(RuntimeDisposition.DUPLICATE)
        except (CacheError, OSError, ValueError):
            return self._result(
                RuntimeDisposition.FAILED,
                ("Jarvis could not safely record this message. No work was started.",),
            )

        async with self._lock:
            self._expire_idle_session(now)
            connection_command = message.text.strip() in {
                "/connections",
                "/connect google",
                "/disconnect google",
            }
            if connection_command:
                return await self._command(message.text, now)
            if self._session and self._session.pending:
                operation = self._claim_modal(message.text, now)
            elif message.text.startswith("/"):
                return await self._command(message.text, now)
            elif self._session and self._session.active:
                return self._result(RuntimeDisposition.BUSY, (BUSY_NOTICE,))
            else:
                session = self._ensure_session(now)
                active = _Active(uuid4().hex, now, asyncio.current_task())
                session.active = active
                session.last_activity_at = now
                self.trace.record(
                    "request_started",
                    {
                        "session_id": session.session_id,
                        "request_id": active.request_id,
                        "message_id": message.message_id,
                    },
                )
                operation = ("run", message.text, active.request_id)

        if operation is None:
            return self._result(RuntimeDisposition.IGNORED)
        kind = operation[0]
        if kind == "save":
            return await self._save_and_resume(operation[1])
        if kind == "resume":
            return await self._resume(operation[1])
        if kind == "cancel":
            return self._result(RuntimeDisposition.CANCELLED, (CANCELLED_NOTICE,))
        return await self._run(operation[1], operation[2])

    async def close(self) -> None:
        async with self._lock:
            task = self._clear_session()
        await _cancel_task(task)

    def _claim_modal(self, text: str, now: datetime) -> tuple[str, object] | None:
        assert self._session is not None and self._session.pending is not None
        pending = self._session.pending
        choice = text.strip()
        if choice == "/cancel":
            self.request_runner.cancel_pending(pending.continuation)
            task = self._cancel_work(now)
            if task and task is not asyncio.current_task():
                task.cancel()
            return ("cancel", None)
        allowed = {"1", "9"}
        if pending.action.allow_save_permission:
            allowed.add("2")
        if choice not in allowed or pending.claimed:
            return None
        pending.claimed = True
        decision = {
            "1": ApprovalDecision.APPROVE_ONCE,
            "2": ApprovalDecision.SAVE_PERMISSION,
            "9": ApprovalDecision.REJECT,
        }[choice]
        self.trace.record(
            "approval_choice",
            {
                "choice": choice,
                "decision": decision.value,
                "action": {
                    "host": pending.action.host,
                    "prefix": pending.action.prefix,
                    "display": pending.action.display,
                },
            },
        )
        return ("save" if choice == "2" else "resume", decision)

    async def _save_and_resume(self, decision: object) -> RuntimeResult:
        async with self._lock:
            pending = self._pending()
            action = pending.action
        try:
            self.permission_store.add(action.host, action.prefix)
        except (OSError, ValueError):
            async with self._lock:
                if self._session and self._session.pending is pending:
                    pending.claimed = False
            return self._result(
                RuntimeDisposition.PERMISSION_SAVE_FAILED,
                ("The permission could not be saved; the action is still pending.",),
            )
        return await self._resume(decision)

    async def _resume(self, decision: object) -> RuntimeResult:
        assert isinstance(decision, ApprovalDecision)
        async with self._lock:
            pending = self._pending()
            session = self._session
            assert session is not None and session.active is not None
            session.active.phase = "resuming"
            session.active.task = asyncio.current_task()
            continuation = pending.continuation
        try:
            step = await self.request_runner.resume(decision, continuation)
        except asyncio.CancelledError:
            return self._result(RuntimeDisposition.IGNORED)
        except Exception:  # noqa: BLE001 - runner failures become bounded replies
            return await self._finish_failure()
        if decision is ApprovalDecision.REJECT and isinstance(step, Completed):
            result = await self._finish(step)
            return RuntimeResult(
                RuntimeDisposition.REJECTED, result.replies, result.status
            )
        return await self._apply_step(step)

    async def _run(self, text: object, request_id: object) -> RuntimeResult:
        assert isinstance(text, str) and isinstance(request_id, str)
        session = self._session
        assert session is not None
        model, reasoning = session.model, session.reasoning
        try:
            step = await self.request_runner.run(
                text,
                model=model,
                reasoning=reasoning,
                system_prompt=self.system_prompt,
            )
        except asyncio.CancelledError:
            return self._result(RuntimeDisposition.IGNORED)
        except Exception:  # noqa: BLE001 - runner failures become bounded replies
            return await self._finish_failure()
        async with self._lock:
            if (
                self._session is None
                or self._session.active is None
                or self._session.active.request_id != request_id
            ):
                return self._result(RuntimeDisposition.IGNORED)
        return await self._apply_step(step)

    async def _apply_step(self, step: RequestStep) -> RuntimeResult:
        if isinstance(step, ContextLimitReached):
            async with self._lock:
                self._clear_session()
            return self._result(
                RuntimeDisposition.CONTEXT_LIMIT, (CONTEXT_LIMIT_NOTICE,)
            )
        if isinstance(step, ApprovalRequired):
            async with self._lock:
                assert self._session is not None and self._session.active is not None
                self._session.active.phase = "awaiting_approval"
                self._session.active.task = None
                self._session.pending = _Pending(step.action, step.continuation)
            suffix = (
                APPROVAL_SUFFIX
                if step.action.allow_save_permission
                else "Reply 1 to approve once or 9 to reject."
            )
            return self._result(
                RuntimeDisposition.APPROVAL_REQUIRED,
                (f"{step.action.display}\n{suffix}",),
            )
        if isinstance(step, Completed):
            return await self._finish(step)
        return await self._finish_failure()

    async def _finish(self, step: Completed) -> RuntimeResult:
        async with self._lock:
            if self._session is None:
                return self._result(RuntimeDisposition.IGNORED)
            self._session.active = None
            self._session.pending = None
            self._session.last_activity_at = _utc(self.clock.now())
        return self._result(RuntimeDisposition.COMPLETED, step.replies)

    async def _finish_failure(self) -> RuntimeResult:
        async with self._lock:
            if self._session:
                self._session.active = None
                self._session.pending = None
        return self._result(
            RuntimeDisposition.FAILED,
            ("The request failed without completing the requested work.",),
        )

    async def _command(self, text: str, now: datetime) -> RuntimeResult:
        command, *args = text.strip().split()
        known = {
            "/help",
            "/new",
            "/status",
            "/cancel",
            "/model",
            "/reasoning",
            "/permissions",
            "/forget-permission",
            "/connections",
            "/connect",
            "/disconnect",
        }
        if command not in known:
            return self._result(RuntimeDisposition.UNKNOWN_COMMAND, (HELP_TEXT,))
        if command == "/help" and not args:
            return self._result(RuntimeDisposition.COMMAND, (HELP_TEXT,))
        if command == "/status" and not args:
            return self._result(
                RuntimeDisposition.COMMAND, (_status_text(self.status()),)
            )
        if command == "/connections" and not args:
            return self._result(
                RuntimeDisposition.COMMAND, (self.connections.status(),)
            )
        if command == "/connect" and args == ["google"]:
            try:
                reply = await self.connections.connect()
            except Exception:  # noqa: BLE001 - credential/network details stay private
                return self._result(
                    RuntimeDisposition.FAILED,
                    ("Google connection failed. Check the runtime trace.",),
                )
            return self._result(RuntimeDisposition.COMMAND, (reply,))
        if command == "/disconnect" and args == ["google"]:
            return self._result(
                RuntimeDisposition.COMMAND, (self.connections.disconnect(),)
            )
        if command == "/new" and not args:
            task = self._clear_session()
            self._ensure_session(now)
            if task and task is not asyncio.current_task():
                task.cancel()
            return self._result(
                RuntimeDisposition.NEW_SESSION, ("Started a new working session.",)
            )
        if command == "/cancel" and not args:
            had_work = bool(
                self._session
                and (
                    self._session.active is not None
                    or self._session.pending is not None
                )
            )
            task = self._cancel_work(now)
            if task and task is not asyncio.current_task():
                task.cancel()
            return self._result(
                RuntimeDisposition.CANCELLED
                if had_work
                else RuntimeDisposition.NOTHING_TO_CANCEL,
                (CANCELLED_NOTICE if had_work else "There is no active request.",),
            )
        if command in {"/model", "/reasoning"}:
            return self._set_selection(command, args)
        if command == "/permissions" and not args:
            rules = self.permission_store.list_rules()
            reply = (
                "No saved command permissions."
                if not rules
                else "Saved command permissions:\n"
                + "\n".join(f"{rule.id}: {rule.host} {rule.prefix}" for rule in rules)
            )
            return self._result(RuntimeDisposition.COMMAND, (reply,))
        if command == "/forget-permission" and len(args) == 1:
            removed = self.permission_store.remove(args[0])
            reply = (
                "Permission removed."
                if removed
                else "No saved permission matched that ID."
            )
            return self._result(RuntimeDisposition.COMMAND, (reply,))
        return self._result(
            RuntimeDisposition.MALFORMED_COMMAND,
            (f"Invalid use of {command}. {HELP_TEXT}",),
        )

    def _set_selection(self, command: str, args: list[str]) -> RuntimeResult:
        if len(args) != 1:
            return self._result(RuntimeDisposition.MALFORMED_COMMAND, (HELP_TEXT,))
        if self._session and self._session.active:
            return self._result(
                RuntimeDisposition.CONFIGURATION_BLOCKED,
                ("Model and reasoning cannot change during an active request.",),
            )
        value = args[0]
        value = f"gpt-5.6-{value}" if value in {"luna", "sol", "terra"} else value
        allowed = (
            self.config.allowed_models
            if command == "/model"
            else self.config.allowed_reasoning_efforts
        )
        if value not in allowed:
            return self._result(
                RuntimeDisposition.INVALID_CONFIGURATION,
                (f"Allowed values: {', '.join(allowed)}",),
            )
        if command == "/model":
            self._model = value
            if self._session:
                self._session.model = value
        else:
            self._reasoning = value
            if self._session:
                self._session.reasoning = value
        return self._result(RuntimeDisposition.COMMAND, (f"Set to {value}.",))

    def _ensure_session(self, now: datetime) -> _Session:
        if self._session is None:
            start_session = getattr(self.request_runner, "start_session", None)
            if callable(start_session):
                start_session()
            self._session = _Session(
                uuid4().hex,
                now,
                now,
                self._model,
                self._reasoning,
            )
        return self._session

    def _expire_idle_session(self, now: datetime) -> None:
        if self._session is None or self._session.active or self._session.pending:
            return
        if now - self._session.last_activity_at >= timedelta(
            minutes=self.config.inactivity_minutes
        ):
            self._session = None

    def _pending(self) -> _Pending:
        if self._session is None or self._session.pending is None:
            raise RuntimeError("pending action disappeared")
        return self._session.pending

    def _clear_session(self) -> asyncio.Task[object] | None:
        task = (
            self._session.active.task
            if self._session and self._session.active
            else None
        )
        self._session = None
        return task

    def _cancel_work(self, now: datetime) -> asyncio.Task[object] | None:
        if self._session is None:
            return None
        task = self._session.active.task if self._session.active else None
        self._session.active = None
        self._session.pending = None
        self._session.last_activity_at = now
        return task

    def _result(
        self, disposition: RuntimeDisposition, replies: tuple[str, ...] = ()
    ) -> RuntimeResult:
        self.trace.record(
            "runtime_result",
            {"disposition": disposition.value, "replies": list(replies)},
        )
        take_warning = getattr(self.trace, "take_warning", None)
        warning = take_warning() if callable(take_warning) else None
        if warning and warning not in replies:
            replies = (*replies, warning)
        return RuntimeResult(disposition, replies, self.status())


def build_runtime(
    root: str | Path,
    *,
    request_runner: RequestRunner,
    clock: Clock | None = None,
    trace: RuntimeTrace | None = None,
    connections: ConnectionControl | None = None,
) -> PersonalRuntime:
    """Load all three runtime files and compose replacement-owned persistence."""

    loaded: LoadedRuntimeConfig = load_runtime_config(root)
    return build_runtime_from_loaded(
        loaded,
        request_runner=request_runner,
        clock=clock,
        trace=trace,
        connections=connections,
    )


def build_runtime_from_loaded(
    loaded: LoadedRuntimeConfig,
    *,
    request_runner: RequestRunner,
    clock: Clock | None = None,
    trace: RuntimeTrace | None = None,
    connections: ConnectionControl | None = None,
) -> PersonalRuntime:
    """Compose a runtime from one already validated configuration snapshot."""

    return PersonalRuntime(
        loaded.config,
        request_runner=request_runner,
        system_prompt=loaded.system_prompt,
        cache=MessageIdCache(
            loaded.config.message_cache_path,
            timedelta(days=loaded.config.message_cache_retention_days),
        ),
        permission_store=TomlPermissionStore(loaded.toml_path),
        clock=clock,
        connections=connections,
        trace=trace
        or getattr(request_runner, "trace", None)
        or build_runtime_trace(loaded.config),
    )


async def _cancel_task(task: asyncio.Task[object] | None) -> None:
    if task is None or task is asyncio.current_task() or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _status_text(status: RuntimeStatus) -> str:
    session = status.session_id or "none"
    active = status.active_request.phase if status.active_request else "none"
    pending = "yes" if status.pending_action else "no"
    return (
        f"Session: {session}; model: {status.model}; reasoning: {status.reasoning}; "
        f"active request: {active}; pending action: {pending}; "
        f"saved permissions: {status.permission_count}."
    )


__all__ = [
    "APPROVAL_SUFFIX",
    "BUSY_NOTICE",
    "CANCELLED_NOTICE",
    "CONTEXT_LIMIT_NOTICE",
    "ApprovalDecision",
    "ApprovalRequired",
    "Completed",
    "ContextLimitReached",
    "InboundText",
    "PendingAction",
    "PersonalRuntime",
    "RuntimeDisposition",
    "RuntimeResult",
    "RuntimeStatus",
    "build_runtime",
    "build_runtime_from_loaded",
]

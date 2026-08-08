"""Explicit durable-assistant-memory parsing and local action dispatch."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from threading import RLock

from .models import DurableMemory, FrozenActionProposal
from .ports import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ActionDispatcherError,
    ActionDispatchHandle,
    Clock,
    DurableStateStore,
    StateStoreError,
)

_NATURAL_REMEMBER = re.compile(
    r"^(?:please\s+)?remember(?:\s+that)?\s+(.+)$",
    re.IGNORECASE | re.DOTALL,
)


class MemoryOperation(str, Enum):
    """Closed operation names understood by the explicit memory surface."""

    LIST = "list"
    SEARCH = "search"
    INSPECT = "inspect"
    USE = "use"
    REMEMBER = "remember"
    REPLACE = "replace"
    FORGET = "forget"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class MemoryCommand:
    """A parsed memory command with content kept out of normalization."""

    operation: str
    memory_id: str | None = None
    content: str | None = None
    source: str = "slash"
    error: str | None = None

    def __post_init__(self) -> None:
        operation = MemoryOperation(self.operation)
        object.__setattr__(self, "operation", operation)
        if operation not in {
            MemoryOperation.LIST,
            MemoryOperation.SEARCH,
            MemoryOperation.INSPECT,
            MemoryOperation.USE,
            MemoryOperation.REMEMBER,
            MemoryOperation.REPLACE,
            MemoryOperation.FORGET,
            MemoryOperation.INVALID,
        }:
            raise ValueError("unsupported memory operation")
        if self.source not in {"natural", "slash"}:
            raise ValueError("memory command source must be natural or slash")
        if self.memory_id is not None and (
            not isinstance(self.memory_id, str) or not self.memory_id.strip()
        ):
            raise ValueError("memory_id must be non-blank when provided")
        if self.content is not None and (
            not isinstance(self.content, str) or not self.content.strip()
        ):
            raise ValueError("memory content must be non-blank when provided")

    @property
    def is_mutation(self) -> bool:
        return self.operation in {
            MemoryOperation.REMEMBER,
            MemoryOperation.REPLACE,
            MemoryOperation.FORGET,
        }

    @property
    def is_read(self) -> bool:
        return self.operation in {
            MemoryOperation.LIST,
            MemoryOperation.SEARCH,
            MemoryOperation.INSPECT,
        }

    @property
    def is_valid(self) -> bool:
        return self.operation != MemoryOperation.INVALID and self.error is None


def parse_memory_command(text: str) -> MemoryCommand | None:
    """Parse an explicit ``remember`` request or a ``/memory`` command.

    Ordinary conversation is intentionally represented by ``None``.  In
    particular, phrases such as ``I remember ...`` never create a memory.
    Command arguments retain their original case and punctuation so an exact
    operator preview is possible.
    """

    if not isinstance(text, str):
        raise TypeError("memory command text must be a string")
    stripped = text.strip()
    natural = _NATURAL_REMEMBER.fullmatch(stripped)
    if natural is not None:
        return MemoryCommand(
            operation=MemoryOperation.REMEMBER,
            content=natural.group(1).strip(),
            source="natural",
        )

    if not stripped.startswith("/"):
        return None
    parts = stripped.split(None, 3)
    if not parts or parts[0].casefold() != "/memory":
        return None
    if len(parts) == 1:
        return MemoryCommand(operation=MemoryOperation.LIST)

    operation = parts[1].casefold()
    if operation == MemoryOperation.LIST and len(parts) == 2:
        return MemoryCommand(operation=MemoryOperation.LIST)
    if operation == MemoryOperation.SEARCH:
        command_parts = stripped.split(None, 2)
        query = command_parts[2].strip() if len(command_parts) == 3 else ""
        return MemoryCommand(
            operation=MemoryOperation.SEARCH,
            content=query or None,
            error=None if query else "search text is required",
        )
    if operation == MemoryOperation.INSPECT and len(parts) == 3:
        return MemoryCommand(
            operation=MemoryOperation.INSPECT,
            memory_id=parts[2].strip(),
            error=None if parts[2].strip() else "memory ID is required",
        )
    if operation == MemoryOperation.USE and len(parts) == 4:
        memory_id, content = parts[2], parts[3].strip()
        return MemoryCommand(
            operation=MemoryOperation.USE,
            memory_id=memory_id,
            content=content or None,
            error=(
                "memory ID and request text are required"
                if not memory_id.strip() or not content
                else None
            ),
        )
    if operation == MemoryOperation.REMEMBER:
        command_parts = stripped.split(None, 2)
        content = command_parts[2].strip() if len(command_parts) == 3 else ""
        return MemoryCommand(
            operation=MemoryOperation.REMEMBER,
            content=content or None,
            error=None if content else "memory content is required",
        )
    if operation == MemoryOperation.REPLACE and len(parts) == 4:
        memory_id, content = parts[2], parts[3].strip()
        return MemoryCommand(
            operation=MemoryOperation.REPLACE,
            memory_id=memory_id,
            content=content,
            error=(
                "memory ID and replacement content are required"
                if not memory_id.strip() or not content
                else None
            ),
        )
    if operation == MemoryOperation.FORGET and len(parts) == 3:
        return MemoryCommand(
            operation=MemoryOperation.FORGET,
            memory_id=parts[2].strip(),
            error=None if parts[2].strip() else "memory ID is required",
        )
    return MemoryCommand(
        operation=MemoryOperation.INVALID,
        error=(
            "usage: /memory [list|search <text>|inspect <id>|use <id> <request>|"
            "remember <text>|replace <id> <text>|forget <id>"
        ),
    )


class _DurableMemoryActionHandle:
    """One cancellable local memory mutation prepared from a frozen payload."""

    def __init__(
        self,
        *,
        action_id: str,
        payload: dict[str, object],
        state: DurableStateStore,
        clock: Clock,
    ) -> None:
        self.action_id = action_id
        self._payload = payload
        self._state = state
        self._clock = clock
        self._lock = RLock()
        self._started = False
        self._completed = False
        self._cancelled = False
        self._result: object | None = None

    def run(self) -> object | None:
        with self._lock:
            if self._cancelled:
                raise ActionDispatcherError("durable memory action was cancelled")
            if self._completed:
                return self._result
            if self._started:
                raise ActionDispatcherError(
                    "durable memory action is already running",
                    may_have_dispatched=True,
                )
            self._started = True
        try:
            result = self._mutate()
        except StateStoreError as exc:
            raise ActionDispatcherError(str(exc)) from exc
        with self._lock:
            self._result = result
            self._completed = True
        return result

    def cancel(self) -> ActionCancellationResult:
        with self._lock:
            if self._started:
                return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
            self._cancelled = True
            return ActionCancellationResult(ActionCancellationStatus.NOT_STARTED)

    def _mutate(self) -> DurableMemory | None:
        operation = self._payload["operation"]
        now = self._clock.now()
        if operation == MemoryOperation.REMEMBER:
            return self._state.create_memory(
                DurableMemory(
                    memory_id=_required_string(self._payload, "memory_id"),
                    content=_required_string(self._payload, "content"),
                    created_at=now,
                    updated_at=now,
                    source_message_id=_optional_string(
                        self._payload, "source_message_id"
                    ),
                )
            )
        if operation == MemoryOperation.REPLACE:
            replacement = DurableMemory(
                memory_id=_required_string(self._payload, "new_memory_id"),
                content=_required_string(self._payload, "content"),
                created_at=now,
                updated_at=now,
                source_message_id=_optional_string(self._payload, "source_message_id"),
            )
            return self._state.replace_memory(
                _required_string(self._payload, "memory_id"),
                replacement,
                expected_revision=_optional_string(self._payload, "expected_revision"),
            )
        if operation == MemoryOperation.FORGET:
            return self._state.forget_memory(
                _required_string(self._payload, "memory_id"),
                expected_revision=_optional_string(self._payload, "expected_revision"),
                updated_at=now,
            )
        raise ActionDispatcherError("unsupported durable memory mutation")


class DurableMemoryActionDispatcher:
    """Closed local capability used by the broker for approved memory writes."""

    def __init__(self, *, state: DurableStateStore, clock: Clock) -> None:
        self._state = state
        self._clock = clock
        self._handles: dict[str, _DurableMemoryActionHandle] = {}
        self._lock = RLock()

    def prepare(self, action: FrozenActionProposal) -> ActionDispatchHandle:
        if not isinstance(action, FrozenActionProposal):
            raise TypeError("memory dispatcher requires a frozen action")
        if action.kind != "durable_memory":
            raise ActionDispatcherError(
                "memory dispatcher received another action kind"
            )
        try:
            payload = json.loads(action.payload)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ActionDispatcherError(
                "durable memory action payload is invalid"
            ) from exc
        if not isinstance(payload, dict):
            raise ActionDispatcherError(
                "durable memory action payload must be an object"
            )
        self._validate_payload(payload)
        with self._lock:
            if action.action_id in self._handles:
                raise ActionDispatcherError("durable memory action is already prepared")
            handle = _DurableMemoryActionHandle(
                action_id=action.action_id,
                payload=payload,
                state=self._state,
                clock=self._clock,
            )
            self._handles[action.action_id] = handle
            return handle

    def cancel(self, *, action_id: str) -> ActionCancellationResult:
        with self._lock:
            handle = self._handles.get(action_id)
        if handle is None:
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
        return handle.cancel()

    def finalize(self, *, action_id: str) -> None:
        with self._lock:
            self._handles.pop(action_id, None)

    @staticmethod
    def _validate_payload(payload: dict[str, object]) -> None:
        operation = payload.get("operation")
        if operation == MemoryOperation.REMEMBER:
            _required_string(payload, "memory_id")
            _required_string(payload, "content")
        elif operation == MemoryOperation.REPLACE:
            _required_string(payload, "memory_id")
            _required_string(payload, "new_memory_id")
            _required_string(payload, "content")
            _required_string(payload, "expected_revision")
        elif operation == MemoryOperation.FORGET:
            _required_string(payload, "memory_id")
            _required_string(payload, "expected_revision")
        else:
            raise ActionDispatcherError(
                "durable memory action has an invalid operation"
            )
        _optional_string(payload, "source_message_id")


def _required_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ActionDispatcherError(f"durable memory action requires {key}")
    return value.strip() if key in {"memory_id", "new_memory_id"} else value


def _optional_string(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ActionDispatcherError(f"durable memory action field {key} is invalid")
    return value

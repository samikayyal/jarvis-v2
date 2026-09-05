"""Durable approved one-time Reminder creation and inspection."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .openwa import OpenWASender, OpenWASendError
from .runtime import ApprovalRequired, PendingAction

MAX_BODY_CHARACTERS = 4096
MAX_LIST_ENTRIES = 25
_ID_PATTERN = re.compile(r"^[a-z0-9]{8}$")
_LOCAL_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?$")
_STATUSES = frozenset({"pending", "sent", "failed", "unknown", "cancelled"})


class ReminderError(ValueError):
    """A deterministic rejection at the Reminder boundary."""


class Clock(Protocol):
    def now(self) -> datetime: ...


class SchedulerClock(Clock, Protocol):
    async def wait_until(self, due_at: datetime, wake: asyncio.Event) -> None: ...


class Trace(Protocol):
    def record(self, event: str, payload: dict[str, object]) -> None: ...


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class _SystemSchedulerClock(_SystemClock):
    async def wait_until(self, due_at: datetime, wake: asyncio.Event) -> None:
        delay = max(0.0, (due_at - self.now()).total_seconds())
        try:
            await asyncio.wait_for(wake.wait(), timeout=delay)
        except TimeoutError:
            return


class _NoTrace:
    def record(self, event: str, payload: dict[str, object]) -> None:
        return None


@dataclass(frozen=True, slots=True)
class Reminder:
    id: str
    body: str
    due_at: datetime
    timezone: str
    status: str
    created_at: datetime
    updated_at: datetime
    attempt_at: datetime | None = None
    completed_at: datetime | None = None
    outbound_message_id: str | None = None
    failure_classification: str | None = None


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ReminderError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _body(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > MAX_BODY_CHARACTERS
    ):
        raise ReminderError(
            f"body must be a non-empty string of at most {MAX_BODY_CHARACTERS} characters"
        )
    return value


def _zone(name: object) -> ZoneInfo:
    if not isinstance(name, str) or not name or name.strip() != name:
        raise ReminderError("operator timezone must be a valid IANA timezone")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ReminderError("operator timezone must be a valid IANA timezone") from exc


def _validate_record(record: Reminder, now: datetime) -> Reminder:
    if not isinstance(record, Reminder):
        raise TypeError("record must be a Reminder")
    if not _ID_PATTERN.fullmatch(record.id):
        raise ReminderError("reminder id must be eight lowercase letters or digits")
    body = _body(record.body)
    zone = _zone(record.timezone)
    due_at = _utc(record.due_at, "due_at")
    created_at = _utc(record.created_at, "created_at")
    updated_at = _utc(record.updated_at, "updated_at")
    if record.status not in _STATUSES:
        raise ReminderError("invalid reminder status")
    if record.status == "pending" and due_at <= _utc(now, "now"):
        raise ReminderError("due time must be in the future")
    attempt_at = (
        _utc(record.attempt_at, "attempt_at") if record.attempt_at is not None else None
    )
    completed_at = (
        _utc(record.completed_at, "completed_at")
        if record.completed_at is not None
        else None
    )
    for name, value in (
        ("outbound_message_id", record.outbound_message_id),
        ("failure_classification", record.failure_classification),
    ):
        if value is not None and (
            not isinstance(value, str) or not value or len(value) > 256
        ):
            raise ReminderError(f"{name} must be a bounded non-empty string")
    if record.status == "pending" and any(
        value is not None
        for value in (
            attempt_at,
            completed_at,
            record.outbound_message_id,
            record.failure_classification,
        )
    ):
        raise ReminderError("pending reminders cannot have terminal outcome fields")
    if record.status == "cancelled" and completed_at is None:
        raise ReminderError("cancelled reminders require a completion timestamp")
    # Force timezone construction here so invalid display zones cannot enter state.
    del zone
    return Reminder(
        id=record.id,
        body=body,
        due_at=due_at,
        timezone=record.timezone,
        status=record.status,
        created_at=created_at,
        updated_at=updated_at,
        attempt_at=attempt_at,
        completed_at=completed_at,
        outbound_message_id=record.outbound_message_id,
        failure_classification=record.failure_classification,
    )


class ReminderStore:
    """Own the bounded SQLite persistence boundary for Reminder records."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id TEXT PRIMARY KEY,
                    body TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'sent', 'failed', 'unknown', 'cancelled')
                    ),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    attempt_at TEXT,
                    completed_at TEXT,
                    outbound_message_id TEXT,
                    failure_classification TEXT
                )
                """
            )

    def contains(self, reminder_id: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM reminders WHERE id = ?", (reminder_id,)
            ).fetchone()
        return row is not None

    def get(self, reminder_id: str) -> Reminder | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT id, body, due_at, timezone, status, created_at, updated_at,
                       attempt_at, completed_at, outbound_message_id,
                       failure_classification
                FROM reminders
                WHERE id = ?
                """,
                (reminder_id,),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def save(self, record: Reminder, *, now: datetime) -> None:
        validated = _validate_record(record, now)
        values = (
            validated.id,
            validated.body,
            _timestamp(validated.due_at),
            validated.timezone,
            validated.status,
            _timestamp(validated.created_at),
            _timestamp(validated.updated_at),
            _timestamp(validated.attempt_at) if validated.attempt_at else None,
            _timestamp(validated.completed_at) if validated.completed_at else None,
            validated.outbound_message_id,
            validated.failure_classification,
        )
        try:
            with self._lock, self._connection:
                self._connection.execute(
                    """
                    INSERT INTO reminders (
                        id, body, due_at, timezone, status, created_at, updated_at,
                        attempt_at, completed_at, outbound_message_id,
                        failure_classification
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
        except sqlite3.IntegrityError as exc:
            raise ReminderError(f"reminder id {validated.id} already exists") from exc

    def replace_pending(
        self,
        record: Reminder,
        *,
        expected_updated_at: datetime,
        now: datetime,
    ) -> bool:
        validated = _validate_record(record, now)
        if validated.status != "pending":
            raise ReminderError("replacement reminder must be pending")
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE reminders
                SET body = ?, due_at = ?, timezone = ?, updated_at = ?
                WHERE id = ? AND status = 'pending' AND attempt_at IS NULL
                    AND updated_at = ?
                """,
                (
                    validated.body,
                    _timestamp(validated.due_at),
                    validated.timezone,
                    _timestamp(validated.updated_at),
                    validated.id,
                    _timestamp(_utc(expected_updated_at, "expected_updated_at")),
                ),
            )
        return cursor.rowcount == 1

    def cancel_pending(
        self,
        reminder_id: str,
        *,
        expected_updated_at: datetime,
        now: datetime,
    ) -> bool:
        completed_at = _utc(now, "now")
        timestamp = _timestamp(completed_at)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE reminders
                SET status = 'cancelled', updated_at = ?, completed_at = ?
                WHERE id = ? AND status = 'pending' AND attempt_at IS NULL
                    AND updated_at = ?
                """,
                (
                    timestamp,
                    timestamp,
                    reminder_id,
                    _timestamp(_utc(expected_updated_at, "expected_updated_at")),
                ),
            )
        return cursor.rowcount == 1

    def list(
        self, *, include_terminal: bool = False, limit: int = MAX_LIST_ENTRIES
    ) -> tuple[Reminder, ...]:
        if not isinstance(include_terminal, bool):
            raise ReminderError("include_terminal must be a boolean")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ReminderError("limit must be between 1 and 100")
        where = (
            "WHERE status != 'pending' OR attempt_at IS NULL"
            if include_terminal
            else "WHERE status = 'pending' AND attempt_at IS NULL"
        )
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT id, body, due_at, timezone, status, created_at, updated_at,
                       attempt_at, completed_at, outbound_message_id,
                       failure_classification
                FROM reminders {where}
                ORDER BY due_at ASC, id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def next_pending(self, *, due_after: datetime | None = None) -> Reminder | None:
        cutoff = (
            _timestamp(_utc(due_after, "due_after")) if due_after is not None else None
        )
        with self._lock:
            row = self._connection.execute(
                """
                SELECT id, body, due_at, timezone, status, created_at, updated_at,
                       attempt_at, completed_at, outbound_message_id,
                       failure_classification
                FROM reminders
                WHERE status = 'pending' AND attempt_at IS NULL
                    AND (? IS NULL OR due_at > ?)
                ORDER BY due_at ASC, id ASC
                LIMIT 1
                """,
                (cutoff, cutoff),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def begin_due_attempt(self, reminder_id: str, *, now: datetime) -> Reminder | None:
        """Atomically claim and return one due Reminder for its only send."""

        attempted_at = _utc(now, "now")
        attempted_timestamp = _timestamp(attempted_at)
        with self._lock, self._connection:
            row = self._connection.execute(
                """
                SELECT id, body, due_at, timezone, status, created_at, updated_at,
                       attempt_at, completed_at, outbound_message_id,
                       failure_classification
                FROM reminders
                WHERE id = ? AND status = 'pending' AND attempt_at IS NULL
                    AND due_at <= ?
                """,
                (reminder_id, attempted_timestamp),
            ).fetchone()
            if row is None:
                return None
            cursor = self._connection.execute(
                """
                UPDATE reminders
                SET updated_at = ?, attempt_at = ?
                WHERE id = ? AND status = 'pending' AND attempt_at IS NULL
                """,
                (
                    attempted_timestamp,
                    attempted_timestamp,
                    reminder_id,
                ),
            )
            if cursor.rowcount != 1:
                return None
        return self._from_row(row)

    def finish_attempt(
        self,
        reminder_id: str,
        *,
        status: str,
        now: datetime,
        outbound_message_id: str | None = None,
        failure_classification: str | None = None,
    ) -> None:
        if status not in {"sent", "failed", "unknown"}:
            raise ReminderError("invalid delivery outcome")
        completed_timestamp = _timestamp(_utc(now, "now"))
        for name, value in (
            ("outbound_message_id", outbound_message_id),
            ("failure_classification", failure_classification),
        ):
            if value is not None and (
                not isinstance(value, str) or not value or len(value) > 256
            ):
                raise ReminderError(f"{name} must be a bounded non-empty string")
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE reminders
                SET status = ?, updated_at = ?, completed_at = ?,
                    outbound_message_id = ?, failure_classification = ?
                WHERE id = ? AND status = 'pending' AND attempt_at IS NOT NULL
                """,
                (
                    status,
                    completed_timestamp,
                    completed_timestamp,
                    outbound_message_id,
                    failure_classification,
                    reminder_id,
                ),
            )
        if cursor.rowcount != 1:
            raise ReminderError("reminder attempt is not in progress")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Reminder:
        return Reminder(
            id=str(row["id"]),
            body=str(row["body"]),
            due_at=_parse_timestamp(str(row["due_at"])),
            timezone=str(row["timezone"]),
            status=str(row["status"]),
            created_at=_parse_timestamp(str(row["created_at"])),
            updated_at=_parse_timestamp(str(row["updated_at"])),
            attempt_at=(
                _parse_timestamp(str(row["attempt_at"]))
                if row["attempt_at"] is not None
                else None
            ),
            completed_at=(
                _parse_timestamp(str(row["completed_at"]))
                if row["completed_at"] is not None
                else None
            ),
            outbound_message_id=row["outbound_message_id"],
            failure_classification=row["failure_classification"],
        )


@dataclass(slots=True)
class _CreateContinuation:
    reminder_id: str
    body: str
    due_at: datetime
    resolved: bool = False


@dataclass(slots=True)
class _EditContinuation:
    original_updated_at: datetime
    reminder_id: str
    body: str
    due_at: datetime
    resolved: bool = False


@dataclass(slots=True)
class _CancelContinuation:
    original_updated_at: datetime
    reminder_id: str
    resolved: bool = False


class ReminderTools:
    """Prepared create, edit, list, and cancel Reminder operations."""

    definitions: tuple[dict[str, object], ...] = (
        {
            "type": "function",
            "name": "create_reminder",
            "description": (
                "Propose one exact one-time Reminder for approval. due_local must "
                "be a strict local ISO-8601 date-time in the configured timezone."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "body": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_BODY_CHARACTERS,
                    },
                    "due_local": {
                        "type": "string",
                        "pattern": (
                            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}"
                            r"(?::\d{2})?$"
                        ),
                    },
                },
                "required": ["body", "due_local"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "edit_reminder",
            "description": (
                "Propose changes to one pending Reminder by stable ID. Use "
                "list_reminders to resolve conversational references. If more than "
                "one pending Reminder could match, ask the operator which one they "
                "mean. Never guess an ID. Pass null for each unchanged field."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "reminder_id": {
                        "type": "string",
                        "pattern": r"^[a-z0-9]{8}$",
                    },
                    "body": {
                        "type": ["string", "null"],
                        "minLength": 1,
                        "maxLength": MAX_BODY_CHARACTERS,
                    },
                    "due_local": {
                        "type": ["string", "null"],
                        "pattern": (
                            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}"
                            r"(?::\d{2})?$"
                        ),
                    },
                },
                "required": ["reminder_id", "body", "due_local"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "list_reminders",
            "description": (
                "List Reminders. Set include_terminal to false for the default "
                "pending-only view, or true to include retained terminal records."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {"include_terminal": {"type": "boolean"}},
                "required": ["include_terminal"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "cancel_reminder",
            "description": (
                "Propose cancellation of one pending Reminder by stable ID. Use "
                "list_reminders to resolve conversational references. If more than "
                "one pending Reminder could match, ask the operator which one they "
                "mean. Never guess an ID."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "reminder_id": {
                        "type": "string",
                        "pattern": r"^[a-z0-9]{8}$",
                    }
                },
                "required": ["reminder_id"],
                "additionalProperties": False,
            },
        },
    )

    def __init__(
        self,
        store: ReminderStore,
        *,
        operator_timezone: str,
        clock: Clock | None = None,
        id_generator: Callable[[], str] | None = None,
        trace: Trace | None = None,
        on_change: Callable[[], object] | None = None,
        max_result_chars: int = 65_536,
    ) -> None:
        if not isinstance(store, ReminderStore):
            raise TypeError("store must be a ReminderStore")
        if (
            isinstance(max_result_chars, bool)
            or not isinstance(max_result_chars, int)
            or max_result_chars < 2
        ):
            raise ValueError("max_result_chars must be at least 2")
        self._store = store
        self._timezone_name = operator_timezone
        self._timezone = _zone(operator_timezone)
        self._clock = clock or _SystemClock()
        self._id_generator = id_generator or (lambda: secrets.token_hex(4))
        self._trace = trace or _NoTrace()
        self._on_change = on_change or (lambda: None)
        self._max_result_chars = max_result_chars

    @property
    def database_path(self) -> Path:
        return self._store.path

    async def execute(
        self, name: str, arguments: dict[str, object]
    ) -> str | ApprovalRequired:
        if name == "create_reminder":
            return self._create(arguments)
        if name == "edit_reminder":
            return self._edit(arguments)
        if name == "list_reminders":
            return self._list(arguments)
        if name == "cancel_reminder":
            return self._cancel(arguments)
        raise ReminderError(f"unknown prepared tool: {name}")

    async def resume(self, continuation: object, *, approved: bool) -> str:
        if isinstance(continuation, _CreateContinuation):
            return self._resume_create(continuation, approved=approved)
        if isinstance(continuation, _EditContinuation):
            return self._resume_edit(continuation, approved=approved)
        if isinstance(continuation, _CancelContinuation):
            return self._resume_cancel(continuation, approved=approved)
        raise TypeError("invalid Reminder continuation")

    def _resume_create(
        self, continuation: _CreateContinuation, *, approved: bool
    ) -> str:
        if continuation.resolved:
            return _canonical({"error": "already_resolved"})
        continuation.resolved = True
        if not approved:
            self._trace.record(
                "reminder_create_approval",
                {"id": continuation.reminder_id, "outcome": "rejected"},
            )
            return _canonical({"rejected": True})
        now = _utc(self._clock.now(), "now")
        if continuation.due_at <= now:
            self._trace.record(
                "reminder_create_approval",
                {"id": continuation.reminder_id, "outcome": "expired"},
            )
            return _canonical({"error": "due_time_not_future"})
        record = Reminder(
            id=continuation.reminder_id,
            body=continuation.body,
            due_at=continuation.due_at,
            timezone=self._timezone_name,
            status="pending",
            created_at=now,
            updated_at=now,
        )
        self._store.save(record, now=now)
        self._trace.record(
            "reminder_create_approval",
            {"id": record.id, "outcome": "approved"},
        )
        self._trace.record(
            "reminder_created",
            {
                "id": record.id,
                "body": record.body,
                "due_at": _timestamp(record.due_at),
                "timezone": record.timezone,
            },
        )
        self._on_change()
        return _canonical({"created": True, "id": record.id})

    def _resume_edit(self, continuation: _EditContinuation, *, approved: bool) -> str:
        if continuation.resolved:
            return _canonical({"error": "already_resolved"})
        continuation.resolved = True
        if not approved:
            self._trace.record(
                "reminder_edit_approval",
                {"id": continuation.reminder_id, "outcome": "rejected"},
            )
            return _canonical({"rejected": True})
        now = _utc(self._clock.now(), "now")
        if continuation.due_at <= now:
            self._trace.record(
                "reminder_edit_approval",
                {"id": continuation.reminder_id, "outcome": "expired"},
            )
            return _canonical({"error": "due_time_not_future"})
        original = self._store.get(continuation.reminder_id)
        if original is None or original.status != "pending" or original.attempt_at:
            self._trace.record(
                "reminder_edit_approval",
                {"id": continuation.reminder_id, "outcome": "stale"},
            )
            return _canonical({"error": "reminder_no_longer_pending"})
        replacement = Reminder(
            id=continuation.reminder_id,
            body=continuation.body,
            due_at=continuation.due_at,
            timezone=self._timezone_name,
            status="pending",
            created_at=original.created_at,
            updated_at=now,
        )
        changed = self._store.replace_pending(
            replacement,
            expected_updated_at=continuation.original_updated_at,
            now=now,
        )
        if not changed:
            self._trace.record(
                "reminder_edit_approval",
                {"id": continuation.reminder_id, "outcome": "stale"},
            )
            return _canonical({"error": "reminder_changed_before_approval"})
        self._trace.record(
            "reminder_edit_approval",
            {"id": continuation.reminder_id, "outcome": "approved"},
        )
        self._trace.record(
            "reminder_edited",
            {
                "id": replacement.id,
                "body": replacement.body,
                "due_at": _timestamp(replacement.due_at),
                "timezone": replacement.timezone,
            },
        )
        self._on_change()
        return _canonical({"edited": True, "id": replacement.id})

    def _resume_cancel(
        self, continuation: _CancelContinuation, *, approved: bool
    ) -> str:
        if continuation.resolved:
            return _canonical({"error": "already_resolved"})
        continuation.resolved = True
        if not approved:
            self._trace.record(
                "reminder_cancel_approval",
                {"id": continuation.reminder_id, "outcome": "rejected"},
            )
            return _canonical({"rejected": True})
        changed = self._store.cancel_pending(
            continuation.reminder_id,
            expected_updated_at=continuation.original_updated_at,
            now=self._clock.now(),
        )
        if not changed:
            self._trace.record(
                "reminder_cancel_approval",
                {"id": continuation.reminder_id, "outcome": "stale"},
            )
            return _canonical({"error": "reminder_changed_before_approval"})
        self._trace.record(
            "reminder_cancel_approval",
            {"id": continuation.reminder_id, "outcome": "approved"},
        )
        self._trace.record("reminder_cancelled", {"id": continuation.reminder_id})
        self._on_change()
        return _canonical({"cancelled": True, "id": continuation.reminder_id})

    def _create(self, arguments: dict[str, object]) -> ApprovalRequired:
        if set(arguments) != {"body", "due_local"}:
            raise ReminderError("create_reminder arguments must be body and due_local")
        body = _body(arguments["body"])
        due_at, local = self._resolve_local(arguments["due_local"])
        if due_at <= _utc(self._clock.now(), "now"):
            raise ReminderError("due time must be in the future")
        reminder_id = self._new_id()
        display = self._display(
            "Create reminder?",
            reminder_id=reminder_id,
            body=body,
            local=local,
            timezone=self._timezone_name,
        )
        self._trace.record(
            "reminder_create_proposed",
            {
                "id": reminder_id,
                "body": body,
                "due_at": _timestamp(due_at),
                "timezone": self._timezone_name,
            },
        )
        return ApprovalRequired(
            PendingAction(
                host="reminder",
                prefix="create_reminder",
                display=display,
                allow_save_permission=False,
            ),
            _CreateContinuation(reminder_id, body, due_at),
        )

    def _edit(self, arguments: dict[str, object]) -> ApprovalRequired:
        reminder_id = arguments.get("reminder_id")
        try:
            if set(arguments) != {"reminder_id", "body", "due_local"}:
                raise ReminderError(
                    "edit_reminder requires reminder_id, body, and due_local"
                )
            record = self._pending(reminder_id)
            replacement_body = arguments["body"]
            replacement_due = arguments["due_local"]
            if replacement_body is None and replacement_due is None:
                raise ReminderError("edit_reminder requires at least one replacement")
            body = record.body if replacement_body is None else _body(replacement_body)
            if replacement_due is None:
                due_at = record.due_at
                local = due_at.astimezone(self._timezone).replace(tzinfo=None)
            else:
                due_at, local = self._resolve_local(replacement_due)
            now = _utc(self._clock.now(), "now")
            candidate = Reminder(
                id=record.id,
                body=body,
                due_at=due_at,
                timezone=self._timezone_name,
                status="pending",
                created_at=record.created_at,
                updated_at=now,
            )
            _validate_record(candidate, now)
        except (ReminderError, TypeError) as exc:
            self._trace.record(
                "reminder_edit_validation_failed",
                {
                    "id": reminder_id if isinstance(reminder_id, str) else None,
                    "error": str(exc),
                },
            )
            raise
        display = self._display(
            "Edit reminder?",
            reminder_id=candidate.id,
            body=candidate.body,
            local=local,
            timezone=candidate.timezone,
        )
        self._trace.record(
            "reminder_edit_proposed",
            {
                "id": candidate.id,
                "body": candidate.body,
                "due_at": _timestamp(candidate.due_at),
                "timezone": candidate.timezone,
            },
        )
        return ApprovalRequired(
            PendingAction(
                host="reminder",
                prefix="edit_reminder",
                display=display,
                allow_save_permission=False,
            ),
            _EditContinuation(record.updated_at, record.id, body, due_at),
        )

    def _cancel(self, arguments: dict[str, object]) -> ApprovalRequired:
        reminder_id = arguments.get("reminder_id")
        try:
            if set(arguments) != {"reminder_id"}:
                raise ReminderError("cancel_reminder requires reminder_id")
            record = self._pending(reminder_id)
        except (ReminderError, TypeError) as exc:
            self._trace.record(
                "reminder_cancel_validation_failed",
                {
                    "id": reminder_id if isinstance(reminder_id, str) else None,
                    "error": str(exc),
                },
            )
            raise
        local = record.due_at.astimezone(self._timezone).replace(tzinfo=None)
        self._trace.record(
            "reminder_cancel_proposed",
            {
                "id": record.id,
                "body": record.body,
                "due_at": _timestamp(record.due_at),
                "timezone": record.timezone,
            },
        )
        return ApprovalRequired(
            PendingAction(
                host="reminder",
                prefix="cancel_reminder",
                display=self._display(
                    "Cancel reminder?",
                    reminder_id=record.id,
                    body=record.body,
                    local=local,
                    timezone=record.timezone,
                ),
                allow_save_permission=False,
            ),
            _CancelContinuation(record.updated_at, record.id),
        )

    def _pending(self, reminder_id: object) -> Reminder:
        if not isinstance(reminder_id, str) or not _ID_PATTERN.fullmatch(reminder_id):
            raise ReminderError("reminder id must be eight lowercase letters or digits")
        record = self._store.get(reminder_id)
        if record is None:
            raise ReminderError("reminder not found")
        if record.status != "pending" or record.attempt_at is not None:
            raise ReminderError("reminder is not pending")
        return record

    @staticmethod
    def _display(
        label: str,
        *,
        reminder_id: str,
        body: str,
        local: datetime,
        timezone: str,
    ) -> str:
        return (
            f"{label}\n"
            f"ID: {reminder_id}\n"
            f"Body: {body}\n"
            f"Date: {local.date().isoformat()}\n"
            f"Time: {local.time().isoformat()}\n"
            f"Timezone: {timezone}"
        )

    def _list(self, arguments: dict[str, object]) -> str:
        if set(arguments) != {"include_terminal"}:
            raise ReminderError("list_reminders requires include_terminal")
        include_terminal = arguments["include_terminal"]
        if not isinstance(include_terminal, bool):
            raise ReminderError("include_terminal must be a boolean")
        records = self._store.list(
            include_terminal=include_terminal, limit=MAX_LIST_ENTRIES + 1
        )
        entries: list[dict[str, object]] = []
        truncated = len(records) > MAX_LIST_ENTRIES
        for record in records[:MAX_LIST_ENTRIES]:
            entry = self._entry(record)
            candidate = {
                "include_terminal": include_terminal,
                "reminders": [*entries, entry],
                "truncated": truncated,
            }
            if len(_canonical(candidate)) > self._max_result_chars:
                truncated = True
                break
            entries.append(entry)
        result = {
            "include_terminal": include_terminal,
            "reminders": entries,
            "truncated": truncated,
        }
        encoded = _canonical(result)
        if len(encoded) > self._max_result_chars:
            raise ReminderError(
                "configured output limit is too small for a Reminder list"
            )
        self._trace.record(
            "reminder_listed",
            {
                "include_terminal": include_terminal,
                "returned": len(entries),
                "truncated": truncated,
            },
        )
        return encoded

    def _resolve_local(self, value: object) -> tuple[datetime, datetime]:
        if not isinstance(value, str) or not _LOCAL_PATTERN.fullmatch(value):
            raise ReminderError("due_local must be a strict local ISO-8601 date-time")
        try:
            local = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ReminderError(
                "due_local must be a strict local ISO-8601 date-time"
            ) from exc
        candidates: set[datetime] = set()
        for fold in (0, 1):
            aware = local.replace(tzinfo=self._timezone, fold=fold)
            instant = aware.astimezone(UTC)
            if instant.astimezone(self._timezone).replace(tzinfo=None) == local:
                candidates.add(instant)
        if len(candidates) != 1:
            raise ReminderError("due_local is ambiguous or nonexistent")
        return candidates.pop(), local

    def _new_id(self) -> str:
        for _ in range(32):
            candidate = self._id_generator()
            if not isinstance(candidate, str) or not _ID_PATTERN.fullmatch(candidate):
                raise ReminderError("generated reminder id is invalid")
            if not self._store.contains(candidate):
                return candidate
        raise ReminderError("could not allocate a unique reminder id")

    @staticmethod
    def _entry(record: Reminder) -> dict[str, object]:
        local = record.due_at.astimezone(_zone(record.timezone))
        return {
            "id": record.id,
            "body": record.body,
            "due": {
                "date": local.date().isoformat(),
                "time": local.time().replace(tzinfo=None).isoformat(),
                "timezone": record.timezone,
            },
            "status": record.status,
        }


class ReminderScheduler:
    """Wait for and make the one transport attempt for each due Reminder."""

    def __init__(
        self,
        store: ReminderStore,
        *,
        sender: OpenWASender,
        operator_chat_id: str,
        clock: SchedulerClock | None = None,
        trace: Trace | None = None,
    ) -> None:
        if not isinstance(store, ReminderStore):
            raise TypeError("store must be a ReminderStore")
        if not isinstance(operator_chat_id, str) or not operator_chat_id:
            raise ValueError("operator_chat_id must be non-empty")
        self.store = store
        self.sender = sender
        self.operator_chat_id = operator_chat_id
        self._clock = clock or _SystemSchedulerClock()
        self._trace = trace or _NoTrace()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._started_at: datetime | None = None
        self._stopping = False

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = False
            self._started_at = _utc(self._clock.now(), "now")
            self._task = asyncio.create_task(self.run())

    def wake(self) -> None:
        self._wake.set()

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        self._stopping = True
        self.wake()
        await task

    async def run(self) -> None:
        started_at = self._started_at or _utc(self._clock.now(), "now")
        while not self._stopping:
            self._wake.clear()
            reminder = self.store.next_pending(due_after=started_at)
            if reminder is None:
                await self._wake.wait()
                continue
            await self._clock.wait_until(reminder.due_at, self._wake)
            if self._wake.is_set() or self._stopping:
                continue
            now = _utc(self._clock.now(), "now")
            claimed = self.store.begin_due_attempt(reminder.id, now=now)
            if claimed is None:
                continue
            self._trace.record(
                "reminder_delivery_attempt",
                {"id": claimed.id, "attempt_at": _timestamp(now)},
            )
            await self._deliver(claimed)

    async def _deliver(self, reminder: Reminder) -> None:
        outbound_id: str | None = None
        failure_classification: str | None = None
        try:
            outbound_id = await asyncio.to_thread(
                self.sender.send_text, self.operator_chat_id, reminder.body
            )
            if (
                not isinstance(outbound_id, str)
                or not outbound_id
                or len(outbound_id) > 256
            ):
                outbound_id = None
                raise OpenWASendError("invalid_response", may_have_sent=True)
        except OpenWASendError as exc:
            status = "unknown" if exc.may_have_sent else "failed"
            failure_classification = exc.code
        except Exception:  # noqa: BLE001 - an unclassified send may have succeeded
            status = "unknown"
            failure_classification = "unexpected_error"
        else:
            status = "sent"
        self.store.finish_attempt(
            reminder.id,
            status=status,
            now=self._clock.now(),
            outbound_message_id=outbound_id,
            failure_classification=failure_classification,
        )
        payload: dict[str, object] = {"id": reminder.id, "status": status}
        if outbound_id is not None:
            payload["outbound_message_id"] = outbound_id
        if failure_classification is not None:
            payload["failure_classification"] = failure_classification
        self._trace.record("reminder_delivery_outcome", payload)


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "MAX_BODY_CHARACTERS",
    "Reminder",
    "ReminderError",
    "ReminderScheduler",
    "ReminderStore",
    "ReminderTools",
]

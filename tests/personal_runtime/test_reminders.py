from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jarvis_personal_runtime.reminders import (
    Reminder,
    ReminderError,
    ReminderStore,
    ReminderTools,
)
from jarvis_personal_runtime.responses import DirectResponsesRunner, ResponsesResult
from jarvis_personal_runtime.runtime import Completed, InboundText, PersonalRuntime


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.value = now

    def now(self) -> datetime:
        return self.value


class MemoryTrace:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def record(self, event: str, payload: dict[str, object]) -> None:
        self.events.append((event, payload))


def execute(
    tools: ReminderTools, name: str, arguments: dict[str, object]
) -> dict[str, object]:
    result = asyncio.run(tools.execute(name, arguments))
    assert isinstance(result, str)
    return json.loads(result)


def make_tools(
    tmp_path: Path,
    *,
    ids: list[str] | None = None,
) -> tuple[ReminderStore, ReminderTools, MutableClock, MemoryTrace]:
    clock = MutableClock(datetime(2026, 9, 5, 9, tzinfo=UTC))
    trace = MemoryTrace()
    values = iter(ids or ["a1b2c3d4"])
    store = ReminderStore(tmp_path / "data" / "reminders.sqlite3")
    tools = ReminderTools(
        store,
        operator_timezone="Asia/Amman",
        clock=clock,
        id_generator=lambda: next(values),
        trace=trace,
    )
    return store, tools, clock, trace


def create(
    tools: ReminderTools, body: str = "Call Sara", due_local: str = "2026-09-06T12:00"
) -> dict[str, object]:
    return execute(tools, "create_reminder", {"body": body, "due_local": due_local})


def listed(
    tools: ReminderTools, *, include_terminal: bool = False
) -> dict[str, object]:
    return execute(tools, "list_reminders", {"include_terminal": include_terminal})


def test_create_commits_immediately_and_preserves_exact_details(tmp_path: Path) -> None:
    store, tools, _, trace = make_tools(tmp_path)
    arguments: dict[str, object] = {
        "body": "Call Sara — bring the signed copy.",
        "due_local": "2026-09-06T12:30",
    }

    created = execute(tools, "create_reminder", arguments)
    assert created["created"] is True
    assert created["reminder"]["id"] == "a1b2c3d4"
    arguments["body"] = "changed after execution"
    reminders = listed(tools)["reminders"]
    assert created["reminder"] == reminders[0]
    assert reminders == [
        {
            "body": "Call Sara — bring the signed copy.",
            "due": {
                "date": "2026-09-06",
                "time": "12:30:00",
                "timezone": "Asia/Amman",
            },
            "id": "a1b2c3d4",
            "status": "pending",
        }
    ]
    assert len(store.list()) == 1
    assert [event for event, _ in trace.events] == [
        "reminder_created",
        "reminder_listed",
    ]


@pytest.mark.parametrize(
    ("body", "due_local", "needle"),
    [
        ("", "2026-09-06T12:00", "body"),
        ("   ", "2026-09-06T12:00", "body"),
        ("x" * 4097, "2026-09-06T12:00", "body"),
        ("valid", "tomorrow", "due_local"),
        ("valid", "2026-09-06 12:00", "due_local"),
        ("valid", "2026-09-06T12:00+03:00", "due_local"),
        ("valid", "2026-09-05T11:59", "future"),
    ],
)
def test_create_rejects_invalid_values_without_saving(
    tmp_path: Path, body: str, due_local: str, needle: str
) -> None:
    store, tools, _, _ = make_tools(tmp_path)
    with pytest.raises(ReminderError, match=needle):
        create(tools, body, due_local)
    assert store.list(include_terminal=True) == ()


def test_local_time_resolution_rejects_dst_gaps_and_folds(tmp_path: Path) -> None:
    store = ReminderStore(tmp_path / "reminders.sqlite3")
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    tools = ReminderTools(store, operator_timezone="America/New_York", clock=clock)
    for due_local in ("2026-03-08T02:30", "2026-11-01T01:30"):
        with pytest.raises(ReminderError, match="ambiguous or nonexistent"):
            create(tools, "DST boundary", due_local)
    assert store.list() == ()


def test_reopen_preserves_reminder_and_rejects_id_collision(tmp_path: Path) -> None:
    store, tools, clock, _ = make_tools(
        tmp_path, ids=["same0001", "same0001", "second01"]
    )
    assert create(tools)["reminder"]["id"] == "same0001"
    assert create(tools, "Another", "2026-09-06T13:00")["reminder"]["id"] == "second01"
    with pytest.raises(ReminderError, match="already exists"):
        store.save(store.list()[0], now=clock.now())
    with pytest.raises(ReminderError, match="body"):
        store.save(
            replace(store.list()[0], id="bad00001", body="x" * 4097), now=clock.now()
        )
    store.close()

    reopened = ReminderStore(tmp_path / "data" / "reminders.sqlite3")
    assert {item.id for item in reopened.list()} == {"same0001", "second01"}


def test_exact_transport_limit_is_accepted(tmp_path: Path) -> None:
    store, tools, _, _ = make_tools(tmp_path)
    assert create(tools, "x" * 4096)["created"] is True
    assert len(store.list()[0].body) == 4096


def test_list_pending_and_terminal_records(tmp_path: Path) -> None:
    store, tools, clock, _ = make_tools(tmp_path, ids=["later001", "early001"])
    create(tools, "Later", "2026-09-06T14:00")
    create(tools, "Earlier", "2026-09-06T12:00")
    cancelled = replace(
        store.list()[0],
        id="cancel01",
        status="cancelled",
        completed_at=clock.now() + timedelta(minutes=1),
    )
    store.save(cancelled, now=clock.now())

    assert [item["id"] for item in listed(tools)["reminders"]] == [
        "early001",
        "later001",
    ]
    history = listed(tools, include_terminal=True)
    assert [item["id"] for item in history["reminders"]] == [
        "cancel01",
        "early001",
        "later001",
    ]
    assert history["reminders"][0]["status"] == "cancelled"
    assert history["truncated"] is False


def test_list_caps_entries_and_reports_truncation(tmp_path: Path) -> None:
    store = ReminderStore(tmp_path / "reminders.sqlite3")
    clock = MutableClock(datetime(2026, 9, 5, 9, tzinfo=UTC))
    for index in range(26):
        store.save(
            Reminder(
                id=f"r{index:07d}",
                body=f"Reminder {index}",
                due_at=clock.now() + timedelta(days=1, minutes=index),
                timezone="Asia/Amman",
                status="pending",
                created_at=clock.now(),
                updated_at=clock.now(),
            ),
            now=clock.now(),
        )
    tools = ReminderTools(store, operator_timezone="Asia/Amman", clock=clock)
    result = listed(tools)
    assert len(result["reminders"]) == 25
    assert result["truncated"] is True


def test_list_schema_keeps_explicit_pending_default() -> None:
    definition = next(
        item for item in ReminderTools.definitions if item["name"] == "list_reminders"
    )
    assert "false for the default pending-only view" in definition["description"]
    assert definition["parameters"]["required"] == ["include_terminal"]


@pytest.mark.parametrize(
    ("body", "due_local", "expected_body", "expected_time"),
    [
        ("Reworded reminder", None, "Reworded reminder", "12:00:00"),
        (None, "2026-09-06T14:30", "Original reminder", "14:30:00"),
        ("Changed together", "2026-09-07T08:15", "Changed together", "08:15:00"),
    ],
)
def test_edit_commits_immediately_and_preserves_id(
    tmp_path: Path,
    body: str | None,
    due_local: str | None,
    expected_body: str,
    expected_time: str,
) -> None:
    store, tools, _, trace = make_tools(tmp_path)
    create(tools, "Original reminder")
    edited = execute(
        tools,
        "edit_reminder",
        {"reminder_id": "a1b2c3d4", "body": body, "due_local": due_local},
    )
    assert edited["edited"] is True
    assert edited["reminder"]["id"] == "a1b2c3d4"
    record = listed(tools)["reminders"][0]
    assert edited["reminder"] == record
    assert record["body"] == expected_body
    assert record["due"]["time"] == expected_time
    assert len(store.list()) == 1
    assert [event for event, _ in trace.events].count("reminder_edited") == 1


def test_edit_rejects_invalid_and_terminal_targets_without_mutation(
    tmp_path: Path,
) -> None:
    store, tools, _, trace = make_tools(tmp_path)
    create(tools, "Keep exact bytes")
    original = store.list()[0]
    for body, due in (
        (None, None),
        ("", None),
        (None, "tomorrow"),
        (None, "2026-09-05T11:59"),
    ):
        with pytest.raises(ReminderError):
            execute(
                tools,
                "edit_reminder",
                {"reminder_id": original.id, "body": body, "due_local": due},
            )
    assert store.list()[0] == original
    assert [event for event, _ in trace.events].count(
        "reminder_edit_validation_failed"
    ) == 4

    with pytest.raises(ReminderError, match="not found"):
        execute(
            tools,
            "edit_reminder",
            {"reminder_id": "missing1", "body": "New", "due_local": None},
        )
    assert store.list()[0] == original


def test_cancel_commits_immediately_and_retains_history(tmp_path: Path) -> None:
    store, tools, _, trace = make_tools(tmp_path)
    create(tools, "Cancel this exact body")
    assert execute(tools, "cancel_reminder", {"reminder_id": "a1b2c3d4"}) == {
        "cancelled": True,
        "id": "a1b2c3d4",
    }
    assert store.list() == ()
    assert store.list(include_terminal=True)[0].status == "cancelled"
    assert [event for event, _ in trace.events] == [
        "reminder_created",
        "reminder_cancelled",
    ]
    with pytest.raises(ReminderError, match="not pending"):
        execute(tools, "cancel_reminder", {"reminder_id": "a1b2c3d4"})


def test_missing_and_terminal_edit_or_cancel_are_rejected(tmp_path: Path) -> None:
    store, tools, clock, _ = make_tools(tmp_path)
    store.save(
        Reminder(
            id="terminal",
            body="Historical",
            due_at=clock.now() + timedelta(days=1),
            timezone="Asia/Amman",
            status="cancelled",
            created_at=clock.now(),
            updated_at=clock.now(),
            completed_at=clock.now(),
        ),
        now=clock.now(),
    )
    for name, extra in (
        ("edit_reminder", {"body": "New", "due_local": None}),
        ("cancel_reminder", {}),
    ):
        with pytest.raises(ReminderError, match="not found"):
            execute(tools, name, {"reminder_id": "missing1", **extra})
        with pytest.raises(ReminderError, match="not pending"):
            execute(tools, name, {"reminder_id": "terminal", **extra})


@pytest.mark.parametrize("status", ["sent", "failed", "unknown", "cancelled"])
def test_terminal_outcomes_reopen_without_becoming_pending(
    tmp_path: Path, status: str
) -> None:
    path = tmp_path / "reminders.sqlite3"
    store = ReminderStore(path)
    now = datetime(2026, 9, 5, 9, tzinfo=UTC)
    record = Reminder(
        id="terminal",
        body="Historical",
        due_at=now - timedelta(hours=1),
        timezone="Asia/Amman",
        status=status,
        created_at=now - timedelta(days=1),
        updated_at=now,
        attempt_at=now - timedelta(minutes=1) if status != "cancelled" else None,
        completed_at=now,
        outbound_message_id="out-1" if status == "sent" else None,
        failure_classification=status if status in {"failed", "unknown"} else None,
    )
    store.save(record, now=now)
    store.close()
    reopened = ReminderStore(path)
    assert reopened.list() == ()
    assert reopened.list(include_terminal=True) == (record,)


def test_runtime_completes_reminder_without_approval_prompt(tmp_path: Path) -> None:
    store, tools, clock, trace = make_tools(tmp_path)

    class Responses:
        def __init__(self) -> None:
            self.requests: list[dict[str, object]] = []
            self.results = [
                ResponsesResult(
                    output=(
                        {
                            "type": "function_call",
                            "name": "create_reminder",
                            "call_id": "call-reminder",
                            "arguments": json.dumps(
                                {"body": "One step", "due_local": "2026-09-06T12:00"}
                            ),
                        },
                    ),
                    output_text="",
                ),
                ResponsesResult(
                    output=(), output_text="Reminder set for tomorrow at noon."
                ),
            ]

        async def create(
            self, request: dict[str, object], *, timeout: float
        ) -> ResponsesResult:
            self.requests.append(request)
            return self.results.pop(0)

    responses = Responses()
    runner = DirectResponsesRunner(
        responses, tools=tools, request_timeout_seconds=30, trace=trace
    )
    runtime = PersonalRuntime(request_runner=runner, clock=clock, trace=trace)

    result = asyncio.run(runtime.receive(InboundText("m1", "remind me", clock.now())))

    assert result.disposition == "completed"
    assert result.replies == ("Reminder set for tomorrow at noon.",)
    assert len(store.list()) == 1
    assert len(responses.requests) == 2
    assert '"created":true' in str(responses.requests[1]["input"])
    assert not any(event == "approval_required" for event, _ in trace.events)


def test_mutation_descriptions_require_disambiguation() -> None:
    definitions = {str(item["name"]): item for item in ReminderTools.definitions}
    assert "immediately" in str(definitions["create_reminder"]["description"])
    for name in ("edit_reminder", "cancel_reminder"):
        description = str(definitions[name]["description"])
        assert "immediately" in description
        assert "list_reminders" in description
        assert "more than one" in description
        assert "ask the operator" in description
        assert "Never guess" in description


@pytest.mark.parametrize("operation", ["edit", "cancel"])
def test_ambiguous_reference_lists_and_asks_without_guessing_id(
    tmp_path: Path, operation: str
) -> None:
    store, tools, _, trace = make_tools(tmp_path, ids=["first001", "second01"])
    create(tools, "Call Sara about the first draft")
    create(tools, "Call Sara about the final draft", "2026-09-06T13:00")

    class Responses:
        def __init__(self) -> None:
            self.results = [
                ResponsesResult(
                    output=(
                        {
                            "type": "function_call",
                            "name": "list_reminders",
                            "call_id": "call-list",
                            "arguments": json.dumps({"include_terminal": False}),
                        },
                    ),
                    output_text="",
                ),
                ResponsesResult(
                    output=(),
                    output_text="Which one do you mean: first001 or second01?",
                ),
            ]

        async def create(
            self, request: dict[str, object], *, timeout: float
        ) -> ResponsesResult:
            return self.results.pop(0)

    runner = DirectResponsesRunner(
        Responses(), tools=tools, request_timeout_seconds=30, trace=trace
    )
    result = asyncio.run(
        runner.run(
            f"{operation} my Sara reminder",
            model="gpt-test",
            reasoning="low",
            system_prompt="Use the prepared Reminder operations exactly as described.",
        )
    )
    assert isinstance(result, Completed)
    assert "Which one do you mean" in str(result.reply)
    assert [call["name"] for event, call in trace.events if event == "tool_call"] == [
        "list_reminders"
    ]
    assert len(store.list()) == 2

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
from jarvis_personal_runtime.runtime import (
    ApprovalDecision,
    ApprovalRequired,
    Completed,
    InboundText,
    PersonalRuntime,
)


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
) -> str | ApprovalRequired:
    return asyncio.run(tools.execute(name, arguments))


def resume(
    tools: ReminderTools, continuation: object, approved: bool
) -> dict[str, object]:
    return json.loads(asyncio.run(tools.resume(continuation, approved=approved)))


def listed(
    tools: ReminderTools, *, include_terminal: bool = False
) -> dict[str, object]:
    result = execute(tools, "list_reminders", {"include_terminal": include_terminal})
    assert isinstance(result, str)
    return json.loads(result)


def make_tools(
    tmp_path: Path,
    *,
    clock: MutableClock | None = None,
    ids: list[str] | None = None,
    trace: MemoryTrace | None = None,
) -> tuple[ReminderStore, ReminderTools, MutableClock, MemoryTrace]:
    actual_clock = clock or MutableClock(datetime(2026, 9, 5, 9, tzinfo=UTC))
    actual_trace = trace or MemoryTrace()
    values = iter(ids or ["a1b2c3d4"])
    store = ReminderStore(tmp_path / "data" / "reminders.sqlite3")
    tools = ReminderTools(
        store,
        operator_timezone="Asia/Amman",
        clock=actual_clock,
        id_generator=lambda: next(values),
        trace=actual_trace,
    )
    return store, tools, actual_clock, actual_trace


def test_create_proposal_freezes_exact_values_and_persists_only_on_approval(
    tmp_path: Path,
) -> None:
    store, tools, _, trace = make_tools(tmp_path)
    arguments: dict[str, object] = {
        "body": "Call Sara — bring the signed copy.",
        "due_local": "2026-09-06T12:30",
    }

    proposed = execute(tools, "create_reminder", arguments)

    assert isinstance(proposed, ApprovalRequired)
    assert proposed.action.allow_save_permission is False
    assert proposed.action.display == (
        "Create reminder?\n"
        "ID: a1b2c3d4\n"
        "Body: Call Sara — bring the signed copy.\n"
        "Date: 2026-09-06\n"
        "Time: 12:30:00\n"
        "Timezone: Asia/Amman"
    )
    assert store.list(include_terminal=True) == ()

    arguments["body"] = "changed after proposal"
    result = resume(tools, proposed.continuation, True)

    assert result == {"created": True, "id": "a1b2c3d4"}
    records = listed(tools)["reminders"]
    assert records == [
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
    names = [event for event, _ in trace.events]
    assert names == [
        "reminder_create_proposed",
        "reminder_create_approval",
        "reminder_created",
        "reminder_listed",
    ]


def test_rejection_and_expired_approval_leave_no_reminder(tmp_path: Path) -> None:
    store, tools, clock, trace = make_tools(tmp_path, ids=["reject01", "expired1"])
    rejected = execute(
        tools,
        "create_reminder",
        {"body": "Do not save", "due_local": "2026-09-05T13:00"},
    )
    assert isinstance(rejected, ApprovalRequired)
    assert resume(tools, rejected.continuation, False) == {"rejected": True}

    expiring = execute(
        tools,
        "create_reminder",
        {"body": "Too late", "due_local": "2026-09-05T13:01"},
    )
    assert isinstance(expiring, ApprovalRequired)
    clock.value = datetime(2026, 9, 5, 10, 1, tzinfo=UTC)
    assert resume(tools, expiring.continuation, True) == {
        "error": "due_time_not_future"
    }

    assert store.list(include_terminal=True) == ()
    approvals = [
        payload
        for event, payload in trace.events
        if event == "reminder_create_approval"
    ]
    assert [item["outcome"] for item in approvals] == ["rejected", "expired"]


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
def test_create_rejects_invalid_body_and_strict_local_time(
    tmp_path: Path, body: str, due_local: str, needle: str
) -> None:
    _, tools, _, _ = make_tools(tmp_path)

    with pytest.raises(ReminderError, match=needle):
        execute(
            tools,
            "create_reminder",
            {"body": body, "due_local": due_local},
        )


def test_local_time_resolution_rejects_ambiguous_and_nonexistent_times(
    tmp_path: Path,
) -> None:
    store = ReminderStore(tmp_path / "reminders.sqlite3")
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    tools = ReminderTools(store, operator_timezone="America/New_York", clock=clock)

    for due_local in ("2026-03-08T02:30", "2026-11-01T01:30"):
        with pytest.raises(ReminderError, match="ambiguous or nonexistent"):
            execute(
                tools,
                "create_reminder",
                {"body": "DST boundary", "due_local": due_local},
            )


def test_listing_is_bounded_deterministic_and_can_include_terminal_records(
    tmp_path: Path,
) -> None:
    store, tools, clock, _ = make_tools(tmp_path, ids=["later001", "early001"])
    for body, due_local in (
        ("Later", "2026-09-06T14:00"),
        ("Earlier", "2026-09-06T12:00"),
    ):
        proposal = execute(
            tools, "create_reminder", {"body": body, "due_local": due_local}
        )
        assert isinstance(proposal, ApprovalRequired)
        resume(tools, proposal.continuation, True)

    pending = store.list(include_terminal=False)
    cancelled = replace(
        pending[0],
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


def test_list_schema_names_explicit_false_as_the_default_pending_view() -> None:
    definition = next(
        item for item in ReminderTools.definitions if item["name"] == "list_reminders"
    )

    assert "false for the default pending-only view" in definition["description"]
    assert definition["parameters"]["required"] == ["include_terminal"]


def test_store_reopen_and_new_tool_session_preserve_stable_reminder(
    tmp_path: Path,
) -> None:
    path = tmp_path / "data" / "reminders.sqlite3"
    store, tools, clock, _ = make_tools(tmp_path)
    proposal = execute(
        tools,
        "create_reminder",
        {"body": "Survive /new", "due_local": "2026-09-06T12:00"},
    )
    assert isinstance(proposal, ApprovalRequired)
    resume(tools, proposal.continuation, True)
    store.close()

    reopened = ReminderStore(path)
    new_session_tools = ReminderTools(
        reopened, operator_timezone="Asia/Amman", clock=clock
    )

    assert listed(new_session_tools)["reminders"][0]["id"] == "a1b2c3d4"


def test_store_collision_check_and_state_boundary_validation(tmp_path: Path) -> None:
    store, tools, clock, _ = make_tools(tmp_path, ids=["same0001", "same0001"])
    proposal = execute(
        tools,
        "create_reminder",
        {"body": "First", "due_local": "2026-09-06T12:00"},
    )
    assert isinstance(proposal, ApprovalRequired)
    resume(tools, proposal.continuation, True)

    with pytest.raises(ReminderError, match="already exists"):
        store.save(store.list(include_terminal=False)[0], now=clock.now())
    with pytest.raises(ReminderError, match="body"):
        store.save(
            replace(
                store.list(include_terminal=False)[0],
                id="bad00001",
                body="x" * 4097,
            ),
            now=clock.now(),
        )


def test_exact_transport_limit_is_accepted_and_ids_are_collision_checked(
    tmp_path: Path,
) -> None:
    store, tools, _, _ = make_tools(tmp_path, ids=["first001", "first001", "second01"])
    first = execute(
        tools,
        "create_reminder",
        {"body": "x" * 4096, "due_local": "2026-09-06T12:00"},
    )
    assert isinstance(first, ApprovalRequired)
    resume(tools, first.continuation, True)

    second = execute(
        tools,
        "create_reminder",
        {"body": "Another", "due_local": "2026-09-06T13:00"},
    )
    assert isinstance(second, ApprovalRequired)
    assert "ID: second01" in second.action.display
    resume(tools, second.continuation, True)

    assert [record.id for record in store.list(include_terminal=True)] == [
        "first001",
        "second01",
    ]


def test_terminal_outcome_fields_reopen_and_list_without_becoming_pending(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reminders.sqlite3"
    store = ReminderStore(path)
    now = datetime(2026, 9, 5, 9, tzinfo=UTC)
    record = Reminder(
        id="failed01",
        body="Retained failure",
        due_at=now - timedelta(hours=1),
        timezone="Asia/Amman",
        status="failed",
        created_at=now - timedelta(days=1),
        updated_at=now,
        attempt_at=now - timedelta(minutes=1),
        completed_at=now,
        failure_classification="http_error",
    )
    store.save(record, now=now)
    store.close()

    reopened = ReminderStore(path)
    assert reopened.list(include_terminal=False) == ()
    assert reopened.list(include_terminal=True) == (record,)


def test_list_caps_entries_and_reports_truncation(tmp_path: Path) -> None:
    store = ReminderStore(tmp_path / "reminders.sqlite3")
    clock = MutableClock(datetime(2026, 9, 5, 9, tzinfo=UTC))
    for index in range(26):
        instant = clock.now() + timedelta(days=1, minutes=index)
        store.save(
            Reminder(
                id=f"r{index:07d}",
                body=f"Reminder {index}",
                due_at=instant,
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


@pytest.mark.parametrize(("choice", "expected_count"), [("1", 1), ("9", 0)])
def test_runtime_uses_exact_non_saveable_approval_grammar(
    tmp_path: Path, choice: str, expected_count: int
) -> None:
    store, tools, clock, _ = make_tools(tmp_path)

    class Runner:
        def cancel_pending(self, continuation: object) -> None:
            return None

        async def run(self, *args: object, **kwargs: object) -> ApprovalRequired:
            result = await tools.execute(
                "create_reminder",
                {"body": "Use exact approval", "due_local": "2026-09-06T12:00"},
            )
            assert isinstance(result, ApprovalRequired)
            return result

        async def resume(
            self, decision: ApprovalDecision, continuation: object
        ) -> Completed:
            output = await tools.resume(
                continuation, approved=decision is ApprovalDecision.APPROVE_ONCE
            )
            return Completed(output)

    runtime = PersonalRuntime(request_runner=Runner(), clock=clock)

    async def scenario() -> None:
        pending = await runtime.receive(InboundText("m1", "schedule it", clock.now()))
        assert pending.disposition == "approval_required"
        assert pending.replies[0].endswith("Reply 1 to approve once or 9 to reject.")

        ignored = await runtime.receive(InboundText("m2", "yes", clock.now()))
        assert ignored.disposition == "ignored"
        assert store.list(include_terminal=True) == ()

        decided = await runtime.receive(InboundText("m3", choice, clock.now()))
        assert decided.disposition == ("completed" if choice == "1" else "rejected")

    asyncio.run(scenario())
    assert len(store.list(include_terminal=True)) == expected_count


def test_direct_responses_rejection_is_traced_by_the_reminder_operation(
    tmp_path: Path,
) -> None:
    store, tools, clock, trace = make_tools(tmp_path)

    class Responses:
        def __init__(self) -> None:
            self.results = [
                ResponsesResult(
                    output=(
                        {
                            "type": "function_call",
                            "name": "create_reminder",
                            "call_id": "call-reminder",
                            "arguments": json.dumps(
                                {
                                    "body": "Reject through owner",
                                    "due_local": "2026-09-06T12:00",
                                }
                            ),
                        },
                    ),
                    output_text="",
                ),
                ResponsesResult(output=(), output_text="Not saved."),
            ]

        async def create(
            self, request: dict[str, object], *, timeout: float
        ) -> ResponsesResult:
            return self.results.pop(0)

    runner = DirectResponsesRunner(
        Responses(),
        tools=tools,
        request_timeout_seconds=30,
        trace=trace,
    )
    runtime = PersonalRuntime(request_runner=runner, clock=clock, trace=trace)

    async def scenario() -> None:
        pending = await runtime.receive(InboundText("m1", "schedule", clock.now()))
        assert pending.disposition == "approval_required"
        rejected = await runtime.receive(InboundText("m2", "9", clock.now()))
        assert rejected.disposition == "rejected"

    asyncio.run(scenario())

    assert store.list(include_terminal=True) == ()
    assert (
        "reminder_create_approval",
        {"id": "a1b2c3d4", "outcome": "rejected"},
    ) in trace.events


@pytest.mark.parametrize(
    ("body", "due_local", "expected_body", "expected_time"),
    [
        ("Reworded reminder", None, "Reworded reminder", "12:00:00"),
        (None, "2026-09-06T14:30", "Original reminder", "14:30:00"),
        ("Changed together", "2026-09-07T08:15", "Changed together", "08:15:00"),
    ],
)
def test_edit_pending_reminder_preserves_id_and_supports_each_change_shape(
    tmp_path: Path,
    body: str | None,
    due_local: str | None,
    expected_body: str,
    expected_time: str,
) -> None:
    _store, tools, _, _ = make_tools(tmp_path)
    created = execute(
        tools,
        "create_reminder",
        {"body": "Original reminder", "due_local": "2026-09-06T12:00"},
    )
    assert isinstance(created, ApprovalRequired)
    resume(tools, created.continuation, True)

    proposed = execute(
        tools,
        "edit_reminder",
        {"reminder_id": "a1b2c3d4", "body": body, "due_local": due_local},
    )

    assert isinstance(proposed, ApprovalRequired)
    assert proposed.action.allow_save_permission is False
    assert proposed.action.display == (
        "Edit reminder?\n"
        "ID: a1b2c3d4\n"
        f"Body: {expected_body}\n"
        f"Date: {'2026-09-07' if expected_time == '08:15:00' else '2026-09-06'}\n"
        f"Time: {expected_time}\n"
        "Timezone: Asia/Amman"
    )
    before = listed(tools)["reminders"][0]
    assert before["body"] == "Original reminder"
    result = resume(tools, proposed.continuation, True)
    after = listed(tools)["reminders"][0]

    assert result == {"edited": True, "id": "a1b2c3d4"}
    assert after["id"] == "a1b2c3d4"
    assert after["body"] == expected_body
    assert after["due"]["time"] == expected_time
    assert resume(tools, proposed.continuation, True) == {"error": "already_resolved"}
    assert listed(tools)["reminders"][0] == after


def test_rejected_and_expired_edits_leave_existing_reminder_unchanged(
    tmp_path: Path,
) -> None:
    store, tools, clock, trace = make_tools(tmp_path)
    created = execute(
        tools,
        "create_reminder",
        {"body": "Keep exact bytes", "due_local": "2026-09-06T12:00"},
    )
    assert isinstance(created, ApprovalRequired)
    resume(tools, created.continuation, True)
    original = store.list()[0]

    rejected = execute(
        tools,
        "edit_reminder",
        {"reminder_id": original.id, "body": "Rejected", "due_local": None},
    )
    assert isinstance(rejected, ApprovalRequired)
    assert resume(tools, rejected.continuation, False) == {"rejected": True}
    assert store.list()[0] == original

    expired = execute(
        tools,
        "edit_reminder",
        {"reminder_id": original.id, "body": None, "due_local": "2026-09-05T13:01"},
    )
    assert isinstance(expired, ApprovalRequired)
    clock.value = datetime(2026, 9, 5, 10, 1, tzinfo=UTC)
    assert resume(tools, expired.continuation, True) == {"error": "due_time_not_future"}
    assert store.list()[0] == original
    outcomes = [
        payload["outcome"]
        for event, payload in trace.events
        if event == "reminder_edit_approval"
    ]
    assert outcomes == ["rejected", "expired"]


@pytest.mark.parametrize("status", ["sent", "failed", "unknown", "cancelled"])
def test_edit_rejects_missing_and_terminal_targets(tmp_path: Path, status: str) -> None:
    store, tools, clock, trace = make_tools(tmp_path)
    with pytest.raises(ReminderError, match="not found"):
        execute(
            tools,
            "edit_reminder",
            {"reminder_id": "missing1", "body": "No target", "due_local": None},
        )

    terminal = Reminder(
        id="terminal",
        body="Historical",
        due_at=clock.now() + timedelta(days=1),
        timezone="Asia/Amman",
        status=status,
        created_at=clock.now(),
        updated_at=clock.now(),
        attempt_at=clock.now() if status != "cancelled" else None,
        completed_at=clock.now(),
        outbound_message_id="out-1" if status == "sent" else None,
        failure_classification=status if status in {"failed", "unknown"} else None,
    )
    store.save(terminal, now=clock.now())

    with pytest.raises(ReminderError, match="not pending"):
        execute(
            tools,
            "edit_reminder",
            {"reminder_id": "terminal", "body": "Rewrite", "due_local": None},
        )
    assert [event for event, _ in trace.events].count(
        "reminder_edit_validation_failed"
    ) == 2


def test_edit_validates_complete_result_and_requires_a_replacement(
    tmp_path: Path,
) -> None:
    _store, tools, _, trace = make_tools(tmp_path)
    created = execute(
        tools,
        "create_reminder",
        {"body": "Valid original", "due_local": "2026-09-06T12:00"},
    )
    assert isinstance(created, ApprovalRequired)
    resume(tools, created.continuation, True)

    for arguments in (
        {"reminder_id": "a1b2c3d4", "body": None, "due_local": None},
        {"reminder_id": "a1b2c3d4", "body": "", "due_local": None},
        {"reminder_id": "a1b2c3d4", "body": None, "due_local": "tomorrow"},
        {
            "reminder_id": "a1b2c3d4",
            "body": None,
            "due_local": "2026-09-05T11:59",
        },
    ):
        with pytest.raises(ReminderError):
            execute(tools, "edit_reminder", arguments)
    assert [event for event, _ in trace.events].count(
        "reminder_edit_validation_failed"
    ) == 4


def test_cancel_requires_approval_and_retains_terminal_history(tmp_path: Path) -> None:
    store, tools, _, trace = make_tools(tmp_path)
    created = execute(
        tools,
        "create_reminder",
        {"body": "Cancel this exact body", "due_local": "2026-09-06T12:00"},
    )
    assert isinstance(created, ApprovalRequired)
    resume(tools, created.continuation, True)

    proposed = execute(tools, "cancel_reminder", {"reminder_id": "a1b2c3d4"})

    assert isinstance(proposed, ApprovalRequired)
    assert proposed.action.allow_save_permission is False
    assert proposed.action.display == (
        "Cancel reminder?\n"
        "ID: a1b2c3d4\n"
        "Body: Cancel this exact body\n"
        "Date: 2026-09-06\n"
        "Time: 12:00:00\n"
        "Timezone: Asia/Amman"
    )
    assert store.list()[0].status == "pending"
    assert resume(tools, proposed.continuation, True) == {
        "cancelled": True,
        "id": "a1b2c3d4",
    }
    assert store.list() == ()
    history = store.list(include_terminal=True)
    assert len(history) == 1
    assert history[0].status == "cancelled"
    assert resume(tools, proposed.continuation, True) == {"error": "already_resolved"}
    assert [event for event, _ in trace.events][-3:] == [
        "reminder_cancel_proposed",
        "reminder_cancel_approval",
        "reminder_cancelled",
    ]


def test_rejected_cancel_is_unchanged_and_missing_or_terminal_targets_fail(
    tmp_path: Path,
) -> None:
    store, tools, _, trace = make_tools(tmp_path)
    created = execute(
        tools,
        "create_reminder",
        {"body": "Remain pending", "due_local": "2026-09-06T12:00"},
    )
    assert isinstance(created, ApprovalRequired)
    resume(tools, created.continuation, True)
    original = store.list()[0]
    rejected = execute(tools, "cancel_reminder", {"reminder_id": "a1b2c3d4"})
    assert isinstance(rejected, ApprovalRequired)
    assert resume(tools, rejected.continuation, False) == {"rejected": True}
    assert store.list()[0] == original

    with pytest.raises(ReminderError, match="not found"):
        execute(tools, "cancel_reminder", {"reminder_id": "missing1"})

    approved = execute(tools, "cancel_reminder", {"reminder_id": "a1b2c3d4"})
    assert isinstance(approved, ApprovalRequired)
    resume(tools, approved.continuation, True)
    with pytest.raises(ReminderError, match="not pending"):
        execute(tools, "cancel_reminder", {"reminder_id": "a1b2c3d4"})
    assert [event for event, _ in trace.events].count(
        "reminder_cancel_validation_failed"
    ) == 2


@pytest.mark.parametrize("status", ["sent", "failed", "unknown", "cancelled"])
def test_cancel_rejects_every_terminal_status(tmp_path: Path, status: str) -> None:
    store, tools, clock, trace = make_tools(tmp_path)
    store.save(
        Reminder(
            id="terminal",
            body="Historical",
            due_at=clock.now() + timedelta(days=1),
            timezone="Asia/Amman",
            status=status,
            created_at=clock.now(),
            updated_at=clock.now(),
            attempt_at=clock.now() if status != "cancelled" else None,
            completed_at=clock.now(),
            outbound_message_id="out-1" if status == "sent" else None,
            failure_classification=(
                status if status in {"failed", "unknown"} else None
            ),
        ),
        now=clock.now(),
    )

    with pytest.raises(ReminderError, match="not pending"):
        execute(tools, "cancel_reminder", {"reminder_id": "terminal"})
    assert trace.events[-1][0] == "reminder_cancel_validation_failed"


def test_mutation_tool_descriptions_forbid_guessing_ambiguous_references() -> None:
    definitions = {str(item["name"]): item for item in ReminderTools.definitions}

    for name in ("edit_reminder", "cancel_reminder"):
        description = str(definitions[name]["description"])
        assert "list_reminders" in description
        assert "more than one" in description
        assert "ask the operator" in description
        assert "Never guess" in description


@pytest.mark.parametrize("operation", ["edit", "cancel"])
def test_ambiguous_conversational_match_lists_and_asks_without_guessing_id(
    tmp_path: Path, operation: str
) -> None:
    _store, tools, _, trace = make_tools(tmp_path, ids=["first001", "second01"])
    for body, due_local in (
        ("Call Sara about the first draft", "2026-09-06T12:00"),
        ("Call Sara about the final draft", "2026-09-06T13:00"),
    ):
        proposal = execute(
            tools,
            "create_reminder",
            {"body": body, "due_local": due_local},
        )
        assert isinstance(proposal, ApprovalRequired)
        resume(tools, proposal.continuation, True)

    class Responses:
        def __init__(self) -> None:
            self.requests: list[dict[str, object]] = []
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
                    output_text=(
                        "I found two matching pending Reminders. Which one do you mean: "
                        "first001 or second01?"
                    ),
                ),
            ]

        async def create(
            self, request: dict[str, object], *, timeout: float
        ) -> ResponsesResult:
            self.requests.append(request)
            return self.results.pop(0)

    responses = Responses()
    runner = DirectResponsesRunner(
        responses,
        tools=tools,
        request_timeout_seconds=30,
        trace=trace,
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
    assert result.reply is not None
    assert "Which one do you mean" in result.reply
    tool_calls = [payload for event, payload in trace.events if event == "tool_call"]
    assert [call["name"] for call in tool_calls] == ["list_reminders"]
    assert len(responses.requests) == 2
    assert '"id":"first001"' in str(responses.requests[1]["input"])
    assert '"id":"second01"' in str(responses.requests[1]["input"])

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

import pytest

from jarvis_personal_runtime.openwa import OpenWASendError
from jarvis_personal_runtime.reminders import (
    ReminderScheduler,
    ReminderStore,
    ReminderTools,
)
from jarvis_personal_runtime.runtime import Completed, InboundText, PersonalRuntime

NOW = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
OPERATOR = "962790000000@c.us"


class ControlledSchedulerClock:
    def __init__(self) -> None:
        self.current = NOW
        self.changed = asyncio.Event()
        self.waited_for: list[datetime] = []

    def now(self) -> datetime:
        return self.current

    async def wait_until(self, due_at: datetime, wake: asyncio.Event) -> None:
        self.waited_for.append(due_at)
        while self.current < due_at and not wake.is_set():
            changed = asyncio.create_task(self.changed.wait())
            woken = asyncio.create_task(wake.wait())
            done, pending = await asyncio.wait(
                (changed, woken), return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if changed in done:
                self.changed.clear()

    def advance(self, value: datetime) -> None:
        self.current = value
        self.changed.set()


class RecordingSender:
    def __init__(self, error: OpenWASendError | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str]] = []
        self.called = asyncio.Event()

    def send_text(self, chat_id: str, text: str) -> str:
        self.calls.append((chat_id, text))
        if self.error is not None:
            raise self.error
        return f"outbound-{len(self.calls)}"


class MemoryTrace:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def record(self, event: str, payload: dict[str, object]) -> None:
        self.events.append((event, payload))


class BlockingRunner:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, *_args: object, **_kwargs: object) -> Completed:
        self.entered.set()
        await self.release.wait()
        return Completed("ordinary reply")

    async def resume(self, *_args: object, **_kwargs: object) -> Completed:
        raise AssertionError("approval was not expected")


class BlockingSender(RecordingSender):
    def __init__(self) -> None:
        super().__init__()
        self.entered = Event()
        self.release = Event()

    def send_text(self, chat_id: str, text: str) -> str:
        self.entered.set()
        self.release.wait(timeout=2)
        return super().send_text(chat_id, text)


async def approve(tools: ReminderTools, body: str, due_local: str) -> str:
    proposed = await tools.execute(
        "create_reminder", {"body": body, "due_local": due_local}
    )
    result = await tools.resume(proposed.continuation, approved=True)
    await asyncio.sleep(0)
    return result


async def eventually(predicate: object) -> None:
    for _ in range(100):
        if predicate():  # type: ignore[operator]
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached")


def make_capability(
    tmp_path: Path, sender: RecordingSender
) -> tuple[ReminderStore, ReminderTools, ReminderScheduler, ControlledSchedulerClock]:
    store = ReminderStore(tmp_path / "reminders.sqlite3")
    clock = ControlledSchedulerClock()
    scheduler = ReminderScheduler(
        store,
        sender=sender,
        operator_chat_id=OPERATOR,
        clock=clock,
    )
    ids = iter(("later001", "early001"))
    tools = ReminderTools(
        store,
        operator_timezone="Asia/Amman",
        clock=clock,
        id_generator=lambda: next(ids),
        on_change=scheduler.wake,
    )
    return store, tools, scheduler, clock


def test_approved_creation_wakes_next_due_and_delivers_in_chronological_order(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        sender = RecordingSender()
        store, tools, scheduler, clock = make_capability(tmp_path, sender)
        await approve(tools, "later exact body", "2026-09-05T15:00:00")
        scheduler.start()
        await eventually(
            lambda: clock.waited_for == [datetime(2026, 9, 5, 12, tzinfo=UTC)]
        )

        await approve(tools, "earlier exact body", "2026-09-05T14:00:00")
        await eventually(
            lambda: clock.waited_for[-1] == datetime(2026, 9, 5, 11, tzinfo=UTC)
        )
        clock.advance(datetime(2026, 9, 5, 11, tzinfo=UTC))
        await eventually(lambda: len(sender.calls) == 1)
        clock.advance(datetime(2026, 9, 5, 12, tzinfo=UTC))
        await eventually(lambda: len(sender.calls) == 2)

        assert sender.calls == [
            (OPERATOR, "earlier exact body"),
            (OPERATOR, "later exact body"),
        ]
        assert store.list() == ()
        assert [record.status for record in store.list(include_terminal=True)] == [
            "sent",
            "sent",
        ]
        assert [
            record.outbound_message_id for record in store.list(include_terminal=True)
        ] == ["outbound-1", "outbound-2"]
        await scheduler.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("error", "expected_status", "failure"),
    [
        (None, "sent", None),
        (OpenWASendError("rejected", may_have_sent=False), "failed", "rejected"),
        (OpenWASendError("timeout", may_have_sent=True), "unknown", "timeout"),
    ],
)
def test_each_transport_outcome_is_terminal_and_never_retried(
    tmp_path: Path,
    error: OpenWASendError | None,
    expected_status: str,
    failure: str | None,
) -> None:
    async def scenario() -> None:
        sender = RecordingSender(error)
        store = ReminderStore(tmp_path / "reminders.sqlite3")
        clock = ControlledSchedulerClock()
        scheduler = ReminderScheduler(
            store, sender=sender, operator_chat_id=OPERATOR, clock=clock
        )
        tools = ReminderTools(
            store,
            operator_timezone="Asia/Amman",
            clock=clock,
            id_generator=lambda: "single01",
            on_change=scheduler.wake,
        )
        await approve(tools, "send me once", "2026-09-05T13:00:00")
        scheduler.start()
        clock.advance(datetime(2026, 9, 5, 10, tzinfo=UTC))
        await eventually(lambda: len(sender.calls) == 1)
        await eventually(
            lambda: store.list(include_terminal=True)[0].status == expected_status
        )

        record = store.list(include_terminal=True)[0]
        assert record.attempt_at == datetime(2026, 9, 5, 10, tzinfo=UTC)
        assert record.completed_at == datetime(2026, 9, 5, 10, tzinfo=UTC)
        assert record.outbound_message_id == ("outbound-1" if error is None else None)
        assert record.failure_classification == failure
        clock.advance(datetime(2026, 9, 6, 10, tzinfo=UTC))
        scheduler.wake()
        await asyncio.sleep(0.05)
        assert sender.calls == [(OPERATOR, "send me once")]
        await scheduler.stop()

    asyncio.run(scenario())


def test_unusable_accepted_message_id_is_terminal_unknown(tmp_path: Path) -> None:
    class OversizedIdSender(RecordingSender):
        def send_text(self, chat_id: str, text: str) -> str:
            super().send_text(chat_id, text)
            return "x" * 257

    async def scenario() -> None:
        sender = OversizedIdSender()
        store, tools, scheduler, clock = make_capability(tmp_path, sender)
        await approve(tools, "ambiguous acceptance", "2026-09-05T13:00:00")
        scheduler.start()
        clock.advance(datetime(2026, 9, 5, 10, tzinfo=UTC))
        await eventually(
            lambda: (
                bool(store.list(include_terminal=True))
                and store.list(include_terminal=True)[0].status == "unknown"
            )
        )

        record = store.list(include_terminal=True)[0]
        assert record.status == "unknown"
        assert record.outbound_message_id is None
        assert record.failure_classification == "invalid_response"
        assert sender.calls == [(OPERATOR, "ambiguous acceptance")]
        await scheduler.stop()

    asyncio.run(scenario())


def test_due_reminder_sends_while_an_ordinary_request_is_active(tmp_path: Path) -> None:
    async def scenario() -> None:
        sender = RecordingSender()
        _store, tools, scheduler, clock = make_capability(tmp_path, sender)
        await approve(tools, "independent reminder", "2026-09-05T13:00:00")
        runner = BlockingRunner()
        runtime = PersonalRuntime(request_runner=runner, clock=clock)
        foreground = asyncio.create_task(
            runtime.receive(InboundText("inbound-1", "ordinary work", clock.now()))
        )
        await runner.entered.wait()

        scheduler.start()
        clock.advance(datetime(2026, 9, 5, 10, tzinfo=UTC))
        await eventually(lambda: sender.calls == [(OPERATOR, "independent reminder")])
        assert foreground.done() is False

        runner.release.set()
        await foreground
        await scheduler.stop()

    asyncio.run(scenario())


def test_scheduler_traces_attempt_and_terminal_outcome(tmp_path: Path) -> None:
    async def scenario() -> None:
        sender = RecordingSender()
        store = ReminderStore(tmp_path / "reminders.sqlite3")
        clock = ControlledSchedulerClock()
        trace = MemoryTrace()
        scheduler = ReminderScheduler(
            store,
            sender=sender,
            operator_chat_id=OPERATOR,
            clock=clock,
            trace=trace,
        )
        tools = ReminderTools(
            store,
            operator_timezone="Asia/Amman",
            clock=clock,
            id_generator=lambda: "traced01",
            on_change=scheduler.wake,
        )
        await approve(tools, "trace me", "2026-09-05T13:00:00")
        scheduler.start()
        clock.advance(datetime(2026, 9, 5, 10, tzinfo=UTC))
        await eventually(lambda: len(trace.events) == 2)

        assert trace.events == [
            (
                "reminder_delivery_attempt",
                {"id": "traced01", "attempt_at": "2026-09-05T10:00:00Z"},
            ),
            (
                "reminder_delivery_outcome",
                {
                    "id": "traced01",
                    "status": "sent",
                    "outbound_message_id": "outbound-1",
                },
            ),
        ]
        await scheduler.stop()

    asyncio.run(scenario())


def test_scheduler_does_not_recover_a_reminder_that_became_due_while_stopped(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        sender = RecordingSender()
        store, tools, scheduler, clock = make_capability(tmp_path, sender)
        await approve(tools, "missed while stopped", "2026-09-05T13:00:00")
        clock.advance(datetime(2026, 9, 5, 11, tzinfo=UTC))

        scheduler.start()
        await asyncio.sleep(0.05)

        assert sender.calls == []
        assert store.list()[0].status == "pending"
        await scheduler.stop()

    asyncio.run(scenario())


def test_in_flight_attempt_is_not_exposed_as_a_terminal_outcome(tmp_path: Path) -> None:
    async def scenario() -> None:
        sender = BlockingSender()
        store, tools, scheduler, clock = make_capability(tmp_path, sender)
        await approve(tools, "still sending", "2026-09-05T13:00:00")
        scheduler.start()
        clock.advance(datetime(2026, 9, 5, 10, tzinfo=UTC))
        await asyncio.wait_for(asyncio.to_thread(sender.entered.wait), timeout=1)

        assert store.list() == ()
        assert store.list(include_terminal=True) == ()

        sender.release.set()
        await eventually(
            lambda: (
                bool(store.list(include_terminal=True))
                and store.list(include_terminal=True)[0].status == "sent"
            )
        )
        await scheduler.stop()

    asyncio.run(scenario())


def test_shutdown_waits_for_an_active_attempt_and_records_its_outcome(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        sender = BlockingSender()
        store = ReminderStore(tmp_path / "reminders.sqlite3")
        clock = ControlledSchedulerClock()
        trace = MemoryTrace()
        scheduler = ReminderScheduler(
            store,
            sender=sender,
            operator_chat_id=OPERATOR,
            clock=clock,
            trace=trace,
        )
        tools = ReminderTools(
            store,
            operator_timezone="Asia/Amman",
            clock=clock,
            id_generator=lambda: "stopped1",
            on_change=scheduler.wake,
        )
        await approve(tools, "finish on shutdown", "2026-09-05T13:00:00")
        scheduler.start()
        clock.advance(datetime(2026, 9, 5, 10, tzinfo=UTC))
        await asyncio.wait_for(asyncio.to_thread(sender.entered.wait), timeout=1)

        stopping = asyncio.create_task(scheduler.stop())
        await asyncio.sleep(0)
        assert stopping.done() is False
        sender.release.set()
        await stopping

        record = store.list(include_terminal=True)[0]
        assert record.status == "sent"
        assert trace.events[-1] == (
            "reminder_delivery_outcome",
            {
                "id": "stopped1",
                "status": "sent",
                "outbound_message_id": "outbound-1",
            },
        )

    asyncio.run(scenario())


def test_concurrent_scheduler_claims_still_make_one_transport_call(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        sender = BlockingSender()
        store = ReminderStore(tmp_path / "reminders.sqlite3")
        clock = ControlledSchedulerClock()
        first = ReminderScheduler(
            store, sender=sender, operator_chat_id=OPERATOR, clock=clock
        )
        second = ReminderScheduler(
            store, sender=sender, operator_chat_id=OPERATOR, clock=clock
        )
        tools = ReminderTools(
            store,
            operator_timezone="Asia/Amman",
            clock=clock,
            id_generator=lambda: "claimed1",
            on_change=lambda: (first.wake(), second.wake()),
        )
        await approve(tools, "claim only once", "2026-09-05T13:00:00")
        first.start()
        second.start()
        await eventually(lambda: len(clock.waited_for) == 2)
        clock.advance(datetime(2026, 9, 5, 10, tzinfo=UTC))
        await asyncio.wait_for(asyncio.to_thread(sender.entered.wait), timeout=1)
        await asyncio.sleep(0.05)

        assert sender.calls == []
        sender.release.set()
        await eventually(lambda: len(sender.calls) >= 1)
        await asyncio.sleep(0.05)
        assert sender.calls == [(OPERATOR, "claim only once")]
        await first.stop()
        await second.stop()

    asyncio.run(scenario())


def test_approved_edit_and_cancel_wake_and_recalculate_next_due(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        sender = RecordingSender()
        store, tools, scheduler, clock = make_capability(tmp_path, sender)
        await approve(tools, "first body", "2026-09-05T14:00:00")
        await approve(tools, "second body", "2026-09-05T15:00:00")
        scheduler.start()
        await eventually(
            lambda: (
                bool(clock.waited_for)
                and clock.waited_for[-1] == datetime(2026, 9, 5, 11, tzinfo=UTC)
            )
        )

        edited = await tools.execute(
            "edit_reminder",
            {
                "reminder_id": "later001",
                "body": None,
                "due_local": "2026-09-05T16:00:00",
            },
        )
        await tools.resume(edited.continuation, approved=True)
        await eventually(
            lambda: clock.waited_for[-1] == datetime(2026, 9, 5, 12, tzinfo=UTC)
        )

        cancelled = await tools.execute("cancel_reminder", {"reminder_id": "early001"})
        await tools.resume(cancelled.continuation, approved=True)
        await eventually(
            lambda: clock.waited_for[-1] == datetime(2026, 9, 5, 13, tzinfo=UTC)
        )

        clock.advance(datetime(2026, 9, 5, 13, tzinfo=UTC))
        await eventually(lambda: sender.calls == [(OPERATOR, "first body")])
        history = {record.id: record for record in store.list(include_terminal=True)}
        assert history["early001"].status == "cancelled"
        assert history["later001"].status == "sent"
        await scheduler.stop()

    asyncio.run(scenario())

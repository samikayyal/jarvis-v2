# ruff: noqa: F401, I001, RUF100 -- split modules retain ticket context.
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from test_support import build_receiver_components

from jarvis_control_plane import (
    DurableMemory,
    InboundMessage,
    InMemoryDurableStateStore,
    MemoryLifecycle,
    MemoryOperation,
    MemorySearchLimitExceeded,
    SignedInboundEvent,
    SQLiteDurableStateStore,
    StateStoreError,
    parse_memory_command,
)

from jarvis_control_plane.control_grammar import MessageKind, parse_control

from jarvis_control_plane.models import MemorySelection

from jarvis_control_plane.sessions import (
    InMemoryWorkingSessionStore,
    ModelAvailability,
    PendingActionState,
    accept_request,
    install_pending_action,
)

NOW = datetime(2026, 8, 8, 9, 0, tzinfo=UTC)


class _UnavailableModelProvider:
    def current(self) -> ModelAvailability:
        raise RuntimeError("model provider is unavailable")


def _set_working_session_state(components, *, pending: bool) -> None:
    store = components.broker.working_sessions
    current = store.load()
    assert current is not None
    accepted = accept_request(
        current,
        now=NOW,
        request_id="ticket22-gate-request",
        originating_message_id="ticket22-gate-message",
    )
    assert accepted.cancellation_token is not None
    updated = accepted.state
    if pending:
        action = PendingActionState.create(
            action_id="ticket22-gate-action",
            session_id=updated.session_id,
            request_id="ticket22-gate-request",
            kind="placeholder",
            summary="A bounded pending action",
            created_at=NOW,
        )
        updated = install_pending_action(updated, action, now=NOW).state
    store.compare_and_set(current, updated)


def _event(components, *, message_id: str, text: str) -> SignedInboundEvent:
    message = InboundMessage(
        event_type="message.received",
        session_id=components.config.session_id,
        event_id=f"event-{message_id}",
        message_id=message_id,
        sender_id="operator.test",
        chat_id="operator.test",
        chat_type="direct",
        message_type="text",
        from_me=False,
        text=text,
    )
    return SignedInboundEvent.from_message(message, components.config.signing_secret)


def test_ordinary_conversation_does_not_create_durable_memory() -> None:
    components = build_receiver_components(
        operator_id="operator.test",
        transport_session_id="session.ticket22.ordinary",
        signing_secret=b"ticket22-ordinary-secret",
        now=NOW,
        id_prefix="ticket22-ordinary",
    )

    result = components.receiver.receive(
        _event(
            components, message_id="message-ordinary", text="I prefer concise updates."
        )
    )

    assert result.disposition == "completed"
    assert components.state.list_memories() == ()
    assert components.orchestration.calls[0].memories == ()


def test_explicit_remember_requires_confirmation_then_persists_memory() -> None:
    components = build_receiver_components(
        operator_id="operator.test",
        transport_session_id="session.ticket22.remember",
        signing_secret=b"ticket22-remember-secret",
        now=NOW,
        id_prefix="ticket22-remember",
    )

    pending = components.receiver.receive(
        _event(
            components,
            message_id="message-remember",
            text="Please remember that the operator prefers concise updates.",
        )
    )

    assert pending.disposition == "pending_action"
    assert pending.reply is None
    assert components.state.list_memories() == ()
    assert any(
        "the operator prefers concise updates" in reply.body
        for reply in components.outbound.sent
    )

    approved = components.receiver.receive(
        _event(components, message_id="message-approve", text="1")
    )

    assert approved.disposition == "action_dispatched"
    memories = components.state.list_memories(include_terminal=False)
    assert len(memories) == 1
    assert memories[0].content == "the operator prefers concise updates."
    assert memories[0].source_message_id == "message-remember"
    assert all(
        "the operator prefers concise updates." not in str(record.details)
        for record in components.audit.records
    )


def test_memory_list_and_search_return_metadata_until_exact_inspection() -> None:
    components = build_receiver_components(
        operator_id="operator.test",
        transport_session_id="session.ticket22.reads",
        signing_secret=b"ticket22-reads-secret",
        now=NOW,
        id_prefix="ticket22-reads",
    )
    components.state.create_memory(
        DurableMemory(
            memory_id="memory-read-secret",
            content="The API key is sk-proj-ticket22-read-value.",
            created_at=NOW,
            updated_at=NOW,
        )
    )

    listed = components.receiver.receive(
        _event(components, message_id="message-memory-list", text="/memory list")
    )
    assert listed.disposition == "memory_list"
    assert listed.reply is not None
    assert "memory-read-secret" in listed.reply.body
    assert "sk-proj-ticket22-read-value" not in listed.reply.body

    searched = components.receiver.receive(
        _event(
            components,
            message_id="message-memory-search",
            text="/memory search API key",
        )
    )
    assert searched.disposition == "memory_search"
    assert searched.reply is not None
    assert "memory-read-secret" in searched.reply.body
    assert "sk-proj-ticket22-read-value" not in searched.reply.body


def test_memory_inspection_uses_lossless_multipart_delivery_for_long_content() -> None:
    components = build_receiver_components(
        operator_id="operator.test",
        transport_session_id="session.ticket22.long-inspect",
        signing_secret=b"ticket22-long-inspect-secret",
        now=NOW,
        id_prefix="ticket22-long-inspect",
    )
    content = "remembered-value-" * 400
    components.state.create_memory(
        DurableMemory(
            memory_id="memory-long-inspect",
            content=content,
            created_at=NOW,
            updated_at=NOW,
        )
    )

    inspected = components.receiver.receive(
        _event(
            components,
            message_id="message-long-inspect",
            text="/memory inspect memory-long-inspect",
        )
    )

    assert inspected.disposition == "memory_inspect"
    parts = [
        reply.body
        for reply in components.outbound.sent
        if reply.body.startswith("Durable-memory inspection part ")
    ]
    assert len(parts) > 1
    assert all(len(part) <= 4_096 for part in parts)
    reassembled = "".join(part.split("\n", 1)[1] for part in parts)
    assert f"Exact content: {content}" in reassembled
    assert "[truncated]" not in reassembled


def test_sqlite_reclassifies_persisted_memories_before_rebuilding_fts(tmp_path) -> None:
    database = tmp_path / "stale-memory-classification.sqlite3"
    state = SQLiteDurableStateStore(database)
    state.create_memory(
        DurableMemory(
            memory_id="memory-stale-secret",
            content="The API key is sk-proj-ticket22-stale-value.",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    state.connection.execute(
        "UPDATE durable_assistant_memory SET credential_like = 0 WHERE memory_id = ?",
        ("memory-stale-secret",),
    )
    state.connection.execute(
        "INSERT INTO durable_assistant_memory_fts(memory_id, content) VALUES (?, ?)",
        ("memory-stale-secret", "The API key is sk-proj-ticket22-stale-value."),
    )
    state.connection.commit()
    state.close()

    reopened = SQLiteDurableStateStore(database)
    try:
        persisted = reopened.get_memory("memory-stale-secret")
        assert persisted is not None
        assert persisted.credential_like is True
        assert (
            reopened.connection.execute(
                "SELECT 1 FROM durable_assistant_memory_fts WHERE memory_id = ?",
                ("memory-stale-secret",),
            ).fetchone()
            is None
        )
        assert (
            reopened.select_memories_for_context(text="API key", limit=5).memories == ()
        )
    finally:
        reopened.close()


def test_sqlite_memory_search_reports_a_bounded_secret_record_scan(tmp_path) -> None:
    state = SQLiteDurableStateStore(tmp_path / "bounded-memory-search.sqlite3")
    try:
        state.connection.executemany(
            """
            INSERT INTO durable_assistant_memory(
                memory_id, content, created_at, updated_at, source_message_id,
                status, credential_like, replaced_by_memory_id
            ) VALUES (?, ?, ?, ?, ?, 'active', 1, NULL)
            """,
            (
                (
                    f"memory-scan-{index:05d}",
                    f"API key: sk-proj-ticket22-scan-{index:05d}.",
                    NOW.isoformat(),
                    NOW.isoformat(),
                    None,
                )
                for index in range(10_001)
            ),
        )
        state.connection.commit()

        with pytest.raises(MemorySearchLimitExceeded):
            state.search_memories(text="needle", limit=20)
    finally:
        state.close()


def test_credential_like_memory_is_searchable_and_inspectable_but_not_automatic_context() -> (
    None
):
    components = build_receiver_components(
        operator_id="operator.test",
        transport_session_id="session.ticket22.secret",
        signing_secret=b"ticket22-secret-secret",
        now=NOW,
        id_prefix="ticket22-secure",
    )
    components.state.create_memory(
        DurableMemory(
            memory_id="memory-secret",
            content="The API key is sk-proj-ticket22-secret-value.",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    components.state.create_memory(
        DurableMemory(
            memory_id="memory-safe",
            content="The operator prefers concise status updates.",
            created_at=NOW,
            updated_at=NOW,
        )
    )

    automatic = components.state.select_memories_for_context(
        text="What status updates does the operator prefer?",
        limit=5,
    )
    assert [memory.memory_id for memory in automatic.memories] == ["memory-safe"]
    assert [
        memory.memory_id for memory in components.state.search_memories(text="API key")
    ] == ["memory-secret"]

    ordinary = components.receiver.receive(
        _event(
            components,
            message_id="message-memory-context",
            text="What status updates do I prefer?",
        )
    )
    assert ordinary.disposition == "completed"
    assert [
        memory.memory_id for memory in components.orchestration.calls[0].memories
    ] == ["memory-safe"]
    assert "sk-proj-ticket22-secret-value" not in str(
        components.orchestration.calls[0].memories
    )

    inspected = components.receiver.receive(
        _event(
            components,
            message_id="message-memory-inspect",
            text="/memory inspect memory-secret",
        )
    )
    assert inspected.disposition == "memory_inspect"
    assert inspected.reply is not None
    assert "sk-proj-ticket22-secret-value" in inspected.reply.body


def test_exact_confirmed_replace_and_forget_only_change_selected_memory() -> None:
    components = build_receiver_components(
        operator_id="operator.test",
        transport_session_id="session.ticket22.lifecycle",
        signing_secret=b"ticket22-lifecycle-secret",
        now=NOW,
        id_prefix="ticket22-lifecycle",
    )
    first = DurableMemory(
        memory_id="memory-first",
        content="The operator prefers tea.",
        created_at=NOW,
        updated_at=NOW,
    )
    second = DurableMemory(
        memory_id="memory-second",
        content="The operator prefers coffee.",
        created_at=NOW,
        updated_at=NOW,
    )
    components.state.create_memory(first)
    components.state.create_memory(second)

    replace_pending = components.receiver.receive(
        _event(
            components,
            message_id="message-replace",
            text="/memory replace memory-first The operator prefers herbal tea.",
        )
    )
    assert replace_pending.disposition == "pending_action"
    replace_result = components.receiver.receive(
        _event(components, message_id="message-replace-approve", text="1")
    )
    assert replace_result.disposition == "action_dispatched"

    replaced = components.state.get_memory("memory-first")
    assert replaced is not None
    assert replaced.status is MemoryLifecycle.REPLACED
    assert replaced.content is None
    replacement_id = replaced.replaced_by_memory_id
    assert replacement_id is not None
    replacement = components.state.get_memory(replacement_id)
    assert replacement is not None
    assert replacement.content == "The operator prefers herbal tea."
    assert components.state.get_memory("memory-second") == second

    forget_pending = components.receiver.receive(
        _event(
            components,
            message_id="message-forget",
            text="/memory forget memory-second",
        )
    )
    assert forget_pending.disposition == "pending_action"
    forget_result = components.receiver.receive(
        _event(components, message_id="message-forget-approve", text="1")
    )
    assert forget_result.disposition == "action_dispatched"
    forgotten = components.state.get_memory("memory-second")
    assert forgotten is not None
    assert forgotten.status is MemoryLifecycle.FORGOTTEN
    assert forgotten.content is None


def test_stale_exact_memory_preview_fails_closed_without_creating_replacement() -> None:
    components = build_receiver_components(
        operator_id="operator.test",
        transport_session_id="session.ticket22.stale",
        signing_secret=b"ticket22-stale-secret",
        now=NOW,
        id_prefix="ticket22-stale",
    )
    components.state.create_memory(
        DurableMemory(
            memory_id="memory-stale",
            content="The original exact memory.",
            created_at=NOW,
            updated_at=NOW,
        )
    )

    pending = components.receiver.receive(
        _event(
            components,
            message_id="message-stale-replace",
            text="/memory replace memory-stale The replacement exact memory.",
        )
    )
    assert pending.disposition == "pending_action"
    components.state.forget_memory("memory-stale", updated_at=NOW)

    approved = components.receiver.receive(
        _event(components, message_id="message-stale-approve", text="1")
    )

    assert approved.disposition == "action_dispatch_failed"
    assert (
        components.state.get_memory("memory-stale").status is MemoryLifecycle.FORGOTTEN
    )
    assert len(components.state.list_memories(include_terminal=False)) == 0
